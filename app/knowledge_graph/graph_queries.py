"""
Knowledge Graph Query Functions
---------------------------------
All functions return serialisable dicts optimised for LM consumption.

Design goal: replace raw 50-row SQL dumps with structured graph insights
that a language model can reason about reliably.  Every dict returned here
is meant to be passed directly as a tool result to Gemini.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from app.knowledge_graph.graph_builder import get_graph, is_ready, _hub_tier
from app.services.workset_service import AIRPORT_INFO, AIRLINE_NAMES, CARRIER_TYPE


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe(v: Any, default: Any = 0) -> Any:
    try:
        if v is None:
            return default
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return default
        return v
    except Exception:
        return default


def _airport_label(code: str) -> str:
    info = AIRPORT_INFO.get(code, {})
    city = info.get("city", code)
    country = info.get("country", "")
    return f"{city}, {country}" if country else city


# ─────────────────────────────────────────────────────────────────────────────
# Airport hub profile
# ─────────────────────────────────────────────────────────────────────────────

def get_hub_profile(airport: str) -> Dict[str, Any]:
    """
    Return a structured hub profile for an airport.

    Includes: hub tier, degree metrics, top destinations, top airlines.
    Replaces the airport_overview + opp tables for structural understanding.
    """
    G = get_graph()
    ap = airport.upper().strip()

    if not G:
        return {"airport": ap, "found": False, "note": "Knowledge graph not yet built."}
    if ap not in G:
        return {"airport": ap, "found": False, "note": f"{ap} not found in route network."}

    node = G.nodes[ap]
    dest_count    = _safe(node.get("dest_count", 0))
    airline_count = _safe(node.get("airline_count", 0))
    out_freq      = _safe(node.get("out_freq", 0))
    hub_score     = _safe(node.get("hub_score", 0.0))
    hub_tier      = node.get("hub_tier", _hub_tier(dest_count))

    # Top outbound destinations by weekly flight frequency
    dest_freq: Dict[str, int] = {}
    for _, v, data in G.out_edges(ap, data=True):
        dest_freq[v] = dest_freq.get(v, 0) + _safe(data.get("unique_flights", 0))
    top_dests: List[Tuple[str, int]] = sorted(
        dest_freq.items(), key=lambda x: x[1], reverse=True
    )[:15]

    # Top airlines by outbound weekly frequency
    aln_freq: Dict[str, int] = {}
    for _, _, data in G.out_edges(ap, data=True):
        al = data.get("airline", "")
        if al:
            aln_freq[al] = aln_freq.get(al, 0) + _safe(data.get("unique_flights", 0))
    top_alns: List[Tuple[str, int]] = sorted(
        aln_freq.items(), key=lambda x: x[1], reverse=True
    )[:10]

    info = AIRPORT_INFO.get(ap, {})
    return {
        "airport":             ap,
        "found":               True,
        "city":                info.get("city", ap),
        "country":             info.get("country", ""),
        "utc_offset":          info.get("utc", "unknown"),
        "hub_tier":            hub_tier,
        "hub_score":           hub_score,
        "destinations_served": dest_count,
        "airlines_operating":  airline_count,
        "weekly_outbound_frequency": out_freq,
        "top_destinations": [
            {
                "airport":       d,
                "city":          AIRPORT_INFO.get(d, {}).get("city", d),
                "country":       AIRPORT_INFO.get(d, {}).get("country", ""),
                "weekly_flights": f,
            }
            for d, f in top_dests
        ],
        "top_airlines": [
            {
                "code":           al,
                "name":           AIRLINE_NAMES.get(al, al),
                "carrier_type":   CARRIER_TYPE.get(al, "Full-service"),
                "weekly_flights": f,
            }
            for al, f in top_alns
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Route graph context (direct + 1-stop connectivity)
# ─────────────────────────────────────────────────────────────────────────────

def get_route_graph_context(origin: str, dest: str) -> Dict[str, Any]:
    """
    Return graph-derived structural context for an O&D pair.

    Provides:
    - Direct route summary (airlines, frequency, aircraft families)
    - 1-stop connectivity via major hubs (when no direct service)
    - Hub tier of both endpoints
    - Relative market position of airlines on this route

    This gives the LM structural understanding BEFORE it queries SQL tables.
    """
    G = get_graph()
    o, d = origin.upper().strip(), dest.upper().strip()

    if not G:
        return {"origin": o, "destination": d, "found": False,
                "note": "Knowledge graph not yet built."}

    o_exists = G.has_node(o)
    d_exists = G.has_node(d)

    # ── Direct routes ────────────────────────────────────────────────────────
    direct_edges: List[Dict[str, Any]] = []
    if o_exists and d_exists:
        edge_data = G.get_edge_data(o, d) or {}
        for key, data in edge_data.items():
            al = data.get("airline", str(key))
            direct_edges.append({
                "airline":       al,
                "airline_name":  AIRLINE_NAMES.get(al, al),
                "carrier_type":  CARRIER_TYPE.get(al, "Full-service"),
                "weekly_flights": _safe(data.get("unique_flights", 0)),
                "avg_block_min": _safe(data.get("avg_block_min", 0)),
                "aircraft_types": data.get("aircraft_types", []),
            })
        direct_edges.sort(key=lambda x: x["weekly_flights"], reverse=True)

    # ── 1-stop via hub connectivity ──────────────────────────────────────────
    connecting_hubs: List[Dict[str, Any]] = []
    if o_exists and d_exists:
        o_dests   = {v for _, v in G.out_edges(o)}
        d_origins = {u for u, _ in G.in_edges(d)}
        potential_hubs = o_dests & d_origins

        # Rank hubs by hub_score (prefer major connecting hubs)
        ranked_hubs = sorted(
            potential_hubs,
            key=lambda h: _safe(G.nodes[h].get("hub_score", 0), 0.0),
            reverse=True,
        )[:8]

        for hub in ranked_hubs:
            leg1_data = list((G.get_edge_data(o, hub) or {}).values())
            leg2_data = list((G.get_edge_data(hub, d) or {}).values())
            leg1_airlines = sorted({e.get("airline", "") for e in leg1_data if e.get("airline")})
            leg2_airlines = sorted({e.get("airline", "") for e in leg2_data if e.get("airline")})
            leg1_freq = sum(e.get("unique_flights", 0) for e in leg1_data)
            leg2_freq = sum(e.get("unique_flights", 0) for e in leg2_data)

            connecting_hubs.append({
                "hub":           hub,
                "hub_city":      AIRPORT_INFO.get(hub, {}).get("city", hub),
                "hub_tier":      G.nodes[hub].get("hub_tier", "unknown"),
                "hub_score":     _safe(G.nodes[hub].get("hub_score", 0), 0.0),
                "leg1_airlines": leg1_airlines,
                "leg2_airlines": leg2_airlines,
                "leg1_weekly_freq": leg1_freq,
                "leg2_weekly_freq": leg2_freq,
                "common_airlines": sorted(set(leg1_airlines) & set(leg2_airlines)),
            })

    # ── Endpoint hub profiles (compact) ─────────────────────────────────────
    o_node = G.nodes.get(o, {}) if o_exists else {}
    d_node = G.nodes.get(d, {}) if d_exists else {}
    o_info = AIRPORT_INFO.get(o, {})
    d_info = AIRPORT_INFO.get(d, {})

    return {
        "origin":           o,
        "destination":      d,
        "origin_city":      o_info.get("city", o),
        "dest_city":        d_info.get("city", d),
        "found":            True,
        "has_direct_service": len(direct_edges) > 0,
        "direct_airline_count": len(direct_edges),
        "total_weekly_direct_flights": sum(e["weekly_flights"] for e in direct_edges),
        "direct_routes": direct_edges,
        "connecting_hubs": connecting_hubs,
        "origin_hub_profile": {
            "code":               o,
            "city":               o_info.get("city", o),
            "tier":               o_node.get("hub_tier", _hub_tier(_safe(o_node.get("dest_count", 0)))),
            "destinations_served": _safe(o_node.get("dest_count", 0)),
            "airlines_operating": _safe(o_node.get("airline_count", 0)),
            "hub_score":          _safe(o_node.get("hub_score", 0.0), 0.0),
        },
        "dest_hub_profile": {
            "code":               d,
            "city":               d_info.get("city", d),
            "tier":               d_node.get("hub_tier", _hub_tier(_safe(d_node.get("dest_count", 0)))),
            "destinations_served": _safe(d_node.get("dest_count", 0)),
            "airlines_operating": _safe(d_node.get("airline_count", 0)),
            "hub_score":          _safe(d_node.get("hub_score", 0.0), 0.0),
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Airline network profile
# ─────────────────────────────────────────────────────────────────────────────

def get_airline_network(airline: str) -> Dict[str, Any]:
    """
    Return a network-level summary for an airline.

    Covers: total routes, airports served, primary hub(s), aircraft mix,
    top routes by frequency, carrier type.
    """
    G = get_graph()
    al = airline.upper().strip()

    if not G:
        return {"airline": al, "found": False, "note": "Knowledge graph not yet built."}

    routes: List[Tuple[str, str, Dict]] = [
        (u, v, data)
        for u, v, data in G.edges(data=True)
        if (data.get("airline") or "").upper() == al
    ]

    if not routes:
        return {
            "airline": al,
            "found":   False,
            "note":    f"No routes found for airline {al} in the schedule.",
        }

    airports = set()
    for u, v, _ in routes:
        airports.add(u)
        airports.add(v)

    # Primary hubs = airports with most outgoing routes for this airline
    hub_counts: Dict[str, int] = {}
    for u, _, data in routes:
        hub_counts[u] = hub_counts.get(u, 0) + _safe(data.get("unique_flights", 0))
    primary_hubs = sorted(hub_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    # Aircraft mix across the network
    ac_set: Dict[str, int] = {}
    for _, _, data in routes:
        for ac in data.get("aircraft_types", []):
            if ac:
                ac_set[ac] = ac_set.get(ac, 0) + 1

    total_weekly = sum(_safe(d.get("unique_flights", 0)) for _, _, d in routes)

    return {
        "airline":       al,
        "name":          AIRLINE_NAMES.get(al, al),
        "carrier_type":  CARRIER_TYPE.get(al, "Full-service"),
        "found":         True,
        "total_routes":  len(routes),
        "airports_served": len(airports),
        "total_weekly_flights": total_weekly,
        "primary_hubs": [
            {
                "airport":            ap,
                "city":               AIRPORT_INFO.get(ap, {}).get("city", ap),
                "weekly_departures":  freq,
                "hub_tier":           G.nodes[ap].get("hub_tier", "unknown") if ap in G else "unknown",
            }
            for ap, freq in primary_hubs
        ],
        "aircraft_fleet": [
            {"type": ac, "route_count": cnt}
            for ac, cnt in sorted(ac_set.items(), key=lambda x: x[1], reverse=True)[:10]
        ],
        "top_routes": sorted(
            [
                {
                    "route":          f"{u}-{v}",
                    "origin_city":    AIRPORT_INFO.get(u, {}).get("city", u),
                    "dest_city":      AIRPORT_INFO.get(v, {}).get("city", v),
                    "weekly_flights": _safe(d.get("unique_flights", 0)),
                    "avg_block_min":  _safe(d.get("avg_block_min", 0)),
                }
                for u, v, d in routes
            ],
            key=lambda x: x["weekly_flights"],
            reverse=True,
        )[:10],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Global network summary
# ─────────────────────────────────────────────────────────────────────────────

def get_network_summary() -> Dict[str, Any]:
    """
    Return a high-level summary of the entire route network:
    top hubs by score, total airports/routes, network breadth.
    """
    G = get_graph()
    if not G:
        return {"found": False, "note": "Knowledge graph not yet built."}

    all_nodes = list(G.nodes(data=True))
    top_hubs = sorted(
        all_nodes,
        key=lambda x: _safe(x[1].get("hub_score", 0), 0.0),
        reverse=True,
    )[:25]

    return {
        "found":               True,
        "total_airports":      G.number_of_nodes(),
        "total_airline_routes": G.number_of_edges(),
        "top_hubs": [
            {
                "airport":             ap,
                "city":                AIRPORT_INFO.get(ap, {}).get("city", ap),
                "country":             AIRPORT_INFO.get(ap, {}).get("country", ""),
                "hub_tier":            data.get("hub_tier", "unknown"),
                "destinations_served": _safe(data.get("dest_count", 0)),
                "airlines_operating":  _safe(data.get("airline_count", 0)),
                "weekly_frequency":    _safe(data.get("out_freq", 0)),
                "hub_score":           _safe(data.get("hub_score", 0.0), 0.0),
            }
            for ap, data in top_hubs
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Corridor competition (airlines competing on a given route from graph)
# ─────────────────────────────────────────────────────────────────────────────

def get_corridor_competition(origin: str, dest: str) -> Dict[str, Any]:
    """
    Return a competition snapshot for a route from the graph layer.
    Faster than querying SQL — uses cached graph data.
    """
    ctx = get_route_graph_context(origin, dest)
    if not ctx.get("found"):
        return ctx

    total = max(ctx["total_weekly_direct_flights"], 1)
    airlines_ranked = [
        {
            **r,
            "market_share_pct": round(r["weekly_flights"] / total * 100, 1),
        }
        for r in ctx["direct_routes"]
    ]

    return {
        "origin":         ctx["origin"],
        "destination":    ctx["destination"],
        "origin_city":    ctx["origin_city"],
        "dest_city":      ctx["dest_city"],
        "total_weekly_flights": ctx["total_weekly_direct_flights"],
        "airline_count":  ctx["direct_airline_count"],
        "market_leader":  airlines_ranked[0]["airline"] if airlines_ranked else None,
        "airlines":       airlines_ranked,
        "has_lcc":        any(r["carrier_type"] == "Low-cost" for r in airlines_ranked),
        "has_fsc":        any(r["carrier_type"] == "Full-service" for r in airlines_ranked),
    }
