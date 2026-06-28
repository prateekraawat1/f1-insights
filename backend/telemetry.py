"""
telemetry.py - Mock live telemetry ingestion for the F1 Race Insight prototype.

Simulates a 2-car scenario at Lap 42 of the British Grand Prix at Silverstone.
The simulation runs a scripted event sequence designed to trip every trigger
in the trigger engine: DRS_THREAT → UNDERCUT → PACE_DROP → BOX_WINDOW.

Each tick updates a DriverState in the Redis cache, exactly as a real
telemetry pipeline would after consuming a low-latency stream.
"""

import asyncio
import logging
import random
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from backend.cache import cache
from backend.config import (
    RACE_NAME,
    TELEMETRY_POLL_INTERVAL,
    TOTAL_LAPS,
    TRACK_NAME,
)

logger = logging.getLogger(__name__)


# ─── Data Model ───────────────────────────────────────────────────────────────

@dataclass
class DriverState:
    """Full telemetry snapshot for a single driver at a point in time."""
    code: str                      # 3-letter driver code, e.g. "VER"
    name: str                      # Full name
    team: str                      # Constructor name
    position: int                  # Current race position
    lap: int                       # Current lap number
    lap_time_s: float              # Last completed lap time in seconds
    rolling_avg_lap_s: float       # 3-lap rolling average in seconds
    lap_history: list[float]       # Last 5 lap times for rolling average
    gap_ahead_s: float             # Gap to the car directly ahead in seconds (0 if P1)
    interval_s: float              # Interval to car behind in seconds (0 if last)
    tyre_compound: str             # "SOFT" | "MEDIUM" | "HARD"
    tyre_age_laps: int             # How many laps the current set has been on
    drs_enabled: bool              # Is DRS currently deployed?
    drs_detection_zone: bool       # Is car in a DRS detection zone?
    in_pit: bool                   # Is the car currently in the pit lane?
    pit_stops: int                 # Total pit stops taken
    speed_kph: float               # Current speed in km/h
    throttle_pct: float            # Throttle application 0–100%
    brake_pct: float               # Brake application 0–100%
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─── Scripted Event Sequence ───────────────────────────────────────────────────

# Each entry is (lap_offset, gap_s, ham_in_pit, notes)
# lap_offset is relative to the simulation start lap (42).
SCRIPT: list[tuple[int, float, bool, str]] = [
    (0,  3.80, False, "Opening state — HAM well outside DRS"),
    (0,  2.90, False, "HAM closes at Stowe complex"),
    (0,  2.10, False, "HAM pushes hard on fresh tyres"),
    (0,  1.55, False, "Gap halving — VER on 15-lap-old mediums"),
    (0,  1.12, False, "HAM within striking distance"),
    (0,  0.95, False, "⚡ DRS THREAT: GAP < 1.0s — DRS detection zone"),   # → DRS_THREAT
    (0,  0.78, False, "HAM right on VER's tail through Brooklands"),
    (1,  0.68, False, "Lap 43 begins — HAM fully in DRS window"),
    (1,  1.40, True,  "🔧 HAM pits from 1.40s behind — UNDERCUT BID"),     # → UNDERCUT
    (1,  22.5, False, "HAM rejoins P2 after pit stop — fresh softs"),
    (2,  20.1, False, "VER leading on old mediums — delta narrowing fast"),
    (2,  16.4, False, "HAM setting purple sectors on new rubber"),
    (3,  12.8, False, "VER lap times deteriorating — tyre cliff incoming"), # → PACE_DROP
    (3,  8.50, False, "VER fighting understeer at Copse"),
    (4,  5.20, False, "HAM eating into VER's lead — BOX WINDOW opens"),    # → BOX_WINDOW
    (4,  3.10, False, "VER must pit — 2 laps to close the window"),
    (5,  1.90, False, "HAM pushing for the race lead"),
    (5,  0.55, False, "⚡ DRS THREAT again — HAM chasing hard"),             # → DRS_THREAT
    (6,  0.20, False, "Side-by-side through Brooklands — incredible racing!"),
    (6, -0.30, False, "HAM goes past — P1 changes hands!"),  # negative = HAM leads
]


# ─── Simulator ────────────────────────────────────────────────────────────────

class TelemetrySimulator:
    """
    Drives the scripted race event sequence, writing DriverState objects
    into the cache at every tick.
    """

    START_LAP = 42

    def __init__(self) -> None:
        self._tick        = 0
        self._current_lap = self.START_LAP
        self._ham_pitted  = False
        self._ver_pitted  = False

        # Initialise base lap histories (approx Silverstone pace ~88-89s)
        self._ver_lap_history: list[float] = [88.4, 88.6, 88.5]
        self._ham_lap_history: list[float] = [88.2, 88.3, 88.1]

    def _rolling_avg(self, history: list[float]) -> float:
        last3 = history[-3:]
        return round(sum(last3) / len(last3), 3)

    def _jitter(self, val: float, spread: float = 0.05) -> float:
        return round(val + random.uniform(-spread, spread), 3)

    def _build_ver_state(self, gap: float, in_pit: bool) -> DriverState:
        lap_offset = self._tick // max(1, len(SCRIPT) // TOTAL_LAPS)
        lap = min(self.START_LAP + (self._tick // 4), TOTAL_LAPS)

        # Simulate tyre degradation as laps accumulate
        tyre_age = 15 + self._tick
        deg_factor = min(tyre_age * 0.04, 2.5)

        lap_time = self._jitter(88.5 + deg_factor)
        self._ver_lap_history.append(lap_time)

        speed = self._jitter(300.0 if not in_pit else 80.0, 2.0)
        leading = gap >= 0  # VER leads when gap is positive

        return DriverState(
            code="VER",
            name="Max Verstappen",
            team="Red Bull Racing",
            position=1 if leading else 2,
            lap=lap,
            lap_time_s=round(lap_time, 3),
            rolling_avg_lap_s=self._rolling_avg(self._ver_lap_history),
            lap_history=self._ver_lap_history[-5:],
            gap_ahead_s=0.0,
            interval_s=abs(gap),
            tyre_compound="MEDIUM",
            tyre_age_laps=tyre_age,
            drs_enabled=False,
            drs_detection_zone=False,
            in_pit=in_pit,
            pit_stops=1 if self._ver_pitted else 0,
            speed_kph=round(speed, 1),
            throttle_pct=self._jitter(94.0, 2.0),
            brake_pct=self._jitter(0.0, 1.0),
        )

    def _build_ham_state(self, gap: float, in_pit: bool) -> DriverState:
        lap = min(self.START_LAP + (self._tick // 4), TOTAL_LAPS)

        # HAM fresh tyres after pit — significantly faster
        tyre_age = 1 if self._ham_pitted else (12 + self._tick)
        compound = "SOFT" if self._ham_pitted else "MEDIUM"

        base_pace = 87.8 if self._ham_pitted else 88.2
        lap_time  = self._jitter(base_pace)
        self._ham_lap_history.append(lap_time)

        speed = self._jitter(305.0 if not in_pit else 80.0, 2.0)
        leading = gap < 0  # HAM leads when gap is negative

        return DriverState(
            code="HAM",
            name="Lewis Hamilton",
            team="Mercedes-AMG",
            position=2 if not leading else 1,
            lap=lap,
            lap_time_s=round(lap_time, 3),
            rolling_avg_lap_s=self._rolling_avg(self._ham_lap_history),
            lap_history=self._ham_lap_history[-5:],
            gap_ahead_s=abs(gap) if not leading else 0.0,
            interval_s=0.0,
            tyre_compound=compound,
            tyre_age_laps=tyre_age,
            drs_enabled=gap < 1.0 and not in_pit,
            drs_detection_zone=gap < 1.0,
            in_pit=in_pit,
            pit_stops=1 if self._ham_pitted else 0,
            speed_kph=round(speed, 1),
            throttle_pct=self._jitter(95.0, 2.0),
            brake_pct=self._jitter(0.0, 1.0),
        )

    def tick(self) -> dict[str, Any]:
        """
        Advance the simulation by one step, write states to cache,
        and return a snapshot payload for broadcasting.
        """
        idx = min(self._tick, len(SCRIPT) - 1)
        lap_offset, gap, ham_in_pit, note = SCRIPT[idx]

        if ham_in_pit and not self._ham_pitted:
            self._ham_pitted = True
            logger.info("🔧 Hamilton pits on Lap %s", self.START_LAP + lap_offset)

        ver_state = self._build_ver_state(gap, False)
        ham_state = self._build_ham_state(gap, ham_in_pit)

        cache.set_state("VER", ver_state.to_dict())
        cache.set_state("HAM", ham_state.to_dict())
        cache.set_race_meta("current_lap", ver_state.lap)
        cache.set_race_meta("track", TRACK_NAME)
        cache.set_race_meta("race", RACE_NAME)
        cache.set_race_meta("gap_ver_ham", gap)
        cache.set_race_meta("script_note", note)

        logger.info(
            "📡 Tick %02d | Lap %s | VER–HAM gap: %+.2fs | %s",
            self._tick, ver_state.lap, gap, note,
        )

        self._tick = (self._tick + 1) % len(SCRIPT)
        return {
            "VER": ver_state.to_dict(),
            "HAM": ham_state.to_dict(),
            "meta": {
                "lap": ver_state.lap,
                "track": TRACK_NAME,
                "race": RACE_NAME,
                "gap": gap,
                "note": note,
            },
        }


# ─── Async runner ─────────────────────────────────────────────────────────────

async def run_telemetry_loop(
    simulator: TelemetrySimulator,
    broadcast_callback=None,
) -> None:
    """
    Continuously tick the telemetry simulator and optionally invoke
    a broadcast_callback(snapshot) for each update.
    """
    logger.info("🏁 Telemetry simulator started — track: %s", TRACK_NAME)
    while True:
        snapshot = simulator.tick()
        if broadcast_callback:
            await broadcast_callback(snapshot)
        await asyncio.sleep(TELEMETRY_POLL_INTERVAL)
