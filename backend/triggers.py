"""
triggers.py - Deterministic mathematical trigger engine for F1 race events.

Reads the current grid state from the Redis cache and evaluates four
rule conditions. When a rule trips, it returns a structured TriggerEvent
that the LLM context assembler can act upon.

Triggers implemented:
  1. DRS_THREAT   — Gap < 1.0 s at DRS detection zone
  2. UNDERCUT     — Trailing car pits while within 2.5 s
  3. PACE_DROP    — Last lap > 0.8 s slower than 3-lap rolling average
  4. BOX_WINDOW   — Pit-lane delta matches gap to clear-air traffic
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from backend.cache import cache
from backend.config import (
    BOX_WINDOW_DELTA_MARGIN,
    DRS_THREAT_THRESHOLD_S,
    PACE_DROP_THRESHOLD_S,
    UNDERCUT_GAP_THRESHOLD,
)

logger = logging.getLogger(__name__)


# ─── Trigger Types ────────────────────────────────────────────────────────────

class TriggerType(str, Enum):
    DRS_THREAT  = "DRS_THREAT"
    UNDERCUT    = "UNDERCUT"
    PACE_DROP   = "PACE_DROP"
    BOX_WINDOW  = "BOX_WINDOW"


@dataclass
class TriggerEvent:
    """
    Structured payload describing a tripped trigger.
    This is consumed directly by the LLM context assembler.
    """
    trigger:      TriggerType
    chasing:      str                  # Driver code of the chasing/acting car
    leading:      str                  # Driver code of the leading car
    gap_s:        float                # Current inter-car gap in seconds
    lap:          int                  # Lap number when trigger fired
    track:        str                  # Circuit name
    severity:     str                  # "HIGH" | "MEDIUM" | "LOW"
    extra:        dict[str, Any] = field(default_factory=dict)
    fired_at:     float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trigger":   self.trigger.value,
            "chasing":   self.chasing,
            "leading":   self.leading,
            "gap_s":     self.gap_s,
            "lap":       self.lap,
            "track":     self.track,
            "severity":  self.severity,
            "extra":     self.extra,
            "fired_at":  self.fired_at,
        }


# ─── Cooldown Tracker ─────────────────────────────────────────────────────────

class CooldownTracker:
    """
    Prevents the same trigger from firing repeatedly in a short window.
    Each trigger type has its own cooldown period.
    """
    COOLDOWNS: dict[TriggerType, float] = {
        TriggerType.DRS_THREAT: 12.0,
        TriggerType.UNDERCUT:   30.0,
        TriggerType.PACE_DROP:  20.0,
        TriggerType.BOX_WINDOW: 25.0,
    }

    def __init__(self) -> None:
        self._last_fired: dict[TriggerType, float] = {}

    def is_ready(self, trigger: TriggerType) -> bool:
        cooldown  = self.COOLDOWNS.get(trigger, 15.0)
        last      = self._last_fired.get(trigger, 0.0)
        return (time.monotonic() - last) >= cooldown

    def mark_fired(self, trigger: TriggerType) -> None:
        self._last_fired[trigger] = time.monotonic()


# ─── Pit-Lane Loss Delta (Silverstone approximate) ────────────────────────────
PIT_LANE_LOSS_S = 22.0   # seconds lost per pit stop at Silverstone


# ─── Individual Trigger Evaluators ────────────────────────────────────────────

def _eval_drs_threat(
    ver: dict, ham: dict, meta: dict
) -> TriggerEvent | None:
    """
    DRS_THREAT: Gap between the chasing car (HAM) and the leading car (VER)
    falls below DRS_THREAT_THRESHOLD_S and the chasing car is in the
    DRS detection zone.
    """
    gap = meta.get("gap", 999.0)
    if gap < 0:
        # HAM already leads — no DRS threat in this direction
        return None

    if gap < DRS_THREAT_THRESHOLD_S and ham.get("drs_detection_zone", False):
        lap  = meta.get("lap", 0)
        severity = "HIGH" if gap < 0.5 else "MEDIUM"
        logger.warning("⚡ TRIGGER: DRS_THREAT | gap=%.2fs | lap=%s", gap, lap)
        return TriggerEvent(
            trigger=TriggerType.DRS_THREAT,
            chasing="HAM",
            leading="VER",
            gap_s=round(gap, 3),
            lap=lap,
            track=meta.get("track", "Unknown"),
            severity=severity,
            extra={
                "drs_zone":        True,
                "ham_tyre":        ham.get("tyre_compound"),
                "ham_tyre_age":    ham.get("tyre_age_laps"),
                "ver_tyre_age":    ver.get("tyre_age_laps"),
                "ham_speed_kph":   ham.get("speed_kph"),
            },
        )
    return None


def _eval_undercut(
    ver: dict, ham: dict, meta: dict
) -> TriggerEvent | None:
    """
    UNDERCUT: Trailing car (HAM) enters the pit lane while the gap to
    the leading car (VER) is within UNDERCUT_GAP_THRESHOLD.
    """
    gap = meta.get("gap", 999.0)
    if gap < 0:
        return None  # HAM already leads

    if ham.get("in_pit", False) and gap <= UNDERCUT_GAP_THRESHOLD:
        lap = meta.get("lap", 0)
        logger.warning("🔧 TRIGGER: UNDERCUT | gap=%.2fs | lap=%s", gap, lap)
        return TriggerEvent(
            trigger=TriggerType.UNDERCUT,
            chasing="HAM",
            leading="VER",
            gap_s=round(gap, 3),
            lap=lap,
            track=meta.get("track", "Unknown"),
            severity="HIGH",
            extra={
                "pit_lane_loss_s":    PIT_LANE_LOSS_S,
                "ver_tyre_age":       ver.get("tyre_age_laps"),
                "ver_tyre_compound":  ver.get("tyre_compound"),
                "ham_pit_stops":      ham.get("pit_stops"),
                "projected_fresh_pace_gain_s": round(
                    max(0.0, ver.get("tyre_age_laps", 0) * 0.04 - 0.3), 2
                ),
            },
        )
    return None


def _eval_pace_drop(
    driver_code: str, state: dict, meta: dict
) -> TriggerEvent | None:
    """
    PACE_DROP: A driver's last completed lap time is more than
    PACE_DROP_THRESHOLD_S slower than their 3-lap rolling average.
    """
    last_lap = state.get("lap_time_s", 0.0)
    avg      = state.get("rolling_avg_lap_s", 0.0)

    if avg == 0:
        return None

    delta = last_lap - avg
    if delta > PACE_DROP_THRESHOLD_S:
        lap = meta.get("lap", 0)
        logger.warning(
            "📉 TRIGGER: PACE_DROP | driver=%s | delta=+%.3fs | lap=%s",
            driver_code, delta, lap,
        )

        # Determine the other driver
        other = "HAM" if driver_code == "VER" else "VER"
        other_state = cache.get_state(other) or {}

        return TriggerEvent(
            trigger=TriggerType.PACE_DROP,
            chasing=other,
            leading=driver_code,
            gap_s=round(meta.get("gap", 0.0), 3),
            lap=lap,
            track=meta.get("track", "Unknown"),
            severity="HIGH" if delta > 1.5 else "MEDIUM",
            extra={
                "affected_driver":    driver_code,
                "last_lap_s":         round(last_lap, 3),
                "rolling_avg_s":      round(avg, 3),
                "delta_s":            round(delta, 3),
                "tyre_compound":      state.get("tyre_compound"),
                "tyre_age_laps":      state.get("tyre_age_laps"),
                "laps_to_estimated_cliff": max(0, 4 - int(delta / 0.3)),
            },
        )
    return None


def _eval_box_window(
    ver: dict, ham: dict, meta: dict
) -> TriggerEvent | None:
    """
    BOX_WINDOW: The gap to clear traffic ahead matches the pit-lane
    loss delta within BOX_WINDOW_DELTA_MARGIN — the leader can now
    pit and return in clear air.
    """
    gap = meta.get("gap", 999.0)
    if gap < 0:
        return None  # HAM leading — different scenario

    # VER's gap ahead is effectively infinite (P1); look at VER's interval
    # The "traffic" here is modelled as the gap HAM would need to pit into
    # clear air: gap + PIT_LANE_LOSS_S must be within margin of HAM's gap.
    projected_gap_after_ver_pit = abs(gap) - PIT_LANE_LOSS_S

    if abs(projected_gap_after_ver_pit) <= BOX_WINDOW_DELTA_MARGIN:
        lap = meta.get("lap", 0)
        logger.warning(
            "📦 TRIGGER: BOX_WINDOW | projected_gap=%.2fs | lap=%s",
            projected_gap_after_ver_pit, lap,
        )
        return TriggerEvent(
            trigger=TriggerType.BOX_WINDOW,
            chasing="HAM",
            leading="VER",
            gap_s=round(gap, 3),
            lap=lap,
            track=meta.get("track", "Unknown"),
            severity="HIGH",
            extra={
                "pit_lane_loss_s":          PIT_LANE_LOSS_S,
                "projected_gap_after_pit":  round(projected_gap_after_ver_pit, 2),
                "ver_tyre_age":             ver.get("tyre_age_laps"),
                "recommended_driver":       "VER",
                "laps_window_open":         2,
            },
        )
    return None


# ─── Main Trigger Engine ──────────────────────────────────────────────────────

class TriggerEngine:
    """
    Reads the current grid state from the cache and evaluates all
    trigger conditions in priority order. Returns a list of fired
    TriggerEvent objects (usually 0 or 1 per tick).
    """

    def __init__(self) -> None:
        self._cooldown = CooldownTracker()

    def evaluate(self) -> list[TriggerEvent]:
        """Run all evaluators and return any triggered events."""
        ver  = cache.get_state("VER")
        ham  = cache.get_state("HAM")
        meta = {
            "gap":   cache.get_race_meta("gap_ver_ham") or 999.0,
            "lap":   cache.get_race_meta("current_lap") or 0,
            "track": cache.get_race_meta("track") or "Unknown",
        }

        if not ver or not ham:
            return []

        events: list[TriggerEvent] = []

        evaluators = [
            (TriggerType.DRS_THREAT,  lambda: _eval_drs_threat(ver, ham, meta)),
            (TriggerType.UNDERCUT,    lambda: _eval_undercut(ver, ham, meta)),
            (TriggerType.PACE_DROP,   lambda: _eval_pace_drop("VER", ver, meta)),
            (TriggerType.BOX_WINDOW,  lambda: _eval_box_window(ver, ham, meta)),
        ]

        for trigger_type, evaluator in evaluators:
            if not self._cooldown.is_ready(trigger_type):
                continue
            event = evaluator()
            if event:
                self._cooldown.mark_fired(trigger_type)
                events.append(event)

        return events
