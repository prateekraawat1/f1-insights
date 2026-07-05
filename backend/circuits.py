"""
circuits.py - Per-circuit configuration for the F1 Live Race Insight Architecture.

Contains:
  - Pit-lane loss delta (seconds) for all 24 circuits on the 2024 F1 calendar
  - Total race laps per circuit
  - DRS zone count
  - Circuit short name aliases (matching OpenF1's circuit_short_name field)

These values are used by:
  - triggers.py  → BOX_WINDOW and UNDERCUT pit-delta calculations
  - session.py   → total_laps population on SessionInfo
  - app.py       → broadcast race metadata to frontend
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class CircuitConfig:
    """Static race weekend configuration for a single circuit."""
    name:               str      # Full circuit name
    short_name:         str      # Matches OpenF1 circuit_short_name
    country:            str
    total_laps:         int
    pit_lane_loss_s:    float    # Seconds lost per pit stop (lap delta)
    drs_zones:          int
    circuit_length_km:  float
    timezone:           str      # IANA timezone for local time display

    def box_window_margin(self) -> float:
        """
        Acceptable margin around pit_lane_loss_s to detect the box window.
        Tighter at street circuits (less variation), wider at power tracks.
        """
        if self.circuit_length_km < 4.0:  # Street circuit
            return 0.3
        return 0.5


# ─── 2024 F1 Calendar ─────────────────────────────────────────────────────────

CIRCUITS: dict[str, CircuitConfig] = {
    # ── Round 1 ── Bahrain
    "Bahrain": CircuitConfig(
        name="Bahrain International Circuit",
        short_name="Bahrain",
        country="Bahrain",
        total_laps=57,
        pit_lane_loss_s=21.0,
        drs_zones=3,
        circuit_length_km=5.412,
        timezone="Asia/Bahrain",
    ),
    # ── Round 2 ── Saudi Arabia
    "Jeddah": CircuitConfig(
        name="Jeddah Corniche Circuit",
        short_name="Jeddah",
        country="Saudi Arabia",
        total_laps=50,
        pit_lane_loss_s=24.0,
        drs_zones=3,
        circuit_length_km=6.174,
        timezone="Asia/Riyadh",
    ),
    # ── Round 3 ── Australia
    "Melbourne": CircuitConfig(
        name="Albert Park Circuit",
        short_name="Melbourne",
        country="Australia",
        total_laps=58,
        pit_lane_loss_s=23.0,
        drs_zones=4,
        circuit_length_km=5.278,
        timezone="Australia/Melbourne",
    ),
    # ── Round 4 ── Japan
    "Suzuka": CircuitConfig(
        name="Suzuka International Racing Course",
        short_name="Suzuka",
        country="Japan",
        total_laps=53,
        pit_lane_loss_s=22.0,
        drs_zones=2,
        circuit_length_km=5.807,
        timezone="Asia/Tokyo",
    ),
    # ── Round 5 ── China
    "Shanghai": CircuitConfig(
        name="Shanghai International Circuit",
        short_name="Shanghai",
        country="China",
        total_laps=56,
        pit_lane_loss_s=22.0,
        drs_zones=2,
        circuit_length_km=5.451,
        timezone="Asia/Shanghai",
    ),
    # ── Round 6 ── Miami
    "Miami": CircuitConfig(
        name="Miami International Autodrome",
        short_name="Miami",
        country="United States",
        total_laps=57,
        pit_lane_loss_s=24.0,
        drs_zones=3,
        circuit_length_km=5.412,
        timezone="America/New_York",
    ),
    # ── Round 7 ── Emilia Romagna
    "Imola": CircuitConfig(
        name="Autodromo Enzo e Dino Ferrari",
        short_name="Imola",
        country="Italy",
        total_laps=63,
        pit_lane_loss_s=25.0,
        drs_zones=2,
        circuit_length_km=4.909,
        timezone="Europe/Rome",
    ),
    # ── Round 8 ── Monaco
    "Monaco": CircuitConfig(
        name="Circuit de Monaco",
        short_name="Monaco",
        country="Monaco",
        total_laps=78,
        pit_lane_loss_s=30.0,
        drs_zones=1,
        circuit_length_km=3.337,
        timezone="Europe/Monaco",
    ),
    # ── Round 9 ── Canada
    "Montreal": CircuitConfig(
        name="Circuit Gilles Villeneuve",
        short_name="Montreal",
        country="Canada",
        total_laps=70,
        pit_lane_loss_s=22.0,
        drs_zones=2,
        circuit_length_km=4.361,
        timezone="America/Toronto",
    ),
    # ── Round 10 ── Spain
    "Barcelona": CircuitConfig(
        name="Circuit de Barcelona-Catalunya",
        short_name="Barcelona",
        country="Spain",
        total_laps=66,
        pit_lane_loss_s=21.0,
        drs_zones=2,
        circuit_length_km=4.675,
        timezone="Europe/Madrid",
    ),
    # ── Round 11 ── Austria
    "Spielberg": CircuitConfig(
        name="Red Bull Ring",
        short_name="Spielberg",
        country="Austria",
        total_laps=71,
        pit_lane_loss_s=20.0,
        drs_zones=3,
        circuit_length_km=4.318,
        timezone="Europe/Vienna",
    ),
    # ── Round 12 ── Great Britain
    "Silverstone": CircuitConfig(
        name="Silverstone Circuit",
        short_name="Silverstone",
        country="United Kingdom",
        total_laps=52,
        pit_lane_loss_s=22.0,
        drs_zones=2,
        circuit_length_km=5.891,
        timezone="Europe/London",
    ),
    # ── Round 13 ── Hungary
    "Budapest": CircuitConfig(
        name="Hungaroring",
        short_name="Budapest",
        country="Hungary",
        total_laps=70,
        pit_lane_loss_s=21.0,
        drs_zones=2,
        circuit_length_km=4.381,
        timezone="Europe/Budapest",
    ),
    # ── Round 14 ── Belgium
    "Spa-Francorchamps": CircuitConfig(
        name="Circuit de Spa-Francorchamps",
        short_name="Spa-Francorchamps",
        country="Belgium",
        total_laps=44,
        pit_lane_loss_s=23.0,
        drs_zones=2,
        circuit_length_km=7.004,
        timezone="Europe/Brussels",
    ),
    # ── Round 15 ── Netherlands
    "Zandvoort": CircuitConfig(
        name="Circuit Zandvoort",
        short_name="Zandvoort",
        country="Netherlands",
        total_laps=72,
        pit_lane_loss_s=21.0,
        drs_zones=2,
        circuit_length_km=4.259,
        timezone="Europe/Amsterdam",
    ),
    # ── Round 16 ── Italy
    "Monza": CircuitConfig(
        name="Autodromo Nazionale Monza",
        short_name="Monza",
        country="Italy",
        total_laps=53,
        pit_lane_loss_s=19.0,
        drs_zones=2,
        circuit_length_km=5.793,
        timezone="Europe/Rome",
    ),
    # ── Round 17 ── Azerbaijan
    "Baku": CircuitConfig(
        name="Baku City Circuit",
        short_name="Baku",
        country="Azerbaijan",
        total_laps=51,
        pit_lane_loss_s=24.0,
        drs_zones=2,
        circuit_length_km=6.003,
        timezone="Asia/Baku",
    ),
    # ── Round 18 ── Singapore
    "Singapore": CircuitConfig(
        name="Marina Bay Street Circuit",
        short_name="Singapore",
        country="Singapore",
        total_laps=62,
        pit_lane_loss_s=28.0,
        drs_zones=3,
        circuit_length_km=4.940,
        timezone="Asia/Singapore",
    ),
    # ── Round 19 ── United States
    "Austin": CircuitConfig(
        name="Circuit of The Americas",
        short_name="Austin",
        country="United States",
        total_laps=56,
        pit_lane_loss_s=22.0,
        drs_zones=2,
        circuit_length_km=5.513,
        timezone="America/Chicago",
    ),
    # ── Round 20 ── Mexico
    "Mexico City": CircuitConfig(
        name="Autodromo Hermanos Rodriguez",
        short_name="Mexico City",
        country="Mexico",
        total_laps=71,
        pit_lane_loss_s=21.0,
        drs_zones=3,
        circuit_length_km=4.304,
        timezone="America/Mexico_City",
    ),
    # ── Round 21 ── Brazil
    "Sao Paulo": CircuitConfig(
        name="Autodromo Jose Carlos Pace",
        short_name="Sao Paulo",
        country="Brazil",
        total_laps=71,
        pit_lane_loss_s=21.0,
        drs_zones=2,
        circuit_length_km=4.309,
        timezone="America/Sao_Paulo",
    ),
    # ── Round 22 ── Las Vegas
    "Las Vegas": CircuitConfig(
        name="Las Vegas Street Circuit",
        short_name="Las Vegas",
        country="United States",
        total_laps=50,
        pit_lane_loss_s=25.0,
        drs_zones=2,
        circuit_length_km=6.201,
        timezone="America/Los_Angeles",
    ),
    # ── Round 23 ── Qatar
    "Lusail": CircuitConfig(
        name="Lusail International Circuit",
        short_name="Lusail",
        country="Qatar",
        total_laps=57,
        pit_lane_loss_s=20.0,
        drs_zones=2,
        circuit_length_km=5.380,
        timezone="Asia/Qatar",
    ),
    # ── Round 24 ── Abu Dhabi
    "Yas Marina": CircuitConfig(
        name="Yas Marina Circuit",
        short_name="Yas Marina",
        country="United Arab Emirates",
        total_laps=58,
        pit_lane_loss_s=21.0,
        drs_zones=2,
        circuit_length_km=5.281,
        timezone="Asia/Dubai",
    ),
}

# Default fallback config when circuit is unrecognised
_DEFAULT_CONFIG = CircuitConfig(
    name="Unknown Circuit",
    short_name="Unknown",
    country="Unknown",
    total_laps=50,
    pit_lane_loss_s=22.0,
    drs_zones=2,
    circuit_length_km=5.0,
    timezone="UTC",
)


def get_circuit(short_name: str) -> CircuitConfig:
    """
    Look up a circuit config by OpenF1 short_name.
    Falls back to the default config if not found.
    Performs a case-insensitive prefix search to tolerate minor naming differences.
    """
    # Exact match first
    if short_name in CIRCUITS:
        return CIRCUITS[short_name]

    # Case-insensitive fallback
    lower = short_name.lower()
    for key, cfg in CIRCUITS.items():
        if key.lower() == lower or cfg.short_name.lower() == lower:
            return cfg

    # Prefix match (e.g. "Spa" matches "Spa-Francorchamps")
    for key, cfg in CIRCUITS.items():
        if lower.startswith(key.lower()[:4]) or key.lower().startswith(lower[:4]):
            return cfg

    return _DEFAULT_CONFIG


def get_pit_loss(short_name: str) -> float:
    """Convenience accessor for the pit-lane loss delta."""
    return get_circuit(short_name).pit_lane_loss_s


def get_total_laps(short_name: str) -> int:
    """Convenience accessor for the total race laps."""
    return get_circuit(short_name).total_laps
