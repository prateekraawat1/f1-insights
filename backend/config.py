"""
config.py - Central configuration for the F1 Live Race Insight Architecture.
Loads environment variables and defines system-wide constants.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─── Server ───────────────────────────────────────────────────────────────────
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8000"))

# ─── Redis ────────────────────────────────────────────────────────────────────
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB   = int(os.getenv("REDIS_DB", "0"))

# Namespace prefix for all F1 state keys
REDIS_KEY_PREFIX = "f1:state:"

# ─── OpenAI ───────────────────────────────────────────────────────────────────
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL    = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", "250"))

# ─── Telemetry Simulation ─────────────────────────────────────────────────────
TELEMETRY_POLL_INTERVAL = float(os.getenv("TELEMETRY_POLL_INTERVAL", "2.5"))  # seconds
TRIGGER_CHECK_INTERVAL  = float(os.getenv("TRIGGER_CHECK_INTERVAL",  "1.0"))  # seconds

# ─── Race Context ─────────────────────────────────────────────────────────────
TRACK_NAME = "Silverstone"
RACE_NAME  = "British Grand Prix 2024"
TOTAL_LAPS = 52

# ─── Trigger Thresholds ───────────────────────────────────────────────────────
DRS_THREAT_THRESHOLD_S  = 1.0    # seconds — gap < this triggers DRS_THREAT
UNDERCUT_GAP_THRESHOLD  = 2.5    # seconds — trailing car within this when pitting
PACE_DROP_THRESHOLD_S   = 0.8    # seconds — lap slower than 3-lap avg by this much
BOX_WINDOW_DELTA_MARGIN = 0.5    # seconds — tolerance for pit-window match

# ─── LLM System Prompt ────────────────────────────────────────────────────────
LLM_SYSTEM_PROMPT = """You are an elite Formula 1 race strategist and pit-wall analyst with 20 years of experience 
working for Red Bull Racing. You receive real-time telemetry triggers during a live Grand Prix and must deliver 
a concise, high-impact insight in the style of a top-tier broadcaster like Martin Brundle or Karun Chandhok.

Rules:
- Keep insight to 2–3 punchy sentences.
- Reference specific driver codes (e.g. VER, HAM), lap numbers, and gap data.
- Blend the live data with the historical facts naturally — do NOT list them separately.
- Convey the strategic implication, not just the observation.
- Use racing terminology confidently (undercut, delta, free air, DRS window, blistering, etc.).
"""

# ─── Historical RAG Facts (Static for Prototype) ──────────────────────────────
HISTORICAL_FACTS: dict[str, list[str]] = {
    "DRS_THREAT": [
        "Hamilton has successfully overtaken Verstappen at Silverstone in 8 of their 12 head-to-head encounters.",
        "Verstappen's average DRS defence at Silverstone success rate drops to 43% when the gap falls below 0.6s by Stowe.",
        "The Copse-Maggots-Becketts complex historically neutralises a DRS advantage, making the Wellington Straight the key overtaking zone.",
    ],
    "UNDERCUT": [
        "Hamilton's undercut success rate at Silverstone is 71% when executed within 2.5 seconds of the leader.",
        "A standard pit stop at Silverstone costs approximately 23 seconds in stationary time plus ~6s pit-lane traversal.",
        "Verstappen's Red Bull RB20 historically responds to undercut pressure by extending stints to 'overcut' back.",
    ],
    "PACE_DROP": [
        "Tyre degradation at Silverstone's high-speed corners typically manifests first as understeer at Copse.",
        "A pace drop exceeding 0.8s per lap usually signals a tyre within 3–4 laps of cliff degradation.",
        "Verstappen's tyre management under pressure has historically been weaker than his qualifying pace suggests.",
    ],
    "BOX_WINDOW": [
        "The optimal pit window at Silverstone opens when the gap to traffic exceeds 23 seconds — the total pit-lane delta.",
        "Teams that execute their box window within 2 laps of the trigger gain an average of 1.4 seconds net over competitors.",
        "Mercedes has historically been 0.4 seconds faster than Red Bull in stationary pit-stop time at Silverstone.",
    ],
}
