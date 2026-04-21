"""
Tool registry — maps Gemini tool names to Python backend functions.

All tools return serialisable dicts.  The AI layer MUST call these tools
for any factual or computational claim.
"""

import json
from datetime import datetime
from typing import Any, Dict, Optional
from loguru import logger

from app.services.schedule_service import ScheduleService
from app.services.route_analysis_service import RouteAnalysisService
from app.simulation.add_flight import simulate_add_flight as _sim_add
from app.simulation.retime_flight import simulate_retime_flight as _sim_retime
from app.rules.rule_engine import (
    check_turnaround_standalone,
    check_airport_constraints_standalone,
)

_svc    = ScheduleService()
_ra_svc = RouteAnalysisService()


# ─────────────────────────────────────────────────────────────────────────────
# Tool definitions (OpenAPI-style for Vertex AI FunctionDeclaration)
# ─────────────────────────────────────────────────────────────────────────────

TOOL_DEFINITIONS = [
    {
        "name": "get_graph_insights",
        "description": (
            "Query the airline route knowledge graph for structural intelligence. "
            "Returns pre-computed graph metrics that give the LM deep structural context:\n"
            "  • type='airport' → hub tier (Mega/Major/Secondary/Regional), destinations served, "
            "top airlines by frequency, top routes — call this FIRST for any airport question.\n"
            "  • type='route'   → direct airline summary, 1-stop hub options, endpoint hub profiles, "
            "market leader — call this FIRST for any O&D route question.\n"
            "  • type='airline' → network footprint, primary hubs, aircraft fleet, top routes "
            "— call for airline network questions.\n"
            "  • type='network' → global hub ranking, total airports/routes in the schedule "
            "— call for global/overview questions.\n"
            "ALWAYS call this tool before get_route_analysis or get_competitor_analysis "
            "to obtain the graph-layer structural context."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "description": "Query type: 'airport', 'route', 'airline', or 'network'",
                },
                "airport": {
                    "type": "string",
                    "description": "IATA airport code for type='airport', e.g. DXB",
                },
                "origin": {
                    "type": "string",
                    "description": "IATA origin airport code for type='route', e.g. DXB",
                },
                "destination": {
                    "type": "string",
                    "description": "IATA destination airport code for type='route', e.g. BOM",
                },
                "airline": {
                    "type": "string",
                    "description": "2-letter IATA airline code for type='airline', e.g. EK",
                },
            },
            "required": ["type"],
        },
    },
    {
        "name": "search_schedule",
        "description": (
            "Search flights in the schedule database by origin, destination, "
            "airline code, flight number, and/or day of week. "
            "Use day_of_week to filter by a specific operating day (e.g. 7 for Sunday). "
            "Returns matching flights with departure/arrival times."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "origin":        {"type": "string", "description": "IATA origin airport code, e.g. DXB"},
                "destination":   {"type": "string", "description": "IATA destination airport code, e.g. BOM"},
                "airline":       {"type": "string", "description": "2-letter IATA airline code, e.g. FZ"},
                "flight_number": {"type": "string", "description": "Full flight number, e.g. FZ001"},
                "day_of_week":   {
                    "type": "integer",
                    "description": "IATA day of week: 1=Monday, 2=Tuesday, 3=Wednesday, 4=Thursday, 5=Friday, 6=Saturday, 7=Sunday",
                },
            },
        },
    },
    {
        "name": "get_route_analysis",
        "description": (
            "Get a detailed analysis of a specific route (O&D pair), including: "
            "per-day-of-week frequency, departure times, airlines, aircraft types, market share, "
            "and optionally filtered flights for a specific day. "
            "Use this for any question about route frequency, schedules, day-by-day operations, or market analysis."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "origin":        {"type": "string", "description": "IATA origin airport code, e.g. DXB"},
                "destination":   {"type": "string", "description": "IATA destination airport code, e.g. BOM"},
                "day_of_week":   {
                    "type": "integer",
                    "description": "Optional: filter to specific day. 1=Monday … 7=Sunday",
                },
                "airline":       {"type": "string", "description": "Optional: filter to specific airline"},
            },
            "required": ["origin", "destination"],
        },
    },
    {
        "name": "check_turnaround",
        "description": (
            "Check if there is sufficient turnaround time for an aircraft "
            "between an arriving and departing flight at the same station."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "arrival_datetime":   {"type": "string", "description": "Arrival datetime ISO format, e.g. 2024-01-15 14:30"},
                "departure_datetime": {"type": "string", "description": "Departure datetime ISO format, e.g. 2024-01-15 16:00"},
                "aircraft_type":      {"type": "string", "description": "IATA aircraft type code, e.g. B777"},
                "station":            {"type": "string", "description": "IATA airport code of the turnaround station"},
            },
            "required": ["arrival_datetime", "departure_datetime", "station"],
        },
    },
    {
        "name": "check_airport_constraints",
        "description": (
            "Check curfew and operating hour constraints for a proposed "
            "departure or arrival at an airport."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "airport":          {"type": "string", "description": "IATA airport code"},
                "departure_local":  {"type": "string", "description": "Local departure time HH:MM or full datetime"},
                "arrival_local":    {"type": "string", "description": "Local arrival time HH:MM or full datetime"},
            },
            "required": ["airport"],
        },
    },
    {
        "name": "simulate_add_flight",
        "description": (
            "Run a full feasibility simulation for adding a new flight to the schedule. "
            "Returns feasibility score, network value score, violations, risks, and alternatives."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "origin":           {"type": "string", "description": "IATA origin airport"},
                "destination":      {"type": "string", "description": "IATA destination airport"},
                "departure_local":  {"type": "string", "description": "Proposed departure datetime local, e.g. 2024-01-15 08:00"},
                "arrival_local":    {"type": "string", "description": "Proposed arrival datetime local, e.g. 2024-01-15 12:00"},
                "aircraft_type":    {"type": "string", "description": "Aircraft type code, e.g. B777"},
                "airline":          {"type": "string", "description": "2-letter airline code"},
                "hub":              {"type": "string", "description": "Hub IATA code for connectivity analysis"},
            },
            "required": ["origin", "destination", "departure_local"],
        },
    },
    {
        "name": "simulate_retime_flight",
        "description": (
            "Evaluate the impact of retiming an existing flight to a new departure time. "
            "Returns delta scores, connectivity change, and conflict analysis."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "flight_number":     {"type": "string", "description": "Existing flight number, e.g. EK001"},
                "new_departure_local": {"type": "string", "description": "Proposed new departure datetime local"},
                "hub":               {"type": "string", "description": "Hub IATA code"},
            },
            "required": ["flight_number", "new_departure_local"],
        },
    },
    {
        "name": "get_competitor_analysis",
        "description": (
            "Analyse all airlines competing on a specific O&D route. "
            "Returns per-airline market share (by frequency), aircraft types, seat capacity, "
            "departure time spread, weekly operations, and haul classification. "
            "Use this for any question about competitors, market share, or who flies a route."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "origin":      {"type": "string", "description": "IATA origin airport code, e.g. DXB"},
                "destination": {"type": "string", "description": "IATA destination airport code, e.g. LHR"},
            },
            "required": ["origin", "destination"],
        },
    },
    {
        "name": "get_pax_capacity",
        "description": (
            "Return passenger seating capacity and class mix for a given IATA aircraft type code. "
            "Use this to answer questions about seats, cabin configuration, or aircraft category "
            "(widebody vs narrowbody). Also classifies haul type from a block time."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "aircraft_type": {"type": "string", "description": "IATA aircraft type code, e.g. 388, 77W, 73H, 789"},
                "block_minutes": {"type": "integer", "description": "Optional: block time in minutes for haul classification"},
            },
            "required": ["aircraft_type"],
        },
    },
    {
        "name": "get_terminal_info",
        "description": (
            "Return departure and/or arrival terminal information for an airline at an airport. "
            "Use this when asked about which terminal an airline uses, or terminal info for a route."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "airport":     {"type": "string", "description": "IATA airport code"},
                "airline":     {"type": "string", "description": "IATA 2-letter airline code"},
                "destination": {"type": "string", "description": "Optional: destination airport for route-level terminal info"},
            },
            "required": ["airport", "airline"],
        },
    },
    {
        "name": "get_nonops_flights",
        "description": (
            "Return non-revenue / positioning flights (SSIM service_type='G'). "
            "These are ferry operations, not sold to passengers. "
            "Use this when asked about positioning flights, ferry flights, or non-revenue operations."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "origin":      {"type": "string", "description": "Optional: filter by origin airport"},
                "destination": {"type": "string", "description": "Optional: filter by destination airport"},
                "airline":     {"type": "string", "description": "Optional: filter by airline"},
            },
        },
    },
    {
        "name": "get_route_intelligence",
        "description": (
            "Return a COMPREHENSIVE intelligence dossier for any O&D route combining ALL data sources: "
            "market demand index, airline-by-airline market shares, weekly seat capacity, spill/recapture "
            "analysis (demand pressure), departure time distribution (time-of-day buckets), alliance "
            "memberships, airport dominance profile, timezone info, city info, and traveler recommendations. "
            "ALWAYS call this tool alongside get_route_analysis for any route summary, "
            "itinerary, comparison, or 'tell me about' question. "
            "It provides the demand / commercial intelligence layer that get_route_analysis cannot."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "origin":      {"type": "string", "description": "IATA origin airport code, e.g. DXB"},
                "destination": {"type": "string", "description": "IATA destination airport code, e.g. BOM"},
            },
            "required": ["origin", "destination"],
        },
    },
    {
        "name": "get_airport_overview",
        "description": (
            "Return the competitive airline landscape at a specific airport: "
            "top airlines by market share, top destination routes, city/timezone info. "
            "Use when asked about which airlines dominate an airport, hub analysis, "
            "or airport-level market intelligence."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "airport": {"type": "string", "description": "IATA airport code, e.g. DXB"},
            },
            "required": ["airport"],
        },
    },
    {
        "name": "get_db_schema",
        "description": (
            "Return the full database schema: all tables, their columns (with descriptions), "
            "sample queries, aircraft type catalogue (IATA code → name/body/seats/range), "
            "service type codes, and DuckDB function reference. "
            "ALWAYS call this tool FIRST before writing any execute_sql query, so you know "
            "the exact column names, types, and relationships available. "
            "Use this to understand what data is available for jet-leg analysis, timezone analysis, "
            "passenger type inference, aircraft analysis, connection feasibility, etc."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "execute_sql",
        "description": (
            "Execute a custom SQL SELECT query against the schedule database and return results. "
            "CRITICAL RULES:\n"
            "1. Only SELECT statements — no INSERT/UPDATE/DELETE/DROP/CREATE.\n"
            "2. Call get_db_schema FIRST to know the exact table and column names.\n"
            "3. Results are capped at 300 rows — use aggregation (GROUP BY, COUNT, AVG) "
            "   for large-scale analysis rather than fetching raw rows.\n"
            "4. Use this for ANY question not covered by other tools:\n"
            "   - Jet-leg / multi-segment connection analysis\n"
            "   - Timezone-aware departure time analysis\n"
            "   - Aircraft family breakdown (wide-body vs narrow-body on a route)\n"
            "   - Passenger type inference (carrier type × aircraft × haul)\n"
            "   - Hub bank analysis (waves of departures at a hub)\n"
            "   - Airline frequency comparison across multiple routes\n"
            "   - Operating day patterns (which days a flight operates)\n"
            "   - Spill/demand analysis on specific flights\n"
            "   - Market share trends across O&D pairs\n"
            "5. If you get an error, read the 'hint' field in the response and retry "
            "   with corrected column names from get_db_schema."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "A valid DuckDB SELECT SQL query. "
                        "Use table names exactly as returned by get_db_schema. "
                        "Always use LIMIT to keep results manageable. "
                        "Example: SELECT airline, COUNT(DISTINCT flight_number) AS flights "
                        "FROM flights WHERE origin='DXB' GROUP BY airline ORDER BY flights DESC"
                    ),
                },
                "description": {
                    "type": "string",
                    "description": "One-line plain-English description of what this query computes (for logging).",
                },
            },
            "required": ["query"],
        },
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# Tool execution dispatcher
# ─────────────────────────────────────────────────────────────────────────────

def _parse_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"]:
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def _df_to_list(df) -> list:
    """Convert a DataFrame to a JSON-serialisable list of dicts."""
    if df is None or df.empty:
        return []
    # Stringify datetime columns
    result = []
    for rec in df.to_dict("records"):
        clean = {}
        for k, v in rec.items():
            if hasattr(v, "isoformat"):
                clean[k] = v.isoformat()
            elif v is None:
                clean[k] = None
            else:
                try:
                    json.dumps(v)
                    clean[k] = v
                except (TypeError, ValueError):
                    clean[k] = str(v)
        result.append(clean)
    return result


def execute_tool(tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Dispatch a tool call by name, execute the backend function,
    and return a serialisable result dict.
    """
    logger.info(f"Executing tool: {tool_name} args={list(args.keys())}")

    try:
        if tool_name == "get_graph_insights":
            from app.knowledge_graph.graph_queries import (
                get_hub_profile, get_route_graph_context,
                get_airline_network, get_network_summary,
            )
            query_type = (args.get("type") or "").lower()
            if query_type == "airport":
                ap = (args.get("airport") or "").upper()
                if not ap:
                    return {"tool": tool_name, "error": "airport parameter required for type='airport'"}
                return {"tool": tool_name, **get_hub_profile(ap)}
            elif query_type == "route":
                o = (args.get("origin") or "").upper()
                d = (args.get("destination") or "").upper()
                if not o or not d:
                    return {"tool": tool_name, "error": "origin and destination required for type='route'"}
                return {"tool": tool_name, **get_route_graph_context(o, d)}
            elif query_type == "airline":
                al = (args.get("airline") or "").upper()
                if not al:
                    return {"tool": tool_name, "error": "airline parameter required for type='airline'"}
                return {"tool": tool_name, **get_airline_network(al)}
            elif query_type == "network":
                return {"tool": tool_name, **get_network_summary()}
            else:
                return {"tool": tool_name, "error": f"Unknown type '{query_type}'. Use: airport, route, airline, network"}

        elif tool_name == "search_schedule":
            day_of_week = args.get("day_of_week")
            if day_of_week is not None:
                day_of_week = int(day_of_week)
            df = _svc.search_flights(
                origin=args.get("origin"),
                destination=args.get("destination"),
                airline=args.get("airline"),
                flight_number=args.get("flight_number"),
                day_of_week=day_of_week,
            )
            flights = _df_to_list(df)
            # Deduplicate by (flight_number, dep_time, day_of_operation, airline)
            seen, unique = set(), []
            for f in flights:
                key = (f.get("flight_number"), f.get("departure_local"), f.get("day_of_operation"), f.get("airline"))
                if key not in seen:
                    seen.add(key)
                    unique.append(f)
            return {
                "tool": tool_name,
                "count": len(unique),
                "flights": unique[:50],
            }

        elif tool_name == "get_route_analysis":
            origin      = (args.get("origin") or "").upper()
            destination = (args.get("destination") or "").upper()
            day_of_week = args.get("day_of_week")
            if day_of_week is not None:
                day_of_week = int(day_of_week)
            airline_filter = args.get("airline")

            result = _svc.get_route_day_analysis(origin, destination, day_of_week)

            # Optionally filter to a specific airline
            if airline_filter:
                al = airline_filter.upper()
                result["flights_on_day"] = [
                    f for f in result["flights_on_day"] if (f.get("airline") or "").upper() == al
                ]
                result["flight_count_on_day"] = len(result["flights_on_day"])

            # Cap list size for Vertex AI
            result["flights_on_day"] = result["flights_on_day"][:60]

            # Enrich with graph context (hub tiers, direct airline summary)
            try:
                from app.knowledge_graph.graph_queries import get_route_graph_context
                from app.knowledge_graph.graph_builder import is_ready as _kg_ready
                if _kg_ready():
                    gc = get_route_graph_context(origin, destination)
                    result["graph_context"] = {
                        "origin_hub_tier":    gc.get("origin_hub_profile", {}).get("tier"),
                        "dest_hub_tier":      gc.get("dest_hub_profile", {}).get("tier"),
                        "direct_airline_count": gc.get("direct_airline_count", 0),
                        "airlines_on_route":  [
                            {
                                "airline":       r["airline"],
                                "airline_name":  r["airline_name"],
                                "carrier_type":  r["carrier_type"],
                                "weekly_flights": r["weekly_flights"],
                                "avg_block_min": r["avg_block_min"],
                                "aircraft_types": r["aircraft_types"],
                            }
                            for r in gc.get("direct_routes", [])
                        ],
                        "top_connecting_hubs": [
                            {
                                "hub":      h["hub"],
                                "hub_city": h["hub_city"],
                                "hub_tier": h["hub_tier"],
                                "common_airlines": h["common_airlines"],
                            }
                            for h in gc.get("connecting_hubs", [])[:5]
                        ],
                    }
            except Exception as _kg_exc:
                logger.debug(f"KG enrichment skipped for get_route_analysis: {_kg_exc}")

            return {"tool": tool_name, **result}

        elif tool_name == "get_route_summary":
            result = _ra_svc.get_route_summary(
                origin=args.get("origin"),
                destination=args.get("destination"),
            )
            # Ensure serialisable
            if "departures" in result:
                for d in result["departures"]:
                    for k, v in list(d.items()):
                        if hasattr(v, "isoformat"):
                            d[k] = v.isoformat()
            return {"tool": tool_name, **result}

        elif tool_name == "check_turnaround":
            arr_dt  = _parse_dt(args.get("arrival_datetime", ""))
            dep_dt  = _parse_dt(args.get("departure_datetime", ""))
            ac_type = args.get("aircraft_type", "")
            station = args.get("station", "")
            if not arr_dt or not dep_dt:
                return {"tool": tool_name, "error": "Invalid datetime format."}
            result = check_turnaround_standalone(arr_dt, dep_dt, ac_type, station)
            return {"tool": tool_name, **result}

        elif tool_name == "check_airport_constraints":
            airport  = args.get("airport", "")
            dep_str  = args.get("departure_local")
            arr_str  = args.get("arrival_local")
            dep_dt   = _parse_dt(dep_str) if dep_str else None
            arr_dt   = _parse_dt(arr_str) if arr_str else None
            result   = check_airport_constraints_standalone(airport, dep_dt, arr_dt)
            return {"tool": tool_name, **result}

        elif tool_name == "simulate_add_flight":
            dep_dt  = _parse_dt(args.get("departure_local", ""))
            arr_dt  = _parse_dt(args.get("arrival_local", ""))
            if not dep_dt:
                return {"tool": tool_name, "error": "departure_local is required and must be a valid datetime."}
            proposed = {
                "origin":          (args.get("origin") or "").upper(),
                "destination":     (args.get("destination") or "").upper(),
                "departure_local": dep_dt,
                "arrival_local":   arr_dt,
                "aircraft_type":   args.get("aircraft_type", ""),
                "airline":         (args.get("airline") or "").upper(),
            }
            result = _sim_add(proposed, hub=args.get("hub"))
            # Strip non-serialisable fields
            result.pop("evidence", None)
            return {"tool": tool_name, **result}

        elif tool_name == "simulate_retime_flight":
            flt_num   = args.get("flight_number", "")
            new_dep   = _parse_dt(args.get("new_departure_local", ""))
            if not new_dep:
                return {"tool": tool_name, "error": "new_departure_local must be a valid datetime."}
            # Fetch existing flight
            df = _svc.search_flights(flight_number=flt_num)
            if df.empty:
                return {"tool": tool_name, "error": f"Flight {flt_num} not found in schedule."}
            flt_dict = df.iloc[0].to_dict()
            for k, v in list(flt_dict.items()):
                if hasattr(v, "isoformat"):
                    flt_dict[k] = v  # keep as datetime for rule engine
            result = _sim_retime(flt_dict, new_dep, hub=args.get("hub"))
            result.pop("evidence", None)
            return {"tool": tool_name, **result}

        elif tool_name == "get_competitor_analysis":
            from app.services.intelligence_service import get_competitor_analysis
            origin      = (args.get("origin") or "").upper()
            destination = (args.get("destination") or "").upper()
            if not origin or not destination:
                return {"tool": tool_name, "error": "origin and destination are required."}
            return {"tool": tool_name, **get_competitor_analysis(origin, destination)}

        elif tool_name == "get_pax_capacity":
            from app.services.intelligence_service import get_pax_capacity_info, classify_haul
            ac_type = (args.get("aircraft_type") or "").upper()
            result  = get_pax_capacity_info(ac_type)
            if args.get("block_minutes"):
                result["haul_type"] = classify_haul(int(args["block_minutes"]))
            return {"tool": tool_name, **result}

        elif tool_name == "get_terminal_info":
            from app.services.intelligence_service import get_terminals_for_route, get_terminal_info
            airport     = (args.get("airport") or "").upper()
            airline     = (args.get("airline") or "").upper()
            destination = (args.get("destination") or "").upper()
            if destination:
                return {"tool": tool_name, **get_terminals_for_route(airport, destination, airline)}
            return {"tool": tool_name, **get_terminal_info(airport, airline)}

        elif tool_name == "get_nonops_flights":
            from app.services.intelligence_service import get_nonops_flights
            return {"tool": tool_name, **get_nonops_flights(
                origin=args.get("origin"),
                destination=args.get("destination"),
                airline=args.get("airline"),
            )}

        elif tool_name == "get_route_intelligence":
            from app.services.workset_service import get_route_intelligence
            origin      = (args.get("origin") or "").upper()
            destination = (args.get("destination") or "").upper()
            if not origin or not destination:
                return {"tool": tool_name, "error": "origin and destination are required."}
            return {"tool": tool_name, **get_route_intelligence(origin, destination)}

        elif tool_name == "get_airport_overview":
            from app.services.workset_service import get_airport_overview
            airport = (args.get("airport") or "").upper()
            if not airport:
                return {"tool": tool_name, "error": "airport is required."}
            result = get_airport_overview(airport)

            # Enrich with graph hub profile
            try:
                from app.knowledge_graph.graph_queries import get_hub_profile
                from app.knowledge_graph.graph_builder import is_ready as _kg_ready
                if _kg_ready():
                    hp = get_hub_profile(airport)
                    if hp.get("found"):
                        result["graph_hub_profile"] = {
                            "hub_tier":            hp["hub_tier"],
                            "hub_score":           hp["hub_score"],
                            "destinations_served": hp["destinations_served"],
                            "airlines_operating":  hp["airlines_operating"],
                            "weekly_outbound_frequency": hp["weekly_outbound_frequency"],
                            "top_destinations":    hp["top_destinations"][:10],
                            "top_airlines":        hp["top_airlines"][:8],
                        }
            except Exception as _kg_exc:
                logger.debug(f"KG enrichment skipped for get_airport_overview: {_kg_exc}")

            return {"tool": tool_name, **result}

        elif tool_name == "get_db_schema":
            from app.services.dynamic_query_service import get_db_schema
            return {"tool": tool_name, **get_db_schema()}

        elif tool_name == "execute_sql":
            from app.services.dynamic_query_service import execute_sql
            query = (args.get("query") or "").strip()
            desc  = args.get("description", "")
            if not query:
                return {"tool": tool_name, "error": "query is required."}
            logger.info(f"execute_sql called — {desc or 'no description'} | {query[:120]}")
            return {"tool": tool_name, "description": desc, **execute_sql(query)}

        else:
            return {"tool": tool_name, "error": f"Unknown tool: {tool_name}"}

    except Exception as exc:
        logger.exception(f"Tool {tool_name} raised an exception: {exc}")
        return {"tool": tool_name, "error": str(exc)}
