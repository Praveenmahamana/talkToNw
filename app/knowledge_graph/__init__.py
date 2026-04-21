"""
Airline Schedule Knowledge Graph
---------------------------------
A NetworkX-backed property graph built from the DuckDB flights table.

Recommended by awesome-knowledge-graph (github.com/totogo/awesome-knowledge-graph)
as the best embeddable graph-computing framework for Python applications.

Graph model
  Nodes  = Airports  (IATA code, city, country, UTC offset, hub metrics)
  Edges  = Routes    (one directed edge per airline per O&D pair)
  Edge attributes: airline, carrier_type, weekly_flights, avg_block_min, aircraft_types

Usage
  from app.knowledge_graph import init_graph, get_hub_profile, get_route_graph_context
"""

from app.knowledge_graph.graph_builder import init_graph, get_graph, is_ready
from app.knowledge_graph.graph_queries import (
    get_hub_profile,
    get_route_graph_context,
    get_airline_network,
    get_network_summary,
)

__all__ = [
    "init_graph",
    "get_graph",
    "is_ready",
    "get_hub_profile",
    "get_route_graph_context",
    "get_airline_network",
    "get_network_summary",
]
