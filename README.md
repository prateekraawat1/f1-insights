# 🏎️ F1 Live Race Insight Architecture

> **Real-time Formula 1 race strategy intelligence, powered by AI.**
> Live telemetry ingestion → mathematical trigger detection → LLM commentary → WebSocket dashboard.

---

## Architecture

```
[Telemetry Simulator] ──► [Redis Cache] ──► [Trigger Engine] ──► [LLM Assembly] ──► [WebSocket] ──► [Dashboard]
     telemetry.py              cache.py        triggers.py           app.py             /ws          frontend/
```

## Tech Stack

| Layer | Technology |
|---|---|
| Backend API | FastAPI + Uvicorn |
| State Cache | Redis (with FakeRedis fallback) |
| Telemetry | Mock scripted simulator (OpenF1-compatible) |
| Triggers | Python deterministic rule engine |
| AI / LLM | OpenAI GPT-4o-mini (with offline fallback) |
| Frontend | Vanilla HTML / CSS / JS + WebSockets |

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure (optional)

```bash
cp .env.example .env
# Edit .env — add your OPENAI_API_KEY if you have one
```

> **No OpenAI key?** The system runs a rich built-in commentary engine with no configuration required.
> **No Redis?** The system automatically uses an in-memory FakeRedis. Just run and go.

### 3. Start the backend

```bash
python run.py
```

Or with hot-reload for development:
```bash
python run.py --reload
```

### 4. Open the dashboard

Open `frontend/index.html` in your browser. It will auto-connect to `ws://localhost:8000/ws`.

### 5. Run unit tests

```bash
python -m unittest backend/test_triggers.py -v
```

---

## Trigger Conditions

| Trigger | Condition | Severity |
|---|---|---|
| `DRS_THREAT` | Gap < 1.0s at DRS detection zone | HIGH / MEDIUM |
| `UNDERCUT` | Trailing car pits while within 2.5s | HIGH |
| `PACE_DROP` | Last lap > 0.8s slower than 3-lap rolling avg | HIGH / MEDIUM |
| `BOX_WINDOW` | Pit-lane delta matches gap to clear air | HIGH |

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Server health + cache type |
| `GET` | `/grid` | Current driver states from cache |
| `GET` | `/triggers/test/{type}` | Debug: manually fire a trigger |
| `WS` | `/ws` | WebSocket — telemetry + insight stream |

**Trigger test types:** `drs`, `undercut`, `pace_drop`, `box`

Example:
```bash
curl http://localhost:8000/triggers/test/drs
```

---

## Project Structure

```
f1-insights/
├── backend/
│   ├── __init__.py
│   ├── config.py          # Environment config, thresholds, RAG facts
│   ├── cache.py           # Redis / FakeRedis wrapper
│   ├── telemetry.py       # Mock telemetry simulator
│   ├── triggers.py        # Deterministic trigger engine
│   ├── app.py             # FastAPI app + WebSocket + LLM assembly
│   └── test_triggers.py   # Unit tests
├── frontend/
│   └── index.html         # Premium real-time dashboard
├── .env.example           # Environment template
├── requirements.txt
├── run.py                 # Convenience launcher
└── README.md
```

---

## Simulated Race Scenario

The simulator runs a scripted **Lap 42 British GP** sequence at Silverstone:

1. **Laps 42–43**: HAM closes on VER from 3.8s → `DRS_THREAT` fires at 0.95s gap
2. **Lap 43**: HAM pits from 1.4s behind → `UNDERCUT` fires
3. **Lap 44**: VER tyre degradation accelerates → `PACE_DROP` fires
4. **Lap 44–45**: Gap window matches pit-lane delta → `BOX_WINDOW` fires
5. **Laps 45–46**: HAM chases on fresh softs → second `DRS_THREAT`
6. **Lap 46**: HAM overtakes for the lead 🏆

Each trigger assembles a structured LLM payload with live telemetry + historical RAG facts, then broadcasts the AI insight to all connected WebSocket clients.