"""
Workset KG Loader
=================
Loads the pre-built Knowledge Graph from customKG
(C:\\...\\customKG\\output → data/output/kg/) into:

  • A NetworkX MultiDiGraph   — get_workset_graph()
  • A live DuckDB connection  — get_workset_db()

The KG is built by the customKG dashboard (WORKSET13282).
Every time customKG rebuilds it auto-copies the three artefacts:
  data/output/kg/kg_light.json   — nodes + edges (compact)
  data/output/kg/kg.duckdb       — SQL-queryable store
  data/output/kg/kg.cypher       — Neo4j import script (reference)

Node types   : AIRPORT, LEG, ITINERARY (FLOW only), CARRIER
Edge types   : FLOW_TO, FLOW_THROUGH, HAS_ITINERARY, USES_LEG,
               CONNECTS_TO, OPERATED_BY, DEPARTS_FROM, ARRIVES_AT
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

try:
    import networkx as nx
    NX_AVAILABLE = True
except ImportError:
    NX_AVAILABLE = False

try:
    import duckdb as _duckdb
    DUCKDB_AVAILABLE = True
except ImportError:
    DUCKDB_AVAILABLE = False

# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────

_KG_DIR  = Path(__file__).parent.parent.parent / "data" / "output" / "kg"
_JSON    = _KG_DIR / "kg_light.json"
_DUCKDB  = _KG_DIR / "kg.duckdb"

# ─────────────────────────────────────────────────────────────────────────────
# Singletons
# ─────────────────────────────────────────────────────────────────────────────

_lock:  threading.Lock = threading.Lock()
_graph: Optional[Any]  = None   # nx.MultiDiGraph
_meta:  Dict[str, Any] = {}
_ready: bool           = False
_error: Optional[str]  = None


# ─────────────────────────────────────────────────────────────────────────────
# Init
# ─────────────────────────────────────────────────────────────────────────────

def init_workset_kg(force: bool = False) -> None:
    """Load kg_light.json into a NetworkX MultiDiGraph (thread-safe, idempotent)."""
    global _graph, _meta, _ready, _error

    if not NX_AVAILABLE:
        logger.warning("networkx not installed — workset KG disabled.")
        return

    with _lock:
        if _ready and not force:
            return

        if not _JSON.exists():
            _error = f"kg_light.json not found at {_JSON}"
            logger.error(_error)
            return

        try:
            logger.info(f"Loading workset KG from {_JSON} …")
            raw = json.loads(_JSON.read_text(encoding="utf-8"))
            _meta = {
                "workset_id":   raw.get("workset_id", "unknown"),
                "node_count":   len(raw.get("nodes", [])),
                "edge_count":   len(raw.get("edges", [])),
            }

            g = nx.MultiDiGraph()

            for node in raw.get("nodes", []):
                nid  = node.pop("id")
                ntype = node.pop("type", "UNKNOWN")
                g.add_node(nid, node_type=ntype, **node)

            for edge in raw.get("edges", []):
                src = edge.pop("src")
                tgt = edge.pop("tgt")
                rel = edge.pop("rel", "")
                g.add_edge(src, tgt, rel=rel, **edge)

            _graph = g
            _ready = True
            _error = None
            logger.info(
                f"Workset KG ready — workset={_meta['workset_id']} "
                f"nodes={_meta['node_count']:,} edges={_meta['edge_count']:,}"
            )
        except Exception as exc:
            _error = str(exc)
            logger.exception(f"Failed to load workset KG: {exc}")


def is_workset_kg_ready() -> bool:
    return _ready


def get_workset_graph() -> Optional[Any]:
    """Return the loaded NetworkX MultiDiGraph, or None if not ready."""
    return _graph


def get_workset_meta() -> Dict[str, Any]:
    return {**_meta, "ready": _ready, "error": _error}


def get_workset_db() -> Optional[Any]:
    """
    Open a read-only DuckDB connection to kg.duckdb.
    Caller is responsible for closing.  Returns None if not available.

    Tables:
      nodes    — id, node_type, + all node properties
      edges    — source, target, rel, + all edge properties
      metadata — key, value  (workset_id, total_nodes, total_edges)
    """
    if not DUCKDB_AVAILABLE:
        logger.warning("duckdb not installed.")
        return None
    if not _DUCKDB.exists():
        logger.warning(f"kg.duckdb not found at {_DUCKDB}")
        return None
    return _duckdb.connect(str(_DUCKDB), read_only=True)


# ─────────────────────────────────────────────────────────────────────────────
# High-level query helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_airports() -> List[str]:
    """Return list of all airport codes in the KG."""
    if not _graph:
        return []
    return [n for n, d in _graph.nodes(data=True) if d.get("node_type") == "AIRPORT"]


def get_market_flow(origin: str, dest: str) -> Optional[Dict[str, Any]]:
    """
    Return FLOW_TO edge properties for an OD market pair.
    Includes traffic/demand/spill for all 4 scenarios (HO/LO/HR/LR).
    Returns None if no market edge exists.
    """
    if not _graph:
        return None
    for _, _, data in _graph.edges(origin, data=True):
        if data.get("rel") == "FLOW_TO" and data.get("od") == f"{origin}{dest}":
            return dict(data)
    return None


def get_flow_itineraries(origin: str, dest: str) -> List[Dict[str, Any]]:
    """
    Return all FLOW (multi-hop) itinerary nodes for a given OD market.
    Each dict has: id, route, itin_type, traffic, stops, num_segs.
    """
    if not _graph:
        return []
    itins = []
    od = f"{origin}{dest}"
    for n, d in _graph.nodes(data=True):
        if d.get("node_type") == "ITINERARY" and d.get("market_od") == od:
            itins.append({"id": n, **d})
    return sorted(itins, key=lambda x: x.get("traffic", 0), reverse=True)


def get_legs_between(origin: str, dest: str) -> List[Dict[str, Any]]:
    """
    Return all LEG nodes departing origin and arriving dest.
    """
    if not _graph:
        return []
    legs = []
    for n, d in _graph.nodes(data=True):
        if (d.get("node_type") == "LEG"
                and d.get("origin") == origin
                and d.get("destin") == dest):
            legs.append({"id": n, **d})
    return sorted(legs, key=lambda x: x.get("traffic", 0), reverse=True)


def get_carrier_legs(carrier_code: str) -> List[str]:
    """Return all LEG node IDs operated by a carrier code."""
    if not _graph:
        return []
    return [
        n for n, d in _graph.nodes(data=True)
        if d.get("node_type") == "LEG" and d.get("carrier_code") == carrier_code
    ]


def connecting_airports(origin: str, dest: str, max_hops: int = 2) -> List[str]:
    """
    Find airports that connect origin → dest in ≤ max_hops legs.
    Uses the FLOW_THROUGH edges from FLOW itineraries.
    """
    if not _graph:
        return []
    hubs: set[str] = set()
    for n, d in _graph.nodes(data=True):
        if d.get("node_type") != "ITINERARY":
            continue
        if d.get("market_od") != f"{origin}{dest}":
            continue
        route = d.get("route", "")
        parts = route.split("->")
        if len(parts) > 2:
            hubs.update(parts[1:-1])   # intermediate airports only
    return sorted(hubs)
