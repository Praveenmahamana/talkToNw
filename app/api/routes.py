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
    logger.info(f"POST /query — '{request.query[:80]}'")
    try:
        result = _agent.query(request.query, session_id=request.session_id)
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
