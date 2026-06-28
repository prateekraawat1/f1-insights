"""
test_triggers.py - Unit tests for the F1 trigger engine and cache layer.
"""

import sys
import os
import unittest
import asyncio

# Ensure the project root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.cache import CacheClient
from backend.triggers import (
    TriggerEngine,
    TriggerType,
    _eval_drs_threat,
    _eval_undercut,
    _eval_pace_drop,
    _eval_box_window,
    CooldownTracker,
)


class TestCacheClient(unittest.TestCase):
    """Tests for the Redis/FakeRedis cache wrapper."""

    def setUp(self):
        self.cache = CacheClient()
        self.cache.flush()

    def test_set_and_get_state(self):
        state = {"code": "VER", "lap": 42, "speed_kph": 300.0}
        self.cache.set_state("VER", state)
        retrieved = self.cache.get_state("VER")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved["code"], "VER")
        self.assertEqual(retrieved["lap"], 42)

    def test_get_state_missing(self):
        result = self.cache.get_state("NONEXISTENT")
        self.assertIsNone(result)

    def test_set_and_get_race_meta(self):
        self.cache.set_race_meta("current_lap", 42)
        result = self.cache.get_race_meta("current_lap")
        self.assertEqual(result, 42)

    def test_get_all_drivers(self):
        self.cache.set_state("VER", {"code": "VER"})
        self.cache.set_state("HAM", {"code": "HAM"})
        drivers = self.cache.get_all_drivers()
        self.assertIn("VER", drivers)
        self.assertIn("HAM", drivers)

    def test_flush(self):
        self.cache.set_state("VER", {"code": "VER"})
        self.cache.flush()
        result = self.cache.get_state("VER")
        self.assertIsNone(result)

    def test_backend_type_string(self):
        self.assertIn(self.cache.backend_type, ["Redis", "FakeRedis (in-memory)"])


class TestDRSThreatTrigger(unittest.TestCase):
    """Tests for the DRS_THREAT evaluator."""

    def _build_states(self, gap: float, in_drs_zone: bool = True):
        ver = {
            "tyre_compound": "MEDIUM", "tyre_age_laps": 15,
            "lap_time_s": 88.9, "speed_kph": 298.0,
        }
        ham = {
            "tyre_compound": "SOFT", "tyre_age_laps": 3,
            "drs_detection_zone": in_drs_zone,
            "lap_time_s": 87.8, "speed_kph": 305.0,
        }
        meta = {"gap": gap, "lap": 42, "track": "Silverstone"}
        return ver, ham, meta

    def test_fires_when_gap_below_threshold_in_zone(self):
        ver, ham, meta = self._build_states(gap=0.72, in_drs_zone=True)
        event = _eval_drs_threat(ver, ham, meta)
        self.assertIsNotNone(event)
        self.assertEqual(event.trigger, TriggerType.DRS_THREAT)
        self.assertEqual(event.chasing, "HAM")
        self.assertEqual(event.leading, "VER")
        self.assertAlmostEqual(event.gap_s, 0.72)

    def test_does_not_fire_above_threshold(self):
        ver, ham, meta = self._build_states(gap=1.5)
        event = _eval_drs_threat(ver, ham, meta)
        self.assertIsNone(event)

    def test_does_not_fire_when_not_in_detection_zone(self):
        ver, ham, meta = self._build_states(gap=0.6, in_drs_zone=False)
        event = _eval_drs_threat(ver, ham, meta)
        self.assertIsNone(event)

    def test_does_not_fire_when_ham_leads(self):
        ver, ham, meta = self._build_states(gap=-0.3)
        event = _eval_drs_threat(ver, ham, meta)
        self.assertIsNone(event)

    def test_severity_high_below_half_second(self):
        ver, ham, meta = self._build_states(gap=0.4, in_drs_zone=True)
        event = _eval_drs_threat(ver, ham, meta)
        self.assertEqual(event.severity, "HIGH")

    def test_severity_medium_between_half_and_one_second(self):
        ver, ham, meta = self._build_states(gap=0.85, in_drs_zone=True)
        event = _eval_drs_threat(ver, ham, meta)
        self.assertEqual(event.severity, "MEDIUM")


class TestUndercutTrigger(unittest.TestCase):
    """Tests for the UNDERCUT evaluator."""

    def _states(self, gap: float, ham_in_pit: bool):
        ver = {"tyre_compound": "MEDIUM", "tyre_age_laps": 18}
        ham = {"in_pit": ham_in_pit, "pit_stops": 1, "tyre_compound": "SOFT"}
        meta = {"gap": gap, "lap": 43, "track": "Silverstone"}
        return ver, ham, meta

    def test_fires_when_pit_within_threshold(self):
        ver, ham, meta = self._states(gap=1.8, ham_in_pit=True)
        event = _eval_undercut(ver, ham, meta)
        self.assertIsNotNone(event)
        self.assertEqual(event.trigger, TriggerType.UNDERCUT)

    def test_does_not_fire_when_gap_too_large(self):
        ver, ham, meta = self._states(gap=3.5, ham_in_pit=True)
        event = _eval_undercut(ver, ham, meta)
        self.assertIsNone(event)

    def test_does_not_fire_when_not_pitting(self):
        ver, ham, meta = self._states(gap=1.8, ham_in_pit=False)
        event = _eval_undercut(ver, ham, meta)
        self.assertIsNone(event)

    def test_does_not_fire_when_ham_leads(self):
        ver, ham, meta = self._states(gap=-0.5, ham_in_pit=True)
        event = _eval_undercut(ver, ham, meta)
        self.assertIsNone(event)


class TestPaceDropTrigger(unittest.TestCase):
    """Tests for the PACE_DROP evaluator."""

    def _state(self, last_lap: float, avg: float):
        return {
            "lap_time_s":       last_lap,
            "rolling_avg_lap_s": avg,
            "tyre_compound":    "MEDIUM",
            "tyre_age_laps":    20,
        }

    def test_fires_when_drop_exceeds_threshold(self):
        state = self._state(last_lap=90.5, avg=88.8)   # delta = 1.7s
        meta  = {"gap": 1.2, "lap": 44, "track": "Silverstone"}
        event = _eval_pace_drop("VER", state, meta)
        self.assertIsNotNone(event)
        self.assertEqual(event.trigger, TriggerType.PACE_DROP)
        self.assertAlmostEqual(event.extra["delta_s"], 1.7, places=1)

    def test_does_not_fire_on_small_drop(self):
        state = self._state(last_lap=89.0, avg=88.5)   # delta = 0.5s < 0.8s
        meta  = {"gap": 1.2, "lap": 44, "track": "Silverstone"}
        event = _eval_pace_drop("VER", state, meta)
        self.assertIsNone(event)

    def test_does_not_fire_with_zero_avg(self):
        state = self._state(last_lap=90.0, avg=0.0)
        meta  = {"gap": 1.0, "lap": 44, "track": "Silverstone"}
        event = _eval_pace_drop("VER", state, meta)
        self.assertIsNone(event)


class TestBoxWindowTrigger(unittest.TestCase):
    """Tests for the BOX_WINDOW evaluator."""

    def test_fires_when_gap_matches_pit_loss_within_margin(self):
        # PIT_LANE_LOSS_S = 22.0; margin = 0.5 → gap must be in [21.5, 22.5]
        ver  = {"tyre_age_laps": 22, "tyre_compound": "MEDIUM"}
        ham  = {}
        meta = {"gap": 22.3, "lap": 44, "track": "Silverstone"}
        event = _eval_box_window(ver, ham, meta)
        self.assertIsNotNone(event)
        self.assertEqual(event.trigger, TriggerType.BOX_WINDOW)

    def test_does_not_fire_when_gap_too_far(self):
        ver  = {"tyre_age_laps": 22, "tyre_compound": "MEDIUM"}
        ham  = {}
        meta = {"gap": 15.0, "lap": 44, "track": "Silverstone"}
        event = _eval_box_window(ver, ham, meta)
        self.assertIsNone(event)


class TestCooldownTracker(unittest.TestCase):
    """Tests for the trigger cooldown mechanism."""

    def test_initial_state_is_ready(self):
        tracker = CooldownTracker()
        self.assertTrue(tracker.is_ready(TriggerType.DRS_THREAT))

    def test_not_ready_after_fire(self):
        tracker = CooldownTracker()
        tracker.mark_fired(TriggerType.DRS_THREAT)
        self.assertFalse(tracker.is_ready(TriggerType.DRS_THREAT))

    def test_different_triggers_independent(self):
        tracker = CooldownTracker()
        tracker.mark_fired(TriggerType.DRS_THREAT)
        self.assertTrue(tracker.is_ready(TriggerType.UNDERCUT))


if __name__ == "__main__":
    unittest.main(verbosity=2)
