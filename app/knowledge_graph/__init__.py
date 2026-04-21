"""
Airline Schedule Knowledge Graph — multi-layer stack.

Infrastructure from github.com/totogo/awesome-knowledge-graph:

  ┌─────────────────────────────────────────────────────────────────┐
  │  DuckDB flights table  (source of truth)                        │
  └──────────────────────┬──────────────────────────────────────────┘
                         │  graph_construction.build_all()
         ┌───────────────┼────────────────────────────────┐
         ▼               ▼                                 ▼
  ┌────────────┐  ┌─────────────┐  ┌──────────────────────────────┐
  │  NetworkX  │  │   RDFLib    │  │          Kuzu DB              │
  │  MultiDi   │  │  OWL+SPARQL │  │  Embeddable property graph   │
  │  Graph     │  │  triple     │  │  Cypher traversals            │
  │  (Graph    │  │  store      │  │  (Graph Databases)            │
  │  Computing)│  │  (Triple    │  └──────────────────────────────┘
  └─────┬──────┘  │  Stores)    │
        │         └─────────────┘
        ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │  graph_analytics.py  — PageRank · betweenness · communities     │
  │  (Graph Computing Frameworks)                                   │
  └─────────────────────────────────────────────────────────────────┘
        │
        ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │  graph_viz_routes.py — Cytoscape.js REST API                    │
  │  (Graph Visualization)                                          │
  └─────────────────────────────────────────────────────────────────┘
"""

# ── Graph Computing (NetworkX) ────────────────────────────────────────────────
from app.knowledge_graph.graph_builder import init_graph, get_graph, is_ready, rebuild_graph
from app.knowledge_graph.graph_queries import (
    get_hub_profile,
    get_route_graph_context,
    get_airline_network,
    get_network_summary,
    get_corridor_competition,
)

# ── Graph Construction (unified pipeline) ────────────────────────────────────
from app.knowledge_graph.graph_construction import build_all, rebuild_all, get_build_status

# ── Triple Store (RDFLib) ─────────────────────────────────────────────────────
from app.knowledge_graph.rdf_store import (
    init_rdf_store, is_rdf_ready, sparql_query,
    query_airlines_on_route, query_airports_by_tier,
    query_alliance_carriers, query_fsc_vs_lcc,
)

# ── Graph Database (Kuzu) ─────────────────────────────────────────────────────
from app.knowledge_graph.kuzu_store import (
    init_kuzu, is_kuzu_ready,
    find_direct_routes, find_connecting_airports,
    find_hub_neighbors, find_airline_hubs,
)

# ── Graph Analytics (NetworkX algorithms) ────────────────────────────────────
from app.knowledge_graph.graph_analytics import (
    init_analytics, is_analytics_ready,
    get_airport_analytics, get_network_analytics_summary,
    find_shortest_path,
)

__all__ = [
    # NetworkX
    "init_graph", "get_graph", "is_ready", "rebuild_graph",
    "get_hub_profile", "get_route_graph_context",
    "get_airline_network", "get_network_summary", "get_corridor_competition",
    # Construction
    "build_all", "rebuild_all", "get_build_status",
    # RDF
    "init_rdf_store", "is_rdf_ready", "sparql_query",
    "query_airlines_on_route", "query_airports_by_tier",
    "query_alliance_carriers", "query_fsc_vs_lcc",
    # Kuzu
    "init_kuzu", "is_kuzu_ready",
    "find_direct_routes", "find_connecting_airports",
    "find_hub_neighbors", "find_airline_hubs",
    # Analytics
    "init_analytics", "is_analytics_ready",
    "get_airport_analytics", "get_network_analytics_summary",
    "find_shortest_path",
]
