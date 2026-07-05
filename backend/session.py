"""
session.py - Session discovery and lifecycle management for the F1 Insight Architecture.

Responsibilities:
  • Auto-discover the current live session from the OpenF1 API
  • Validate session type (Race / Qualifying / Practice)
  • Manage the session state machine: IDLE → DISCOVERING → LIVE → POST_SESSION
  • Load and cache the full driver roster for the session
  • Broadcast SESSION_CHANGE events to WebSocket clients

OpenF1 endpoints used:
  GET /v1/sessions?session_key=latest
  GET /v1/drivers?session_key={key}
"""

import asyncio
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any

import httpx

from backend.config import OPENF1_BASE_URL, OPENF1_TIMEOUT, OPENF1_MAX_RETRIES
from backend.cache import cache

logger = logging.getLogger(__name__)


# ─── Session State Machine ─────────────────────────────────────────────────────

class SessionState(str, Enum):
    IDLE         = "IDLE"           # No active or upcoming session
    DISCOVERING  = "DISCOVERING"    # Session found; loading roster + metadata
    LIVE         = "LIVE"           # Race/session actively underway
    POST_SESSION = "POST_SESSION"   # Session ended; trigger post-race RAG update


# ─── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class SessionInfo:
    """Metadata for the current F1 session."""
    session_key:        int
    meeting_key:        int
    session_name:       str          # "Race", "Qualifying", "Sprint", "Free Practice 1", …
    session_type:       str          # "Race", "Qualifying", "Practice"
    circuit_short_name: str
    country_name:       str
    date_start:         str          # ISO 8601 UTC
    date_end:           str          # ISO 8601 UTC
    year:               int
    total_laps:         int = 0      # Populated from circuit config
    is_live:            bool = False  # Computed

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


@dataclass
class DriverInfo:
    """Static metadata for a single driver within a session."""
    driver_number:  int
    name_acronym:   str    # e.g. "VER"
    full_name:      str    # e.g. "Max Verstappen"
    team_name:      str    # e.g. "Red Bull Racing"
    team_colour:    str    # Hex without '#', e.g. "3671C6"
    headshot_url:   str = ""
    country_code:   str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─── Session Monitor ──────────────────────────────────────────────────────────

class SessionMonitor:
    """
    Polls OpenF1 every POLL_INTERVAL seconds to detect and track the current session.
    Transitions the state machine and broadcasts SESSION_INFO updates.
    """

    SESSION_POLL_INTERVAL = 60      # seconds between session status checks
    LIVE_BUFFER_MINUTES   = 30      # extend live window past date_end

    # Session types that should activate the full trigger pipeline
    RACE_SESSION_TYPES = {"Race", "Sprint"}
    ALL_SESSION_TYPES  = {"Race", "Sprint", "Qualifying", "Practice"}

    def __init__(self) -> None:
        self.state:          SessionState       = SessionState.IDLE
        self.session:        SessionInfo | None = None
        self.drivers:        dict[int, DriverInfo] = {}    # keyed by driver_number
        self._broadcast_cb                          = None
        self._http: httpx.AsyncClient | None        = None

    def set_broadcast_callback(self, cb) -> None:
        """Register a coroutine to call whenever session state changes."""
        self._broadcast_cb = cb

    # ──────────────────────────── HTTP ────────────────────────────────────────

    async def _get(self, endpoint: str, params: dict | None = None) -> list[dict]:
        """
        GET {OPENF1_BASE_URL}/{endpoint} with retry + exponential backoff.
        Returns the parsed JSON list, or [] on failure.
        """
        url = f"{OPENF1_BASE_URL}/{endpoint.lstrip('/')}"
        for attempt in range(OPENF1_MAX_RETRIES):
            try:
                resp = await self._http.get(url, params=params, timeout=OPENF1_TIMEOUT)
                if resp.status_code == 429:
                    wait = 2 ** attempt
                    logger.warning("⚠️  Rate limited on %s — waiting %ss", endpoint, wait)
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                data = resp.json()
                return data if isinstance(data, list) else [data]
            except httpx.TimeoutException:
                logger.warning("⏱️  Timeout on %s (attempt %d)", endpoint, attempt + 1)
            except httpx.HTTPError as exc:
                logger.error("❌ HTTP error on %s: %s", endpoint, exc)
            if attempt < OPENF1_MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
        return []

    # ──────────────────────────── Session Discovery ───────────────────────────

    async def _fetch_latest_session(self) -> SessionInfo | None:
        """Query /v1/sessions?session_key=latest and parse the result."""
        rows = await self._get("sessions", {"session_key": "latest"})
        if not rows:
            return None

        row = rows[0]
        try:
            return SessionInfo(
                session_key        = row["session_key"],
                meeting_key        = row["meeting_key"],
                session_name       = row.get("session_name", "Unknown"),
                session_type       = row.get("session_type", "Unknown"),
                circuit_short_name = row.get("circuit_short_name", "Unknown"),
                country_name       = row.get("country_name", "Unknown"),
                date_start         = row.get("date_start", ""),
                date_end           = row.get("date_end", ""),
                year               = row.get("year", datetime.now().year),
            )
        except KeyError as exc:
            logger.error("❌ Unexpected session payload — missing key: %s", exc)
            return None

    def _compute_is_live(self, session: SessionInfo) -> bool:
        """
        A session is considered live if now is between date_start and
        date_end + LIVE_BUFFER_MINUTES.
        """
        now = datetime.now(timezone.utc)
        try:
            start = datetime.fromisoformat(session.date_start.replace("Z", "+00:00"))
            end   = datetime.fromisoformat(session.date_end.replace("Z", "+00:00"))
            end  += timedelta(minutes=self.LIVE_BUFFER_MINUTES)
            return start <= now <= end
        except (ValueError, AttributeError):
            # If dates are missing/malformed, assume live if session_key is "latest"
            return True

    # ──────────────────────────── Driver Roster ───────────────────────────────

    async def _fetch_drivers(self, session_key: int) -> dict[int, DriverInfo]:
        """Load all drivers for the session from /v1/drivers."""
        rows = await self._get("drivers", {"session_key": session_key})
        drivers: dict[int, DriverInfo] = {}
        for row in rows:
            try:
                d = DriverInfo(
                    driver_number = row["driver_number"],
                    name_acronym  = row.get("name_acronym", str(row["driver_number"])),
                    full_name     = row.get("full_name", "Unknown Driver"),
                    team_name     = row.get("team_name", "Unknown Team"),
                    team_colour   = row.get("team_colour", "FFFFFF"),
                    headshot_url  = row.get("headshot_url", ""),
                    country_code  = row.get("country_code", ""),
                )
                drivers[d.driver_number] = d
            except KeyError:
                continue
        logger.info("👥 Loaded %d drivers for session %s", len(drivers), session_key)
        return drivers

    # ──────────────────────────── Cache Persistence ───────────────────────────

    def _persist_to_cache(self) -> None:
        """Write session and driver metadata into Redis for downstream consumers."""
        if self.session:
            cache.set_race_meta("session_key",         self.session.session_key)
            cache.set_race_meta("meeting_key",         self.session.meeting_key)
            cache.set_race_meta("track",               self.session.circuit_short_name)
            cache.set_race_meta("country",             self.session.country_name)
            cache.set_race_meta("session_name",        self.session.session_name)
            cache.set_race_meta("session_type",        self.session.session_type)
            cache.set_race_meta("session_is_live",     self.session.is_live)
            cache.set_race_meta("session_year",        self.session.year)
            cache.set_race_meta("safety_car_active",   False)
            cache.set_race_meta("vsc_active",          False)
            cache.set_race_meta("drs_globally_enabled", True)
            cache.set_race_meta("state",               self.state.value)

        # Store driver number → acronym map for trigger engine lookups
        for num, driver in self.drivers.items():
            cache.set_race_meta(f"driver:{num}:acronym", driver.name_acronym)
            cache.set_race_meta(f"driver:{num}:team",    driver.team_name)
            cache.set_race_meta(f"driver:{num}:colour",  driver.team_colour)

    # ──────────────────────────── State Transitions ───────────────────────────

    async def _transition(self, new_state: SessionState, session: SessionInfo | None = None) -> None:
        """Apply a state transition, update cache, and broadcast."""
        old_state = self.state
        self.state = new_state
        if session:
            self.session = session

        logger.info("🔄 Session state: %s → %s", old_state.value, new_state.value)
        self._persist_to_cache()

        if self._broadcast_cb:
            payload = {
                "type":    "SESSION_INFO",
                "state":   new_state.value,
                "session": self.session.to_dict() if self.session else None,
                "drivers": {
                    str(num): d.to_dict()
                    for num, d in self.drivers.items()
                },
            }
            await self._broadcast_cb(payload)

    # ──────────────────────────── Main Loop ───────────────────────────────────

    async def run(self) -> None:
        """
        Continuously poll for session changes. This loop runs for the lifetime
        of the application.
        """
        async with httpx.AsyncClient() as client:
            self._http = client
            logger.info("🔭 Session monitor started")

            while True:
                await self._check_session()
                await asyncio.sleep(self.SESSION_POLL_INTERVAL)

    async def _check_session(self) -> None:
        """Single poll cycle: discover session, update state machine."""
        fetched = await self._fetch_latest_session()

        if not fetched:
            if self.state != SessionState.IDLE:
                await self._transition(SessionState.IDLE)
            return

        is_live = self._compute_is_live(fetched)
        fetched.is_live = is_live

        # Detect session change (different session_key OR state flip)
        session_changed = (
            self.session is None or
            self.session.session_key != fetched.session_key
        )

        if session_changed:
            await self._transition(SessionState.DISCOVERING, fetched)
            self.drivers = await self._fetch_drivers(fetched.session_key)

        if is_live and self.state != SessionState.LIVE:
            await self._transition(SessionState.LIVE, fetched)
        elif not is_live and self.state == SessionState.LIVE:
            await self._transition(SessionState.POST_SESSION, fetched)
            logger.info("🏁 Session ended — triggering post-session RAG update")
        elif not is_live and self.state not in (SessionState.POST_SESSION,):
            await self._transition(SessionState.IDLE, fetched)

    # ──────────────────────────── Accessors ───────────────────────────────────

    def is_race_session(self) -> bool:
        return (
            self.session is not None and
            self.session.session_type in self.RACE_SESSION_TYPES
        )

    def is_any_live_session(self) -> bool:
        return (
            self.session is not None and
            self.session.is_live and
            self.state == SessionState.LIVE
        )

    def get_driver_by_number(self, number: int) -> DriverInfo | None:
        return self.drivers.get(number)

    def get_driver_acronym(self, number: int) -> str:
        d = self.drivers.get(number)
        return d.name_acronym if d else str(number)

    @property
    def current_session_key(self) -> int | None:
        return self.session.session_key if self.session else None


# ─── Module-level singleton ────────────────────────────────────────────────────
session_monitor = SessionMonitor()
