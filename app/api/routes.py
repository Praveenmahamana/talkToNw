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
    ChartSuggestRequest, ChartSuggestResponse,
    AddFlightRequest, AddFlightResponse,
    RetimeFlightRequest, RetimeFlightResponse,
    HealthResponse, RouteSearchRequest,
)
from app.services.schedule_service import ScheduleService
from app.services.route_analysis_service import RouteAnalysisService
from app.services.itinerary_service import ItineraryService
from app.services.viz_service import extract_visualizations, suggest_chart_spec
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
    from app.ai.vertex_client import _model_name
    try:
        count = _svc.flight_count()
    except Exception:
        count = 0
    return HealthResponse(
        status="ok",
        db_flight_count=count,
        vertex_ai=is_available(),
        model=_model_name,
    )


@router.get("/schedule/summary", tags=["System"])
async def schedule_summary():
    """Return aggregate statistics about the loaded schedule (used by dashboard)."""
    from app.database.queries import get_summary_stats
    return get_summary_stats()


@router.get("/kg-viz", tags=["System"])
async def kg_viz_data(top_airports: int = 80):
    """
    Knowledge-graph data for the boot loader and Brain tab visualisation.
    Returns top airports by movements, ALL routes between those airports,
    and top airlines — derived from the live schedule via DuckDB.
    """
    from app.database.db import fetchdf
    import math

    def _clean(val):
        if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
            return 0
        if hasattr(val, "item"):
            return val.item()
        return val

    try:
        # ── Top N airports by total movements ───────────────────────────────
        ap_df = fetchdf(f"""
            SELECT airport, COUNT(*) AS movements
            FROM (
                SELECT origin      AS airport FROM flights
                UNION ALL
                SELECT destination AS airport FROM flights
            ) t
            GROUP BY airport
            ORDER BY movements DESC
            LIMIT {int(top_airports)}
        """)
        airports = [{"iata": r["airport"], "movements": _clean(r["movements"])}
                    for _, r in ap_df.iterrows() if r["airport"]]

        # Build in-list for DuckDB (safe: IATA codes are alpha only)
        iata_set = {a["iata"] for a in airports}
        in_clause = ", ".join(f"'{c}'" for c in iata_set)

        # ── ALL routes where BOTH endpoints are in the airport set ──────────
        # This is the real inter-airport graph — can be thousands of edges
        rt_df = fetchdf(f"""
            SELECT origin, destination,
                   COUNT(*)                AS flights,
                   COUNT(DISTINCT airline) AS airlines
            FROM flights
            WHERE origin      IN ({in_clause})
              AND destination IN ({in_clause})
            GROUP BY origin, destination
            ORDER BY flights DESC
        """)
        routes = [{"o": r["origin"], "d": r["destination"],
                   "flights": _clean(r["flights"]), "airlines": _clean(r["airlines"])}
                  for _, r in rt_df.iterrows()]

        # ── Top 30 airlines by flight count ─────────────────────────────────
        al_df = fetchdf("""
            SELECT airline, COUNT(*) AS flights
            FROM flights
            GROUP BY airline
            ORDER BY flights DESC
            LIMIT 30
        """)
        airlines = [{"iata": r["airline"], "flights": _clean(r["flights"])}
                    for _, r in al_df.iterrows() if r["airline"]]

        # ── Workset model data: BASEDATA + SPILLDATA (graceful degradation) ──
        base_airports, base_routes, spill_markets, spill_airlines = [], [], [], []

        try:
            ba_df = fetchdf(f"""
                SELECT origin AS airport,
                       CAST(SUM(apm_dmd)   AS INTEGER) AS weekly_demand,
                       CAST(SUM(apm_pax)   AS INTEGER) AS weekly_pax,
                       CAST(SUM(apm_spill) AS INTEGER) AS weekly_spill,
                       ROUND(AVG(CASE WHEN apm_cap > 0
                                      THEN CAST(apm_pax AS FLOAT) / apm_cap
                                      ELSE NULL END), 3) AS avg_lf
                FROM workset_base
                WHERE mkt_ind <= 1
                  AND origin IN ({in_clause})
                GROUP BY origin
                ORDER BY weekly_demand DESC
            """)
            base_airports = [{"iata": r["airport"],
                              "demand": _clean(r["weekly_demand"]),
                              "pax":    _clean(r["weekly_pax"]),
                              "spill":  _clean(r["weekly_spill"]),
                              "lf":     _clean(r["avg_lf"])}
                             for _, r in ba_df.iterrows() if r.get("airport")]
            logger.info(f"kg-viz: {len(base_airports)} airports with BASEDATA demand")
        except Exception as exc:
            logger.info(f"kg-viz workset_base airport query skipped: {exc}")

        try:
            br_df = fetchdf(f"""
                SELECT origin, dest,
                       CAST(SUM(apm_dmd)   AS INTEGER) AS weekly_demand,
                       CAST(SUM(apm_pax)   AS INTEGER) AS weekly_pax,
                       CAST(SUM(apm_spill) AS INTEGER) AS weekly_spill,
                       ROUND(AVG(CASE WHEN apm_cap > 0
                                      THEN CAST(apm_pax AS FLOAT) / apm_cap
                                      ELSE NULL END), 3) AS avg_lf,
                       COUNT(*) AS departures
                FROM workset_base
                WHERE mkt_ind <= 1
                  AND origin IN ({in_clause})
                  AND dest   IN ({in_clause})
                GROUP BY origin, dest
                ORDER BY weekly_demand DESC
            """)
            base_routes = [{"o": r["origin"], "d": r["dest"],
                            "demand": _clean(r["weekly_demand"]),
                            "pax":    _clean(r["weekly_pax"]),
                            "spill":  _clean(r["weekly_spill"]),
                            "lf":     _clean(r["avg_lf"]),
                            "deps":   _clean(r["departures"])}
                           for _, r in br_df.iterrows()]
            logger.info(f"kg-viz: {len(base_routes)} O-D routes with BASEDATA demand")
        except Exception as exc:
            logger.info(f"kg-viz workset_base route query skipped: {exc}")

        try:
            sm_df = fetchdf(f"""
                SELECT market_origin AS o, market_dest AS d,
                       CAST(SUM(total_demand) AS INTEGER) AS total_demand,
                       CAST(SUM(total_pax)    AS INTEGER) AS total_pax,
                       CAST(SUM(total_spill)  AS INTEGER) AS total_spill,
                       ROUND(AVG(mkt_share), 4) AS avg_share
                FROM workset_spill
                WHERE market_origin IN ({in_clause})
                  AND market_dest   IN ({in_clause})
                GROUP BY market_origin, market_dest
                ORDER BY total_demand DESC
                LIMIT 200
            """)
            spill_markets = [{"o": r["o"], "d": r["d"],
                              "demand": _clean(r["total_demand"]),
                              "pax":    _clean(r["total_pax"]),
                              "spill":  _clean(r["total_spill"]),
                              "share":  _clean(r["avg_share"])}
                             for _, r in sm_df.iterrows()]
            logger.info(f"kg-viz: {len(spill_markets)} O-D pairs from SPILLDATA")
        except Exception as exc:
            logger.info(f"kg-viz workset_spill market query skipped: {exc}")

        try:
            sa_df = fetchdf("""
                SELECT airline,
                       COUNT(DISTINCT market_origin || '-' || market_dest) AS markets,
                       CAST(SUM(total_demand) AS INTEGER) AS total_demand,
                       CAST(SUM(total_pax)    AS INTEGER) AS total_pax,
                       ROUND(AVG(mkt_share), 4) AS avg_share
                FROM workset_spill
                WHERE airline IS NOT NULL AND LENGTH(TRIM(airline)) > 0
                GROUP BY airline
                ORDER BY total_demand DESC
                LIMIT 30
            """)
            spill_airlines = [{"iata": r["airline"],
                               "markets": _clean(r["markets"]),
                               "demand":  _clean(r["total_demand"]),
                               "pax":     _clean(r["total_pax"]),
                               "share":   _clean(r["avg_share"])}
                              for _, r in sa_df.iterrows() if r.get("airline")]
            logger.info(f"kg-viz: {len(spill_airlines)} airlines from SPILLDATA")
        except Exception as exc:
            logger.info(f"kg-viz workset_spill airline query skipped: {exc}")

        # ── KG-level stats for NER persona derivation ───────────────────────
        stats = {
            "total_airports":  len(airports),
            "total_routes":    len(routes),
            "total_airlines":  len(airlines),
            "base_routes":     len(base_routes),
            "spill_markets":   len(spill_markets),
            "has_demand_data": len(base_routes) > 0,
        }

        # ── Workset flat files: alliance.dat + mktSize.dat ───────────────────
        import csv, os
        from pathlib import Path
        alliances, markets = [], []
        opp_data: list = []
        airport_regions: dict = {}
        cnct_markets: list = []
        _wd = None
        try:
            _data_folder = os.getenv("SCHEDAI_DATA_FOLDER", "")
            _p = Path(_data_folder)
            _wd = _p.parent if _p.name == "out" else _p
        except Exception:
            pass

        if _wd:
            try:
                aln_file = _wd / "data" / "alliance.dat"
                if aln_file.exists():
                    aln_dict: dict = {}
                    with open(aln_file, newline="", encoding="utf-8-sig") as f:
                        for row in csv.DictReader(f):
                            name = (row.get("ALLNCENM") or "").strip()
                            code = (row.get("ALNCD") or "").strip()
                            if name and code:
                                aln_dict.setdefault(name, []).append(code)
                    alliances = sorted(
                        [{"name": k, "members": v} for k, v in aln_dict.items() if len(v) >= 3],
                        key=lambda x: -len(x["members"])
                    )[:12]
                    logger.info(f"kg-viz: loaded {len(alliances)} alliance groups from alliance.dat")
            except Exception as exc:
                logger.warning(f"alliance.dat parse failed: {exc}")

            try:
                mkt_file = _wd / "data" / "mktSize.dat"
                if mkt_file.exists():
                    mkt_rows: list = []
                    with open(mkt_file, newline="", encoding="utf-8-sig") as f:
                        for row in csv.DictReader(f):
                            o = (row.get("ORG") or "").strip()
                            d = (row.get("DEST") or "").strip()
                            try:
                                dem = int(float(row.get("WKLYDMD", 0) or 0))
                            except Exception:
                                dem = 0
                            if o in iata_set and d in iata_set and dem > 0:
                                mkt_rows.append({"o": o, "d": d, "wkly_demand": dem})
                    mkt_rows.sort(key=lambda x: -x["wkly_demand"])
                    markets = mkt_rows[:150]
                    logger.info(f"kg-viz: loaded {len(markets)} markets from mktSize.dat")
            except Exception as exc:
                logger.warning(f"mktSize.dat parse failed: {exc}")

            # ── opp.dat: airline market share at airports ────────────────────
            opp_data: list = []
            try:
                opp_file = _wd / "data" / "opp.dat"
                if opp_file.exists():
                    all_al_set = {a["iata"] for a in airlines}
                    with open(opp_file, encoding="utf-8-sig") as f:
                        for line in f:
                            parts = line.strip().split()
                            if len(parts) >= 3:
                                arp, aln = parts[0].strip(), parts[1].strip()
                                try:
                                    share = float(parts[2])
                                except Exception:
                                    continue
                                if arp in iata_set and aln in all_al_set and share > 0.05:
                                    opp_data.append({"arp": arp, "aln": aln, "share": round(share, 4)})
                    opp_data.sort(key=lambda x: -x["share"])
                    logger.info(f"kg-viz: loaded {len(opp_data)} airline-airport presence records from opp.dat")
            except Exception as exc:
                logger.warning(f"opp.dat parse failed: {exc}")

            # ── regnList.dat: airport→region mapping ────────────────────────
            airport_regions: dict = {}
            try:
                regn_file = _wd / "data" / "regnList.dat"
                if regn_file.exists():
                    with open(regn_file, newline="", encoding="utf-8-sig") as f:
                        for row in csv.DictReader(f):
                            arp = (row.get("ARPCD") or "").strip()
                            rgn = (row.get("REGNCD") or "").strip()
                            if arp in iata_set and rgn:
                                airport_regions[arp] = rgn
                    logger.info(f"kg-viz: loaded {len(airport_regions)} airport-region mappings from regnList.dat")
            except Exception as exc:
                logger.warning(f"regnList.dat parse failed: {exc}")

            # ── cnctDataIn.dat: connection itinerary O-D pairs ───────────────
            cnct_markets: list = []
            try:
                cnct_file = _wd / "data" / "cnctDataIn.dat"
                if cnct_file.exists():
                    seen_cnct: set = set()
                    with open(cnct_file, encoding="utf-8-sig") as f:
                        for line in f:
                            parts = line.strip().split(",")
                            if len(parts) >= 2:
                                o, d = parts[0].strip(), parts[1].strip()
                                key = f"{o}:{d}"
                                if o in iata_set and d in iata_set and key not in seen_cnct:
                                    seen_cnct.add(key)
                                    cnct_markets.append({"o": o, "d": d})
                    logger.info(f"kg-viz: loaded {len(cnct_markets)} connection markets from cnctDataIn.dat")
            except Exception as exc:
                logger.warning(f"cnctDataIn.dat parse failed: {exc}")

        return {
            "airports":        airports,
            "routes":          routes,
            "airlines":        airlines,
            "alliances":       alliances,
            "markets":         markets,
            "base_airports":   base_airports,
            "base_routes":     base_routes,
            "spill_markets":   spill_markets,
            "spill_airlines":  spill_airlines,
            "opp_data":        opp_data,
            "airport_regions": airport_regions,
            "cnct_markets":    cnct_markets,
            "stats":           stats,
        }

    except Exception as exc:
        logger.warning(f"kg-viz query failed: {exc}")
        return {"airports": [], "routes": [], "airlines": [], "stats": {}}


# ─────────────────────────────────────────────────────────────────────────────
# Model management
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/model", tags=["System"])
async def get_model_info():
    """Return the currently active model and the full list of available models."""
    from app.ai.vertex_client import _model_name, AVAILABLE_MODELS
    return {
        "active": _model_name,
        "models": [{"id": mid, "label": lbl, "provider": prov} for mid, lbl, prov in AVAILABLE_MODELS],
        "claude_setup_url": "https://console.cloud.google.com/vertex-ai/publishers/anthropic/model-garden/claude-sonnet-4-5",
    }


@router.post("/model", tags=["System"])
async def switch_model(body: dict):
    """
    Hot-swap the active AI model at runtime — no restart required.
    Body: {"model": "claude-sonnet-4-5"}
    """
    from app.ai.vertex_client import set_model, AVAILABLE_MODELS
    model_id = (body.get("model") or "").strip()
    valid_ids = [m[0] for m in AVAILABLE_MODELS]
    if not model_id:
        raise HTTPException(status_code=400, detail="Missing 'model' field")
    if model_id not in valid_ids:
        raise HTTPException(status_code=400, detail=f"Unknown model '{model_id}'. Valid: {valid_ids}")
    set_model(model_id)
    return {"active": model_id, "status": "switched"}


# ─────────────────────────────────────────────────────────────────────────────
# Ingestion
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/ingest", response_model=IngestResponse, tags=["Ingestion"])
async def ingest_schedules(request: IngestRequest):
    """
    Load and normalise schedule files from a local folder path.
    Supports CSV, TSV, TXT, and SSIM formats.
    """
    logger.info(f"POST /ingest — folder: {request.folder_path}, clear={request.clear}")
    try:
        if request.clear:
            from app.database.queries import clear_flights
            cleared = clear_flights()
            logger.info(f"Pre-ingest clear: removed {cleared} existing flight records.")
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
        result = _agent.query(request.query, session_id=request.session_id, persona=request.persona, panel_context=request.panel_context)
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

    answer_snippet   = request.answer[:500].strip()
    entities_str     = ", ".join(request.entities[:10]) if request.entities else "none highlighted"
    workset_ctx_line = f"Workset context: {request.workset_context}\n" if request.workset_context else ""

    user_msg = (
        f"User asked: {request.query}\n\n"
        f"AI answered (excerpt): {answer_snippet}\n\n"
        f"Active persona: {persona_name} — focuses on {persona_desc}\n"
        f"{workset_ctx_line}"
        f"Entities in graph context: {entities_str}\n\n"
        "Generate exactly 4 follow-up research questions that:\n"
        "1. Continue naturally from the user's direction (don't repeat the same angle)\n"
        "2. Go progressively deeper or branch into related strategic angles\n"
        "3. Match the persona's focus area\n"
        "4. Are specific to the workset airlines and airports — use actual codes from the context\n"
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
# AI Chart Suggestion
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/chart-suggest", response_model=ChartSuggestResponse, tags=["Query"])
async def chart_suggest(request: ChartSuggestRequest):
    """
    Ask Gemini to choose the best chart type (bubble, network, scatter, bar, pie)
    for the data returned by a query, and return a structured spec the frontend
    can render directly — no extra SQL round-trip needed.
    """
    if not is_available():
        return ChartSuggestResponse(error="Vertex AI not available")
    if not request.columns or not request.rows:
        return ChartSuggestResponse(error="No data provided")
    try:
        spec = suggest_chart_spec(
            query=request.query,
            answer=request.answer,
            columns=request.columns,
            rows=request.rows,
        )
        if spec is None:
            return ChartSuggestResponse(error="Chart suggestion failed")
        return ChartSuggestResponse(spec=spec)
    except Exception as exc:
        logger.warning(f"chart-suggest error: {exc}")
        return ChartSuggestResponse(error=str(exc))


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
    Return top-10 ranked flight-addition opportunities for an O&D pair.

    Uses PM (Passenger Model) workset data — BASEDATA + SPILLDATA — for calibrated
    demand, spill, recapture and load-factor scoring per time slot.
    Logit distance-band parameters (from Default_Logit_Profiles.csv) drive
    wide-body bonus and diversion rate estimates per the Sabre PM methodology.
    """
    from app.database.db import get_connection
    from datetime import date, timedelta
    import math

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

    # Distance band → logit diversion rate (from Default_Logit_Profiles.csv β_conn values)
    # β_conn (SkedSens): USH=-1.5 SH=-2.0 MH=-2.25 LH=-2.5 ULH=-3.0
    # Diversion rate ≈ 1/(1+exp(-β_conn × 0.1)) rescaled to 8-25% range
    if avg_block < 90:   diversion_rate = 0.25; dist_band = "USH"
    elif avg_block < 180: diversion_rate = 0.20; dist_band = "SH"
    elif avg_block < 300: diversion_rate = 0.16; dist_band = "MH"
    elif avg_block < 480: diversion_rate = 0.12; dist_band = "LH"
    else:                 diversion_rate = 0.08; dist_band = "ULH"

    # Wide-body bonus from logit model: wide-body utility +1.3–1.7 → ~13% demand uplift
    _WIDEBODY = {"77W", "789", "77L", "388", "359", "351", "346", "380", "77X", "76F"}
    widebody_bonus = 1.13 if default_ac in _WIDEBODY else 1.0

    # ── 2. Workset base demand (PM BASEDATA — unconstrained demand per flight) ─
    daily_demand = float(default_cap) * 0.78   # fallback
    daily_spill  = daily_demand * 0.12
    weekly_spill_total = None
    try:
        row = conn.execute("""
            SELECT SUM(apm_dmd), SUM(apm_spill), SUM(apm_pax), AVG(apm_cap)
            FROM workset_base
            WHERE origin = ? AND dest = ?
        """, [origin, dest]).fetchone()
        if row and row[0]:
            weekly_demand  = float(row[0])           # apm_dmd total (unconstrained)
            weekly_spill   = float(row[1] or 0)      # apm_dmd - apm_pax (spilled demand)
            weekly_booked  = float(row[2] or 0)      # apm_pax (actual traffic)
            daily_demand   = weekly_demand / 7
            daily_spill    = weekly_spill  / 7
            weekly_spill_total = int(weekly_spill)
            if row[3]:
                default_cap = int(row[3])
    except Exception:
        pass

    # ── 2b. Workset base: per-dep-time LF + market share from BASEDATA ────────
    # workset_base has clear column mapping: apm_pax (col12) / apm_cap (col10) = real LF
    # Much more reliable than workset_spill where lf_pax encoding varies by route type
    base_by_hour: dict = {}        # dep_hour -> {lf, spill, demand, cap}
    pm_airline_traffic: dict = {}  # airline -> total weekly booked pax (for market share)
    avg_total_lf   = 0.78          # fallback
    avg_recap_rate = 0.20          # fallback (20% of spilled pax recaptured elsewhere)
    has_pm_spill   = False
    try:
        base_rows = conn.execute("""
            SELECT
                TRY_CAST(
                    CASE WHEN dep_time LIKE '__:__' THEN SPLIT_PART(dep_time,':',1)
                         WHEN LENGTH(TRIM(dep_time)) >= 4 THEN SUBSTRING(TRIM(dep_time),1,2)
                         ELSE NULL END
                AS INTEGER)                              AS dep_hour,
                op_airline,
                SUM(apm_pax)                             AS tot_booked,
                SUM(apm_cap)                             AS tot_cap,
                SUM(apm_spill)                           AS tot_spill,
                SUM(apm_dmd)                             AS tot_demand
            FROM workset_base
            WHERE origin = ? AND dest = ?
              AND dep_time IS NOT NULL
            GROUP BY dep_hour, op_airline
            ORDER BY dep_hour
        """, [origin, dest]).fetchall()

        if base_rows:
            has_pm_spill = True
            total_booked_all_slots = 0.0
            total_cap_all_slots    = 0.0

            # Aggregate by hour across airlines (route-level slot picture)
            for r in base_rows:
                h  = r[0]
                al = str(r[1] or "").strip()
                if h is None:
                    continue

                tot_b = float(r[2] or 0)
                tot_c = float(r[3] or 0)           # 0 not 1: avoid false LF when cap=0
                slot_lf = min(1.0, tot_b / tot_c) if tot_c > 0 else 0.0  # cap at 100%

                if h not in base_by_hour:
                    base_by_hour[h] = {"lf": 0.0, "spill": 0.0, "demand": 0.0, "cap": 0.0}
                slot = base_by_hour[h]
                slot["lf"]     = max(slot["lf"], slot_lf)  # peak LF in this hour
                slot["spill"] += float(r[4] or 0)
                slot["demand"]+= float(r[5] or 0)
                slot["cap"]   += tot_c

                total_booked_all_slots += tot_b
                total_cap_all_slots    += tot_c

                # Accumulate for airline market share (booked-pax weighted)
                if al:
                    pm_airline_traffic[al] = pm_airline_traffic.get(al, 0.0) + tot_b

            # Booked-pax market share per airline (from BASEDATA, well-calibrated)
            total_booked_all = sum(pm_airline_traffic.values()) or 1
            airline_share_pm = {
                al: round(booked / total_booked_all * 100)
                for al, booked in pm_airline_traffic.items() if booked > 0
            }

            # Overall market LF from totals (not average of per-slot ratios — avoids NaN/Inf)
            avg_total_lf = min(1.0, total_booked_all_slots / total_cap_all_slots) \
                           if total_cap_all_slots > 0 else 0.78

            # Recapture rate: adjust for market LF — higher LF → fewer empty seats → lower recap
            avg_recap_rate = min(0.35, max(0.12, 0.30 - avg_total_lf * 0.15))
        else:
            airline_share_pm = {}

    except Exception as e:
        logger.warning(f"workset_base slot query failed for {origin}-{dest}: {e}")
        airline_share_pm = {}

    # ── 3. Competitor airline map (freq-based fallback, replaced by PM if available) ─
    competitor_map: dict = {}
    for r in ex_rows:
        al = r[2]
        if not al: continue
        if al not in competitor_map:
            competitor_map[al] = {"flights": 0, "hours": []}
        competitor_map[al]["flights"] += int(r[4] or 1)
        if r[0] is not None:
            competitor_map[al]["hours"].append(int(r[0]))

    total_flights = sum(v["flights"] for v in competitor_map.values()) or 1
    airline_share_freq = {
        al: round(v["flights"] / total_flights * 100)
        for al, v in competitor_map.items()
    }
    # Prefer PM model shares if available (from SPILLDATA, calibrated against actuals)
    airline_share = airline_share_pm if airline_share_pm else airline_share_freq

    # ── 4. Score every hour using PM LF data (with heuristic fallback) ─────────
    # PM scoring: slots with high LF = constrained market = better opportunity
    # market-saturation bonus: high total_lf means more passengers seeking alternatives
    candidates = []
    weekly_spill_val = weekly_spill_total or int(daily_spill * 7)

    for hour in range(24):
        base = _SLOT_BASE_SCORES.get(hour, 3)
        proximity_penalty = sum(max(0, 36 - abs(hour - eh) * 14) for eh in existing_hours)

        if has_pm_spill and base_by_hour:
            if hour in base_by_hour:
                # Existing flights in this hour → score by how loaded they are
                s = base_by_hour[hour]
                slot_lf_score  = int(s["lf"] * 45)       # 0..45: high LF = congested
                sat_bonus      = int(avg_total_lf * 10)   # 0..10: whole market tight
                spill_frac     = s["spill"] / max(1, daily_spill * 7) * 20
                pm_score = min(55, slot_lf_score + sat_bonus + int(spill_frac))
            else:
                # Gap in coverage: demand exists but no flights in this hour
                pm_score = int(avg_total_lf * 30 + base * 0.4)
            # Blend PM and heuristic (70/30 mix)
            score = max(0, int(pm_score * 0.70 + base * 0.30) - proximity_penalty)
        else:
            # No PM data: pure heuristic
            score = max(0, base - proximity_penalty)

        candidates.append((score, hour, base))

    candidates.sort(reverse=True)
    top10 = candidates[:10]

    # ── 5. Build opportunity cards ────────────────────────────────────────────
    today = date.today()
    days_to_sun = (6 - today.weekday()) % 7 or 7
    dep_date = today + timedelta(days=days_to_sun)
    minutes_cycle = [30, 0, 45, 15, 50, 10, 40, 20, 55, 5]

    results = []
    for rank, (score, hour, base_score) in enumerate(top10, 1):
        minute   = minutes_cycle[rank - 1]
        dep_time = f"{hour:02d}:{minute:02d}"
        gap_h    = min((abs(hour - eh) for eh in existing_hours), default=12)

        # ── Pax estimate using PM BASEDATA (or heuristic fallback) ───────────
        if has_pm_spill and base_by_hour:
            if hour in base_by_hour:
                s = base_by_hour[hour]
                # Spill from existing slots at this hour that we'd recapture
                slot_spill_recap = s["spill"] * avg_recap_rate
                # Additional demand attraction from schedule timing preference
                sched_factor = 0.18 if base_score >= 35 else (0.12 if base_score >= 20 else 0.07)
                demand_attr  = daily_demand * sched_factor
                pax_est = max(40, int(min(
                    (slot_spill_recap + demand_attr) * widebody_bonus,
                    default_cap * 0.88
                )))
            else:
                # Gap in coverage: capture market overflow from high-LF adjacent slots
                sched_factor = 0.20 if base_score >= 35 else (0.14 if base_score >= 20 else 0.08)
                pax_est = max(40, int(min(
                    (daily_demand * sched_factor + daily_spill * avg_recap_rate) * widebody_bonus,
                    default_cap * 0.88
                )))
        else:
            # Heuristic fallback
            slot_share = 0.22 if base_score >= 35 else (0.16 if base_score >= 20 else 0.10)
            pax_est = max(40, int(min(
                (daily_spill * 0.85 + daily_demand * slot_share) * widebody_bonus,
                default_cap * 0.88
            )))

        rev_est = pax_est * yield_usd

        # ── Spill recapture split ─────────────────────────────────────────────
        # From PM BASEDATA: per-hour slot spill gives real recapture opportunity
        if has_pm_spill and hour in base_by_hour:
            slot_recap_pax = int(base_by_hour[hour]["spill"] * avg_recap_rate)
        else:
            slot_recap_pax = max(0, int(weekly_spill_val * (avg_recap_rate / 7)))

        spill_pax     = min(pax_est, max(slot_recap_pax, int(pax_est * 0.45)))
        diversion_pax = max(0, pax_est - spill_pax)

        # ── Competitor share at risk (PM-calibrated diversion rate by dist band) ─
        slot_competitors = []
        own_al = (airline.upper().strip() or "EK")
        for al, info in competitor_map.items():
            if al == own_al:
                continue
            nearby = [h for h in info["hours"] if abs(h - hour) <= 2]
            if nearby:
                shr = airline_share.get(al, 0)
                # Timing proximity → more diversion for closer flights
                timing_factor = 1.3 if any(abs(h - hour) <= 1 for h in nearby) else 1.0
                at_risk = max(1, int(pax_est * (shr / 100) * diversion_rate * timing_factor))
                slot_competitors.append({
                    "airline":      al,
                    "market_share": shr,
                    "pax_at_risk":  at_risk,
                })
        slot_competitors.sort(key=lambda x: x["pax_at_risk"], reverse=True)

        # ── Reasoning (transparent PM methodology) ────────────────────────────
        pm_note = ""
        if has_pm_spill and hour in base_by_hour:
            s  = base_by_hour[hour]
            pm_note = (f"PM LF {s['lf']:.0%} · market avg LF {avg_total_lf:.0%} · "
                       f"recap rate {avg_recap_rate:.0%}")
        elif has_pm_spill:
            pm_note = f"Schedule gap · market avg LF {avg_total_lf:.0%} · recap {avg_recap_rate:.0%}"
        else:
            pm_note = "heuristic scoring (no PM BASEDATA)"

        wb_note = f" · wide-body +{(widebody_bonus-1)*100:.0f}%" if widebody_bonus > 1 else ""
        reasoning = (
            f"{_slot_label(hour)} · {gap_h:.0f}h schedule gap · "
            f"{spill_pax} spill recapture + {diversion_pax} schedule diversion · "
            f"{dist_band} {pm_note}{wb_note}"
        )

        results.append({
            "rank":              rank,
            "origin":            origin,
            "destination":       dest,
            "departure_time":    dep_time,
            "departure_local":   f"{dep_date.isoformat()} {dep_time}",
            "aircraft_type":     default_ac,
            "airline":           own_al,
            "est_pax":           pax_est,
            "est_revenue":       int(rev_est),
            "opportunity_score": round(score),
            "slot_label":        _slot_label(hour),
            "gap_hours":         round(gap_h, 1),
            "spill_recapture":   spill_pax,
            "diversion_pax":     diversion_pax,
            "weekly_spill_total": weekly_spill_val,
            "competitors_at_risk": slot_competitors[:3],
            "reasoning":         reasoning,
        })

    return {
        "opportunities": results,
        "context": {
            "origin":                  origin,
            "destination":             dest,
            "existing_weekly_flights": len(ex_rows),
            "daily_demand_est":        int(daily_demand),
            "weekly_spill_total":      weekly_spill_val,
            "avg_block_min":           int(avg_block),
            "yield_usd":               yield_usd,
            "dist_band":               dist_band,
            "widebody_bonus_pct":      round((widebody_bonus - 1) * 100),
            "avg_recap_rate_pct":      round(avg_recap_rate * 100),
            "market_lf_pct":           round(avg_total_lf * 100),
            "pm_data_available":       has_pm_spill,
            "airline_market_share":    airline_share,
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
# Dashboard intelligence endpoints (insightsDB-style tables)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/workset/dashboard/profile", tags=["Workset"])
async def dashboard_profile():
    """Return workset profile (host airline, workset name, etc.) plus live workset hints."""
    from app.services.workset_service import get_dashboard_profile, get_workset_hints, dashboard_is_ready, init_dashboard
    init_dashboard()
    profile = get_dashboard_profile()
    hints   = get_workset_hints()
    return {"ready": dashboard_is_ready(), "profile": {**profile, **hints}}


@router.get("/workset/dashboard/network", tags=["Workset"])
async def dashboard_network(top_n: int = 200):
    """Level 1 OD network summary — one row per host OD pair."""
    from app.services.workset_service import get_network_summary, init_dashboard
    init_dashboard()
    rows = get_network_summary(top_n=top_n)
    return {"rows": rows, "count": len(rows)}


@router.get("/workset/dashboard/flight-report", tags=["Workset"])
async def dashboard_flight_report(orig: str = "", dest: str = "", carrier: str = "", top_n: int = 500):
    """Flight View — sampled rows + full-dataset KPIs from dm_flight_report."""
    from app.services.workset_service import get_flight_report, get_flight_count, get_flight_kpis, init_dashboard
    init_dashboard()
    rows  = get_flight_report(orig=orig, dest=dest, carrier=carrier, top_n=top_n)
    total_count = get_flight_count(orig=orig, dest=dest, carrier=carrier)
    kpis  = get_flight_kpis(orig=orig, dest=dest, carrier=carrier)
    return {"rows": rows, "count": len(rows), "total_count": total_count, "kpis": kpis}


@router.get("/workset/dashboard/market-summary", tags=["Workset"])
async def dashboard_market_summary(orig: str = "", dest: str = ""):
    """O&D market summary by airline — demand/traffic/revenue shares."""
    from app.services.workset_service import get_market_summary, init_dashboard
    init_dashboard()
    rows = get_market_summary(orig=orig, dest=dest)
    return {"rows": rows, "count": len(rows)}


@router.get("/workset/dashboard/itin-report", tags=["Workset"])
async def dashboard_itin_report(orig: str = "", dest: str = "", carrier: str = "", top_n: int = 500):
    """Itinerary Report — all itineraries optionally filtered by OD and/or carrier."""
    from app.services.workset_service import get_itin_report, init_dashboard
    init_dashboard()
    rows = get_itin_report(orig=orig, dest=dest, carrier=carrier, top_n=top_n)
    return {"rows": rows, "count": len(rows)}


@router.get("/workset/dashboard/market-carrier", tags=["Workset"])
async def dashboard_market_carrier(orig: str, dest: str):
    """Full Market Summary by Airline for an OD pair (for OD detail panel)."""
    from app.services.workset_service import get_market_carrier_detail, init_dashboard
    init_dashboard()
    rows = get_market_carrier_detail(orig=orig, dest=dest)
    return {"rows": rows, "count": len(rows)}


@router.get("/workset/dashboard/flight-flow-od", tags=["Workset"])
async def dashboard_flight_flow_od(flt: str):
    """Flow OD pax distribution for a specific flight (all segments matched)."""
    from app.services.workset_service import get_flight_flow_od, init_dashboard
    init_dashboard()
    rows = get_flight_flow_od(flt=flt)
    total_traffic = sum(float(r.get("total_traffic") or 0) for r in rows)
    for r in rows:
        t = float(r.get("total_traffic") or 0)
        r["traffic_share_pct"] = round(t / total_traffic * 100, 1) if total_traffic else 0
    return {"rows": rows, "count": len(rows), "total_traffic": round(total_traffic, 1)}


@router.get("/workset/dashboard/route-market-report", tags=["Workset"])
async def dashboard_route_market_report(orig: str, dest: str, top_n: int = 200):
    """Market Report for a route — all O&D markets whose pax flow through orig→dest.
    Matches PMCal flow_traf-report-market.csv column structure."""
    from app.services.workset_service import get_route_market_report, init_dashboard
    init_dashboard()
    rows = get_route_market_report(orig=orig, dest=dest, top_n=top_n)
    return {"rows": rows, "count": len(rows)}


@router.get("/workset/dashboard/route-flow-itins", tags=["Workset"])
async def dashboard_route_flow_itins(orig: str, dest: str, top_n: int = 500):
    """Flow Itinerary Report for a route — all itineraries whose pax flow through orig→dest.
    Matches PMCal flow_traf-report-itin.csv column structure."""
    from app.services.workset_service import get_route_flow_itins, init_dashboard
    init_dashboard()
    rows = get_route_flow_itins(orig=orig, dest=dest, top_n=top_n)
    return {"rows": rows, "count": len(rows)}


@router.post("/workset/dashboard/rebuild", tags=["Workset"])
async def rebuild_dashboard_tables():
    """Drop and regenerate dm_flight_report and dm_network_summary from workset_base.
    Call this after workset data changes or schema fixes."""
    from app.services.workset_service import rebuild_dm_tables
    result = rebuild_dm_tables()
    return result


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
