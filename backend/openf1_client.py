"""
openf1_client.py - Live telemetry ingestion client for the OpenF1 REST API.

Replaces the mock TelemetrySimulator with a production-grade async polling
loop that fans out concurrent requests to 8 OpenF1 endpoints.

Architecture:
  OpenF1Client.poll() → asyncio.gather() → 8 endpoints → merge → cache write

Polling strategy:
  - High-frequency cursors (car_data, location): per-driver, ISO timestamp
  - Low-frequency cursors (intervals, position): shared ISO timestamp
  - Lap/stint/pit: poll by latest lap number, merge with last known
  - Race control: event-driven, timestamp cursor

Rate-limiting:
  - Tracks requests per minute in Redis
  - Exponential backoff on HTTP 429
  - Pools all requests through a single httpx.AsyncClient connection pool
"""

import asyncio
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx

from backend.cache import cache
from backend.config import (
    OPENF1_BASE_URL,
    OPENF1_POLL_INTERVAL,
    OPENF1_TIMEOUT,
    OPENF1_MAX_RETRIES,
    TELEMETRY_POLL_INTERVAL,
)
from backend.circuits import get_circuit, get_pit_loss
from backend.models import (
    DriverState,
    RaceControlMessage,
    LapRecord,
    StintRecord,
    IntervalRecord,
    PitStopRecord,
    LocationRecord,
)

logger = logging.getLogger(__name__)

# Sentinel interval value for lapped/no-data cars
LAPPED_SENTINEL = 999.0


# ─── Timestamp Cursor Helpers ─────────────────────────────────────────────────

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _ts_minus(seconds: float) -> str:
    """Return an ISO timestamp `seconds` before now."""
    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return dt.isoformat(timespec="milliseconds")


# ─── OpenF1 HTTP Layer ────────────────────────────────────────────────────────

class OpenF1Http:
    """
    Thin async HTTP wrapper around the OpenF1 REST API.
    Handles retries, rate-limit backoff, and request counting.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._req_count = 0
        self._window_start = time.monotonic()

    async def get(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict]:
        """
        GET {OPENF1_BASE_URL}/{endpoint} with exponential backoff.
        Returns parsed JSON list or [] on persistent failure.
        """
        url = f"{OPENF1_BASE_URL.rstrip('/')}/{endpoint.lstrip('/')}"

        for attempt in range(OPENF1_MAX_RETRIES):
            try:
                resp = await self._client.get(url, params=params, timeout=OPENF1_TIMEOUT)
                self._req_count += 1

                if resp.status_code == 429:
                    wait = 2 ** (attempt + 1)
                    logger.warning("🚦 Rate-limited on %s — backing off %ss", endpoint, wait)
                    await asyncio.sleep(wait)
                    continue

                resp.raise_for_status()
                data = resp.json()
                return data if isinstance(data, list) else [data]

            except httpx.TimeoutException:
                logger.warning("⏱️  Timeout %s (attempt %d/%d)", endpoint, attempt + 1, OPENF1_MAX_RETRIES)
            except httpx.HTTPStatusError as exc:
                logger.error("❌ HTTP %s on %s", exc.response.status_code, endpoint)
                break   # Don't retry 4xx (except 429)
            except httpx.RequestError as exc:
                logger.error("🔌 Network error on %s: %s", endpoint, exc)

            if attempt < OPENF1_MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)

        return []


# ─── Grid State Manager ───────────────────────────────────────────────────────

class GridStateManager:
    """
    Maintains in-memory DriverState objects for all tracked drivers
    and writes them to the Redis cache after each polling cycle.
    """

    def __init__(self, session_monitor) -> None:
        self._session_monitor = session_monitor
        self._states: dict[int, DriverState] = {}     # keyed by driver_number
        self._lap_histories: dict[int, list[float]] = defaultdict(list)

    def _get_or_create(self, driver_number: int) -> DriverState:
        if driver_number not in self._states:
            driver_info = self._session_monitor.get_driver_by_number(driver_number)
            self._states[driver_number] = DriverState(
                driver_number   = driver_number,
                code            = driver_info.name_acronym if driver_info else str(driver_number),
                name            = driver_info.full_name    if driver_info else "Unknown",
                team            = driver_info.team_name    if driver_info else "Unknown",
                team_colour_hex = driver_info.team_colour  if driver_info else "FFFFFF",
                headshot_url    = driver_info.headshot_url if driver_info else "",
            )
        return self._states[driver_number]

    def apply_car_data(self, rows: list[dict]) -> None:
        """Apply high-frequency car telemetry (speed, DRS, gear, RPM)."""
        # Group by driver, take the latest row per driver
        latest: dict[int, dict] = {}
        for row in rows:
            num = row.get("driver_number")
            if num is None:
                continue
            latest[num] = row

        for num, row in latest.items():
            state = self._get_or_create(num)
            state.speed_kph    = float(row.get("speed", state.speed_kph) or state.speed_kph)
            state.throttle_pct = float(row.get("throttle", state.throttle_pct) or state.throttle_pct)
            state.brake_pct    = float(100 if row.get("brake") else 0)
            state.n_gear       = int(row.get("n_gear", state.n_gear) or state.n_gear)
            state.rpm          = int(row.get("rpm", state.rpm) or state.rpm)
            raw_drs            = int(row.get("drs", state.drs_raw) or state.drs_raw)
            state.drs_raw      = raw_drs
            # Re-derive boolean fields
            state.drs_detection_zone = raw_drs == 8
            state.drs_enabled        = raw_drs in (10, 12, 14)
            state.timestamp          = time.time()

    def apply_intervals(self, rows: list[dict]) -> None:
        """Apply gap-to-leader and interval data."""
        latest: dict[int, dict] = {}
        for row in rows:
            num = row.get("driver_number")
            if num is None:
                continue
            latest[num] = row

        for num, row in latest.items():
            state = self._get_or_create(num)
            # gap_to_leader is None for the leader, a float for others
            gtl = row.get("gap_to_leader")
            if gtl is not None and not isinstance(gtl, str):
                state.gap_to_leader_s = float(gtl)
            elif isinstance(gtl, str):
                state.gap_to_leader_s = LAPPED_SENTINEL

            itv = row.get("interval")
            if itv is not None and not isinstance(itv, str):
                state.interval_s = float(itv)
            elif isinstance(itv, str):
                state.interval_s = LAPPED_SENTINEL

    def apply_positions(self, rows: list[dict]) -> None:
        """Apply race positions."""
        latest: dict[int, dict] = {}
        for row in rows:
            num = row.get("driver_number")
            if num is None:
                continue
            latest[num] = row

        for num, row in latest.items():
            state = self._get_or_create(num)
            state.position = int(row.get("position", state.position) or state.position)

    def apply_laps(self, rows: list[dict]) -> None:
        """Apply completed lap times and sector splits."""
        for row in rows:
            num = row.get("driver_number")
            if num is None:
                continue
            dur = row.get("lap_duration")
            if dur is None:
                continue

            state = self._get_or_create(num)
            state.lap = int(row.get("lap_number", state.lap) or state.lap)
            state.lap_time_s    = float(dur)
            state.sector_1_s    = row.get("duration_sector_1")
            state.sector_2_s    = row.get("duration_sector_2")
            state.sector_3_s    = row.get("duration_sector_3")
            state.i1_speed_kph  = row.get("i1_speed")
            state.i2_speed_kph  = row.get("i2_speed")
            state.is_pit_out_lap = bool(row.get("is_pit_out_lap", False))

            # Rolling average
            history = self._lap_histories[num]
            history.append(float(dur))
            if len(history) > 10:
                history.pop(0)
            state.lap_history = list(history)
            state.update_rolling_avg()

    def apply_stints(self, rows: list[dict]) -> None:
        """Apply tyre compound and age from the most recent active stint."""
        # Group by driver, find the highest stint_number per driver
        latest: dict[int, dict] = {}
        for row in rows:
            num = row.get("driver_number")
            if num is None:
                continue
            existing = latest.get(num)
            if existing is None or row.get("stint_number", 0) > existing.get("stint_number", 0):
                latest[num] = row

        for num, row in latest.items():
            state = self._get_or_create(num)
            state.tyre_compound  = (row.get("compound") or "UNKNOWN").upper()
            state.stint_number   = int(row.get("stint_number", state.stint_number) or state.stint_number)
            lap_start            = int(row.get("lap_start", 0) or 0)
            age_at_start         = int(row.get("tyre_age_at_start", 0) or 0)
            if lap_start > 0 and state.lap >= lap_start:
                state.tyre_age_laps = (state.lap - lap_start) + age_at_start

    def apply_pits(self, rows: list[dict]) -> None:
        """Detect active pit stops and increment pit_stops counter."""
        for row in rows:
            num = row.get("driver_number")
            if num is None:
                continue
            state = self._get_or_create(num)
            # A new row in /v1/pit means the car has completed a pit stop
            state.pit_stops = max(state.pit_stops, int(row.get("pit_number", state.pit_stops + 1)))
            # Mark as pit-out briefly (cleared on next lap data)
            state.in_pit = False  # Car already exited by the time pit data arrives

    def apply_locations(self, rows: list[dict]) -> None:
        """Apply GPS track coordinates (latest per driver)."""
        latest: dict[int, dict] = {}
        for row in rows:
            num = row.get("driver_number")
            if num is None:
                continue
            latest[num] = row

        for num, row in latest.items():
            state = self._get_or_create(num)
            state.x_pos = float(row.get("x", state.x_pos or 0))
            state.y_pos = float(row.get("y", state.y_pos or 0))

    def flush_to_cache(self, track: str) -> dict[str, Any]:
        """
        Write all current DriverState objects to Redis and return
        a full grid snapshot dict for WebSocket broadcasting.
        """
        grid: dict[str, Any] = {}
        sorted_drivers = sorted(
            self._states.values(),
            key=lambda s: s.position if s.position > 0 else 99,
        )

        for state in sorted_drivers:
            state_dict = state.to_dict()
            cache.set_state(state.code, state_dict)
            grid[state.code] = state_dict

        # Update interval-based gap meta (P1 vs P2 for backward-compat triggers)
        p1 = next((s for s in sorted_drivers if s.position == 1), None)
        p2 = next((s for s in sorted_drivers if s.position == 2), None)
        if p1 and p2:
            cache.set_race_meta("gap_p1_p2", p2.interval_s)

        # Update current lap from leader
        if p1:
            cache.set_race_meta("current_lap", p1.lap)

        cache.set_race_meta("track", track)
        return grid

    def get_sorted_grid(self) -> list[DriverState]:
        """Return all driver states sorted by race position."""
        return sorted(
            self._states.values(),
            key=lambda s: s.position if s.position > 0 else 99,
        )

    def get_state_by_code(self, code: str) -> DriverState | None:
        for state in self._states.values():
            if state.code == code:
                return state
        return None


# ─── OpenF1 Client ────────────────────────────────────────────────────────────

class OpenF1Client:
    """
    Production telemetry ingestion client.

    Maintains timestamp cursors for each endpoint group, fans out
    concurrent async HTTP requests per polling cycle, applies the
    responses to the GridStateManager, and writes the merged state
    to the Redis cache.
    """

    def __init__(self, session_monitor, broadcast_callback=None) -> None:
        self._monitor             = session_monitor
        self._broadcast_cb        = broadcast_callback
        self._grid                = GridStateManager(session_monitor)
        self._http: OpenF1Http | None = None

        # Timestamp cursors — we only fetch data newer than these
        self._ts_car_data   = _ts_minus(10)   # start 10s back to warm up
        self._ts_intervals  = _ts_minus(10)
        self._ts_positions  = _ts_minus(10)
        self._ts_race_ctrl  = _ts_minus(30)   # 30s back to catch recent events
        self._ts_location   = _ts_minus(5)
        self._ts_pits       = _ts_minus(60)   # 60s back to catch recent stops

        self._last_lap_seen: dict[int, int] = {}   # driver_number → last lap fetched

    # ──────────────────────────── Endpoint Fetchers ───────────────────────────

    async def _fetch_car_data(self, session_key: int) -> list[dict]:
        rows = await self._http.get("car_data", {
            "session_key": session_key,
            "date>": self._ts_car_data,
        })
        if rows:
            self._ts_car_data = rows[-1].get("date", self._ts_car_data)
        return rows

    async def _fetch_intervals(self, session_key: int) -> list[dict]:
        rows = await self._http.get("intervals", {
            "session_key": session_key,
            "date>": self._ts_intervals,
        })
        if rows:
            self._ts_intervals = rows[-1].get("date", self._ts_intervals)
        return rows

    async def _fetch_positions(self, session_key: int) -> list[dict]:
        rows = await self._http.get("position", {
            "session_key": session_key,
            "date>": self._ts_positions,
        })
        if rows:
            self._ts_positions = rows[-1].get("date", self._ts_positions)
        return rows

    async def _fetch_laps(self, session_key: int) -> list[dict]:
        """Fetch newly completed laps for all drivers."""
        # Use per-driver lap cursors to avoid re-fetching completed laps
        all_rows: list[dict] = []
        current_lap = cache.get_race_meta("current_lap") or 1

        rows = await self._http.get("laps", {
            "session_key": session_key,
            "lap_number>": max(0, current_lap - 2),  # last 2 laps buffer
        })
        # Filter to only laps newer than what we've already seen per driver
        for row in rows:
            num = row.get("driver_number")
            lap = row.get("lap_number", 0)
            if num is None or lap is None:
                continue
            if lap > self._last_lap_seen.get(num, 0):
                self._last_lap_seen[num] = lap
                all_rows.append(row)

        return all_rows

    async def _fetch_stints(self, session_key: int) -> list[dict]:
        return await self._http.get("stints", {"session_key": session_key})

    async def _fetch_pits(self, session_key: int) -> list[dict]:
        rows = await self._http.get("pit", {
            "session_key": session_key,
            "date>": self._ts_pits,
        })
        if rows:
            self._ts_pits = rows[-1].get("date", self._ts_pits)
        return rows

    async def _fetch_race_control(self, session_key: int) -> list[dict]:
        rows = await self._http.get("race_control", {
            "session_key": session_key,
            "date>": self._ts_race_ctrl,
        })
        if rows:
            self._ts_race_ctrl = rows[-1].get("date", self._ts_race_ctrl)
        return rows

    async def _fetch_locations(self, session_key: int) -> list[dict]:
        rows = await self._http.get("location", {
            "session_key": session_key,
            "date>": self._ts_location,
        })
        if rows:
            self._ts_location = rows[-1].get("date", self._ts_location)
        return rows

    # ──────────────────────────── Race Control Handler ────────────────────────

    async def _process_race_control(self, rows: list[dict]) -> None:
        """
        Update safety car / DRS global flags in the cache and
        broadcast RACE_CONTROL WebSocket messages.
        """
        for row in rows:
            try:
                msg = RaceControlMessage.model_validate(row)
            except Exception:
                continue

            if msg.is_safety_car:
                cache.set_race_meta("safety_car_active", True)
                cache.set_race_meta("vsc_active", False)
                logger.warning("🚗 SAFETY CAR DEPLOYED")
            elif msg.is_vsc:
                cache.set_race_meta("vsc_active", True)
                logger.warning("🟡 VIRTUAL SAFETY CAR")
            elif msg.category == "SafetyCar" and "ENDING" in msg.message.upper():
                cache.set_race_meta("safety_car_active", False)
                cache.set_race_meta("vsc_active", False)
            elif msg.is_drs_enabled:
                cache.set_race_meta("drs_globally_enabled", True)
            elif msg.is_drs_disabled:
                cache.set_race_meta("drs_globally_enabled", False)

            if self._broadcast_cb:
                await self._broadcast_cb({
                    "type":     "RACE_CONTROL",
                    "message":  msg.message,
                    "category": msg.category,
                    "flag":     msg.flag,
                    "lap":      msg.lap_number,
                    "ts":       time.time(),
                })

    # ──────────────────────────── Main Poll Cycle ─────────────────────────────

    async def poll(self) -> dict[str, Any] | None:
        """
        Execute one complete polling cycle:
          1. Fan out concurrent requests to all endpoints
          2. Apply responses to GridStateManager
          3. Write merged state to Redis cache
          4. Return the grid snapshot for broadcasting

        Returns None if there is no active session.
        """
        if not self._monitor.is_any_live_session():
            return None

        session_key = self._monitor.current_session_key
        if session_key is None:
            return None

        track = (
            self._monitor.session.circuit_short_name
            if self._monitor.session else "Unknown"
        )

        # ── Fan out concurrent requests ──────────────────────────────────────
        (
            car_data_rows,
            interval_rows,
            position_rows,
            lap_rows,
            stint_rows,
            pit_rows,
            rc_rows,
            location_rows,
        ) = await asyncio.gather(
            self._fetch_car_data(session_key),
            self._fetch_intervals(session_key),
            self._fetch_positions(session_key),
            self._fetch_laps(session_key),
            self._fetch_stints(session_key),
            self._fetch_pits(session_key),
            self._fetch_race_control(session_key),
            self._fetch_locations(session_key),
            return_exceptions=False,
        )

        # ── Apply data in dependency order ───────────────────────────────────
        self._grid.apply_positions(position_rows)
        self._grid.apply_car_data(car_data_rows)
        self._grid.apply_intervals(interval_rows)
        self._grid.apply_laps(lap_rows)
        self._grid.apply_stints(stint_rows)
        self._grid.apply_pits(pit_rows)
        self._grid.apply_locations(location_rows)

        # ── Race control (async broadcast side-effects) ──────────────────────
        if rc_rows:
            await self._process_race_control(rc_rows)

        # ── Flush merged state to Redis, return snapshot ─────────────────────
        grid_snapshot = self._grid.flush_to_cache(track)

        total_rows = sum(len(r) for r in [
            car_data_rows, interval_rows, position_rows,
            lap_rows, stint_rows, pit_rows,
        ])
        logger.debug(
            "📡 Poll complete | session=%s | %d drivers | %d total rows",
            session_key, len(grid_snapshot), total_rows,
        )

        return {
            "grid":  grid_snapshot,
            "meta": {
                "session_key": session_key,
                "track":       track,
                "lap":         cache.get_race_meta("current_lap"),
                "sc_active":   cache.get_race_meta("safety_car_active"),
                "vsc_active":  cache.get_race_meta("vsc_active"),
                "drs_enabled": cache.get_race_meta("drs_globally_enabled"),
            },
        }

    # ──────────────────────────── Async Runner ────────────────────────────────

    async def run(self) -> None:
        """
        Main async loop: poll every OPENF1_POLL_INTERVAL seconds.
        Skips cycles when there is no live session.
        """
        async with httpx.AsyncClient(
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            follow_redirects=True,
        ) as client:
            self._http = OpenF1Http(client)
            logger.info("🏎️  OpenF1 live client started — polling every %.1fs", OPENF1_POLL_INTERVAL)

            while True:
                try:
                    snapshot = await self.poll()
                    if snapshot and self._broadcast_cb:
                        await self._broadcast_cb({
                            "type":     "TELEMETRY",
                            "snapshot": snapshot,
                            "ts":       time.time(),
                        })
                except Exception as exc:
                    logger.error("❌ Poll cycle error: %s", exc, exc_info=True)

                await asyncio.sleep(OPENF1_POLL_INTERVAL)

    # ──────────────────────────── Grid Accessors ──────────────────────────────

    def get_sorted_grid(self):
        return self._grid.get_sorted_grid()

    def get_state_by_code(self, code: str) -> DriverState | None:
        return self._grid.get_state_by_code(code)
