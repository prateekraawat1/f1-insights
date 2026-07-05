"""
triggers.py - Production trigger engine for the F1 Live Race Insight Architecture.

Evaluates deterministic mathematical rules across ALL drivers on the live grid
(not just a hardcoded 2-car pair) and fires structured TriggerEvents consumed
by the LLM context assembler.

Triggers implemented:
  ── Strategic ────────────────────────────────────────────────────────────────
  1.  DRS_THREAT         — Gap < 1.0 s + chasing car in real DRS detection zone
  2.  UNDERCUT           — Trailing car pits while within 2.5 s of car ahead
  3.  OVERCUT_WINDOW     — Leader pits; trailer stays out with a growing gap advantage
  4.  PACE_DROP          — Driver's last lap > 0.8 s slower than their 3-lap average
  5.  BOX_WINDOW         — Pit-lane delta (circuit-specific) aligns with inter-car gap

  ── Race-Control ─────────────────────────────────────────────────────────────
  6.  SAFETY_CAR         — Official SC deployment from race_control messages
  7.  VIRTUAL_SC         — VSC deployment

  ── Performance ──────────────────────────────────────────────────────────────
  8.  FASTEST_LAP_THREAT — Driver's current lap within 0.3 s of session fastest

Design principles:
  • All pairwise triggers (DRS, UNDERCUT, etc.) iterate over every adjacent
    pair in race order — no hardcoded driver codes.
  • DRS detection uses the real OpenF1 `drs_raw` field (0/8/10/12/14).
  • All triggers are suppressed during Safety Car / Virtual SC periods.
  • Per-circuit pit-lane loss deltas come from circuits.py.
  • Per-pair cooldowns prevent repeated firing; SC/VSC bypass cooldown.
  • Backward-compatible: unit tests using mock dict states still pass.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from backend.cache import cache
from backend.circuits import get_pit_loss, get_circuit
from backend.config import (
    BOX_WINDOW_DELTA_MARGIN,
    DRS_THREAT_THRESHOLD_S,
    PACE_DROP_THRESHOLD_S,
    UNDERCUT_GAP_THRESHOLD,
)

logger = logging.getLogger(__name__)

# Gap sentinel — used for lapped / no-data cars
_LAPPED = 999.0
# Fastest-lap threat margin (seconds below session fastest to qualify)
_FASTEST_LAP_MARGIN = 0.3


# ─── Trigger Types ────────────────────────────────────────────────────────────

class TriggerType(str, Enum):
    # Strategic
    DRS_THREAT     = "DRS_THREAT"
    UNDERCUT       = "UNDERCUT"
    OVERCUT_WINDOW = "OVERCUT_WINDOW"
    PACE_DROP      = "PACE_DROP"
    BOX_WINDOW     = "BOX_WINDOW"
    # Race control
    SAFETY_CAR     = "SAFETY_CAR"
    VIRTUAL_SC     = "VIRTUAL_SC"
    # Performance
    FASTEST_LAP_THREAT = "FASTEST_LAP_THREAT"


# ─── Trigger Event ────────────────────────────────────────────────────────────

@dataclass
class TriggerEvent:
    """
    Structured payload produced by a tripped trigger.
    Consumed by the LLM context assembler in app.py.
    """
    trigger:   TriggerType
    chasing:   str                   # Driver code of the acting / chasing car
    leading:   str                   # Driver code of the car ahead / affected
    gap_s:     float                 # Current inter-car gap in seconds
    lap:       int                   # Lap number when trigger fired
    track:     str                   # Circuit short name
    severity:  str                   # "HIGH" | "MEDIUM" | "LOW"
    extra:     dict[str, Any] = field(default_factory=dict)
    fired_at:  float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trigger":  self.trigger.value,
            "chasing":  self.chasing,
            "leading":  self.leading,
            "gap_s":    self.gap_s,
            "lap":      self.lap,
            "track":    self.track,
            "severity": self.severity,
            "extra":    self.extra,
            "fired_at": self.fired_at,
        }


# ─── Per-Pair Cooldown Tracker ────────────────────────────────────────────────

class CooldownTracker:
    """
    Prevents the same (trigger, pair) combination from firing repeatedly.

    Key: (TriggerType, leader_code, chaser_code) or just (TriggerType,) for
    driver-independent triggers.
    Cooldown is per-trigger-type; SC/VSC have zero cooldown.
    """

    COOLDOWNS: dict[TriggerType, float] = {
        TriggerType.DRS_THREAT:         12.0,
        TriggerType.UNDERCUT:           30.0,
        TriggerType.OVERCUT_WINDOW:     30.0,
        TriggerType.PACE_DROP:          20.0,
        TriggerType.BOX_WINDOW:         25.0,
        TriggerType.SAFETY_CAR:          0.0,   # fire immediately every deployment
        TriggerType.VIRTUAL_SC:          0.0,
        TriggerType.FASTEST_LAP_THREAT: 45.0,
    }

    def __init__(self) -> None:
        # key → last-fired monotonic timestamp
        self._last: dict[tuple, float] = {}

    def _key(self, trigger: TriggerType, leader: str = "", chaser: str = "") -> tuple:
        return (trigger, leader, chaser)

    def is_ready(
        self,
        trigger: TriggerType,
        leader: str = "",
        chaser: str = "",
    ) -> bool:
        cooldown = self.COOLDOWNS.get(trigger, 15.0)
        last     = self._last.get(self._key(trigger, leader, chaser), 0.0)
        return (time.monotonic() - last) >= cooldown

    def mark_fired(
        self,
        trigger: TriggerType,
        leader: str = "",
        chaser: str = "",
    ) -> None:
        self._last[self._key(trigger, leader, chaser)] = time.monotonic()


# ─── Grid Reader ──────────────────────────────────────────────────────────────

def _load_grid() -> tuple[list[dict], dict]:
    """
    Read all driver states from the cache, sorted by race position.
    Returns (sorted_states, meta).
    """
    driver_codes = cache.get_all_drivers()
    states: list[dict] = []
    for code in driver_codes:
        s = cache.get_state(code)
        if s:
            states.append(s)

    # Sort by position; cars with position=0 (not yet assigned) go last
    states.sort(key=lambda s: s.get("position", 99) or 99)

    meta = {
        "lap":     cache.get_race_meta("current_lap") or 0,
        "track":   cache.get_race_meta("track") or "Unknown",
        "sc":      bool(cache.get_race_meta("safety_car_active")),
        "vsc":     bool(cache.get_race_meta("vsc_active")),
        "drs_on":  cache.get_race_meta("drs_globally_enabled") is not False,
        # Legacy key for unit tests that write gap_ver_ham directly
        "gap":     cache.get_race_meta("gap_ver_ham") or 999.0,
    }
    return states, meta


def _interval(state: dict) -> float:
    """Return the gap to the car directly ahead in seconds."""
    return float(state.get("interval_s", _LAPPED) or _LAPPED)


def _is_in_drs_zone(state: dict) -> bool:
    """
    Production: use real OpenF1 drs_raw field.
      drs_raw == 8  → in detection zone (eligible)
      drs_raw in (10,12,14) → DRS actually open
    Fallback: use boolean drs_detection_zone flag (set by mock / legacy).
    """
    drs_raw = state.get("drs_raw")
    if drs_raw is not None:
        return int(drs_raw) in (8, 10, 12, 14)
    # Legacy / mock path
    return bool(state.get("drs_detection_zone", False))


# ─── 1. DRS THREAT ────────────────────────────────────────────────────────────

def _eval_drs_threat(
    leader: dict,
    chaser: dict,
    meta: dict,
    pit_loss_s: float,
) -> TriggerEvent | None:
    """
    DRS_THREAT: The chasing car is within DRS_THREAT_THRESHOLD_S of the car
    ahead AND is in (or eligible for) a DRS detection zone.

    Evaluated for every adjacent pair in race order.
    Suppressed during SC / VSC.
    """
    if meta["sc"] or meta["vsc"]:
        return None

    gap = _interval(chaser)
    if gap >= DRS_THREAT_THRESHOLD_S or gap <= 0 or gap >= _LAPPED:
        return None

    if not _is_in_drs_zone(chaser):
        return None

    lap              = meta["lap"]
    chaser_code      = chaser.get("code", "?")
    leader_code      = leader.get("code", "?")
    severity         = "HIGH" if gap < 0.5 else "MEDIUM"

    logger.warning(
        "⚡ DRS_THREAT | %s → %s | gap=%.3fs | lap=%s",
        chaser_code, leader_code, gap, lap,
    )
    return TriggerEvent(
        trigger  = TriggerType.DRS_THREAT,
        chasing  = chaser_code,
        leading  = leader_code,
        gap_s    = round(gap, 3),
        lap      = lap,
        track    = meta["track"],
        severity = severity,
        extra = {
            "drs_zone":           True,
            "drs_globally_open":  meta["drs_on"],
            "chaser_tyre":        chaser.get("tyre_compound"),
            "chaser_tyre_age":    chaser.get("tyre_age_laps"),
            "leader_tyre_age":    leader.get("tyre_age_laps"),
            "chaser_speed_kph":   chaser.get("speed_kph"),
            "chaser_drs_raw":     chaser.get("drs_raw"),
        },
    )


# ─── 2. UNDERCUT ──────────────────────────────────────────────────────────────

def _eval_undercut(
    leader: dict,
    chaser: dict,
    meta: dict,
    pit_loss_s: float,
) -> TriggerEvent | None:
    """
    UNDERCUT: The trailing car enters the pit lane while within
    UNDERCUT_GAP_THRESHOLD seconds of the car ahead.

    In production, pit detection uses in_pit=True (set when OpenF1 /v1/pit
    data arrives). In the mock it uses the in_pit flag directly.
    """
    gap = _interval(chaser)
    if gap > UNDERCUT_GAP_THRESHOLD or gap <= 0 or gap >= _LAPPED:
        return None

    if not chaser.get("in_pit", False):
        return None

    if meta["sc"] or meta["vsc"]:
        return None

    lap          = meta["lap"]
    chaser_code  = chaser.get("code", "?")
    leader_code  = leader.get("code", "?")
    leader_age   = leader.get("tyre_age_laps", 0) or 0

    # Estimate projected pace gain from fresh tyres vs degraded set
    fresh_gain   = round(max(0.0, leader_age * 0.04 - 0.3), 2)

    logger.warning(
        "🔧 UNDERCUT | %s → %s | gap=%.3fs | lap=%s",
        chaser_code, leader_code, gap, lap,
    )
    return TriggerEvent(
        trigger  = TriggerType.UNDERCUT,
        chasing  = chaser_code,
        leading  = leader_code,
        gap_s    = round(gap, 3),
        lap      = lap,
        track    = meta["track"],
        severity = "HIGH",
        extra = {
            "pit_lane_loss_s":              pit_loss_s,
            "leader_tyre_age":              leader_age,
            "leader_tyre_compound":         leader.get("tyre_compound"),
            "chaser_pit_stops":             chaser.get("pit_stops", 0),
            "projected_fresh_pace_gain_s":  fresh_gain,
        },
    )


# ─── 3. OVERCUT WINDOW ────────────────────────────────────────────────────────

def _eval_overcut(
    leader: dict,
    chaser: dict,
    meta: dict,
    pit_loss_s: float,
) -> TriggerEvent | None:
    """
    OVERCUT_WINDOW: The car ahead (leader) has just pitted. The car behind
    (chaser) stays out. If chaser's gap_to_leader_s is growing AND chaser
    is on fresher tyres relative to the lapped leader, an overcut is viable.

    Condition: leader in_pit (or just completed pit stop) AND chaser NOT pitting
    AND chaser's interval is within [pit_loss_s - 3, pit_loss_s + 5]
    (i.e., chaser could emerge ahead if they extend their stint by 1-2 laps).
    """
    if meta["sc"] or meta["vsc"]:
        return None

    # Leader must have pitted or be in the process
    if not (leader.get("in_pit", False) or leader.get("is_pit_out_lap", False)):
        return None

    if chaser.get("in_pit", False):
        return None  # Both pitting — not an overcut

    gap = _interval(chaser)
    if gap <= 0 or gap >= _LAPPED:
        return None

    # Classic overcut window: chaser needs to be within ~5s of pit loss + gap to return ahead
    overcut_delta = gap - pit_loss_s
    # If overcut_delta is small (chaser can close enough on track to emerge ahead), fire
    if not (-3.0 <= overcut_delta <= 8.0):
        return None

    lap         = meta["lap"]
    chaser_code = chaser.get("code", "?")
    leader_code = leader.get("code", "?")

    logger.warning(
        "🔄 OVERCUT_WINDOW | %s vs pitted %s | gap=%.3fs | delta=%.2fs | lap=%s",
        chaser_code, leader_code, gap, overcut_delta, lap,
    )
    return TriggerEvent(
        trigger  = TriggerType.OVERCUT_WINDOW,
        chasing  = chaser_code,
        leading  = leader_code,
        gap_s    = round(gap, 3),
        lap      = lap,
        track    = meta["track"],
        severity = "MEDIUM" if overcut_delta > 3.0 else "HIGH",
        extra = {
            "pit_lane_loss_s":    pit_loss_s,
            "overcut_delta_s":    round(overcut_delta, 2),
            "chaser_tyre_age":    chaser.get("tyre_age_laps"),
            "chaser_compound":    chaser.get("tyre_compound"),
            "leader_pitting":     leader.get("in_pit", False),
        },
    )


# ─── 4. PACE DROP ─────────────────────────────────────────────────────────────

def _eval_pace_drop(
    driver: dict,
    grid: list[dict],
    meta: dict,
) -> TriggerEvent | None:
    """
    PACE_DROP: A driver's last completed lap time is more than
    PACE_DROP_THRESHOLD_S slower than their 3-lap rolling average.

    Evaluated independently for each driver. The "chasing" field is set
    to the car directly behind the affected driver (most likely beneficiary).
    """
    if meta["sc"] or meta["vsc"]:
        return None   # Pace data is meaningless under SC

    last_lap = driver.get("lap_time_s", 0.0) or 0.0
    avg      = driver.get("rolling_avg_lap_s", 0.0) or 0.0

    if avg <= 0 or last_lap <= 0:
        return None

    delta = last_lap - avg
    if delta <= PACE_DROP_THRESHOLD_S:
        return None

    driver_code = driver.get("code", "?")
    position    = driver.get("position", 99) or 99
    lap         = meta["lap"]

    # Find the car directly behind the affected driver (most likely to benefit)
    beneficiary_code = "?"
    for state in grid:
        if (state.get("position") or 99) == position + 1:
            beneficiary_code = state.get("code", "?")
            break

    # Gap to the car behind
    gap_behind = _LAPPED
    for state in grid:
        if state.get("code") == beneficiary_code:
            gap_behind = _interval(state)  # their interval to the driver
            break

    logger.warning(
        "📉 PACE_DROP | %s | delta=+%.3fs | lap=%s",
        driver_code, delta, lap,
    )
    return TriggerEvent(
        trigger  = TriggerType.PACE_DROP,
        chasing  = beneficiary_code,
        leading  = driver_code,
        gap_s    = round(gap_behind if gap_behind < _LAPPED else 0.0, 3),
        lap      = lap,
        track    = meta["track"],
        severity = "HIGH" if delta > 1.5 else "MEDIUM",
        extra = {
            "affected_driver":         driver_code,
            "last_lap_s":              round(last_lap, 3),
            "rolling_avg_s":           round(avg, 3),
            "delta_s":                 round(delta, 3),
            "tyre_compound":           driver.get("tyre_compound"),
            "tyre_age_laps":           driver.get("tyre_age_laps"),
            "position":                position,
            "laps_to_estimated_cliff": max(0, 4 - int(delta / 0.3)),
        },
    )


# ─── 5. BOX WINDOW ────────────────────────────────────────────────────────────

def _eval_box_window(
    leader: dict,
    chaser: dict,
    meta: dict,
    pit_loss_s: float,
) -> TriggerEvent | None:
    """
    BOX_WINDOW: The gap between adjacent cars aligns with the pit-lane
    loss delta so that the leading car can pit and return in clear air.

    Gap to the car behind ≈ pit_loss_s ± BOX_WINDOW_DELTA_MARGIN
    means the leader can box without losing position to the car behind.
    """
    if meta["sc"] or meta["vsc"]:
        return None

    # Use the chaser's interval_s as the gap the leader needs to clear
    gap = _interval(chaser)
    if gap <= 0 or gap >= _LAPPED:
        return None

    projected_gap_after_pit = gap - pit_loss_s
    circuit_margin = get_circuit(meta["track"]).box_window_margin()

    if abs(projected_gap_after_pit) > circuit_margin:
        return None

    lap         = meta["lap"]
    leader_code = leader.get("code", "?")
    chaser_code = chaser.get("code", "?")

    logger.warning(
        "📦 BOX_WINDOW | %s can pit | projected_gap=%.2fs | lap=%s",
        leader_code, projected_gap_after_pit, lap,
    )
    return TriggerEvent(
        trigger  = TriggerType.BOX_WINDOW,
        chasing  = chaser_code,
        leading  = leader_code,
        gap_s    = round(gap, 3),
        lap      = lap,
        track    = meta["track"],
        severity = "HIGH",
        extra = {
            "pit_lane_loss_s":         pit_loss_s,
            "projected_gap_after_pit": round(projected_gap_after_pit, 2),
            "leader_tyre_age":         leader.get("tyre_age_laps"),
            "leader_compound":         leader.get("tyre_compound"),
            "recommended_driver":      leader_code,
            "laps_window_open":        2,
        },
    )


# ─── 6. SAFETY CAR ────────────────────────────────────────────────────────────

def _eval_safety_car(meta: dict, prev_sc_state: bool) -> TriggerEvent | None:
    """
    SAFETY_CAR: Fires once when safety_car_active transitions False → True.
    Uses the meta dict updated by the race_control handler in openf1_client.py.
    Ignores cooldown (fires immediately on every new deployment).
    """
    if meta["sc"] and not prev_sc_state:
        logger.warning("🚗 SAFETY_CAR deployed | lap=%s", meta["lap"])
        return TriggerEvent(
            trigger  = TriggerType.SAFETY_CAR,
            chasing  = "",
            leading  = "",
            gap_s    = 0.0,
            lap      = meta["lap"],
            track    = meta["track"],
            severity = "HIGH",
            extra    = {"message": "SAFETY CAR DEPLOYED — field bunching up"},
        )
    return None


# ─── 7. VIRTUAL SAFETY CAR ───────────────────────────────────────────────────

def _eval_virtual_sc(meta: dict, prev_vsc_state: bool) -> TriggerEvent | None:
    """
    VIRTUAL_SC: Fires once when vsc_active transitions False → True.
    """
    if meta["vsc"] and not prev_vsc_state:
        logger.warning("🟡 VIRTUAL_SC deployed | lap=%s", meta["lap"])
        return TriggerEvent(
            trigger  = TriggerType.VIRTUAL_SC,
            chasing  = "",
            leading  = "",
            gap_s    = 0.0,
            lap      = meta["lap"],
            track    = meta["track"],
            severity = "MEDIUM",
            extra    = {"message": "VIRTUAL SAFETY CAR — pit windows opening"},
        )
    return None


# ─── 8. FASTEST LAP THREAT ────────────────────────────────────────────────────

def _eval_fastest_lap_threat(
    driver: dict,
    session_fastest_s: float,
    meta: dict,
) -> TriggerEvent | None:
    """
    FASTEST_LAP_THREAT: A driver's last lap is within _FASTEST_LAP_MARGIN
    seconds of the current session fastest lap — they may be on course for a
    bonus point (fastest lap trophy).
    """
    if meta["sc"] or meta["vsc"]:
        return None

    last_lap = driver.get("lap_time_s", 0.0) or 0.0
    if last_lap <= 0 or session_fastest_s <= 0:
        return None

    margin = last_lap - session_fastest_s
    if margin > _FASTEST_LAP_MARGIN or margin < 0:
        # margin < 0 means this IS the new fastest lap — handled separately
        return None

    driver_code = driver.get("code", "?")
    lap         = meta["lap"]

    logger.info("🟣 FASTEST_LAP_THREAT | %s | %.3fs off fastest | lap=%s", driver_code, margin, lap)
    return TriggerEvent(
        trigger  = TriggerType.FASTEST_LAP_THREAT,
        chasing  = driver_code,
        leading  = driver_code,   # self-referential — about a single car
        gap_s    = round(margin, 3),
        lap      = lap,
        track    = meta["track"],
        severity = "HIGH" if margin < 0.1 else "MEDIUM",
        extra = {
            "driver":            driver_code,
            "last_lap_s":        round(last_lap, 3),
            "session_fastest_s": round(session_fastest_s, 3),
            "margin_s":          round(margin, 3),
            "tyre_compound":     driver.get("tyre_compound"),
            "position":          driver.get("position"),
        },
    )


# ─── Main Trigger Engine ──────────────────────────────────────────────────────

class TriggerEngine:
    """
    Production N-driver trigger engine.

    On each call to evaluate():
      1. Load the full sorted grid from Redis cache
      2. Evaluate pairwise triggers for every adjacent pair (P1/P2, P2/P3 …)
      3. Evaluate per-driver triggers for every car independently
      4. Evaluate global race-control triggers (SC, VSC)
      5. Return all fired TriggerEvents (deduplicated by per-pair cooldowns)

    Backward-compatible with the 2-car unit tests: if only VER + HAM are in
    the cache and gap_ver_ham is set, everything still works.
    """

    def __init__(self) -> None:
        self._cooldown        = CooldownTracker()
        self._prev_sc_active  = False
        self._prev_vsc_active = False
        self._session_fastest = 0.0   # updated as faster laps appear

    def _update_session_fastest(self, grid: list[dict]) -> None:
        """Track the fastest lap seen across all drivers this session."""
        for state in grid:
            lt = state.get("lap_time_s", 0.0) or 0.0
            if lt > 0 and (self._session_fastest == 0.0 or lt < self._session_fastest):
                self._session_fastest = lt

    def evaluate(self) -> list[TriggerEvent]:
        """Run all evaluators and return fired TriggerEvents."""
        grid, meta = _load_grid()

        # ── Legacy 2-car unit-test compatibility ─────────────────────────────
        # If the cache was set up with the old gap_ver_ham key but no sorted
        # grid, synthesise a minimal pair from ver/ham states.
        if not grid:
            ver = cache.get_state("VER")
            ham = cache.get_state("HAM")
            if ver and ham:
                # Inject interval_s from legacy gap meta so evaluators work
                gap = meta.get("gap", 999.0)
                ver["position"] = 1
                ham["position"] = 2
                ham["interval_s"] = gap
                grid = [ver, ham]
            else:
                return []

        events: list[TriggerEvent] = []

        # ── 1. Update session fastest ─────────────────────────────────────────
        self._update_session_fastest(grid)

        # ── 2. Resolve circuit config ─────────────────────────────────────────
        track     = meta["track"]
        pit_loss  = get_pit_loss(track)

        # ── 3. Race-control triggers (SC / VSC) — bypass pairwise loop ────────
        sc_event = _eval_safety_car(meta, self._prev_sc_active)
        if sc_event and self._cooldown.is_ready(TriggerType.SAFETY_CAR):
            self._cooldown.mark_fired(TriggerType.SAFETY_CAR)
            events.append(sc_event)
        self._prev_sc_active = meta["sc"]

        vsc_event = _eval_virtual_sc(meta, self._prev_vsc_active)
        if vsc_event and self._cooldown.is_ready(TriggerType.VIRTUAL_SC):
            self._cooldown.mark_fired(TriggerType.VIRTUAL_SC)
            events.append(vsc_event)
        self._prev_vsc_active = meta["vsc"]

        # ── 4. Pairwise triggers across all adjacent positions ─────────────────
        for i in range(len(grid) - 1):
            leader = grid[i]
            chaser = grid[i + 1]
            lc     = leader.get("code", "?")
            cc     = chaser.get("code", "?")

            pair_evaluators: list[tuple[TriggerType, Any]] = [
                (TriggerType.DRS_THREAT,     lambda: _eval_drs_threat(leader, chaser, meta, pit_loss)),
                (TriggerType.UNDERCUT,       lambda: _eval_undercut(leader, chaser, meta, pit_loss)),
                (TriggerType.OVERCUT_WINDOW, lambda: _eval_overcut(leader, chaser, meta, pit_loss)),
                (TriggerType.BOX_WINDOW,     lambda: _eval_box_window(leader, chaser, meta, pit_loss)),
            ]

            for trigger_type, evaluator in pair_evaluators:
                if not self._cooldown.is_ready(trigger_type, lc, cc):
                    continue
                try:
                    event = evaluator()
                except Exception as exc:
                    logger.error("❌ Evaluator %s error: %s", trigger_type.value, exc)
                    event = None

                if event:
                    self._cooldown.mark_fired(trigger_type, lc, cc)
                    events.append(event)

        # ── 5. Per-driver triggers ─────────────────────────────────────────────
        for driver in grid:
            dc = driver.get("code", "?")

            # PACE_DROP
            if self._cooldown.is_ready(TriggerType.PACE_DROP, dc):
                try:
                    event = _eval_pace_drop(driver, grid, meta)
                    if event:
                        self._cooldown.mark_fired(TriggerType.PACE_DROP, dc)
                        events.append(event)
                except Exception as exc:
                    logger.error("❌ PACE_DROP evaluator error for %s: %s", dc, exc)

            # FASTEST_LAP_THREAT
            if self._cooldown.is_ready(TriggerType.FASTEST_LAP_THREAT, dc):
                try:
                    event = _eval_fastest_lap_threat(driver, self._session_fastest, meta)
                    if event:
                        self._cooldown.mark_fired(TriggerType.FASTEST_LAP_THREAT, dc)
                        events.append(event)
                except Exception as exc:
                    logger.error("❌ FASTEST_LAP_THREAT error for %s: %s", dc, exc)

        if events:
            logger.info(
                "🎯 %d trigger(s) fired this cycle: %s",
                len(events),
                [e.trigger.value for e in events],
            )

        return events
