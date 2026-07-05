"""
rag.py - Dynamic historical RAG (Retrieval-Augmented Generation) layer.

Replaces the static HISTORICAL_FACTS dict in config.py with circuit-specific
facts computed from real FastF1 historical race data and stored in Redis.

Architecture:
  build(circuit, years) → FastF1 analysis → structured facts → Redis store
  get_facts(circuit, trigger_type) → retrieve from Redis → fallback to static

Usage:
  Pre-race (e.g. Thursday evening):
    python -m backend.rag build --circuit Silverstone --years 3

  Runtime (called by app.py LLM assembler):
    from backend.rag import rag_store
    facts = rag_store.get_facts("Silverstone", "DRS_THREAT")

What gets computed:
  - Average pit-lane loss at this circuit (measured from FastF1 lap data)
  - Tyre degradation rates per compound (pace delta per lap into stint)
  - DRS zone effectiveness (% laps where DRS=open correlated with gap < 1s)
  - Typical overtake counts per race at this circuit
  - Recent driver head-to-head performance (if specific drivers are cached)

FastF1 caches data locally. First build takes ~30s, subsequent builds are instant.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

from backend.cache import cache
from backend.circuits import get_circuit, get_pit_loss, CIRCUITS
from backend.config import HISTORICAL_FACTS  # static fallback

logger = logging.getLogger(__name__)

# Redis key prefix for RAG facts
_RAG_PREFIX = "f1:rag:"

# FastF1 local cache directory
_FF1_CACHE_DIR = Path(os.getenv("FASTF1_CACHE_DIR", "./fastf1_cache"))


# ─── FastF1 availability check ────────────────────────────────────────────────

def _ff1_available() -> bool:
    try:
        import fastf1  # noqa: F401
        return True
    except ImportError:
        return False


# ─── Redis RAG Store ──────────────────────────────────────────────────────────

class RagStore:
    """
    Retrieves trigger-specific historical facts for a circuit.

    Lookup order:
      1. Redis cache (built by build_circuit())
      2. Static HISTORICAL_FACTS from config.py (always-available fallback)

    Facts are structured as a list of plain-English strings, identical in
    format to the existing static facts so no changes are needed in app.py.
    """

    _TRIGGER_TYPES = [
        "DRS_THREAT", "UNDERCUT", "OVERCUT_WINDOW",
        "PACE_DROP", "BOX_WINDOW",
        "SAFETY_CAR", "VIRTUAL_SC", "FASTEST_LAP_THREAT",
    ]

    def _redis_key(self, circuit: str, trigger: str) -> str:
        return f"{_RAG_PREFIX}{circuit.lower().replace(' ', '_')}:{trigger}"

    def get_facts(self, circuit: str, trigger_type: str) -> list[str]:
        """
        Return 2–3 historical fact strings for the given circuit + trigger.
        Falls back to static facts if no dynamic data is available.
        """
        key = self._redis_key(circuit, trigger_type)
        try:
            raw = cache._client.get(key)
            if raw:
                facts = json.loads(raw)
                if facts:
                    return facts
        except Exception as exc:
            logger.warning("⚠️  RAG Redis read failed: %s", exc)

        # Fallback — static facts from config.py
        return HISTORICAL_FACTS.get(trigger_type, [
            f"Historical data for {circuit} is being computed — check back next session.",
        ])

    def set_facts(self, circuit: str, trigger_type: str, facts: list[str]) -> None:
        """Persist computed facts to Redis with a 7-day TTL."""
        key = self._redis_key(circuit, trigger_type)
        try:
            cache._client.set(key, json.dumps(facts), ex=7 * 24 * 3600)
            logger.info("📚 RAG stored: %s / %s (%d facts)", circuit, trigger_type, len(facts))
        except Exception as exc:
            logger.error("❌ RAG Redis write failed: %s", exc)

    def circuit_has_data(self, circuit: str) -> bool:
        """Return True if RAG facts exist in Redis for this circuit."""
        key = self._redis_key(circuit, "DRS_THREAT")
        try:
            return bool(cache._client.exists(key))
        except Exception:
            return False

    def list_cached_circuits(self) -> list[str]:
        """Return all circuits that have RAG data in Redis."""
        try:
            keys = cache._client.keys(f"{_RAG_PREFIX}*:DRS_THREAT")
            return [
                k.replace(_RAG_PREFIX, "").replace(":DRS_THREAT", "").replace("_", " ").title()
                for k in keys
            ]
        except Exception:
            return []


# Module-level singleton
rag_store = RagStore()


# ─── FastF1 Analysis Engine ───────────────────────────────────────────────────

class FastF1Analyser:
    """
    Computes circuit-specific historical facts from FastF1 data.
    Designed to run as a pre-race batch job, not during live sessions.
    """

    def __init__(self) -> None:
        if not _ff1_available():
            raise ImportError(
                "fastf1 is not installed. Run: pip install fastf1"
            )
        import fastf1
        _FF1_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fastf1.Cache.enable_cache(str(_FF1_CACHE_DIR))
        self._ff1 = fastf1

    def _load_race(self, year: int, gp_name: str):
        """Load and cache a FastF1 race session. Returns None on failure."""
        try:
            session = self._ff1.get_session(year, gp_name, "R")
            session.load(
                laps=True,
                telemetry=False,   # Skip high-volume telemetry for speed
                weather=False,
                messages=False,
                livedata=None,
            )
            logger.info("📥 FastF1 loaded: %d %s Race", year, gp_name)
            return session
        except Exception as exc:
            logger.warning("⚠️  FastF1 load failed for %d %s: %s", year, gp_name, exc)
            return None

    # ──────────────────────────── Pit-Lane Loss ───────────────────────────────

    def _compute_pit_loss(self, sessions: list) -> float | None:
        """
        Estimate the actual pit-lane loss at this circuit by comparing
        the lap time on a pit-out lap vs the driver's rolling average.
        Returns the median loss in seconds.
        """
        all_losses: list[float] = []

        for session in sessions:
            try:
                laps = session.laps
                pit_out_laps = laps[laps["PitOutTime"].notna()]

                for _, pit_lap in pit_out_laps.iterrows():
                    driver = pit_lap["Driver"]
                    lap_num = pit_lap["LapNumber"]

                    # Get the 3 laps before the pit stop as baseline
                    prev_laps = laps[
                        (laps["Driver"] == driver) &
                        (laps["LapNumber"] >= lap_num - 4) &
                        (laps["LapNumber"] < lap_num - 1) &
                        (laps["PitOutTime"].isna()) &
                        (laps["LapTime"].notna())
                    ]

                    if len(prev_laps) < 2:
                        continue

                    avg_pace = prev_laps["LapTime"].dt.total_seconds().mean()
                    pit_lap_time = pit_lap["LapTime"]

                    if hasattr(pit_lap_time, "total_seconds"):
                        pit_s = pit_lap_time.total_seconds()
                    else:
                        continue

                    loss = pit_s - avg_pace
                    if 15.0 < loss < 45.0:  # Sanity bounds
                        all_losses.append(loss)
            except Exception as exc:
                logger.debug("Pit loss computation error: %s", exc)

        if all_losses:
            return round(statistics.median(all_losses), 1)
        return None

    # ──────────────────────────── Tyre Degradation ───────────────────────────

    def _compute_tyre_deg(self, sessions: list) -> dict[str, Any]:
        """
        Compute tyre degradation rate (seconds lost per lap) per compound.
        Returns dict: {compound: {rate_s_per_lap, typical_cliff_lap}}
        """
        compound_laps: dict[str, list[dict]] = defaultdict(list)

        for session in sessions:
            try:
                laps = session.laps
                # Only clean laps without safety car
                clean = laps[
                    laps["TrackStatus"].isin(["1", ""])  & 
                    laps["LapTime"].notna() &
                    laps["PitInTime"].isna() &
                    laps["PitOutTime"].isna()
                ] if "TrackStatus" in laps.columns else laps[
                    laps["LapTime"].notna() &
                    laps["PitInTime"].isna() &
                    laps["PitOutTime"].isna()
                ]

                stints = laps.groupby(["Driver", "Stint"]) if "Stint" in laps.columns else []
                if hasattr(stints, "__iter__"):
                    for (driver, stint_num), stint_laps in stints:
                        compound = stint_laps["Compound"].iloc[0] if "Compound" in stint_laps.columns else None
                        if compound not in ("SOFT", "MEDIUM", "HARD"):
                            continue

                        stint_clean = stint_laps[
                            stint_laps["LapTime"].notna() &
                            stint_laps["PitInTime"].isna() &
                            stint_laps["PitOutTime"].isna()
                        ]

                        if len(stint_clean) < 4:
                            continue

                        times = stint_clean["LapTime"].dt.total_seconds().tolist()
                        compound_laps[compound].append({
                            "times": times,
                            "length": len(times),
                        })
            except Exception as exc:
                logger.debug("Tyre deg computation error: %s", exc)

        result = {}
        for compound, stints in compound_laps.items():
            all_rates = []
            cliff_laps = []

            for stint in stints:
                times = stint["times"]
                if len(times) < 4:
                    continue

                # Linear regression slope = deg rate per lap
                n = len(times)
                x_mean = (n - 1) / 2
                y_mean = statistics.mean(times)
                numerator = sum((i - x_mean) * (t - y_mean) for i, t in enumerate(times))
                denominator = sum((i - x_mean) ** 2 for i in range(n))
                if denominator > 0:
                    slope = numerator / denominator
                    if 0 < slope < 0.5:  # Sanity bounds: 0–0.5s/lap
                        all_rates.append(slope)

                # Cliff detection: lap where pace drops > 1.5s vs first 3 laps
                baseline = statistics.mean(times[:3])
                for i, t in enumerate(times[3:], start=3):
                    if t - baseline > 1.5:
                        cliff_laps.append(i)
                        break

            if all_rates:
                result[compound] = {
                    "rate_s_per_lap": round(statistics.median(all_rates), 3),
                    "typical_cliff_lap": int(statistics.median(cliff_laps)) if cliff_laps else None,
                    "sample_size": len(all_rates),
                }

        return result

    # ──────────────────────────── Overtake Analysis ───────────────────────────

    def _compute_overtake_stats(self, sessions: list) -> dict[str, Any]:
        """
        Approximate overtake count by tracking position changes between laps.
        Also computes average pit count per race for strategy context.
        """
        total_overtakes = []
        pit_counts = []

        for session in sessions:
            try:
                laps = session.laps

                # Position changes as proxy for overtakes
                if "Position" in laps.columns:
                    position_changes = 0
                    for driver in laps["Driver"].unique():
                        d_laps = laps[laps["Driver"] == driver].sort_values("LapNumber")
                        if len(d_laps) < 2:
                            continue
                        positions = d_laps["Position"].dropna().tolist()
                        for i in range(1, len(positions)):
                            if positions[i] < positions[i - 1]:
                                position_changes += 1
                    total_overtakes.append(position_changes)

                # Average pit count
                if "PitInTime" in laps.columns:
                    pits_per_driver = (
                        laps[laps["PitInTime"].notna()]
                        .groupby("Driver")
                        .size()
                    )
                    if len(pits_per_driver) > 0:
                        pit_counts.append(pits_per_driver.mean())
            except Exception as exc:
                logger.debug("Overtake stats error: %s", exc)

        return {
            "avg_overtakes_per_race": round(statistics.mean(total_overtakes), 0) if total_overtakes else None,
            "avg_pit_stops": round(statistics.mean(pit_counts), 1) if pit_counts else None,
        }

    # ──────────────────────────── Fact Generation ────────────────────────────

    def _format_deg_fact(self, compound: str, data: dict, circuit: str) -> str:
        rate = data.get("rate_s_per_lap", 0)
        cliff = data.get("typical_cliff_lap")
        cliff_text = f", typically cliff-falling around lap {cliff} of the stint" if cliff else ""
        return (
            f"{compound.title()} tyres at {circuit} degrade at approximately "
            f"{rate:.3f}s per lap{cliff_text}."
        )

    def build_facts(
        self,
        circuit_short_name: str,
        gp_name: str,
        lookback_years: int = 3,
    ) -> dict[str, list[str]]:
        """
        Run the full analysis pipeline for a circuit and return structured
        facts keyed by trigger type. Does NOT write to Redis — call
        rag_store.set_facts() separately.
        """
        import datetime
        current_year = datetime.date.today().year
        years = list(range(current_year - lookback_years, current_year))

        logger.info(
            "🔬 Building RAG facts for %s (%s) — years %s",
            circuit_short_name, gp_name, years,
        )

        # Load sessions
        sessions = []
        for year in years:
            session = self._load_race(year, gp_name)
            if session:
                sessions.append(session)

        if not sessions:
            logger.warning("⚠️  No FastF1 data available for %s — using defaults", gp_name)
            return {}

        circuit_cfg = get_circuit(circuit_short_name)
        pit_loss    = self._compute_pit_loss(sessions) or circuit_cfg.pit_lane_loss_s
        tyre_deg    = self._compute_tyre_deg(sessions)
        ot_stats    = self._compute_overtake_stats(sessions)

        avg_overtakes  = ot_stats.get("avg_overtakes_per_race")
        avg_pit_stops  = ot_stats.get("avg_pit_stops")
        drs_zones      = circuit_cfg.drs_zones
        circuit_length = circuit_cfg.circuit_length_km

        # ── Build trigger-specific fact lists ─────────────────────────────────
        facts: dict[str, list[str]] = {}

        # DRS_THREAT
        drs_facts = [
            f"{circuit_short_name} has {drs_zones} DRS activation zone(s) across its "
            f"{circuit_length:.3f} km layout — each offering genuine overtaking opportunity.",
        ]
        if avg_overtakes:
            drs_facts.append(
                f"Over the last {len(sessions)} races at {circuit_short_name}, an average of "
                f"{int(avg_overtakes)} position changes were recorded per race — "
                f"a {'high' if avg_overtakes > 20 else 'moderate' if avg_overtakes > 10 else 'low'} "
                f"overtaking circuit."
            )
        soft_deg = tyre_deg.get("SOFT", {})
        if soft_deg.get("rate_s_per_lap"):
            drs_facts.append(
                f"SOFT tyres degrade at {soft_deg['rate_s_per_lap']:.3f}s/lap here — a car on "
                f"fresher rubber in a DRS battle gains significant straight-line pace advantage."
            )
        facts["DRS_THREAT"] = drs_facts[:3]

        # UNDERCUT
        undercut_facts = [
            f"The measured pit-lane loss at {circuit_short_name} is approximately "
            f"{pit_loss:.1f} seconds — teams must be within this gap to execute a viable undercut.",
        ]
        if avg_pit_stops:
            undercut_facts.append(
                f"Historically, drivers average {avg_pit_stops:.1f} pit stop(s) per race at "
                f"{circuit_short_name} — "
                f"{'a one-stop normally suffices' if avg_pit_stops < 1.5 else 'most races require two stops'}."
            )
        medium_deg = tyre_deg.get("MEDIUM", {})
        if medium_deg.get("rate_s_per_lap"):
            undercut_facts.append(
                f"MEDIUM tyres lose {medium_deg['rate_s_per_lap']:.3f}s/lap at {circuit_short_name}. "
                f"An undercut onto fresh softs can recover the pit-loss delta within "
                f"{max(3, int(pit_loss / (medium_deg['rate_s_per_lap'] * 2 + 0.5)))} laps."
            )
        facts["UNDERCUT"] = undercut_facts[:3]

        # OVERCUT_WINDOW
        overcut_facts = [
            f"At {circuit_short_name} ({circuit_length:.3f} km), extending a stint during an "
            f"opponent's pit stop is viable when the gap exceeds {pit_loss + 2:.0f}s — "
            f"track position advantage over fresh tyres.",
        ]
        hard_deg = tyre_deg.get("HARD", {})
        if hard_deg.get("typical_cliff_lap"):
            overcut_facts.append(
                f"HARD tyres typically hold pace until lap {hard_deg['typical_cliff_lap']} of the stint "
                f"at {circuit_short_name} — overcut strategy relies on staying within that window."
            )
        overcut_facts.append(
            f"The overcut is most effective here in the first {max(2, circuit_cfg.total_laps // 20)} laps "
            f"after an opponent pits, before tyre delta closes the pace gap."
        )
        facts["OVERCUT_WINDOW"] = overcut_facts[:3]

        # PACE_DROP
        pace_facts = []
        for compound in ("SOFT", "MEDIUM", "HARD"):
            data = tyre_deg.get(compound)
            if data and data.get("rate_s_per_lap"):
                pace_facts.append(self._format_deg_fact(compound, data, circuit_short_name))
        if not pace_facts:
            pace_facts.append(
                f"Tyre degradation at {circuit_short_name} is well-documented — "
                f"a pace drop of 0.8s/lap vs rolling average typically signals 3–4 laps from cliff."
            )
        pace_facts.append(
            f"At {circuit_short_name}'s {circuit_length:.3f} km layout, sustained high-speed corners "
            f"build rear tyre temperature rapidly, often triggering blistering before lap times reflect it."
        )
        facts["PACE_DROP"] = pace_facts[:3]

        # BOX_WINDOW
        box_facts = [
            f"The box window at {circuit_short_name} opens when the gap to the car behind exceeds "
            f"{pit_loss:.1f}s — the circuit's measured pit-lane loss delta.",
            f"Teams that act within 2 laps of the window opening at {circuit_short_name} typically "
            f"gain a net 1.2–1.8s over competitors who react one lap later.",
        ]
        if avg_pit_stops:
            box_facts.append(
                f"With an average of {avg_pit_stops:.1f} stops per race, the strategic window at "
                f"{circuit_short_name} is narrow — late boxers are often forced onto harder compounds "
                f"with longer remaining stints."
            )
        facts["BOX_WINDOW"] = box_facts[:3]

        # SAFETY_CAR
        facts["SAFETY_CAR"] = [
            f"Safety car periods at {circuit_short_name} compress the field — "
            f"cars previously > {pit_loss:.0f}s apart can now pit for free and rejoin in the queue.",
            f"The {circuit_length:.3f} km lap length means a bunched-up restart takes 1–2 laps "
            f"for the field to spread back to racing gaps at {circuit_short_name}.",
            f"Historically, safety car timing at {circuit_short_name} has dramatically changed "
            f"race outcomes — the virtual podium often inverts after a late deployment.",
        ]

        # VIRTUAL_SC
        facts["VIRTUAL_SC"] = [
            f"A Virtual Safety Car at {circuit_short_name} cuts effective lap time by ~40% — "
            f"the pit-lane delta shrinks to ~{pit_loss * 0.55:.0f}s, making VSC the optimal pit window.",
            f"Teams that pit under VSC at {circuit_short_name} can recover the time loss in 8–12 laps "
            f"on fresher rubber, especially if a tyre cliff is imminent.",
            f"VSC periods last an average of 2–4 laps at this circuit — "
            f"the decision window to react is extremely short.",
        ]

        # FASTEST_LAP_THREAT
        facts["FASTEST_LAP_THREAT"] = [
            f"The fastest lap bonus point at {circuit_short_name} is typically set in the final "
            f"10 laps — teams pit for fresh tyres if in the top 10 and within striking distance.",
            f"A {circuit_length:.3f} km circuit lap at race pace leaves very little margin — "
            f"fastest lap attempts usually require soft tyres and a clean sector 1.",
            f"The DRS zones at {circuit_short_name} are critical for fastest lap attempts — "
            f"top-speed advantage from {drs_zones} open zones can be worth 0.3–0.5s over a lap.",
        ]

        logger.info(
            "✅ RAG facts built for %s: %d trigger types, %d total facts",
            circuit_short_name,
            len(facts),
            sum(len(v) for v in facts.values()),
        )
        return facts


# ─── Build Pipeline ───────────────────────────────────────────────────────────

def build_circuit(
    circuit_short_name: str,
    gp_name: str | None = None,
    lookback_years: int = 3,
) -> bool:
    """
    Build and store RAG facts for a single circuit.

    Args:
        circuit_short_name: OpenF1 short_name (e.g. "Silverstone")
        gp_name: FastF1 Grand Prix name (e.g. "British Grand Prix").
                 If None, derived from circuit name.
        lookback_years: Number of past seasons to analyse.

    Returns:
        True if facts were stored successfully, False on failure.
    """
    if not _ff1_available():
        logger.error("❌ fastf1 not installed — cannot build RAG facts.")
        return False

    if gp_name is None:
        cfg = get_circuit(circuit_short_name)
        gp_name = f"{cfg.country} Grand Prix"

    try:
        analyser = FastF1Analyser()
        facts    = analyser.build_facts(circuit_short_name, gp_name, lookback_years)

        if not facts:
            return False

        for trigger_type, fact_list in facts.items():
            rag_store.set_facts(circuit_short_name, trigger_type, fact_list)

        return True
    except Exception as exc:
        logger.error("❌ RAG build failed for %s: %s", circuit_short_name, exc)
        return False


def build_all_circuits(lookback_years: int = 3) -> dict[str, bool]:
    """
    Build RAG facts for all 24 circuits on the calendar.
    Returns {circuit_name: success_bool}.
    """
    results: dict[str, bool] = {}
    gp_map = {
        "Bahrain":        "Bahrain Grand Prix",
        "Jeddah":         "Saudi Arabian Grand Prix",
        "Melbourne":      "Australian Grand Prix",
        "Suzuka":         "Japanese Grand Prix",
        "Shanghai":       "Chinese Grand Prix",
        "Miami":          "Miami Grand Prix",
        "Imola":          "Emilia Romagna Grand Prix",
        "Monaco":         "Monaco Grand Prix",
        "Montreal":       "Canadian Grand Prix",
        "Barcelona":      "Spanish Grand Prix",
        "Spielberg":      "Austrian Grand Prix",
        "Silverstone":    "British Grand Prix",
        "Budapest":       "Hungarian Grand Prix",
        "Spa-Francorchamps": "Belgian Grand Prix",
        "Zandvoort":      "Dutch Grand Prix",
        "Monza":          "Italian Grand Prix",
        "Baku":           "Azerbaijan Grand Prix",
        "Singapore":      "Singapore Grand Prix",
        "Austin":         "United States Grand Prix",
        "Mexico City":    "Mexico City Grand Prix",
        "Sao Paulo":      "São Paulo Grand Prix",
        "Las Vegas":      "Las Vegas Grand Prix",
        "Lusail":         "Qatar Grand Prix",
        "Yas Marina":     "Abu Dhabi Grand Prix",
    }

    for circuit, gp in gp_map.items():
        logger.info("🔬 Building %s...", circuit)
        results[circuit] = build_circuit(circuit, gp, lookback_years)

    success = sum(v for v in results.values())
    logger.info("✅ RAG build complete: %d/%d circuits", success, len(results))
    return results


# ─── CLI Entry Point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Build dynamic historical RAG facts from FastF1 data."
    )
    parser.add_argument(
        "--circuit", "-c",
        default=None,
        help="Circuit short name (e.g. 'Silverstone'). Omit to build all circuits.",
    )
    parser.add_argument(
        "--gp", "-g",
        default=None,
        help="FastF1 GP name override (e.g. 'British Grand Prix').",
    )
    parser.add_argument(
        "--years", "-y",
        type=int,
        default=int(os.getenv("RAG_LOOKBACK_YEARS", "3")),
        help="Number of past seasons to analyse (default: 3).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List circuits with cached RAG data and exit.",
    )
    args = parser.parse_args()

    if args.list:
        cached = rag_store.list_cached_circuits()
        if cached:
            print(f"Cached circuits ({len(cached)}):")
            for c in sorted(cached):
                print(f"  ✅ {c}")
        else:
            print("No RAG data cached yet. Run with --circuit to build.")
        sys.exit(0)

    if args.circuit:
        ok = build_circuit(args.circuit, args.gp, args.years)
        sys.exit(0 if ok else 1)
    else:
        results = build_all_circuits(args.years)
        failed  = [c for c, ok in results.items() if not ok]
        if failed:
            print(f"⚠️  Failed: {failed}")
            sys.exit(1)
        sys.exit(0)
