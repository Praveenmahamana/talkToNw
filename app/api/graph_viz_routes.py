"""
Graph Visualization API — Cytoscape.js compatible endpoints.

Category: Graph Visualization (awesome-knowledge-graph)
Library:  Cytoscape.js (https://js.cytoscape.org) — loaded via CDN in the frontend

Returns JSON in Cytoscape.js elements format:
  {
    "elements": {
      "nodes": [{"data": {"id": "DXB", "label": "Dubai (DXB)", "hub_tier": "Mega-hub", ...}}],
      "edges": [{"data": {"id": "DXB-BOM-EK", "source": "DXB", "target": "BOM", ...}}]
    },
    "stats": {...}
  }

Endpoints:
  GET /api/v1/graph/hub?airport=DXB&max_nodes=30
  GET /api/v1/graph/route?origin=DXB&dest=BOM
  GET /api/v1/graph/airline?code=EK&top_routes=40
  GET /api/v1/graph/analytics   ← network-level PageRank + communities
  GET /api/v1/graph/status      ← KG layer readiness check
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from loguru import logger

router = APIRouter(prefix="/graph", tags=["Graph Visualization"])

# Community palette — vivid colours for dark canvas, 15 distinct slots
_COMM_COLORS = [
    "#60a5fa","#f87171","#34d399","#fbbf24","#a78bfa",
    "#f472b6","#2dd4bf","#fb923c","#38bdf8","#a3e635",
    "#818cf8","#f9a8d4","#6ee7b7","#fcd34d","#c4b5fd",
]

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _node_size(hub_tier: str) -> int:
    return {"Mega-hub": 60, "Major hub": 45, "Secondary hub": 32,
            "Regional hub": 22, "Point-to-point": 14}.get(hub_tier, 18)


def _node_color(hub_tier: str) -> str:
    return {"Mega-hub": "#2065d1", "Major hub": "#0ea5e9", "Secondary hub": "#10b981",
            "Regional hub": "#f59e0b", "Point-to-point": "#94a3b8"}.get(hub_tier, "#94a3b8")


def _make_node(code: str, data: Dict[str, Any]) -> Dict[str, Any]:
    from app.services.workset_service import AIRPORT_INFO
    info = AIRPORT_INFO.get(code, {})
    hub_tier = data.get("hub_tier", "Point-to-point")
    return {
        "data": {
            "id":         code,
            "label":      f"{code}\n{info.get('city', '')}",
            "city":       info.get("city", code),
            "country":    info.get("country", ""),
            "hub_tier":   hub_tier,
            "hub_score":  round(float(data.get("hub_score", 0.0)), 1),
            "dest_count": int(data.get("dest_count", 0)),
            "out_freq":   int(data.get("out_freq", 0)),
            "size":       _node_size(hub_tier),
            "color":      _node_color(hub_tier),
        }
    }


def _make_edge(src: str, tgt: str, data: Dict[str, Any]) -> Dict[str, Any]:
    al = data.get("airline", "")
    freq = int(data.get("unique_flights", 0))
    return {
        "data": {
            "id":            f"{src}-{tgt}-{al}",
            "source":        src,
            "target":        tgt,
            "airline":       al,
            "airline_name":  data.get("airline_name", al),
            "carrier_type":  data.get("carrier_type", ""),
            "weekly_flights": freq,
            "avg_block_min": int(data.get("avg_block_min", 0)),
            "weight":        max(1, min(10, freq // 2)),   # visual thickness 1–10
        }
    }


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/status")
async def graph_status():
    """Return readiness status of all KG layers."""
    try:
        from app.knowledge_graph.graph_construction import get_build_status
        return get_build_status()
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.get("/hub")
async def graph_hub(
    airport: str = Query(..., description="IATA airport code, e.g. DXB"),
    max_nodes: int = Query(30, ge=5, le=80, description="Max neighbor nodes to include"),
):
    """
    Return a Cytoscape.js graph centred on an airport hub.
    Shows the airport and its top outbound neighbors with route edges.
    """
    from app.knowledge_graph.graph_builder import get_graph, is_ready
    from app.knowledge_graph.graph_analytics import get_analytics_cache

    ap = airport.upper().strip()
    if not is_ready():
        return JSONResponse(status_code=503, content={"error": "Knowledge graph not ready."})

    G = get_graph()
    if ap not in G:
        return JSONResponse(status_code=404, content={"error": f"{ap} not found in network."})

    # Top neighbors by total weekly frequency
    dest_freq: Dict[str, int] = {}
    for _, v, data in G.out_edges(ap, data=True):
        dest_freq[v] = dest_freq.get(v, 0) + int(data.get("unique_flights", 0))

    top_dests = sorted(dest_freq.items(), key=lambda x: x[1], reverse=True)[:max_nodes]
    top_dest_set = {d for d, _ in top_dests}

    # Build Cytoscape elements
    nodes: List[Dict] = [_make_node(ap, G.nodes[ap])]
    edges: List[Dict] = []
    for d in top_dest_set:
        if d in G:
            nodes.append(_make_node(d, G.nodes[d]))
    for u, v, data in G.out_edges(ap, data=True):
        if v in top_dest_set:
            edges.append(_make_edge(u, v, data))

    # Annotate centre node
    nodes[0]["data"]["is_centre"] = True

    analytics = get_analytics_cache()
    return {
        "elements": {"nodes": nodes, "edges": edges},
        "centre": ap,
        "stats": {
            "nodes": len(nodes),
            "edges": len(edges),
            "hub_tier": G.nodes[ap].get("hub_tier", "unknown"),
            "pagerank_score": analytics["pagerank"].get(ap, 0.0) if analytics else None,
            "betweenness_score": analytics["betweenness"].get(ap, 0.0) if analytics else None,
        },
    }


@router.get("/route")
async def graph_route(
    origin: str = Query(..., description="IATA origin airport, e.g. DXB"),
    dest: str   = Query(..., description="IATA destination airport, e.g. BOM"),
):
    """
    Return a Cytoscape.js graph for an O&D pair.
    Shows origin, destination, direct airlines, and top 1-stop connecting hubs.
    """
    from app.knowledge_graph.graph_builder import get_graph, is_ready
    from app.knowledge_graph.graph_queries import get_route_graph_context

    o, d = origin.upper().strip(), dest.upper().strip()
    if not is_ready():
        return JSONResponse(status_code=503, content={"error": "Knowledge graph not ready."})

    G = get_graph()
    ctx = get_route_graph_context(o, d)

    nodes_map: Dict[str, Dict] = {}
    edges: List[Dict] = []

    def _ensure_node(code: str):
        if code not in nodes_map and code in G:
            nodes_map[code] = _make_node(code, G.nodes[code])

    _ensure_node(o)
    _ensure_node(d)

    # Direct edges
    edge_data = G.get_edge_data(o, d) or {}
    for key, data in edge_data.items():
        edges.append(_make_edge(o, d, data))

    # Connecting hubs (up to 5)
    for hub_info in ctx.get("connecting_hubs", [])[:5]:
        hub = hub_info["hub"]
        _ensure_node(hub)
        # Leg 1: origin → hub
        leg1 = G.get_edge_data(o, hub) or {}
        for key, data in list(leg1.items())[:3]:
            edges.append(_make_edge(o, hub, data))
        # Leg 2: hub → dest
        leg2 = G.get_edge_data(hub, d) or {}
        for key, data in list(leg2.items())[:3]:
            edges.append(_make_edge(hub, d, data))

    # Mark roles
    for code, node in nodes_map.items():
        if code == o:
            node["data"]["role"] = "origin"
        elif code == d:
            node["data"]["role"] = "destination"
        else:
            node["data"]["role"] = "hub"

    return {
        "elements": {"nodes": list(nodes_map.values()), "edges": edges},
        "stats": {
            "nodes": len(nodes_map),
            "edges": len(edges),
            "has_direct": ctx.get("has_direct_service", False),
            "direct_airlines": ctx.get("direct_airline_count", 0),
            "connecting_hubs": len(ctx.get("connecting_hubs", [])),
        },
    }


@router.get("/airline")
async def graph_airline(
    code: str      = Query(..., description="IATA 2-letter airline code, e.g. EK"),
    top_routes: int = Query(40, ge=5, le=100, description="Max routes to include"),
):
    """
    Return a Cytoscape.js graph for an airline's route network.
    Shows primary hubs as large nodes, routes as edges.
    """
    from app.knowledge_graph.graph_builder import get_graph, is_ready
    from app.knowledge_graph.graph_queries import get_airline_network

    al = code.upper().strip()
    if not is_ready():
        return JSONResponse(status_code=503, content={"error": "Knowledge graph not ready."})

    G = get_graph()
    network = get_airline_network(al)
    if not network.get("found"):
        return JSONResponse(status_code=404, content={"error": f"Airline {al} not found in schedule."})

    # Get all routes for this airline, sorted by frequency
    routes = [
        (u, v, data)
        for u, v, data in G.edges(data=True)
        if (data.get("airline") or "").upper() == al
    ]
    routes.sort(key=lambda x: int(x[2].get("unique_flights", 0)), reverse=True)
    routes = routes[:top_routes]

    nodes_map: Dict[str, Dict] = {}
    edges: List[Dict] = []

    for u, v, data in routes:
        for ap in [u, v]:
            if ap not in nodes_map and ap in G:
                nodes_map[ap] = _make_node(ap, G.nodes[ap])
        edges.append(_make_edge(u, v, data))

    # Mark primary hubs
    primary_hub_codes = {h["airport"] for h in network.get("primary_hubs", [])}
    for code_key, node in nodes_map.items():
        node["data"]["is_hub"] = code_key in primary_hub_codes

    return {
        "elements": {"nodes": list(nodes_map.values()), "edges": edges},
        "airline": al,
        "airline_name": network.get("name", al),
        "carrier_type": network.get("carrier_type", ""),
        "stats": {
            "nodes": len(nodes_map),
            "edges": len(edges),
            "total_routes": network.get("total_routes", 0),
            "airports_served": network.get("airports_served", 0),
            "weekly_flights": network.get("total_weekly_flights", 0),
        },
    }


@router.get("/analytics")
async def graph_analytics_endpoint():
    """
    Return network-level graph analytics:
    PageRank top-20, betweenness top-20, community summaries.
    """
    from app.knowledge_graph.graph_analytics import get_network_analytics_summary, is_analytics_ready

    if not is_analytics_ready():
        return JSONResponse(status_code=503, content={
            "error": "Graph analytics not yet computed. Please wait for background build to complete."
        })
    return get_network_analytics_summary()


@router.get("/airport-analytics")
async def airport_analytics_endpoint(
    airport: str = Query(..., description="IATA airport code, e.g. DXB"),
):
    """Return detailed analytics for a single airport."""
    from app.knowledge_graph.graph_analytics import get_airport_analytics
    return get_airport_analytics(airport.upper().strip())


@router.get("/sparql")
async def sparql_endpoint(
    query_type: str = Query(..., description=(
        "Predefined query type: airlines_on_route | hub_airports | alliance | "
        "carriers_at_airport | airline_alliance | fsc_vs_lcc"
    )),
    origin: str      = Query("", description="Origin airport (for route queries)"),
    dest: str        = Query("", description="Destination airport (for route queries)"),
    airport: str     = Query("", description="Airport code (for airport queries)"),
    airline: str     = Query("", description="Airline code (for airline queries)"),
    alliance: str    = Query("", description="Alliance name (for alliance queries)"),
    tier: str        = Query("Mega-hub", description="Hub tier (for hub_airports query)"),
    carrier_class: str = Query("", description="Carrier class filter"),
):
    """Run predefined SPARQL queries against the RDF triple store."""
    from app.knowledge_graph.rdf_store import (
        query_airlines_on_route, query_airports_by_tier,
        query_alliance_carriers, query_carriers_at_airport,
        query_airline_alliance, query_fsc_vs_lcc, is_rdf_ready,
    )

    if not is_rdf_ready():
        return JSONResponse(status_code=503, content={"error": "RDF triple store not ready."})

    qt = query_type.lower()
    try:
        if qt == "airlines_on_route":
            return {"result": query_airlines_on_route(origin.upper(), dest.upper())}
        elif qt == "hub_airports":
            return {"result": query_airports_by_tier(tier)}
        elif qt == "alliance":
            return {"result": query_alliance_carriers(alliance)}
        elif qt == "carriers_at_airport":
            return {"result": query_carriers_at_airport(
                airport.upper(), carrier_class or "Airline"
            )}
        elif qt == "airline_alliance":
            return {"result": query_airline_alliance(airline.upper())}
        elif qt == "fsc_vs_lcc":
            return {"result": query_fsc_vs_lcc(origin.upper(), dest.upper())}
        else:
            return JSONResponse(status_code=400, content={"error": f"Unknown query_type: {qt}"})
    except Exception as exc:
        logger.error(f"SPARQL endpoint error: {exc}")
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.get("/overview")
async def graph_overview(
    max_nodes: int = Query(2000, ge=20, le=5000, description="Max airports to include (default=all)"),
):
    """
    Return the full 'brain' overview graph: ALL airports ranked by PageRank
    with ALL edges between them. Nodes coloured by community cluster.
    Supports animated streaming: frontend loads nodes first, then edges.
    """
    from app.knowledge_graph.graph_builder import get_graph, is_ready
    from app.knowledge_graph.graph_analytics import get_analytics_cache, is_analytics_ready
    from app.services.workset_service import AIRPORT_INFO

    if not is_ready():
        return JSONResponse(status_code=503, content={"error": "Knowledge graph not ready."})
    if not is_analytics_ready():
        return JSONResponse(status_code=503, content={"error": "Graph analytics not ready."})

    G        = get_graph()
    cache    = get_analytics_cache()
    pagerank = cache["pagerank"]
    between  = cache["betweenness"]
    ap_comm  = cache["airport_community"]

    # ALL airports sorted by pagerank (not limited to cache top-N list)
    pr_max    = max(pagerank.values()) if pagerank else 1.0
    all_sorted = sorted(pagerank.items(), key=lambda x: x[1], reverse=True)[:max_nodes]
    top_codes  = {ap for ap, _ in all_sorted}

    # Build nodes
    nodes: List[Dict] = []
    for ap, pr_score in all_sorted:
        if ap not in G:
            continue
        gnode    = G.nodes[ap]
        comm_id  = ap_comm.get(ap, 0)
        # Node size 8–48 px proportional to pagerank
        size = max(8, min(48, int(8 + pr_score * 0.40)))
        info = AIRPORT_INFO.get(ap, {})
        nodes.append({"data": {
            "id":                ap,
            "code":              ap,
            "label":             ap,
            "city":              info.get("city", ap),
            "country":           info.get("country", ""),
            "hub_tier":          gnode.get("hub_tier", "Point-to-point"),
            "community_id":      comm_id,
            "comm_color":        _COMM_COLORS[comm_id % len(_COMM_COLORS)],
            "pagerank_score":    round(pr_score, 1),
            "betweenness_score": round(between.get(ap, 0.0), 1),
            "dest_count":        int(gnode.get("dest_count", 0)),
            "out_freq":          int(gnode.get("out_freq", 0)),
            "size":              size,
        }})

    # Build ALL edges between included airports (deduplicated bidirectional)
    edges: List[Dict] = []
    seen_pairs: set = set()
    freq_max = 1
    for u, v, data in G.edges(data=True):
        if u not in top_codes or v not in top_codes:
            continue
        pair = (min(u, v), max(u, v))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        freq = int(data.get("unique_flights", 0))
        if freq > freq_max:
            freq_max = freq
        edges.append({"data": {
            "id":             f"{u}--{v}",
            "source":         u,
            "target":         v,
            "weekly_flights": freq,
        }})

    # Normalise edge weight to 0.5–5 visual thickness
    for e in edges:
        freq = e["data"]["weekly_flights"]
        e["data"]["weight"] = round(0.5 + (freq / freq_max) * 4.5, 2)

    # Sort edges heaviest first (frontend streams them in this order so
    # important connections appear before minor ones)
    edges.sort(key=lambda e: e["data"]["weekly_flights"], reverse=True)

    return {
        "elements": {"nodes": nodes, "edges": edges},
        "stats": {
            "nodes":           len(nodes),
            "edges":           len(edges),
            "total_airports":  cache["total_airports"],
            "total_routes":    cache["total_routes"],
            "community_count": len(set(ap_comm.values())),
        },
    }
