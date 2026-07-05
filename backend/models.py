"""
models.py - Pydantic data models for the F1 Live Race Insight Architecture.

Replaces the plain dataclass DriverState with fully validated Pydantic models
that map directly to fields returned by the OpenF1 API.

All models use `model_config = ConfigDict(populate_by_name=True)` so they
can be instantiated either from API field names (snake_case) or aliases.
"""

from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel, Field, model_validator, ConfigDict


# ─── Driver State ─────────────────────────────────────────────────────────────

class DriverState(BaseModel):
    """
    Full telemetry + strategy snapshot for a single driver.

    Fields are sourced from these OpenF1 endpoints:
      /v1/car_data   → speed, throttle, brake, drs_raw, n_gear, rpm
      /v1/intervals  → gap_to_leader_s, interval_s
      /v1/position   → position
      /v1/laps       → lap_time_s, sector_*_s, i1/i2_speed_kph, rolling_avg_lap_s
      /v1/stints     → tyre_compound, tyre_age_laps, stint_number
      /v1/pit        → in_pit, pit_stops
      /v1/location   → x_pos, y_pos
      /v1/drivers    → code, name, team, team_colour_hex, headshot_url
    """

    model_config = ConfigDict(populate_by_name=True)

    # ── Identity ───────────────────────────────────────────────────────────
    driver_number:   int             = Field(..., description="FIA car number, e.g. 1")
    code:            str             = Field(..., description="3-letter acronym, e.g. 'VER'")
    name:            str             = Field(..., description="Full name")
    team:            str             = Field(..., description="Constructor name")
    team_colour_hex: str             = Field(default="FFFFFF", description="Hex team colour without #")
    headshot_url:    str             = Field(default="")

    # ── Race position & lap ────────────────────────────────────────────────
    position:        int             = Field(default=0, ge=0, le=20)
    lap:             int             = Field(default=0, ge=0)

    # ── Lap timing ────────────────────────────────────────────────────────
    lap_time_s:      float           = Field(default=0.0, ge=0.0, description="Last completed lap in seconds")
    rolling_avg_lap_s: float         = Field(default=0.0, ge=0.0, description="3-lap rolling average")
    lap_history:     list[float]     = Field(default_factory=list, description="Last 5 lap times")

    # ── Sector times (populated after lap completion) ─────────────────────
    sector_1_s:      Optional[float] = Field(default=None, ge=0.0)
    sector_2_s:      Optional[float] = Field(default=None, ge=0.0)
    sector_3_s:      Optional[float] = Field(default=None, ge=0.0)

    # ── Speed traps ───────────────────────────────────────────────────────
    i1_speed_kph:    Optional[float] = Field(default=None, ge=0.0)
    i2_speed_kph:    Optional[float] = Field(default=None, ge=0.0)

    # ── Gaps & intervals ──────────────────────────────────────────────────
    gap_to_leader_s: float           = Field(default=0.0, ge=0.0, description="Gap to P1 in seconds")
    interval_s:      float           = Field(default=0.0, ge=0.0, description="Gap to car directly ahead")

    # ── Tyre strategy ─────────────────────────────────────────────────────
    tyre_compound:   str             = Field(default="UNKNOWN", description="SOFT/MEDIUM/HARD/INTERMEDIATE/WET")
    tyre_age_laps:   int             = Field(default=0, ge=0)
    stint_number:    int             = Field(default=1, ge=1)
    is_pit_out_lap:  bool            = Field(default=False)

    # ── DRS ───────────────────────────────────────────────────────────────
    drs_raw:           int           = Field(default=0, description="Raw DRS code from OpenF1 (0,1,8,10,12,14)")
    drs_detection_zone: bool         = Field(default=False)
    drs_enabled:        bool         = Field(default=False)

    # ── Pit status ────────────────────────────────────────────────────────
    in_pit:          bool            = Field(default=False)
    pit_stops:       int             = Field(default=0, ge=0)

    # ── Live car telemetry ────────────────────────────────────────────────
    speed_kph:       float           = Field(default=0.0, ge=0.0)
    throttle_pct:    float           = Field(default=0.0, ge=0.0, le=100.0)
    brake_pct:       float           = Field(default=0.0, ge=0.0, le=100.0)
    n_gear:          int             = Field(default=0, ge=0, le=8)
    rpm:             int             = Field(default=0, ge=0)

    # ── GPS location (track coords) ───────────────────────────────────────
    x_pos:           Optional[float] = Field(default=None)
    y_pos:           Optional[float] = Field(default=None)

    # ── Timestamp ─────────────────────────────────────────────────────────
    timestamp:       float           = Field(default=0.0)

    # ── Derived field validators ──────────────────────────────────────────
    @model_validator(mode="after")
    def derive_drs_fields(self) -> "DriverState":
        """Compute boolean DRS fields from raw integer code."""
        self.drs_detection_zone = self.drs_raw == 8
        self.drs_enabled        = self.drs_raw in (10, 12, 14)
        return self

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()

    # ── Rolling average helper ────────────────────────────────────────────
    def update_rolling_avg(self) -> None:
        """Recompute the 3-lap rolling average from lap_history."""
        if self.lap_history:
            recent = self.lap_history[-3:]
            self.rolling_avg_lap_s = round(sum(recent) / len(recent), 3)

    # ── Tyre compound helpers ─────────────────────────────────────────────
    @property
    def tyre_css_class(self) -> str:
        return self.tyre_compound.lower()

    @property
    def is_on_slicks(self) -> bool:
        return self.tyre_compound in ("SOFT", "MEDIUM", "HARD")


# ─── Race Control Message ─────────────────────────────────────────────────────

class RaceControlMessage(BaseModel):
    """A single message from the OpenF1 /v1/race_control endpoint."""

    model_config = ConfigDict(populate_by_name=True)

    date:          str
    category:      str            = Field(description="SessionStatus/CarEvent/Drs/Flag/SafetyCar")
    message:       str            = Field(default="")
    flag:          Optional[str]  = Field(default=None, description="GREEN/YELLOW/RED/etc.")
    driver_number: Optional[int]  = Field(default=None)
    lap_number:    Optional[int]  = Field(default=None)
    session_key:   int            = Field(default=0)
    meeting_key:   int            = Field(default=0)

    @property
    def is_safety_car(self) -> bool:
        return self.category == "SafetyCar" and "SAFETY CAR" in self.message.upper()

    @property
    def is_vsc(self) -> bool:
        return "VIRTUAL SAFETY CAR" in self.message.upper()

    @property
    def is_drs_enabled(self) -> bool:
        return self.category == "Drs" and "ENABLED" in self.message.upper()

    @property
    def is_drs_disabled(self) -> bool:
        return self.category == "Drs" and "DISABLED" in self.message.upper()

    @property
    def is_red_flag(self) -> bool:
        return self.flag == "RED"

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


# ─── Lap Record ───────────────────────────────────────────────────────────────

class LapRecord(BaseModel):
    """Parsed response from /v1/laps for a single completed lap."""

    model_config = ConfigDict(populate_by_name=True)

    driver_number:     int
    lap_number:        int
    lap_duration:      Optional[float]  = None    # None if lap incomplete/deleted
    duration_sector_1: Optional[float]  = None
    duration_sector_2: Optional[float]  = None
    duration_sector_3: Optional[float]  = None
    i1_speed:          Optional[float]  = None
    i2_speed:          Optional[float]  = None
    is_pit_out_lap:    bool             = False
    date_start:        str              = ""
    session_key:       int              = 0


# ─── Stint Record ─────────────────────────────────────────────────────────────

class StintRecord(BaseModel):
    """Parsed response from /v1/stints for an active or completed tyre stint."""

    model_config = ConfigDict(populate_by_name=True)

    driver_number:      int
    stint_number:       int
    compound:           str             # SOFT / MEDIUM / HARD / INTERMEDIATE / WET
    lap_start:          int
    lap_end:            Optional[int]   = None    # None during active stint
    tyre_age_at_start:  int             = 0
    session_key:        int             = 0

    def current_tyre_age(self, current_lap: int) -> int:
        """Compute how many laps this set has been on as of current_lap."""
        return (current_lap - self.lap_start) + self.tyre_age_at_start


# ─── Interval Record ──────────────────────────────────────────────────────────

class IntervalRecord(BaseModel):
    """Parsed response from /v1/intervals."""

    model_config = ConfigDict(populate_by_name=True)

    driver_number:  int
    gap_to_leader:  Optional[float]  = None   # None for leader; "+1 LAP" becomes large float
    interval:       Optional[float]  = None   # Gap to the car directly ahead
    date:           str              = ""
    session_key:    int              = 0

    @model_validator(mode="before")
    @classmethod
    def coerce_gap_strings(cls, values: dict) -> dict:
        """
        OpenF1 returns "+1 LAP" as a string for lapped cars.
        Coerce non-numeric strings to a sentinel value (999.0).
        """
        for key in ("gap_to_leader", "interval"):
            val = values.get(key)
            if isinstance(val, str):
                try:
                    values[key] = float(val)
                except (ValueError, TypeError):
                    values[key] = 999.0   # Lapped / no data
        return values


# ─── Pit Stop Record ──────────────────────────────────────────────────────────

class PitStopRecord(BaseModel):
    """Parsed response from /v1/pit."""

    model_config = ConfigDict(populate_by_name=True)

    driver_number: int
    lap_number:    int
    pit_duration:  Optional[float] = None   # Stationary time in seconds
    date:          str             = ""
    session_key:   int             = 0


# ─── Location Record ──────────────────────────────────────────────────────────

class LocationRecord(BaseModel):
    """Parsed response from /v1/location (GPS track coordinates)."""

    model_config = ConfigDict(populate_by_name=True)

    driver_number: int
    x:             float
    y:             float
    z:             float = 0.0
    date:          str   = ""
    session_key:   int   = 0
