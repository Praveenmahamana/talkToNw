# Airline Schedule Intelligence Application

A **production-grade hybrid AI + deterministic rule engine** for airline schedule analysis, feasibility simulation, and natural language querying.

---

## Architecture

```
User Query
    │
    ▼
FastAPI (app/main.py)
    │
    ├── POST /ingest          → Ingestion layer (loader + normaliser) → DuckDB
    ├── POST /query           → Gemini Agent → Tool Registry → Rule Engine → Answer
    ├── POST /simulate/add-flight   → Simulation → Rule Engine
    └── POST /simulate/retime-flight → Simulation → Rule Engine
```

### Key Design Principles

| Principle | Implementation |
|-----------|---------------|
| **AI orchestrates, never computes** | Gemini calls Python tools for all feasibility |
| **Deterministic rule engine** | Pure Python, no LLM for any calculation |
| **Structured outputs** | Every response has verdict, facts, violations, confidence |
| **Configurable rules** | All thresholds in `app/config/rules.yaml` |
| **Graceful degradation** | Works without Vertex AI (deterministic-only mode) |

---

## Project Structure

```
airline_schedule_app/
├── app/
│   ├── main.py                    # FastAPI entry point
│   ├── api/
│   │   ├── routes.py              # REST endpoints
│   │   └── schemas.py             # Pydantic models
│   ├── ingestion/
│   │   ├── loader.py              # CSV / SSIM file loader
│   │   └── normalizer.py          # Column detection + schema normalisation
│   ├── database/
│   │   ├── db.py                  # DuckDB connection (singleton)
│   │   ├── models.py              # Table DDL
│   │   └── queries.py             # Reusable SQL queries
│   ├── services/
│   │   ├── schedule_service.py    # High-level data access
│   │   ├── route_analysis_service.py
│   │   └── itinerary_service.py   # Connection finder
│   ├── rules/                     # ← ALL feasibility logic lives here
│   │   ├── turnaround.py          # Ground time validation
│   │   ├── curfew.py              # Airport curfew checks
│   │   ├── rotation.py            # Aircraft overlap detection
│   │   ├── connectivity.py        # MCT / connection feasibility
│   │   ├── spacing.py             # Route + airport spacing
│   │   ├── scoring.py             # Hub bank alignment + composite scoring
│   │   └── rule_engine.py         # Orchestrator — runs all rules
│   ├── simulation/
│   │   ├── add_flight.py          # simulate_add_flight()
│   │   └── retime_flight.py       # simulate_retime_flight()
│   ├── ai/
│   │   ├── vertex_client.py       # Vertex AI / Gemini SDK wrapper
│   │   ├── tool_registry.py       # Tool definitions + dispatcher
│   │   ├── agent.py               # Agent execution loop
│   │   └── prompts.py             # System prompt
│   ├── config/
│   │   └── rules.yaml             # Configurable constraints
│   └── utils/
│       ├── time_utils.py          # Time / timezone helpers
│       └── logging.py             # Loguru setup
├── tests/
│   ├── conftest.py
│   ├── test_rule_engine.py
│   ├── test_simulation.py
│   └── test_ingestion.py
├── data/
│   ├── schedules/                 # Drop schedule files here
│   └── output/                    # DuckDB database file
├── logs/
├── requirements.txt
└── README.md
```

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. (Optional) Configure Vertex AI

```bash
export GOOGLE_CLOUD_PROJECT="your-gcp-project"
export GOOGLE_CLOUD_LOCATION="us-central1"
```

If not configured, the API runs in **deterministic-only mode** (all rule-engine features work, only the `/query` NL endpoint is disabled).

### 3. Place schedule files in `data/schedules/`

Supported formats:
- **CSV / TSV / TXT** — auto-column detection (DEP/ARR/ORG/DST/STD/STA/…)
- **SSIM** — IATA Standard Schedules Information Manual (`.ssim`)

### 4. Start the server

```bash
uvicorn app.main:app --reload --port 8000
```

Or directly:
```bash
python app/main.py
```

### 5. Open interactive docs

```
http://localhost:8000/docs
```

---

## API Reference

### `GET /api/v1/health`
Returns API status, flight count, and Vertex AI availability.

### `POST /api/v1/ingest`
Load schedule files from a local folder.
```json
{ "folder_path": "/path/to/data/schedules" }
```

### `POST /api/v1/query`
Natural language query (requires Vertex AI).
```json
{ "query": "What flights operate from DXB to LHR on Mondays?" }
```

### `GET /api/v1/schedule/search?origin=DXB&destination=LHR`
Search flights by O&D, airline, or flight number.

### `GET /api/v1/schedule/route/{origin}/{destination}`
Full route analysis: frequency, market share, gaps, departures.

### `POST /api/v1/simulate/add-flight`
Full feasibility simulation.
```json
{
  "origin": "DXB",
  "destination": "LHR",
  "departure_local": "2024-03-15 08:00",
  "arrival_local": "2024-03-15 13:00",
  "aircraft_type": "B777",
  "airline": "EK",
  "hub": "DXB"
}
```

**Response includes:**
- `verdict` — plain-English conclusion
- `feasibility_score` — 0–100
- `network_value_score` — 0–100
- `violations` — specific rule failures
- `risks` — operational risks
- `alternatives` — suggested departure windows
- `why_not` — explanation if infeasible
- `confidence` — High / Medium / Low

### `POST /api/v1/simulate/retime-flight`
Evaluate changing an existing flight's departure time.
```json
{
  "flight_number": "EK500",
  "new_departure_local": "2024-03-15 10:00",
  "hub": "DXB"
}
```

---

## Rule Engine

All rules are implemented in `app/rules/` and configured via `app/config/rules.yaml`.

| Rule | File | Description |
|------|------|-------------|
| Turnaround | `turnaround.py` | Min ground time by aircraft type |
| Curfew | `curfew.py` | Airport operating hour restrictions |
| Aircraft overlap | `rotation.py` | Prevents double-booking same aircraft |
| Route spacing | `spacing.py` | Min gap between flights on same O&D |
| Connection MCT | `connectivity.py` | Minimum Connection Time at hubs |
| Hub bank alignment | `scoring.py` | Bank window matching at hub airports |
| Composite scoring | `scoring.py` | 0–100 feasibility + network value |

All rules return:
```python
{
    "feasible":   bool,
    "violations": list[str],
    "warnings":   list[str],
    "metrics":    dict,
}
```

---

## Configuration (`app/config/rules.yaml`)

Key configurable parameters:

```yaml
turnaround:
  minimum_minutes:
    default: 45
    wide_body: 90    # B777, B787, A350, etc.
    regional: 30     # E190, ATR, etc.

curfew:
  airports:
    LHR:
      start: "23:00"
      end: "06:00"
      timezone: "Europe/London"

spacing:
  minimum_minutes_same_route: 60
  minimum_minutes_same_airport: 15

connectivity:
  minimum_connection_minutes: 45
  maximum_connection_minutes: 240
```

---

## Running Tests

```bash
pytest tests/ -v
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SCHEDAI_DB_PATH` | `data/output/schedules.duckdb` | DuckDB file path |
| `SCHEDAI_LOG_LEVEL` | `INFO` | Log level |
| `SCHEDAI_LOG_FILE` | `logs/app.log` | Log file path |
| `SCHEDAI_DATA_FOLDER` | `data/schedules` | Auto-ingest folder |
| `GOOGLE_CLOUD_PROJECT` | — | GCP project for Vertex AI |
| `GOOGLE_CLOUD_LOCATION` | `us-central1` | Vertex AI region |
