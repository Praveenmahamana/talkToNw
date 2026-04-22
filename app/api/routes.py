"""
FastAPI route definitions for the Airline Schedule Intelligence API.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Depends
from loguru import logger

from app.api.schemas import (
    IngestRequest, IngestResponse,
    QueryRequest, QueryResponse,
    SuggestRequest, SuggestResponse,
    AddFlightRequest, AddFlightResponse,
    RetimeFlightRequest, RetimeFlightResponse,
    HealthResponse, RouteSearchRequest,
)
from app.services.schedule_service import ScheduleService
from app.services.route_analysis_service import RouteAnalysisService
from app.services.itinerary_service import ItineraryService
from app.services.viz_service import extract_visualizations
from app.simulation.add_flight import simulate_add_flight
from app.simulation.retime_flight import simulate_retime_flight
from app.ai.agent import ScheduleAgent
from app.ai.vertex_client import is_available


router = APIRouter()

# Shared service instances (stateless)
_svc     = ScheduleService()
_ra_svc  = RouteAnalysisService()
_it_svc  = ItineraryService()
_agent   = ScheduleAgent()


def _parse_dt(s: str) -> Optional[datetime]:
    """Parse ISO-format datetime string."""
    if not s:
        return None
    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"]:
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def _df_to_json(df) -> List[Dict]:
    """Convert DataFrame to JSON-serialisable list (handles NaN, datetime, numpy types)."""
    if df is None or df.empty:
        return []
    import math
    result = []
    for rec in df.to_dict("records"):
        clean = {}
        for k, v in rec.items():
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                clean[k] = None
            elif hasattr(v, "isoformat"):
                clean[k] = v.isoformat()
            elif hasattr(v, "item"):          # numpy scalar → Python native
                clean[k] = v.item()
            elif v is None:
                clean[k] = None
            else:
                clean[k] = v
        result.append(clean)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/health", response_model=HealthResponse, tags=["System"])
async def health_check():
    """Check API, database, and AI availability."""
    try:
        count = _svc.flight_count()
    except Exception:
        count = 0
    return HealthResponse(
        status="ok",
        db_flight_count=count,
        vertex_ai=is_available(),
    )


@router.get("/schedule/summary", tags=["System"])
async def schedule_summary():
    """Return aggregate statistics about the loaded schedule (used by dashboard)."""
    from app.database.queries import get_summary_stats
    return get_summary_stats()


# ─────────────────────────────────────────────────────────────────────────────
# Ingestion
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/ingest", response_model=IngestResponse, tags=["Ingestion"])
async def ingest_schedules(request: IngestRequest):
    """
    Load and normalise schedule files from a local folder path.
    Supports CSV, TSV, TXT, and SSIM formats.
    """
    logger.info(f"POST /ingest — folder: {request.folder_path}")
    try:
        result = _svc.ingest_folder(request.folder_path)

        # Rebuild full knowledge graph stack in background after new data is loaded
        if result.get("rows_inserted", 0) > 0:
            import asyncio
            loop = asyncio.get_event_loop()

            def _rebuild_kg():
                try:
                    from app.knowledge_graph.graph_construction import rebuild_all
                    rebuild_all()
                    from app.ai.agent import init_schedule_name
                    init_schedule_name()
                except Exception as exc:
                    logger.warning(f"Post-ingest KG rebuild failed: {exc}")

            loop.run_in_executor(None, _rebuild_kg)
            logger.info("Post-ingest full KG rebuild queued.")

        return IngestResponse(
            status=result["status"],
            rows_inserted=result.get("rows_inserted", 0),
            rows_skipped=result.get("rows_skipped", 0),
            skip_reasons=result.get("skip_reasons", []),
            report=result.get("report"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception(f"Ingestion error: {exc}")
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Natural language query
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/query", response_model=QueryResponse, tags=["Query"])
async def query_schedule(request: QueryRequest):
    """
    Answer a natural language question about the schedule using
    Gemini + deterministic rule engine.
    """
    logger.info(f"POST /query — '{request.query[:80]}' persona={request.persona}")
    try:
        result = _agent.query(request.query, session_id=request.session_id, persona=request.persona)
        vizs = extract_visualizations(result.get("tool_results", []))
        return QueryResponse(
            answer=result.get("answer", ""),
            session_id=result.get("session_id", ""),
            turn=result.get("turn", 0),
            chat_history=result.get("chat_history", []),
            tools_used=result.get("tools_used", []),
            tool_results=result.get("tool_results", []),
            visualizations=vizs,
            confidence=result.get("confidence", "Low"),
            response_time=result.get("response_time"),
        )
    except Exception as exc:
        logger.exception(f"Query error: {exc}")
        raise HTTPException(status_code=500, detail=f"Query failed: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# LM-powered follow-up question suggestions
# ─────────────────────────────────────────────────────────────────────────────

_PERSONA_LABELS = {
    "route":    ("Route Analyst",    "route metrics, capacity, O&D competition"),
    "network":  ("Network Strategist","hub topology, PageRank, connectivity gaps"),
    "ops":      ("Ops Manager",       "fleet utilization, turnarounds, curfews"),
    "revenue":  ("Revenue Manager",   "yield opportunities, market share, demand"),
    "alliance": ("Alliance Director", "codeshare, interline, partnership analysis"),
}

_SUGGEST_SYSTEM = (
    "You are a research question generator for an airline network intelligence platform. "
    "Your only job is to output a JSON array of 4 short follow-up questions. "
    "Never explain. Never add keys. Output ONLY the JSON array."
)


@router.post("/suggest", response_model=SuggestResponse, tags=["Query"])
async def suggest_questions(request: SuggestRequest):
    """
    Generate 4 LM-powered follow-up questions based on the user's last query,
    the AI answer, the active persona, and any highlighted graph entities.
    Fast single-generation call — no tools, no history.
    """
    from app.ai.vertex_client import generate_content, extract_text, is_available
    import json as _json

    # Gracefully degrade when Vertex AI is not available
    if not is_available():
        return SuggestResponse(questions=[])

    persona_key = (request.persona or "").lower()
    persona_name, persona_desc = _PERSONA_LABELS.get(persona_key, ("General Analyst", "airline schedule intelligence"))

    answer_snippet = request.answer[:500].strip()
    entities_str   = ", ".join(request.entities[:10]) if request.entities else "none highlighted"

    user_msg = (
        f"User asked: {request.query}\n\n"
        f"AI answered (excerpt): {answer_snippet}\n\n"
        f"Active persona: {persona_name} — focuses on {persona_desc}\n"
        f"Entities in graph context: {entities_str}\n\n"
        "Generate exactly 4 follow-up research questions that:\n"
        "1. Continue naturally from the user's direction (don't repeat the same angle)\n"
        "2. Go progressively deeper or branch into related strategic angles\n"
        "3. Match the persona's focus area\n"
        "4. Are specific — include airport codes, airlines, or metrics where relevant\n"
        "5. Are concise (max 15 words each)\n\n"
        'Reply ONLY with a JSON array: ["Q1?", "Q2?", "Q3?", "Q4?"]'
    )

    try:
        response = generate_content(
            contents=[{"role": "user", "parts": [{"text": user_msg}]}],
            system_instruction=_SUGGEST_SYSTEM,
            temperature=0.8,
        )
        raw_text = extract_text(response) or "[]"

        # Parse — handle model wrapping in markdown code fences
        raw_text = raw_text.strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
        raw_text = raw_text.strip()

        questions = _json.loads(raw_text)
        if not isinstance(questions, list):
            questions = []
        questions = [str(q).strip() for q in questions if q][:4]
        return SuggestResponse(questions=questions)

    except Exception as exc:
        logger.warning(f"Suggest endpoint error: {exc}")
        return SuggestResponse(questions=[])


# ─────────────────────────────────────────────────────────────────────────────
# Simulation AI narrative analyser
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/sim-analyze", tags=["Simulation"])
async def sim_analyze(body: dict):
    """
    Generate an expert AI narrative for a simulation result.
    Takes the flight proposal + rule-engine output; returns plain-English
    analysis, 2-3 prioritised action items, and a confidence summary.
    """
    from app.ai.vertex_client import generate_content, extract_text, is_available
    import json as _json

    if not is_available():
        return {"headline": "", "narrative": "AI analysis unavailable.", "actions": [], "confidence": "low", "confidence_reason": ""}

    flight   = body.get("flight_proposal", {})
    sim_res  = body.get("sim_result", {})
    persona  = body.get("persona", "")

    origin      = flight.get("origin", "?")
    dest        = flight.get("destination", "?")
    dep         = flight.get("departure_local", "?")
    ac          = flight.get("aircraft_type", "?")
    airline     = flight.get("airline", "?")
    f_score     = sim_res.get("feasibility_score", "?")
    n_score     = sim_res.get("network_value_score", "?")
    verdict     = sim_res.get("verdict", "")
    violations  = sim_res.get("violations", [])
    warnings    = sim_res.get("warnings", [])
    risks       = sim_res.get("risks", [])
    best_windows= sim_res.get("best_window", [])

    persona_line = f"\nYour persona for this analysis: {persona}.\n" if persona else ""

    user_msg = f"""Analyse this airline simulation result.{persona_line}

PROPOSED FLIGHT:  {airline} {origin} → {dest}  dep {dep}  aircraft {ac}
FEASIBILITY:      {f_score}/100
NETWORK VALUE:    {n_score}/100
VERDICT:          {verdict}
VIOLATIONS ({len(violations)}): {'; '.join(str(v) for v in violations) if violations else 'None'}
WARNINGS  ({len(warnings)}):   {'; '.join(str(w) for w in warnings)   if warnings   else 'None'}
RISKS:            {'; '.join(str(r) for r in risks)[:300] if risks else 'None'}
BEST WINDOWS:     {_json.dumps(best_windows[:2]) if best_windows else 'None'}

Reply in this EXACT JSON (no fences):
{{"headline":"One punchy verdict sentence ≤15 words","narrative":"2-3 sentences plain English — cite scores and violations, explain network impact","actions":["Specific fix with exact values","Second action","Optional third action"],"confidence":"high|medium|low","confidence_reason":"One sentence"}}"""

    system = "You are a senior airline network strategist. Reply only with the requested JSON object — no markdown, no preamble."

    try:
        resp = generate_content(
            contents=[{"role": "user", "parts": [{"text": user_msg}]}],
            system_instruction=system,
            temperature=0.4,
        )
        raw = (extract_text(resp) or "").strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = _json.loads(raw.strip())
        if not isinstance(result.get("actions"), list):
            result["actions"] = []
        return result
    except Exception as exc:
        logger.warning(f"sim-analyze error: {exc}")
        return {
            "headline": verdict or "Simulation complete.",
            "narrative": (f"Feasibility {f_score}/100 · Network value {n_score}/100. " +
                          (f"Key issue: {violations[0]}" if violations else "No rule violations detected.")),
            "actions": [],
            "confidence": "low",
            "confidence_reason": "AI narrative generation failed; showing raw scores."
        }


# ─────────────────────────────────────────────────────────────────────────────
# Smart flight opportunity ranker
# ─────────────────────────────────────────────────────────────────────────────

def _slot_label(hour: int) -> str:
    if  6 <= hour <=  9: return "Business Morning"
    if 17 <= hour <= 20: return "Business Evening"
    if 12 <= hour <= 16: return "Afternoon"
    if 10 <= hour <= 11: return "Late Morning"
    if 21 <= hour <= 23: return "Late Evening"
    return "Overnight"


_SLOT_BASE_SCORES = {
    6: 40, 7: 42, 8: 40, 9: 35,
    17: 35, 18: 38, 19: 35, 20: 30,
    12: 20, 13: 22, 14: 20, 15: 18, 16: 15,
    10: 15, 11: 12,
    21: 10, 22: 8, 23: 5,
    0: 3, 1: 2, 2: 1, 3: 1, 4: 2, 5: 4,
}


@router.get("/flight-opportunities", tags=["Simulation"])
async def flight_opportunities(origin: str, destination: str, airline: str = ""):
    """
    Return 3 ranked flight-addition opportunities for an O&D pair.
    Scores time-slot gaps against existing schedule + workset demand.
    """
    from app.database.db import get_connection
    from datetime import date, timedelta

    origin = origin.upper().strip()
    dest   = destination.upper().strip()
    if not origin or not dest or len(origin) != 3 or len(dest) != 3:
        raise HTTPException(status_code=400, detail="origin and destination must be 3-letter IATA codes")

    conn = get_connection()

    # ── 1. Existing schedule hours for this O&D ───────────────────────────────
    try:
        ex_rows = conn.execute("""
            SELECT HOUR(departure_local) AS dep_hour,
                   aircraft_type, airline AS al, block_time,
                   COUNT(DISTINCT flight_number) AS flights
            FROM flights
            WHERE origin = ? AND destination = ? AND service_type = 'J'
              AND departure_local IS NOT NULL
            GROUP BY dep_hour, aircraft_type, al, block_time
            ORDER BY dep_hour
        """, [origin, dest]).fetchall()
    except Exception:
        ex_rows = []

    existing_hours = {r[0] for r in ex_rows if r[0] is not None}

    # Derive dominant aircraft + avg block time from existing schedule
    default_ac  = "77W"
    default_cap = 364
    avg_block   = 180.0
    if ex_rows:
        ac_counts: dict = {}
        block_sum = 0; block_n = 0
        for r in ex_rows:
            if r[1]: ac_counts[r[1]] = ac_counts.get(r[1], 0) + (r[4] or 1)
            if r[3]: block_sum += r[3]; block_n += 1
        if ac_counts:
            default_ac = max(ac_counts, key=ac_counts.get)
        if block_n:
            avg_block = block_sum / block_n

    # Yield and capacity proxy from block time
    if avg_block < 90:
        yield_usd = 80;  default_cap = 189; ac_hint = "73H"
    elif avg_block < 180:
        yield_usd = 140; default_cap = 189; ac_hint = "73H"
    elif avg_block < 300:
        yield_usd = 220; default_cap = 280; ac_hint = "77W"
    elif avg_block < 480:
        yield_usd = 380; default_cap = 364; ac_hint = "77W"
    else:
        yield_usd = 620; default_cap = 489; ac_hint = "388"

    if not ex_rows:
        default_ac = ac_hint

    # ── 2. Workset demand data ────────────────────────────────────────────────
    daily_demand = float(default_cap) * 0.78   # fallback
    daily_spill  = daily_demand * 0.12
    weekly_spill_total = None
    try:
        row = conn.execute("""
            SELECT SUM(demand_pax), SUM(spill_pax), AVG(cap_total)
            FROM workset_base
            WHERE origin = ? AND dest = ?
        """, [origin, dest]).fetchone()
        if row and row[0]:
            weekly_demand = float(row[0])
            weekly_spill  = float(row[1] or 0)
            daily_demand  = weekly_demand / 7
            daily_spill   = weekly_spill  / 7
            weekly_spill_total = int(weekly_spill)
            if row[2]:
                default_cap = int(row[2])
    except Exception:
        pass

    # ── 3. Score every hour slot ──────────────────────────────────────────────
    candidates = []
    for hour, base in _SLOT_BASE_SCORES.items():
        # Proximity penalty: each existing flight within 3h reduces score
        penalty = sum(max(0, 36 - abs(hour - eh) * 14) for eh in existing_hours)
        score   = max(0, base - penalty)
        candidates.append((score, hour, base))
    candidates.sort(reverse=True)
    top3 = candidates[:3]

    # ── 4. Build opportunity cards ────────────────────────────────────────────
    today = date.today()
    # Next Sunday for a clean week
    days_to_sun = (6 - today.weekday()) % 7 or 7
    dep_date = today + timedelta(days=days_to_sun)

    minutes_cycle = [30, 0, 45]
    results = []
    for rank, (score, hour, base_score) in enumerate(top3, 1):
        minute   = minutes_cycle[rank - 1]
        dep_time = f"{hour:02d}:{minute:02d}"

        # Slot share: better slot = more captured demand
        slot_share = 0.22 if base_score >= 35 else (0.16 if base_score >= 20 else 0.10)
        gap_h      = min((abs(hour - eh) for eh in existing_hours), default=12)
        pax_est    = max(40, int(min(
            daily_spill * 0.85 + daily_demand * slot_share,
            default_cap * 0.88
        )))
        rev_est = pax_est * yield_usd

        results.append({
            "rank":             rank,
            "origin":           origin,
            "destination":      dest,
            "departure_time":   dep_time,
            "departure_local":  f"{dep_date.isoformat()} {dep_time}",
            "aircraft_type":    default_ac,
            "airline":          (airline.upper() or "EK"),
            "est_pax":          pax_est,
            "est_revenue":      int(rev_est),
            "opportunity_score": round(score),
            "slot_label":       _slot_label(hour),
            "gap_hours":        round(gap_h, 1),
            "reasoning": (
                f"{_slot_label(hour)} · {gap_h:.0f}h gap from nearest existing flight"
            ),
        })

    return {
        "opportunities": results,
        "context": {
            "origin":                 origin,
            "destination":            dest,
            "existing_weekly_flights": len(ex_rows),
            "daily_demand_est":       int(daily_demand),
            "weekly_spill_total":     weekly_spill_total,
            "avg_block_min":          int(avg_block),
            "yield_usd":              yield_usd,
        },
    }


@router.get("/session/{session_id}", tags=["Query"])
async def get_session_history(session_id: str):
    """Retrieve the chat history for an existing conversation session."""
    from app.ai import session_store
    if not session_store.session_exists(session_id):
        raise HTTPException(status_code=404, detail="Session not found or expired.")
    return {
        "session_id":   session_id,
        "turn_count":   session_store.get_turn_count(session_id),
        "chat_history": session_store.get_chat_history(session_id),
    }


@router.delete("/session/{session_id}", tags=["Query"])
async def delete_session(session_id: str):
    """Delete a conversation session."""
    from app.ai import session_store
    session_store.delete_session(session_id)
    return {"status": "deleted", "session_id": session_id}


# ─────────────────────────────────────────────────────────────────────────────
# Workset comparison endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/workset/list", tags=["Workset"])
async def list_worksets():
    """Return all workset directories discoverable on the server."""
    from app.services.workset_service import get_workset_dirs
    return {"worksets": get_workset_dirs()}


@router.post("/workset/load-b", tags=["Workset"])
async def load_workset_b(body: dict):
    """Load a second workset from *path* into _b tables for comparison."""
    from app.services.workset_service import load_workset_b as _load_b
    path = body.get("path", "").strip()
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    try:
        result = _load_b(path)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"load-b error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/workset/compare", tags=["Workset"])
async def compare_worksets(origin: str = "", dest: str = "", airline: str = "", top_n: int = 50):
    """
    Compare workset A (primary) vs workset B (loaded via /workset/load-b).
    Returns per-route deltas: demand, spill, load factor, capacity, status.
    """
    from app.services.workset_service import compare_worksets as _compare
    try:
        return _compare(origin=origin, dest=dest, airline=airline, top_n=top_n)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"compare error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workset/ai-summary", tags=["Workset"])
async def workset_ai_summary(body: dict):
    """
    Ask the LM to narrate the key differences between two worksets.
    Accepts the compare result payload and returns a plain-English briefing.
    """
    from app.ai.vertex_client import generate_content, extract_text, is_available
    import json as _json

    if not is_available():
        return {"summary": "AI unavailable.", "bullets": []}

    summary = body.get("summary", {})
    routes  = body.get("routes",  [])[:15]  # top 15 for brevity
    ws_a    = body.get("workset_a", "A")
    ws_b    = body.get("workset_b", "B")

    route_lines = "\n".join(
        f"  {r['origin']}→{r['dest']}: demand Δ{r['demand_delta']:+.0f}  spill Δ{r['spill_delta']:+.0f}  LF {r['lf_a_pct']}%→{r['lf_b_pct']}%  [{r['status']}]"
        for r in routes
    )

    msg = f"""You are a senior airline network analyst comparing two schedule worksets.

WORKSET A: {ws_a}
WORKSET B: {ws_b}

SUMMARY CHANGES:
  Demand delta:         {summary.get('total_demand_delta', 0):+,} pax
  Spill delta:          {summary.get('total_spill_delta', 0):+,} pax
  New routes:           {summary.get('new_routes', 0)}
  Dropped routes:       {summary.get('dropped_routes', 0)}
  Improved routes:      {summary.get('improved_routes', 0)}
  Deteriorated routes:  {summary.get('deteriorated_routes', 0)}

TOP ROUTE CHANGES (by demand delta):
{route_lines}

Write a concise analyst briefing in this JSON (no fences):
{{"headline":"One-line summary of the most significant change","narrative":"3-4 sentences: what changed most, winners and losers, net demand/spill impact","bullets":["Key finding 1","Key finding 2","Key finding 3","Key finding 4 (optional)"],"recommendation":"One specific action the network team should take"}}"""

    try:
        resp = generate_content(
            contents=[{"role": "user", "parts": [{"text": msg}]}],
            system_instruction="You are a senior airline network analyst. Reply only with the requested JSON — no markdown, no preamble.",
            temperature=0.3,
        )
        raw = (extract_text(resp) or "").strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"): raw = raw[4:]
        return _json.loads(raw.strip())
    except Exception as e:
        logger.warning(f"workset ai-summary error: {e}")
        return {
            "headline": f"{ws_b} vs {ws_a}: {summary.get('total_demand_delta', 0):+,} pax demand change",
            "narrative": f"Comparing {ws_a} and {ws_b}: {summary.get('new_routes',0)} new routes, {summary.get('dropped_routes',0)} dropped, net demand delta {summary.get('total_demand_delta',0):+,} pax.",
            "bullets": [],
            "recommendation": "Review the top route deltas manually."
        }


# ─────────────────────────────────────────────────────────────────────────────
# Schedule search
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/schedule/search", tags=["Schedule"])
async def search_schedule(
    origin:      Optional[str] = None,
    destination: Optional[str] = None,
    airline:     Optional[str] = None,
    flight:      Optional[str] = None,
):
    """Search flights by origin, destination, airline, or flight number."""
    df = _svc.search_flights(
        origin=origin,
        destination=destination,
        airline=airline,
        flight_number=flight,
    )
    return {"count": len(df), "flights": _df_to_json(df)[:100]}


@router.get("/schedule/route/{origin}/{destination}", tags=["Schedule"])
async def get_route(origin: str, destination: str):
    """Get all flights and statistics for an O&D pair."""
    import math, json
    def _clean(obj):
        if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
            return None
        if isinstance(obj, dict):
            return {k: _clean(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_clean(v) for v in obj]
        if hasattr(obj, "item"):
            return obj.item()
        return obj

    summary = _ra_svc.get_route_summary(origin, destination)
    gaps    = _ra_svc.find_schedule_gaps(origin, destination)
    spread  = _ra_svc.get_departure_spread(origin, destination)
    market  = _ra_svc.get_market_share(origin, destination)
    return _clean({
        "summary": summary,
        "gaps":    gaps,
        "departure_spread": spread,
        "market_share": market,
    })


@router.get("/schedule/airport/{airport}", tags=["Schedule"])
async def get_airport_schedule(airport: str):
    """Get the full schedule (departures + arrivals) at an airport."""
    df = _svc.get_airport_schedule(airport)
    return {"airport": airport.upper(), "movements": _df_to_json(df)}


@router.get("/schedule/summary", tags=["Schedule"])
async def get_summary():
    """Return overall schedule statistics."""
    return _svc.get_summary_stats()


@router.get("/schedule/routes", tags=["Schedule"])
async def list_routes():
    """List all routes and their flight counts."""
    df = _svc.get_route_summary()
    return {"routes": _df_to_json(df)}


# ─────────────────────────────────────────────────────────────────────────────
# Itinerary
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/itinerary/{origin}/{destination}", tags=["Itinerary"])
async def find_itinerary(
    origin: str,
    destination: str,
    max_stops: int = 1,
):
    """Find direct and connecting itineraries between two airports."""
    return _it_svc.find_itineraries(origin, destination, max_stops=max_stops)


# ─────────────────────────────────────────────────────────────────────────────
# Simulations
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/simulate/add-flight", response_model=AddFlightResponse, tags=["Simulation"])
async def sim_add_flight(request: AddFlightRequest):
    """
    Run full feasibility simulation for adding a new flight.

    Checks:
    - Curfew compliance
    - Aircraft overlap / rotation
    - Route spacing
    - Hub bank alignment
    - Connectivity gain
    - Cannibalization risk

    Returns feasibility score, network value score, violations, and alternatives.
    """
    logger.info(f"POST /simulate/add-flight — {request.origin}→{request.destination} {request.departure_local}")

    dep_dt = _parse_dt(request.departure_local)
    if dep_dt is None:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid departure_local format: '{request.departure_local}'. "
                   "Use ISO format: YYYY-MM-DD HH:MM"
        )

    arr_dt = _parse_dt(request.arrival_local) if request.arrival_local else None

    # Compute arrival from block_time if not given
    if arr_dt is None and request.block_time:
        from datetime import timedelta
        arr_dt = dep_dt + timedelta(minutes=request.block_time)

    proposed = {
        "origin":          request.origin,
        "destination":     request.destination,
        "departure_local": dep_dt,
        "arrival_local":   arr_dt,
        "aircraft_type":   request.aircraft_type or "",
        "airline":         request.airline or "",
        "block_time":      request.block_time,
    }

    try:
        result = simulate_add_flight(proposed, hub=request.hub)
    except Exception as exc:
        logger.exception(f"Simulation error: {exc}")
        raise HTTPException(status_code=500, detail=f"Simulation failed: {exc}")

    # Build structured ScheduleResponse fields
    facts = [
        f"Origin: {request.origin}",
        f"Destination: {request.destination}",
        f"Proposed departure: {dep_dt.strftime('%Y-%m-%d %H:%M')}",
    ]
    if arr_dt:
        facts.append(f"Proposed arrival: {arr_dt.strftime('%Y-%m-%d %H:%M')}")
    if request.aircraft_type:
        facts.append(f"Aircraft type: {request.aircraft_type}")

    checked = list(result.get("evidence", {}).keys())

    return AddFlightResponse(
        verdict             = result["verdict"],
        feasible            = result["feasible"],
        feasibility_score   = result["feasibility_score"],
        network_value_score = result["network_value_score"],
        confidence          = result["confidence"],
        facts               = facts,
        constraints_checked = checked,
        violations          = result.get("violations", []),
        warnings            = result.get("warnings", []),
        risks               = result.get("risks", []),
        alternatives        = result.get("alternatives", []),
        best_window         = result.get("best_window", []),
        why_not             = result.get("why_not"),
        metadata            = result.get("metrics"),
    )


@router.post("/simulate/retime-flight", response_model=RetimeFlightResponse, tags=["Simulation"])
async def sim_retime_flight(request: RetimeFlightRequest):
    """
    Evaluate retiming an existing flight to a new departure time.

    Returns:
    - Delta in feasibility score
    - Connectivity change
    - New violations (if any)
    - Resolved violations
    """
    logger.info(
        f"POST /simulate/retime-flight — {request.flight_number} → {request.new_departure_local}"
    )

    new_dep = _parse_dt(request.new_departure_local)
    if new_dep is None:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid new_departure_local format: '{request.new_departure_local}'."
        )

    # Fetch existing flight
    df = _svc.search_flights(flight_number=request.flight_number)
    if df.empty:
        raise HTTPException(
            status_code=404,
            detail=f"Flight '{request.flight_number}' not found in the schedule database."
        )

    flt_dict = df.iloc[0].to_dict()
    # Keep datetimes as datetime objects for rule engine
    for k in ["departure_local", "arrival_local", "departure_utc", "arrival_utc"]:
        if k in flt_dict and hasattr(flt_dict[k], "isoformat"):
            pass  # already datetime-like from pandas

    try:
        result = simulate_retime_flight(flt_dict, new_dep, hub=request.hub)
    except Exception as exc:
        logger.exception(f"Retime simulation error: {exc}")
        raise HTTPException(status_code=500, detail=f"Simulation failed: {exc}")

    facts = [
        f"Flight: {request.flight_number}",
        f"Current departure: {result['current_timing'].get('departure', 'N/A')}",
        f"Proposed departure: {result['proposed_timing'].get('departure', 'N/A')}",
        f"Shift: {result['delta'].get('departure_shift_minutes', 0):+d} minutes "
        f"({result['delta'].get('departure_shift_direction', 'unchanged')})",
    ]

    return RetimeFlightResponse(
        verdict              = result["verdict"],
        feasible             = result["feasible"],
        feasibility_score    = result["feasibility_score"],
        network_value_score  = result["network_value_score"],
        confidence           = result["confidence"],
        facts                = facts,
        constraints_checked  = list(result.get("evidence", {}).keys()),
        violations           = result.get("violations", []),
        warnings             = result.get("warnings", []),
        risks                = result.get("conflicts", []),
        alternatives         = [],
        current_timing       = result.get("current_timing"),
        proposed_timing      = result.get("proposed_timing"),
        delta                = result.get("delta"),
        connectivity_change  = result.get("connectivity_change"),
    )
