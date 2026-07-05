"""
test_triggers.py - Unit tests for the production F1 trigger engine and cache layer.

Covers:
  • Cache CRUD operations (TestCacheClient)
  • DRS_THREAT evaluator — production drs_raw + legacy boolean (TestDRSThreatTrigger)
  • UNDERCUT evaluator (TestUndercutTrigger)
  • PACE_DROP evaluator (TestPaceDropTrigger)
  • BOX_WINDOW evaluator with per-circuit delta (TestBoxWindowTrigger)
  • OVERCUT_WINDOW evaluator (TestOvercutWindowTrigger)
  • SAFETY_CAR / VIRTUAL_SC evaluators (TestRaceControlTriggers)
  • FASTEST_LAP_THREAT evaluator (TestFastestLapThreatTrigger)
  • CooldownTracker per-pair key logic (TestCooldownTracker)
  • TriggerEngine full N-driver integration (TestTriggerEngineIntegration)
"""

import sys
import os
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.cache import CacheClient
from backend.triggers import (
    TriggerEngine,
    TriggerType,
    CooldownTracker,
    _eval_drs_threat,
    _eval_undercut,
    _eval_pace_drop,
    _eval_box_window,
    _eval_overcut,
    _eval_safety_car,
    _eval_virtual_sc,
    _eval_fastest_lap_threat,
)

# Default pit-lane loss for Silverstone (used in tests)
_PIT_LOSS = 22.0


# ─── Shared helpers ───────────────────────────────────────────────────────────

def _meta(gap=0.0, lap=42, track="Silverstone", sc=False, vsc=False, drs_on=True):
    return {
        "gap":    gap,
        "lap":    lap,
        "track":  track,
        "sc":     sc,
        "vsc":    vsc,
        "drs_on": drs_on,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Cache Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestCacheClient(unittest.TestCase):
    """Tests for the Redis / FakeRedis cache wrapper."""

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
        self.assertIsNone(self.cache.get_state("NONEXISTENT"))

    def test_set_and_get_race_meta(self):
        self.cache.set_race_meta("current_lap", 42)
        self.assertEqual(self.cache.get_race_meta("current_lap"), 42)

    def test_get_all_drivers(self):
        self.cache.set_state("VER", {"code": "VER"})
        self.cache.set_state("HAM", {"code": "HAM"})
        drivers = self.cache.get_all_drivers()
        self.assertIn("VER", drivers)
        self.assertIn("HAM", drivers)

    def test_flush(self):
        self.cache.set_state("VER", {"code": "VER"})
        self.cache.flush()
        self.assertIsNone(self.cache.get_state("VER"))

    def test_backend_type_string(self):
        self.assertIn(self.cache.backend_type, ["Redis", "FakeRedis (in-memory)"])


# ═══════════════════════════════════════════════════════════════════════════════
# DRS_THREAT
# ═══════════════════════════════════════════════════════════════════════════════

class TestDRSThreatTrigger(unittest.TestCase):
    """Tests for the DRS_THREAT evaluator (production + legacy paths)."""

    def _states_legacy(self, gap: float, in_drs_zone: bool = True):
        """Legacy: boolean drs_detection_zone (mock / no drs_raw field)."""
        leader = {
            "code": "VER", "tyre_compound": "MEDIUM", "tyre_age_laps": 15,
            "speed_kph": 298.0, "position": 1,
        }
        chaser = {
            "code": "HAM", "tyre_compound": "SOFT", "tyre_age_laps": 3,
            "drs_detection_zone": in_drs_zone, "speed_kph": 305.0,
            "interval_s": gap, "position": 2,
        }
        return leader, chaser

    def _states_production(self, gap: float, drs_raw: int = 8):
        """Production: raw integer drs_raw from OpenF1."""
        leader = {
            "code": "VER", "tyre_compound": "MEDIUM", "tyre_age_laps": 15,
            "speed_kph": 298.0, "position": 1,
        }
        chaser = {
            "code": "HAM", "tyre_compound": "SOFT", "tyre_age_laps": 3,
            "drs_raw": drs_raw, "speed_kph": 305.0,
            "interval_s": gap, "position": 2,
        }
        return leader, chaser

    # ── Legacy boolean path ───────────────────────────────────────────────────

    def test_fires_when_gap_below_threshold_in_zone(self):
        l, c = self._states_legacy(gap=0.72, in_drs_zone=True)
        event = _eval_drs_threat(l, c, _meta(), _PIT_LOSS)
        self.assertIsNotNone(event)
        self.assertEqual(event.trigger, TriggerType.DRS_THREAT)
        self.assertEqual(event.chasing, "HAM")
        self.assertEqual(event.leading, "VER")
        self.assertAlmostEqual(event.gap_s, 0.72)

    def test_does_not_fire_above_threshold(self):
        l, c = self._states_legacy(gap=1.5)
        self.assertIsNone(_eval_drs_threat(l, c, _meta(), _PIT_LOSS))

    def test_does_not_fire_when_not_in_detection_zone(self):
        l, c = self._states_legacy(gap=0.6, in_drs_zone=False)
        self.assertIsNone(_eval_drs_threat(l, c, _meta(), _PIT_LOSS))

    def test_does_not_fire_when_chaser_leads(self):
        # Negative gap means chaser is actually ahead — not valid
        l, c = self._states_legacy(gap=-0.3)
        self.assertIsNone(_eval_drs_threat(l, c, _meta(), _PIT_LOSS))

    def test_severity_high_below_half_second(self):
        l, c = self._states_legacy(gap=0.4, in_drs_zone=True)
        event = _eval_drs_threat(l, c, _meta(), _PIT_LOSS)
        self.assertEqual(event.severity, "HIGH")

    def test_severity_medium_between_half_and_one_second(self):
        l, c = self._states_legacy(gap=0.85, in_drs_zone=True)
        event = _eval_drs_threat(l, c, _meta(), _PIT_LOSS)
        self.assertEqual(event.severity, "MEDIUM")

    # ── Production drs_raw path ───────────────────────────────────────────────

    def test_fires_with_drs_raw_8(self):
        """drs_raw=8 means car is in DRS detection zone."""
        l, c = self._states_production(gap=0.7, drs_raw=8)
        event = _eval_drs_threat(l, c, _meta(), _PIT_LOSS)
        self.assertIsNotNone(event)

    def test_fires_with_drs_raw_10_open(self):
        """drs_raw=10/12/14 means DRS is actually open — zone entry already passed."""
        l, c = self._states_production(gap=0.5, drs_raw=10)
        event = _eval_drs_threat(l, c, _meta(), _PIT_LOSS)
        self.assertIsNotNone(event)

    def test_does_not_fire_with_drs_raw_0(self):
        """drs_raw=0 means DRS not in zone and not open."""
        l, c = self._states_production(gap=0.5, drs_raw=0)
        self.assertIsNone(_eval_drs_threat(l, c, _meta(), _PIT_LOSS))

    def test_suppressed_during_safety_car(self):
        l, c = self._states_legacy(gap=0.5, in_drs_zone=True)
        self.assertIsNone(_eval_drs_threat(l, c, _meta(sc=True), _PIT_LOSS))

    def test_suppressed_during_vsc(self):
        l, c = self._states_legacy(gap=0.5, in_drs_zone=True)
        self.assertIsNone(_eval_drs_threat(l, c, _meta(vsc=True), _PIT_LOSS))


# ═══════════════════════════════════════════════════════════════════════════════
# UNDERCUT
# ═══════════════════════════════════════════════════════════════════════════════

class TestUndercutTrigger(unittest.TestCase):
    """Tests for the UNDERCUT evaluator."""

    def _states(self, gap: float, chaser_in_pit: bool):
        leader = {"code": "VER", "tyre_compound": "MEDIUM", "tyre_age_laps": 18, "position": 1}
        chaser = {
            "code": "HAM", "in_pit": chaser_in_pit, "pit_stops": 1,
            "tyre_compound": "SOFT", "interval_s": gap, "position": 2,
        }
        return leader, chaser

    def test_fires_when_pit_within_threshold(self):
        l, c = self._states(gap=1.8, chaser_in_pit=True)
        event = _eval_undercut(l, c, _meta(), _PIT_LOSS)
        self.assertIsNotNone(event)
        self.assertEqual(event.trigger, TriggerType.UNDERCUT)

    def test_does_not_fire_when_gap_too_large(self):
        l, c = self._states(gap=3.5, chaser_in_pit=True)
        self.assertIsNone(_eval_undercut(l, c, _meta(), _PIT_LOSS))

    def test_does_not_fire_when_not_pitting(self):
        l, c = self._states(gap=1.8, chaser_in_pit=False)
        self.assertIsNone(_eval_undercut(l, c, _meta(), _PIT_LOSS))

    def test_does_not_fire_when_chaser_leads(self):
        l, c = self._states(gap=-0.5, chaser_in_pit=True)
        self.assertIsNone(_eval_undercut(l, c, _meta(), _PIT_LOSS))

    def test_suppressed_during_safety_car(self):
        l, c = self._states(gap=1.5, chaser_in_pit=True)
        self.assertIsNone(_eval_undercut(l, c, _meta(sc=True), _PIT_LOSS))

    def test_extra_contains_pit_loss(self):
        l, c = self._states(gap=1.8, chaser_in_pit=True)
        event = _eval_undercut(l, c, _meta(), _PIT_LOSS)
        self.assertIn("pit_lane_loss_s", event.extra)
        self.assertAlmostEqual(event.extra["pit_lane_loss_s"], _PIT_LOSS)


# ═══════════════════════════════════════════════════════════════════════════════
# OVERCUT_WINDOW
# ═══════════════════════════════════════════════════════════════════════════════

class TestOvercutWindowTrigger(unittest.TestCase):
    """Tests for the OVERCUT_WINDOW evaluator."""

    def _states(self, gap: float, leader_in_pit: bool = True, chaser_in_pit: bool = False):
        leader = {
            "code": "VER", "position": 1,
            "in_pit": leader_in_pit,
            "is_pit_out_lap": False,
            "tyre_compound": "MEDIUM", "tyre_age_laps": 20,
        }
        chaser = {
            "code": "HAM", "position": 2,
            "in_pit": chaser_in_pit,
            "interval_s": gap,
            "tyre_compound": "SOFT", "tyre_age_laps": 8,
        }
        return leader, chaser

    def test_fires_when_leader_pitting_and_gap_in_window(self):
        # Overcut delta = gap(22) - pit_loss(22) = 0 → inside [-3, +8] window
        l, c = self._states(gap=22.0, leader_in_pit=True)
        event = _eval_overcut(l, c, _meta(), _PIT_LOSS)
        self.assertIsNotNone(event)
        self.assertEqual(event.trigger, TriggerType.OVERCUT_WINDOW)

    def test_does_not_fire_when_leader_not_pitting(self):
        l, c = self._states(gap=22.0, leader_in_pit=False)
        self.assertIsNone(_eval_overcut(l, c, _meta(), _PIT_LOSS))

    def test_does_not_fire_when_chaser_also_pitting(self):
        l, c = self._states(gap=22.0, leader_in_pit=True, chaser_in_pit=True)
        self.assertIsNone(_eval_overcut(l, c, _meta(), _PIT_LOSS))

    def test_does_not_fire_when_gap_out_of_window(self):
        # delta = 40 - 22 = 18 → outside [-3, +8]
        l, c = self._states(gap=40.0, leader_in_pit=True)
        self.assertIsNone(_eval_overcut(l, c, _meta(), _PIT_LOSS))

    def test_suppressed_during_sc(self):
        l, c = self._states(gap=22.0, leader_in_pit=True)
        self.assertIsNone(_eval_overcut(l, c, _meta(sc=True), _PIT_LOSS))


# ═══════════════════════════════════════════════════════════════════════════════
# PACE_DROP
# ═══════════════════════════════════════════════════════════════════════════════

class TestPaceDropTrigger(unittest.TestCase):
    """Tests for the PACE_DROP evaluator."""

    def _driver(self, last_lap: float, avg: float, code: str = "VER", pos: int = 1):
        return {
            "code": code, "position": pos,
            "lap_time_s": last_lap,
            "rolling_avg_lap_s": avg,
            "tyre_compound": "MEDIUM",
            "tyre_age_laps": 20,
        }

    def test_fires_when_drop_exceeds_threshold(self):
        driver = self._driver(last_lap=90.5, avg=88.8)   # delta = 1.7s
        event  = _eval_pace_drop(driver, [driver], _meta(lap=44))
        self.assertIsNotNone(event)
        self.assertEqual(event.trigger, TriggerType.PACE_DROP)
        self.assertAlmostEqual(event.extra["delta_s"], 1.7, places=1)

    def test_does_not_fire_on_small_drop(self):
        driver = self._driver(last_lap=89.0, avg=88.5)   # delta = 0.5s < 0.8
        self.assertIsNone(_eval_pace_drop(driver, [driver], _meta(lap=44)))

    def test_does_not_fire_with_zero_avg(self):
        driver = self._driver(last_lap=90.0, avg=0.0)
        self.assertIsNone(_eval_pace_drop(driver, [driver], _meta(lap=44)))

    def test_severity_high_on_large_drop(self):
        driver = self._driver(last_lap=92.0, avg=88.5)   # delta = 3.5s
        event  = _eval_pace_drop(driver, [driver], _meta(lap=44))
        self.assertEqual(event.severity, "HIGH")

    def test_suppressed_during_safety_car(self):
        driver = self._driver(last_lap=90.5, avg=88.8)
        self.assertIsNone(_eval_pace_drop(driver, [driver], _meta(sc=True)))


# ═══════════════════════════════════════════════════════════════════════════════
# BOX_WINDOW
# ═══════════════════════════════════════════════════════════════════════════════

class TestBoxWindowTrigger(unittest.TestCase):
    """Tests for the BOX_WINDOW evaluator (uses per-circuit pit delta)."""

    def test_fires_when_gap_matches_pit_loss_within_margin(self):
        # Silverstone pit_loss=22.0, margin=0.5 → gap in [21.5, 22.5]
        leader = {"code": "VER", "tyre_age_laps": 22, "tyre_compound": "MEDIUM", "position": 1}
        chaser = {"code": "HAM", "interval_s": 22.3, "position": 2}
        event  = _eval_box_window(leader, chaser, _meta(track="Silverstone"), _PIT_LOSS)
        self.assertIsNotNone(event)
        self.assertEqual(event.trigger, TriggerType.BOX_WINDOW)

    def test_does_not_fire_when_gap_too_far(self):
        leader = {"code": "VER", "tyre_age_laps": 22, "tyre_compound": "MEDIUM", "position": 1}
        chaser = {"code": "HAM", "interval_s": 15.0, "position": 2}
        self.assertIsNone(_eval_box_window(leader, chaser, _meta(track="Silverstone"), _PIT_LOSS))

    def test_monaco_tighter_margin(self):
        """Monaco street circuit has a tighter box_window_margin (0.3)."""
        from backend.circuits import get_pit_loss
        monaco_loss = get_pit_loss("Monaco")  # 30.0
        leader = {"code": "LEC", "tyre_age_laps": 25, "tyre_compound": "MEDIUM", "position": 1}
        chaser = {"code": "SAI", "interval_s": monaco_loss + 0.25, "position": 2}
        # Gap = 30.25 → delta = 0.25 → within Monaco's 0.3 margin → should fire
        event  = _eval_box_window(leader, chaser, _meta(track="Monaco"), monaco_loss)
        self.assertIsNotNone(event)

    def test_suppressed_during_vsc(self):
        leader = {"code": "VER", "tyre_age_laps": 22, "tyre_compound": "MEDIUM", "position": 1}
        chaser = {"code": "HAM", "interval_s": 22.3, "position": 2}
        self.assertIsNone(_eval_box_window(leader, chaser, _meta(vsc=True), _PIT_LOSS))


# ═══════════════════════════════════════════════════════════════════════════════
# SAFETY_CAR / VIRTUAL_SC
# ═══════════════════════════════════════════════════════════════════════════════

class TestRaceControlTriggers(unittest.TestCase):
    """Tests for SAFETY_CAR and VIRTUAL_SC evaluators."""

    def test_safety_car_fires_on_transition(self):
        """Should fire when sc transitions False → True."""
        event = _eval_safety_car(_meta(sc=True), prev_sc_state=False)
        self.assertIsNotNone(event)
        self.assertEqual(event.trigger, TriggerType.SAFETY_CAR)
        self.assertEqual(event.severity, "HIGH")

    def test_safety_car_does_not_refire(self):
        """Should not fire when sc is already True (no transition)."""
        event = _eval_safety_car(_meta(sc=True), prev_sc_state=True)
        self.assertIsNone(event)

    def test_safety_car_does_not_fire_when_not_active(self):
        event = _eval_safety_car(_meta(sc=False), prev_sc_state=False)
        self.assertIsNone(event)

    def test_vsc_fires_on_transition(self):
        event = _eval_virtual_sc(_meta(vsc=True), prev_vsc_state=False)
        self.assertIsNotNone(event)
        self.assertEqual(event.trigger, TriggerType.VIRTUAL_SC)
        self.assertEqual(event.severity, "MEDIUM")

    def test_vsc_does_not_refire(self):
        event = _eval_virtual_sc(_meta(vsc=True), prev_vsc_state=True)
        self.assertIsNone(event)


# ═══════════════════════════════════════════════════════════════════════════════
# FASTEST_LAP_THREAT
# ═══════════════════════════════════════════════════════════════════════════════

class TestFastestLapThreatTrigger(unittest.TestCase):
    """Tests for the FASTEST_LAP_THREAT evaluator."""

    def _driver(self, last_lap: float, code: str = "NOR", pos: int = 3):
        return {
            "code": code, "position": pos,
            "lap_time_s": last_lap,
            "tyre_compound": "SOFT",
        }

    def test_fires_when_within_margin(self):
        # Session fastest = 86.5; lap = 86.7 → margin = 0.2 < 0.3
        event = _eval_fastest_lap_threat(
            self._driver(86.7), session_fastest_s=86.5, meta=_meta()
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.trigger, TriggerType.FASTEST_LAP_THREAT)

    def test_does_not_fire_outside_margin(self):
        # margin = 87.5 - 86.5 = 1.0 > 0.3
        self.assertIsNone(
            _eval_fastest_lap_threat(self._driver(87.5), session_fastest_s=86.5, meta=_meta())
        )

    def test_does_not_fire_when_lap_beats_fastest(self):
        # margin < 0 means this IS the new fastest — handled elsewhere
        self.assertIsNone(
            _eval_fastest_lap_threat(self._driver(86.3), session_fastest_s=86.5, meta=_meta())
        )

    def test_severity_high_within_0_1s(self):
        event = _eval_fastest_lap_threat(
            self._driver(86.55), session_fastest_s=86.5, meta=_meta()
        )
        self.assertEqual(event.severity, "HIGH")

    def test_suppressed_during_sc(self):
        self.assertIsNone(
            _eval_fastest_lap_threat(self._driver(86.6), session_fastest_s=86.5, meta=_meta(sc=True))
        )


# ═══════════════════════════════════════════════════════════════════════════════
# CooldownTracker
# ═══════════════════════════════════════════════════════════════════════════════

class TestCooldownTracker(unittest.TestCase):
    """Tests for the per-pair cooldown mechanism."""

    def test_initial_state_is_ready(self):
        tracker = CooldownTracker()
        self.assertTrue(tracker.is_ready(TriggerType.DRS_THREAT))

    def test_not_ready_after_fire(self):
        tracker = CooldownTracker()
        tracker.mark_fired(TriggerType.DRS_THREAT, "VER", "HAM")
        self.assertFalse(tracker.is_ready(TriggerType.DRS_THREAT, "VER", "HAM"))

    def test_different_pairs_are_independent(self):
        """Cooldown on VER/HAM should not block NOR/SAI."""
        tracker = CooldownTracker()
        tracker.mark_fired(TriggerType.DRS_THREAT, "VER", "HAM")
        self.assertTrue(tracker.is_ready(TriggerType.DRS_THREAT, "NOR", "SAI"))

    def test_different_triggers_independent(self):
        tracker = CooldownTracker()
        tracker.mark_fired(TriggerType.DRS_THREAT)
        self.assertTrue(tracker.is_ready(TriggerType.UNDERCUT))

    def test_sc_has_zero_cooldown(self):
        """SC triggers bypass cooldown — always ready."""
        tracker = CooldownTracker()
        tracker.mark_fired(TriggerType.SAFETY_CAR)
        self.assertTrue(tracker.is_ready(TriggerType.SAFETY_CAR))


# ═══════════════════════════════════════════════════════════════════════════════
# TriggerEngine integration
# ═══════════════════════════════════════════════════════════════════════════════

class TestTriggerEngineIntegration(unittest.TestCase):
    """
    Integration tests for TriggerEngine.evaluate() using a real FakeRedis cache.
    Verifies the engine correctly loads grid data and fires events.
    """

    def setUp(self):
        from backend.cache import CacheClient
        # Replace the module-level singleton with a fresh isolated cache
        import backend.triggers as trig_mod
        import backend.cache as cache_mod
        self._real_cache = cache_mod.cache
        self._test_cache = CacheClient()
        self._test_cache.flush()
        cache_mod.cache = self._test_cache
        trig_mod.cache  = self._test_cache
        self.engine     = TriggerEngine()

    def tearDown(self):
        import backend.triggers as trig_mod
        import backend.cache as cache_mod
        cache_mod.cache = self._real_cache
        trig_mod.cache  = self._real_cache

    def _load_two_car_grid(self, gap: float, drs_zone: bool = False):
        """Populate cache with a simple 2-car grid."""
        self._test_cache.set_state("VER", {
            "code": "VER", "position": 1, "lap": 44,
            "tyre_compound": "MEDIUM", "tyre_age_laps": 20,
            "lap_time_s": 88.5, "rolling_avg_lap_s": 88.4,
            "interval_s": 0.0, "gap_to_leader_s": 0.0,
            "speed_kph": 298.0, "in_pit": False,
        })
        self._test_cache.set_state("HAM", {
            "code": "HAM", "position": 2, "lap": 44,
            "tyre_compound": "SOFT", "tyre_age_laps": 3,
            "lap_time_s": 87.8, "rolling_avg_lap_s": 87.9,
            "interval_s": gap, "gap_to_leader_s": gap,
            "drs_detection_zone": drs_zone,
            "speed_kph": 305.0, "in_pit": False,
        })
        self._test_cache.set_race_meta("current_lap", 44)
        self._test_cache.set_race_meta("track", "Silverstone")
        self._test_cache.set_race_meta("safety_car_active", False)
        self._test_cache.set_race_meta("vsc_active", False)
        self._test_cache.set_race_meta("drs_globally_enabled", True)

    def test_drs_threat_fires_from_engine(self):
        self._load_two_car_grid(gap=0.6, drs_zone=True)
        events = self.engine.evaluate()
        types  = [e.trigger for e in events]
        self.assertIn(TriggerType.DRS_THREAT, types)

    def test_no_events_with_large_gap(self):
        self._load_two_car_grid(gap=8.0, drs_zone=False)
        events = self.engine.evaluate()
        # DRS_THREAT, UNDERCUT should NOT fire; PACE_DROP / BOX_WINDOW possible
        types = [e.trigger for e in events]
        self.assertNotIn(TriggerType.DRS_THREAT, types)
        self.assertNotIn(TriggerType.UNDERCUT, types)

    def test_engine_returns_empty_on_empty_cache(self):
        self._test_cache.flush()
        events = self.engine.evaluate()
        self.assertEqual(events, [])

    def test_safety_car_fires_from_engine(self):
        self._load_two_car_grid(gap=5.0)
        self._test_cache.set_race_meta("safety_car_active", True)
        events = self.engine.evaluate()
        types  = [e.trigger for e in events]
        self.assertIn(TriggerType.SAFETY_CAR, types)

    def test_safety_car_does_not_refire(self):
        self._load_two_car_grid(gap=5.0)
        self._test_cache.set_race_meta("safety_car_active", True)
        self.engine.evaluate()   # first call — SC fires
        events2 = self.engine.evaluate()   # second call — SC already active, no transition
        types2  = [e.trigger for e in events2]
        self.assertNotIn(TriggerType.SAFETY_CAR, types2)


# ─── Runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
