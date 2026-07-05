"""
app.py - FastAPI application for the F1 Live Race Insight Architecture.

Responsibilities:
  • Serve the REST API health endpoint
  • Run background loops for telemetry ingestion and trigger evaluation
  • Assemble rich LLM context payloads when triggers fire
  • Call OpenAI (or fall back to a deterministic commentary engine)
  • Broadcast telemetry snapshots and AI insights to all connected WebSocket clients
"""

import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.cache import cache
from backend.config import (
    LLM_SYSTEM_PROMPT,
    OPENAI_API_KEY,
    OPENAI_MAX_TOKENS,
    OPENAI_MODEL,
    RACE_NAME,
    TRACK_NAME,
    TRIGGER_CHECK_INTERVAL,
)
from backend.rag import rag_store
from backend.session import session_monitor
from backend.openf1_client import OpenF1Client
from backend.triggers import TriggerEngine, TriggerEvent, TriggerType
from backend.monitoring import setup_structured_logging, data_quality_loop

# Initialize structured JSON logging
setup_structured_logging()
logger = logging.getLogger(__name__)


# ─── WebSocket Connection Manager ─────────────────────────────────────────────

class ConnectionManager:
    """Tracks all active WebSocket connections and provides broadcast."""

    def __init__(self) -> None:
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.append(ws)
        logger.info("🔌 Client connected | total=%d", len(self.active))

    def disconnect(self, ws: WebSocket) -> None:
        self.active.remove(ws)
        logger.info("🔌 Client disconnected | total=%d", len(self.active))

    async def broadcast(self, message: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        payload = json.dumps(message, default=str)
        for ws in self.active:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.remove(ws)


manager = ConnectionManager()


# ─── LLM Context Assembly ─────────────────────────────────────────────────────

def _build_llm_payload(event: TriggerEvent, ver: dict, ham: dict) -> dict[str, Any]:
    """
    Assembles the structured JSON payload that is sent to the LLM.
    Includes live telemetry data and dynamic historical RAG facts.
    """
    facts = rag_store.get_facts(event.track, event.trigger.value)
    selected_facts = random.sample(facts, min(2, len(facts)))

    live_data = {
        "chasing":          event.chasing,
        "leading":          event.leading,
        "gap":              f"{abs(event.gap_s):.2f}s",
        "lap":              str(event.lap),
        "total_laps":       "52",
        "track":            event.track,
        "race":             RACE_NAME,
        "severity":         event.severity,
    }

    # Enrich with driver-specific telemetry
    for code, state in [("VER", ver), ("HAM", ham)]:
        live_data[f"{code.lower()}_tyre"]     = state.get("tyre_compound", "?")
        live_data[f"{code.lower()}_tyre_age"] = state.get("tyre_age_laps", "?")
        live_data[f"{code.lower()}_lap_time"] = f"{state.get('lap_time_s', 0):.3f}s"
        live_data[f"{code.lower()}_speed"]    = f"{state.get('speed_kph', 0):.1f}kph"

    # Merge trigger-specific extra data
    live_data.update(event.extra)

    return {
        "trigger":              event.trigger.value,
        "live_data":            live_data,
        "historical_rag_facts": selected_facts,
    }


# ─── OpenAI Integration ───────────────────────────────────────────────────────

async def _call_openai(payload: dict[str, Any]) -> str:
    """
    Calls the OpenAI Chat Completions API with the assembled payload.
    Returns the generated insight string.
    """
    user_message = (
        f"Race trigger: {payload['trigger']}\n\n"
        f"Live data: {json.dumps(payload['live_data'], indent=2)}\n\n"
        f"Historical context: {json.dumps(payload['historical_rag_facts'], indent=2)}\n\n"
        "Generate your high-impact race insight now."
    )

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENAI_MODEL,
                "max_tokens": OPENAI_MAX_TOKENS,
                "messages": [
                    {"role": "system", "content": LLM_SYSTEM_PROMPT},
                    {"role": "user",   "content": user_message},
                ],
                "temperature": 0.75,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()


# ─── Fallback Commentary Engine ────────────────────────────────────────────────

_COMMENTARY_TEMPLATES: dict[TriggerType, list[str]] = {
    TriggerType.DRS_THREAT: [
        (
            "Hamilton is RIGHT in the DRS zone at {track} — {gap}s and closing fast on Lap {lap}! "
            "VER's tyres are {ver_tyre_age} laps old; those mediums are starting to grain badly at the high-speed "
            "complexes. HAM's fresh {ham_tyre} rubber is generating huge traction out of Brooklands — "
            "this could be the move of the race on the Wellington Straight."
        ),
        (
            "The DRS window is OPEN — {gap}s is all that separates Hamilton from Verstappen on Lap {lap}. "
            "With {ver_tyre_age} laps on his mediums, VER's rear tyre degradation is becoming critical. "
            "Historically at {track}, Hamilton's overtake success rate in this zone is exceptional — "
            "Red Bull's pit wall will be sweating right now."
        ),
        (
            "This is where races are won and lost! {gap}s gap at the DRS detection zone — Lap {lap}. "
            "VER on {ver_tyre_age}-lap-old {ver_tyre}s trying to hold off Hamilton's relentless pursuit. "
            "The aerodynamic wash from Verstappen's car is working against him here — "
            "Hamilton can maximise ERS on the straight and that will be very difficult to defend."
        ),
    ],
    TriggerType.UNDERCUT: [
        (
            "BRILLIANT strategic move from Mercedes! Hamilton boxes from {gap}s behind on Lap {lap} — "
            "this is the undercut play. With VER on {ver_tyre_age}-lap-old {ver_tyre}s and Hamilton about to "
            "emerge on fresh rubber, the pace differential could be 1.5 seconds per lap. "
            "Red Bull MUST respond within this lap or risk losing the race lead to the undercut."
        ),
        (
            "Hamilton's in the pits — the undercut is ON! At {gap}s behind with {ver_tyre_age} laps on VER's "
            "{ver_tyre} tyres, this is the moment Mercedes have been building towards. "
            "The stationary stop time plus pit-lane traversal costs ~{pit_lane_loss_s}s, but fresh softs "
            "could recover that in just 3 laps. Verstappen is now in a very uncomfortable position."
        ),
    ],
    TriggerType.PACE_DROP: [
        (
            "WARNING: {affected_driver}'s lap times are deteriorating rapidly — {delta_s}s slower than "
            "their 3-lap average on Lap {lap}! Those {tyre_compound} tyres ({tyre_age_laps} laps old) "
            "are heading for the cliff. At {track}, this typically manifests as severe understeer at "
            "Copse — the window to pit without losing strategic position is closing FAST."
        ),
        (
            "The tyres are GONE on the {affected_driver} car — {delta_s}s drop in lap time vs. rolling "
            "average. Lap {lap} and those {tyre_age_laps}-lap-old {tyre_compound}s are simply done. "
            "Every lap they stay out now costs track position. The opponent is {delta_s}s a lap faster — "
            "this lead will evaporate without an immediate box call."
        ),
    ],
    TriggerType.BOX_WINDOW: [
        (
            "THE BOX WINDOW IS OPEN! On Lap {lap}, the maths align perfectly — pit-lane delta of "
            "{pit_lane_loss_s}s matches the gap to clear air. VER on {ver_tyre_age}-lap-old tyres "
            "can pit NOW and emerge ahead of Hamilton. This window lasts ~2 laps before "
            "Hamilton's fresher tyres close it permanently. It's now or never from the Red Bull pit wall!"
        ),
        (
            "Strategic inflection point at {track} — Lap {lap}. The gap delta equals the pit-lane loss "
            "within margin. VER can execute a reactive pit stop and return to track in free air. "
            "With {ver_tyre_age} laps on his {ver_tyre}s, this is the optimal moment — "
            "delay any longer and the tyre cliff will make this a defensive nightmare."
        ),
    ],
}

def _fallback_commentary(payload: dict[str, Any]) -> str:
    """
    Generates a rich, template-based commentary insight when OpenAI is unavailable.
    Uses the trigger type to select from pre-written, data-filled templates.
    """
    trigger_type = TriggerType(payload["trigger"])
    templates    = _COMMENTARY_TEMPLATES.get(trigger_type, [])

    if not templates:
        return f"⚡ {trigger_type.value} detected — live strategic analysis incoming."

    template = random.choice(templates)
    data     = {**payload["live_data"]}

    try:
        return template.format(**data)
    except KeyError:
        # If template references missing key, return a safe fallback
        return (
            f"🏎️  {trigger_type.value} triggered on Lap {data.get('lap', '?')} "
            f"at {data.get('track', 'the circuit')}. "
            f"Gap: {data.get('gap', '?')} between {data.get('chasing', '?')} and {data.get('leading', '?')}."
        )


# ─── LLM Dispatcher ───────────────────────────────────────────────────────────

async def generate_insight(event: TriggerEvent, ver: dict, ham: dict) -> str:
    """
    Assembles the LLM payload, then dispatches to OpenAI or the fallback engine.
    Returns the final insight string.
    """
    payload = _build_llm_payload(event, ver, ham)

    if OPENAI_API_KEY:
        try:
            logger.info("🤖 Calling OpenAI for %s insight...", event.trigger.value)
            insight = await _call_openai(payload)
            logger.info("✅ OpenAI insight received (%d chars)", len(insight))
            return insight
        except Exception as exc:
            logger.warning("⚠️  OpenAI call failed: %s — using fallback.", exc)

    logger.info("🧠 Using fallback commentary engine for %s", event.trigger.value)
    return _fallback_commentary(payload)


# ─── Background Task: Trigger Loop ────────────────────────────────────────────

_trigger_engine = TriggerEngine()

async def trigger_loop() -> None:
    """
    Runs continuously, evaluating triggers against the cache and
    broadcasting AI insights when events fire.
    """
    logger.info("🔍 Trigger engine loop started")
    while True:
        events = _trigger_engine.evaluate()
        for event in events:
            ver = cache.get_state("VER") or {}
            ham = cache.get_state("HAM") or {}

            # Generate insight (async, may call OpenAI)
            insight = await generate_insight(event, ver, ham)

            message = {
                "type":    "INSIGHT",
                "trigger": event.to_dict(),
                "insight": insight,
                "ts":      time.time(),
            }
            await manager.broadcast(message)
            logger.info(
                "📢 Broadcasted %s insight to %d clients",
                event.trigger.value, len(manager.active),
            )

        await asyncio.sleep(TRIGGER_CHECK_INTERVAL)


# ─── Background Task: Telemetry & Session Loop ───────────────────────────────

openf1_client = OpenF1Client(session_monitor)

async def ws_broadcast(payload: dict) -> None:
    """Broadcast generic payload (TELEMETRY, SESSION_INFO, RACE_CONTROL) to WebSocket clients."""
    await manager.broadcast(payload)

# Register broadcast callbacks for background tasks
session_monitor.set_broadcast_callback(ws_broadcast)
openf1_client._broadcast_cb = ws_broadcast


# ─── App Lifespan ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start background loops on startup."""
    logger.info("🏁 F1 Insight Architecture starting up...")
    logger.info("📦 Cache backend: %s", cache.backend_type)

    # Flush stale state from previous runs
    cache.flush()

    loop = asyncio.get_event_loop()
    t1 = loop.create_task(session_monitor.run(), name="session-monitor")
    t2 = loop.create_task(openf1_client.run(), name="openf1-client")
    t3 = loop.create_task(trigger_loop(), name="trigger-loop")
    t4 = loop.create_task(data_quality_loop(), name="data-quality")

    yield

    t1.cancel()
    t2.cancel()
    t3.cancel()
    t4.cancel()
    logger.info("🛑 Background tasks cancelled — shutdown complete.")


# ─── FastAPI Application ───────────────────────────────────────────────────────

app = FastAPI(
    title="F1 Live Race Insight API",
    description="Real-time Formula 1 telemetry ingestion, trigger detection, and AI-powered race insights.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── New Endpoints for Phase 10 (Analytics & Schedule) ─────────────────────────

import fastf1
import os

_CACHE_DIR = "./fastf1_cache"
os.makedirs(_CACHE_DIR, exist_ok=True)
fastf1.Cache.enable_cache(_CACHE_DIR)

@app.get("/api/analytics")
def get_analytics(track: str):
    """Return historical tyre cliff and pit loss data for the track."""
    from backend.circuits import get_circuit
    circuit = get_circuit(track)
    return {
        "pit_lane_loss_s": circuit.pit_lane_loss_s,
        "degradation": {
            "SOFT": {"slope": 0.112, "cliff_lap": 12},
            "MEDIUM": {"slope": 0.075, "cliff_lap": 24}
        },
        "overtake": {"avg_delta_s": 1.2}
    }

@app.get("/api/results/{year}/{track}")
def get_past_results(year: int, track: str):
    """Fetch top 10 race results for a past session."""
    # Note: This is a blocking call and takes ~10-20s the first time
    try:
        session = fastf1.get_session(year, track, 'R')
        session.load(telemetry=False, weather=False, messages=False)
        results = session.results.head(10)
        
        # Convert to dictionary safely
        res_list = []
        for _, row in results.iterrows():
            res_list.append({
                "Position": row.get("Position", 0),
                "DriverNumber": row.get("DriverNumber", ""),
                "Abbreviation": row.get("Abbreviation", ""),
                "BroadcastName": row.get("BroadcastName", ""),
                "TeamName": row.get("TeamName", ""),
                "Status": row.get("Status", ""),
                "Points": row.get("Points", 0)
            })
        return {"year": year, "track": track, "results": res_list}
    except Exception as e:
        logger.error(f"Error fetching past results: {e}")
        return {"error": str(e)}

@app.get("/api/schedule")
def get_schedule():
    """Fetch the F1 calendar for the current year."""
    try:
        current_year = 2024 # Hardcode 2024 since FastF1 might not have future years
        schedule = fastf1.get_event_schedule(current_year)
        
        # Filter for upcoming races or just return all
        events = []
        for _, row in schedule.iterrows():
            if row.get("EventFormat") != "testing":
                events.append({
                    "RoundNumber": row.get("RoundNumber"),
                    "Country": row.get("Country"),
                    "Location": row.get("Location"),
                    "EventName": row.get("EventName"),
                    "EventDate": str(row.get("EventDate"))
                })
        return {"year": current_year, "schedule": events}
    except Exception as e:
        logger.error(f"Error fetching schedule: {e}")
        return {"error": str(e)}

# ─── System Endpoints ───────────────────────────────────────────────────────────

@app.get("/health", tags=["meta"])
async def health() -> JSONResponse:
    """API health check — returns cache status and connected clients."""
    return JSONResponse({
        "status":          "ok",
        "cache_backend":   cache.backend_type,
        "clients":         len(manager.active),
        "openai_enabled":  bool(OPENAI_API_KEY),
        "track":           TRACK_NAME,
        "race":            RACE_NAME,
    })


@app.get("/grid", tags=["telemetry"])
async def grid() -> JSONResponse:
    """Returns the current state of all tracked drivers from the cache."""
    drivers = cache.get_all_drivers()
    states  = {d: cache.get_state(d) for d in drivers}
    meta    = {
        "lap":   cache.get_race_meta("current_lap"),
        "track": cache.get_race_meta("track"),
        "gap":   cache.get_race_meta("gap_ver_ham"),
    }
    return JSONResponse({"drivers": states, "meta": meta})


@app.get("/triggers/test/{trigger_type}", tags=["debug"])
async def test_trigger(trigger_type: str) -> JSONResponse:
    """
    Debug endpoint to manually fire and preview a trigger event payload
    without waiting for the simulation to reach that state.
    """
    from backend.triggers import TriggerEvent, TriggerType

    mapping = {
        "drs":       TriggerType.DRS_THREAT,
        "undercut":  TriggerType.UNDERCUT,
        "pace_drop": TriggerType.PACE_DROP,
        "box":       TriggerType.BOX_WINDOW,
    }
    tt = mapping.get(trigger_type.lower())
    if not tt:
        return JSONResponse({"error": f"Unknown trigger. Use: {list(mapping.keys())}"}, status_code=400)

    event = TriggerEvent(
        trigger=tt,
        chasing="HAM",
        leading="VER",
        gap_s=0.72,
        lap=42,
        track=TRACK_NAME,
        severity="HIGH",
        extra={"debug": True, "ver_tyre_age": 17, "ver_tyre": "MEDIUM",
               "ham_tyre": "SOFT", "pit_lane_loss_s": 22.0, "delta_s": 1.1,
               "affected_driver": "VER", "tyre_compound": "MEDIUM",
               "tyre_age_laps": 17},
    )
    ver = cache.get_state("VER") or {"tyre_compound": "MEDIUM", "tyre_age_laps": 17, "lap_time_s": 89.6, "speed_kph": 295.0}
    ham = cache.get_state("HAM") or {"tyre_compound": "SOFT", "tyre_age_laps": 1, "lap_time_s": 87.8, "speed_kph": 308.0}

    insight = await generate_insight(event, ver, ham)
    return JSONResponse({
        "trigger": event.to_dict(),
        "insight": insight,
    })


# ─── WebSocket Endpoint ────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """
    Primary WebSocket endpoint. Clients connect here to receive:
      - TELEMETRY: Full grid state snapshots every ~2.5 seconds.
      - INSIGHT: AI-generated commentary when a trigger fires.
    """
    await manager.connect(websocket)

    # Send a welcome handshake immediately
    await websocket.send_text(json.dumps({
        "type":    "CONNECTED",
        "message": f"Connected to F1 Live Race Insight — {RACE_NAME} @ {TRACK_NAME}",
        "cache":   cache.backend_type,
        "ts":      time.time(),
    }))

    try:
        while True:
            # Keep the connection alive; all data is pushed server-side
            await asyncio.sleep(30)
    except WebSocketDisconnect:
        manager.disconnect(websocket)
