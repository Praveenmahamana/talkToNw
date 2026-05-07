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
    """Return readiness status of all KG layers (schedule + workset demand)."""
    try:
        from app.knowledge_graph.graph_construction import get_build_status
        status = get_build_status()
        # Merge workset KG (BASEDATA/SPILLDATA OD→Leg spec) status as an additional layer
        try:
            from app.knowledge_graph.workset_graph_builder import get_status as wkg_status
            status["workset_kg"] = wkg_status()
        except Exception as exc:
            status["workset_kg"] = {"ready": False, "error": str(exc)}
        return status
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
    max_nodes: int = Query(2000, ge=20, le=5000, description="Max airports to include"),
    min_connections: int = Query(5, ge=1, le=50, description="Min route connections for airport inclusion"),
):
    """
    Return the full heterogeneous 'brain' overview graph:
      - Airport nodes (ranked by PageRank, coloured by community)
      - Carrier nodes (top 40 airlines, coloured hot-pink)
      - AircraftType nodes (top 15 fleets, coloured violet)
      - Airport↔Airport edges with relation='connects'
      - Carrier→Airport edges with relation='hubs_at'
      - Carrier→AircraftType edges with relation='uses_aircraft'

    Entity types and relation labels are LM-enriched via Gemini when available.
    """
    from app.knowledge_graph.graph_builder import get_graph, is_ready
    from app.knowledge_graph.graph_analytics import get_analytics_cache, is_analytics_ready
    from app.knowledge_graph.entity_enrichment import (
        build_entity_taxonomy, ENTITY_COLORS, RELATION_COLORS
    )
    from app.services.workset_service import AIRPORT_INFO

    if not is_ready():
        return JSONResponse(status_code=503, content={"error": "Knowledge graph not ready."})

    G = get_graph()
    partial = not is_analytics_ready()

    if partial:
        # Analytics still building — use degree-based fallback
        airport_nodes = [
            (ap, G.nodes[ap].get("dest_count", 0))
            for ap in G.nodes()
            if G.nodes[ap].get("node_type") == "airport"
               and G.nodes[ap].get("dest_count", 0) >= min_connections
        ]
        airport_nodes.sort(key=lambda x: x[1], reverse=True)
        filtered = airport_nodes[:max_nodes]
        top_codes = {ap for ap, _ in filtered}

        nodes: List[Dict] = []
        for ap, dest_cnt in filtered:
            gnode = G.nodes[ap]
            info  = AIRPORT_INFO.get(ap, {})
            nodes.append({"data": {
                "id":                ap,
                "code":              ap,
                "label":             ap,
                "entity_type":       "Airport",
                "city":              info.get("city", ap),
                "country":           info.get("country", ""),
                "hub_tier":          gnode.get("hub_tier", "Point-to-point"),
                "community_id":      0,
                "comm_color":        "#60a5fa",
                "pagerank_score":    0,
                "betweenness_score": 0,
                "dest_count":        int(dest_cnt),
                "out_freq":          int(gnode.get("out_freq", 0)),
            }})
    else:
        cache    = get_analytics_cache()
        pagerank = cache["pagerank"]
        between  = cache["betweenness"]
        ap_comm  = cache["airport_community"]

        # ── Airport nodes (PageRank-ranked, min_connections filter) ──────────────
        all_sorted = sorted(pagerank.items(), key=lambda x: x[1], reverse=True)
        # Keep only airports with enough connections (dest_count >= min_connections)
        filtered = [
            (ap, pr) for ap, pr in all_sorted
            if ap in G and G.nodes[ap].get("dest_count", 0) >= min_connections
        ][:max_nodes]
        top_codes = {ap for ap, _ in filtered}

        nodes: List[Dict] = []
        for ap, pr_score in filtered:
            if ap not in G:
                continue
            gnode   = G.nodes[ap]
            comm_id = ap_comm.get(ap, 0)
            info    = AIRPORT_INFO.get(ap, {})
            nodes.append({"data": {
                "id":                ap,
                "code":              ap,
                "label":             ap,
                "entity_type":       "Airport",
                "city":              info.get("city", ap),
                "country":           info.get("country", ""),
                "hub_tier":          gnode.get("hub_tier", "Point-to-point"),
                "community_id":      comm_id,
                "comm_color":        _COMM_COLORS[comm_id % len(_COMM_COLORS)],
                "pagerank_score":    round(pr_score, 1),
                "betweenness_score": round(between.get(ap, 0.0), 1),
                "dest_count":        int(gnode.get("dest_count", 0)),
                "out_freq":          int(gnode.get("out_freq", 0)),
            }})

    # ── Airport↔Airport edges with relation='connects' ────────────────────────
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
            "relation":       "connects",
            "weekly_flights": freq,
        }})

    # Normalise edge weight 0.5–5
    for e in edges:
        freq = e["data"]["weekly_flights"]
        e["data"]["weight"] = round(0.5 + (freq / freq_max) * 4.5, 2)

    # Sort heaviest first
    edges.sort(key=lambda e: e["data"]["weekly_flights"], reverse=True)

    # ── Carrier + AircraftType nodes & typed edges (via entity taxonomy) ───────
    # Skip in partial mode — taxonomy calls Gemini on first run (slow) and requires analytics
    try:
        taxonomy = None if partial else build_entity_taxonomy(G)
        if taxonomy is not None:
            for cn in taxonomy["carrier_nodes"]:
                nodes.append({"data": {
                    "id":           cn["id"],
                    "label":        cn["label"],
                    "name":         cn.get("name", cn["label"]),
                    "entity_type":  "Carrier",
                    "carrier_type": cn.get("carrier_subtype") or cn.get("carrier_type", ""),
                    "alliance":     cn.get("alliance", "None"),
                    "description":  cn.get("description", ""),
                    "routes":       cn.get("routes", 0),
                    "airports_count": cn.get("airports_count", 0),
                    "total_freq":   cn.get("total_freq", 0),
                }})

            for an in taxonomy["aircraft_nodes"]:
                nodes.append({"data": {
                    "id":          an["id"],
                    "label":       an["label"],
                    "entity_type": "AircraftType",
                    "operators":   an.get("operators", 0),
                    "routes":      an.get("routes", 0),
                }})

            for ce in taxonomy["carrier_edges"]:
                edges.append({"data": {
                    "id":       ce["id"],
                    "source":   ce["source"],
                    "target":   ce["target"],
                    "relation": ce["relation"],
                    "weight":   ce.get("weight", 1.0),
                }})

            for ae in taxonomy["aircraft_edges"]:
                edges.append({"data": {
                    "id":       ae["id"],
                    "source":   ae["source"],
                    "target":   ae["target"],
                    "relation": ae["relation"],
                    "weight":   ae.get("weight", 1.0),
                }})

        carrier_count  = len(taxonomy["carrier_nodes"]) if taxonomy else 0
        aircraft_count = len(taxonomy["aircraft_nodes"]) if taxonomy else 0
    except Exception as exc:
        logger.warning(f"Entity taxonomy failed (continuing without): {exc}")
        carrier_count = aircraft_count = 0

    if partial:
        stats_extra = {
            "total_airports": G.number_of_nodes(),
            "total_routes":   G.number_of_edges(),
            "community_count": 1,
            "partial": True,
        }
    else:
        stats_extra = {
            "total_airports":  cache["total_airports"],
            "total_routes":    cache["total_routes"],
            "community_count": len(set(ap_comm.values())),
            "partial": False,
        }

    # ── Workset KG augmentation — OD→Leg spec (BASEDATA + SPILLDATA) ─────────
    # Merges MARKET nodes + FLOW_THROUGH/MARKET_FLOW edges into the brain graph
    # so the visualization is consistent with the domain KG definition.
    workset_market_count = 0
    workset_flow_count   = 0
    try:
        from app.knowledge_graph import workset_graph_builder as wkg
        if wkg.is_ready():
            ge_df = wkg.get_graph_edges()
            ms_df = wkg.get_market_summary()
            gn_df = wkg.get_graph_nodes()

            if ge_df is not None and not ge_df.empty:
                # 1. FLOW_THROUGH + MARKET_FLOW edges between airports already in brain graph
                flow_types = {"FLOW_THROUGH", "MARKET_FLOW"}
                flow_rows = ge_df[
                    ge_df["edge_type"].isin(flow_types)
                    & ge_df["source"].isin(top_codes)
                    & ge_df["target"].isin(top_codes)
                ]
                # Aggregate traffic per (source, target, edge_type) to collapse duplicates
                if not flow_rows.empty:
                    agg = (
                        flow_rows.groupby(["source", "target", "edge_type"], as_index=False)
                        .agg(traffic=("traffic", "sum"), market_od=("market_od", "first"))
                    )
                    traffic_max = float(agg["traffic"].max()) if len(agg) else 1.0
                    for _, row in agg.iterrows():
                        rel = row["edge_type"].lower()  # flow_through | market_flow
                        w   = max(0.5, min(5.0, 0.5 + 4.5 * float(row["traffic"]) / max(traffic_max, 1)))
                        edges.append({"data": {
                            "id":        f"wkg-{row['source']}-{row['target']}-{rel}",
                            "source":    row["source"],
                            "target":    row["target"],
                            "relation":  rel,
                            "market_od": str(row["market_od"]) if row["market_od"] else "",
                            "traffic":   round(float(row["traffic"]), 1),
                            "weight":    round(w, 2),
                        }})
                    workset_flow_count = len(agg)

            if ms_df is not None and not ms_df.empty and gn_df is not None:
                # 2. MARKET nodes whose origin+destin are both in the brain graph
                # Parse market_od column (format "AAA->BBB") to get origin/destin
                ms = ms_df.copy()
                ms[["m_orig", "m_dest"]] = ms["market_od"].str.split("->", expand=True)
                visible = ms[
                    ms["m_orig"].isin(top_codes) & ms["m_dest"].isin(top_codes)
                ].nlargest(50, "total_traffic")

                for _, row in visible.iterrows():
                    mkt_id = f"MKT_{row['m_orig']}_{row['m_dest']}"
                    nodes.append({"data": {
                        "id":           mkt_id,
                        "label":        row["market_od"],
                        "entity_type":  "Market",
                        "market_od":    row["market_od"],
                        "total_traffic": round(float(row["total_traffic"]), 1),
                        "itin_count":   int(row.get("itinerary_count", 0)),
                        "local_itin":   int(row.get("local_itin_count", 0)),
                        "flow_itin":    int(row.get("flow_itin_count", 0)),
                    }})
                    # MARKET_FLOW edges to origin/destin airports
                    traf = float(row["total_traffic"])
                    edges.append({"data": {
                        "id":        f"wkg-mkt-orig-{mkt_id}",
                        "source":    row["m_orig"],
                        "target":    mkt_id,
                        "relation":  "market_flow",
                        "weight":    1.0,
                        "traffic":   traf,
                    }})
                    edges.append({"data": {
                        "id":        f"wkg-mkt-dest-{mkt_id}",
                        "source":    mkt_id,
                        "target":    row["m_dest"],
                        "relation":  "market_flow",
                        "weight":    1.0,
                        "traffic":   traf,
                    }})
                workset_market_count = len(visible)

    except Exception as exc:
        logger.warning(f"Workset KG augmentation failed (continuing without): {exc}")

    return {
        "elements": {"nodes": nodes, "edges": edges},
        "entity_colors":   ENTITY_COLORS,
        "relation_colors": RELATION_COLORS,
        "stats": {
            "nodes":                len(nodes),
            "edges":                len(edges),
            "airport_count":        len(top_codes),
            "carrier_count":        carrier_count,
            "aircraft_count":       aircraft_count,
            "workset_market_count": workset_market_count,
            "workset_flow_edges":   workset_flow_count,
            **stats_extra,
        },
    }


@router.get("/build-events")
async def build_events():
    """SSE stream of KG build progress events."""
    from app.knowledge_graph.graph_builder import get_graph, is_ready
    from app.knowledge_graph.graph_construction import get_build_status
    from app.knowledge_graph.graph_analytics import is_analytics_ready
    from fastapi.responses import StreamingResponse
    import asyncio, json

    async def event_stream():
        sent_nodes = False
        sent_complete = False
        for _ in range(120):  # max 4 minutes
            try:
                status = get_build_status()
                layers = status.get("layers", {})

                if not sent_nodes and is_ready():
                    G = get_graph()
                    node_count = G.number_of_nodes()
                    edge_count = G.number_of_edges()
                    data = json.dumps({"type": "kg_ready", "nodes": node_count, "edges": edge_count})
                    yield f"data: {data}\n\n"
                    sent_nodes = True

                if sent_nodes and not sent_complete and is_analytics_ready():
                    data = json.dumps({"type": "analytics_ready"})
                    yield f"data: {data}\n\n"
                    sent_complete = True
                    break

                building = [k for k, v in layers.items() if not v.get("ready", False)]
                data = json.dumps({"type": "progress", "building": building, "status": layers})
                yield f"data: {data}\n\n"

            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

            await asyncio.sleep(2)

        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


# ─────────────────────────────────────────────────────────────────────────────
# Workset KG — OD→Leg Contribution Matrix + Flow Edges  ⚡
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/workset/status")
async def workset_graph_status():
    """
    Return build status for the workset knowledge graph
    (LEG / MARKET / ITINERARY nodes + FLOW edges).
    """
    try:
        from app.knowledge_graph.workset_graph_builder import get_status
        return get_status()
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.get("/workset/build")
async def workset_graph_build(force: bool = Query(False, description="Force rebuild")):
    """Trigger (or re-trigger) the workset KG build in the background."""
    import asyncio
    from app.knowledge_graph.workset_graph_builder import init_workset_graph, get_status

    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, lambda: init_workset_graph(force=force))
    return {"message": "Workset graph build triggered", "force": force}


@router.get("/workset/flow")
async def workset_od_flow(
    origin: str = Query(..., description="Market origin airport, e.g. DXB"),
    destin: str = Query(..., description="Market destination airport, e.g. BOM"),
):
    """
    Return all itineraries + FLOW_THROUGH / FLOW_TO edges for a market OD.

    Per spec:
      Market OD AAA→BBB via AAA→CCC→BBB with 120 pax →
        AAA ⚡ CCC  FLOW_THROUGH  traffic=120
        CCC ⚡ BBB  FLOW_THROUGH  traffic=120
        AAA ⚡ BBB  MARKET_FLOW   total_traffic=120
    """
    from app.knowledge_graph.workset_graph_builder import get_od_flow_detail, is_ready
    if not is_ready():
        return JSONResponse(status_code=503, content={
            "error": "Workset graph not ready. Call /graph/workset/build first.",
            "ready": False,
        })
    return get_od_flow_detail(origin, destin)


@router.get("/workset/leg-flow")
async def workset_leg_flow(
    base_index: int = Query(..., description="BASEDATA leg identifier (baseIndex / record_id)"),
):
    """
    Return all market ODs that contribute pax to a specific leg (by baseIndex).
    Shows local vs flow breakdown and top contributing markets.
    """
    from app.knowledge_graph.workset_graph_builder import get_leg_flow_detail, is_ready
    if not is_ready():
        return JSONResponse(status_code=503, content={
            "error": "Workset graph not ready.",
            "ready": False,
        })
    return get_leg_flow_detail(base_index)


@router.get("/workset/od-leg-matrix")
async def workset_od_leg_matrix(
    origin: str = Query("", description="Filter by market origin (optional)"),
    destin: str = Query("", description="Filter by market destination (optional)"),
    limit:  int = Query(500, ge=1, le=5000, description="Max rows to return"),
):
    """
    Return the OD→Leg Contribution Matrix (or a filtered slice).
    Columns: market_od, route, itin_type, leg_seq, baseIndex, leg_od, flt_num, traffic.
    """
    from app.knowledge_graph.workset_graph_builder import get_od_leg_contribution_matrix, is_ready
    if not is_ready():
        return JSONResponse(status_code=503, content={"error": "Workset graph not ready."})

    df = get_od_leg_contribution_matrix()
    if df is None or df.empty:
        return {"rows": [], "total": 0}

    if origin:
        df = df[df["market_origin"] == origin.upper()]
    if destin:
        df = df[df["market_destin"] == destin.upper()]

    cols = ["market_od", "route", "itin_type", "leg_seq", "baseIndex", "leg_od", "flt_num", "traffic"]
    available = [c for c in cols if c in df.columns]
    subset = df[available].head(limit)

    import math as _math
    rows = []
    for rec in subset.to_dict("records"):
        clean = {k: (None if isinstance(v, float) and (_math.isnan(v) or _math.isinf(v)) else v)
                 for k, v in rec.items()}
        rows.append(clean)

    return {"rows": rows, "total": len(df)}


@router.get("/workset/market-summary")
async def workset_market_summary(
    limit: int = Query(100, ge=1, le=2000, description="Max markets to return"),
    sort_by: str = Query("total_traffic", description="Sort column"),
):
    """Return the market_summary table (total traffic, demand, itinerary counts per OD)."""
    from app.knowledge_graph.workset_graph_builder import get_market_summary, is_ready
    if not is_ready():
        return JSONResponse(status_code=503, content={"error": "Workset graph not ready."})

    df = get_market_summary()
    if df is None or df.empty:
        return {"rows": [], "total": 0}

    if sort_by in df.columns:
        df = df.sort_values(sort_by, ascending=False)

    import math as _math
    rows = []
    for rec in df.head(limit).to_dict("records"):
        clean = {k: (None if isinstance(v, float) and (_math.isnan(v) or _math.isinf(v)) else v)
                 for k, v in rec.items()}
        rows.append(clean)
    return {"rows": rows, "total": len(df)}


@router.get("/workset/leg-flow-summary")
async def workset_leg_flow_summary(
    limit: int = Query(200, ge=1, le=2000),
):
    """Return the leg_flow_summary table (total OD contribution per leg)."""
    from app.knowledge_graph.workset_graph_builder import get_leg_flow_summary, is_ready
    if not is_ready():
        return JSONResponse(status_code=503, content={"error": "Workset graph not ready."})

    df = get_leg_flow_summary()
    if df is None or df.empty:
        return {"rows": [], "total": 0}

    df = df.sort_values("total_od_contribution", ascending=False)
    import math as _math
    rows = []
    for rec in df.head(limit).to_dict("records"):
        clean = {k: (None if isinstance(v, float) and (_math.isnan(v) or _math.isinf(v)) else v)
                 for k, v in rec.items()}
        rows.append(clean)
    return {"rows": rows, "total": len(df)}


@router.get("/workset/board-deboard")
async def workset_board_deboard(
    station: str = Query("", description="Filter by station (optional)"),
):
    """Return the airport boarding/deboarding/connecting summary."""
    from app.knowledge_graph.workset_graph_builder import get_airport_board_deboard_summary, is_ready
    if not is_ready():
        return JSONResponse(status_code=503, content={"error": "Workset graph not ready."})

    df = get_airport_board_deboard_summary()
    if df is None or df.empty:
        return {"rows": [], "total": 0}

    if station:
        df = df[df["station"] == station.upper()]

    df = df.sort_values("boarded", ascending=False)
    import math as _math
    rows = []
    for rec in df.to_dict("records"):
        clean = {k: (None if isinstance(v, float) and (_math.isnan(v) or _math.isinf(v)) else v)
                 for k, v in rec.items()}
        rows.append(clean)
    return {"rows": rows, "total": len(df)}


@router.get("/workset/graph-nodes")
async def workset_graph_nodes(
    node_type: str = Query("", description="Filter: AIRPORT | LEG | MARKET | ITINERARY"),
    limit: int     = Query(500, ge=1, le=5000),
):
    """Return graph nodes (all 4 types: AIRPORT, LEG, MARKET, ITINERARY)."""
    from app.knowledge_graph.workset_graph_builder import get_graph_nodes, is_ready
    if not is_ready():
        return JSONResponse(status_code=503, content={"error": "Workset graph not ready."})

    df = get_graph_nodes()
    if df is None or df.empty:
        return {"rows": [], "total": 0}

    if node_type:
        df = df[df["node_type"] == node_type.upper()]

    rows = df.head(limit)[["node_id", "node_type", "label"]].to_dict("records")
    return {"rows": rows, "total": len(df)}


@router.get("/workset/graph-edges")
async def workset_graph_edges(
    edge_type:  str = Query("", description="Filter: FLOW_THROUGH | FLOW_TO | MARKET_FLOW | USES_LEG | HAS_ITINERARY | DEPARTS_ON | ARRIVES_AT"),
    market_od:  str = Query("", description="Filter by market OD, e.g. DXB->BOM"),
    limit:      int = Query(500, ge=1, le=5000),
):
    """Return graph edges. Filter by edge_type and/or market_od."""
    from app.knowledge_graph.workset_graph_builder import get_graph_edges, is_ready
    if not is_ready():
        return JSONResponse(status_code=503, content={"error": "Workset graph not ready."})

    df = get_graph_edges()
    if df is None or df.empty:
        return {"rows": [], "total": 0}

    if edge_type:
        df = df[df["edge_type"] == edge_type.upper()]
    if market_od:
        df = df[df["market_od"] == market_od]

    import math as _math
    rows = []
    for rec in df.head(limit).to_dict("records"):
        clean = {k: (None if isinstance(v, float) and (_math.isnan(v) or _math.isinf(v)) else v)
                 for k, v in rec.items()}
        rows.append(clean)
    return {"rows": rows, "total": len(df)}
