"""
Knowledge Graph Builder
-----------------------
Constructs a NetworkX MultiDiGraph from the DuckDB flights table.

Graph model
  Nodes  = Airports  (IATA code + static metadata + computed hub metrics)
  Edges  = Routes    (directed, one edge per airline per O&D pair)
           Key = airline code (MultiDiGraph allows parallel edges per O&D)

Built once after schedule data is loaded, then cached in-memory.
Thread-safe: uses a lock to prevent double-build under concurrent startup.
"""

from __future__ import annotations

import threading
import math
from typing import Any, Dict, List, Optional, Set

import networkx as nx
from loguru import logger

# Re-use static reference data already defined in workset_service
# (avoids duplication; workset_service is always available regardless of file loads)
from app.services.workset_service import AIRPORT_INFO, AIRLINE_NAMES, CARRIER_TYPE

# ─────────────────────────────────────────────────────────────────────────────
# Singleton graph + build state
# ─────────────────────────────────────────────────────────────────────────────

_graph: Optional[nx.MultiDiGraph] = None
_graph_lock = threading.Lock()
_graph_built = False


# ─────────────────────────────────────────────────────────────────────────────
# Internal build helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_int(v: Any, default: int = 0) -> int:
    try:
        x = int(v)
        return x
    except Exception:
        return default


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return 0.0 if math.isnan(x) or math.isinf(x) else x
    except Exception:
        return default


def _hub_tier(dest_count: int) -> str:
    if dest_count >= 100:
        return "Mega-hub"
    if dest_count >= 50:
        return "Major hub"
    if dest_count >= 20:
        return "Secondary hub"
    if dest_count >= 5:
        return "Regional hub"
    return "Point-to-point"


def _build_graph() -> Optional[nx.MultiDiGraph]:
    """
    Build the property graph from DuckDB.
    Returns a fully annotated MultiDiGraph or None on failure.

    Uses a fresh read-only DuckDB connection so this function is safe to call
    from background threads without interfering with the main server connection.
    """
    # DuckDB cursors are thread-safe and share the same underlying connection
    from app.database.db import get_connection
    conn = get_connection().cursor()

    try:
        # ── 1. Load per-airline route aggregates ─────────────────────────────
        rows = conn.execute("""
            SELECT
                origin,
                destination,
                airline,
                COUNT(DISTINCT flight_number)                           AS unique_flights,
                CAST(ROUND(AVG(block_time), 0) AS INTEGER)              AS avg_block_min,
                STRING_AGG(DISTINCT aircraft_type, ','
                    ORDER BY aircraft_type)                             AS ac_types,
                COUNT(*)                                                AS total_ops,
                -- Codeshare: collect distinct operating airlines for this marketing airline×route
                STRING_AGG(DISTINCT operating_airline, ','
                    ORDER BY operating_airline)                         AS operating_airlines,
                SUM(CASE WHEN is_codeshare THEN 1 ELSE 0 END)          AS codeshare_ops,
                -- Marketing airlines that market this operating airline's route
                STRING_AGG(DISTINCT marketing_airline, ','
                    ORDER BY marketing_airline)                         AS marketing_airlines
            FROM flights
            WHERE service_type != 'G'
              AND origin      IS NOT NULL
              AND destination IS NOT NULL
              AND airline     IS NOT NULL
            GROUP BY origin, destination, airline
        """).fetchall()

        if not rows:
            logger.warning("KG build: no flight rows found — is the schedule loaded?")
            return None

        G: nx.MultiDiGraph = nx.MultiDiGraph()

        # Collect all airport codes present in the data
        airports_in_data: Set[str] = set()
        for r in rows:
            airports_in_data.add(r[0])
            airports_in_data.add(r[1])

        # ── 2. Add airport nodes ──────────────────────────────────────────────
        for ap in airports_in_data:
            info = AIRPORT_INFO.get(ap, {})
            G.add_node(
                ap,
                city=info.get("city", ap),
                country=info.get("country", ""),
                utc_offset=info.get("utc", "unknown"),
            )

        # ── 3. Add route edges (one per airline per O&D) ──────────────────────
        codeshare_pairs: Set[tuple] = set()  # (marketing_al, operating_al) pairs

        for origin, dest, airline, uniq, avg_blk, ac_types_str, ops, \
                op_als_str, cs_ops, mkt_als_str in rows:
            ac_list: List[str] = [
                a.strip()
                for a in (ac_types_str or "").split(",")
                if a.strip()
            ]
            # Parse operating airlines for this marketing airline × route
            op_al_list: List[str] = [
                a.strip() for a in (op_als_str or "").split(",") if a.strip()
            ]
            # Determine primary operating carrier: the most common one (first after sort)
            # If all ops are own-operated, operating = marketing airline
            primary_op_al = op_al_list[0] if op_al_list else airline
            is_codeshare_route = _safe_int(cs_ops) > 0 and primary_op_al != airline

            G.add_edge(
                origin, dest,
                key=airline,
                airline=airline,
                airline_name=AIRLINE_NAMES.get(airline, airline),
                carrier_type=CARRIER_TYPE.get(airline, "Full-service"),
                unique_flights=_safe_int(uniq),
                avg_block_min=_safe_int(avg_blk),
                aircraft_types=ac_list,
                total_ops=_safe_int(ops),
                # Codeshare / carrier role properties
                marketing_airline=airline,
                operating_airline=primary_op_al,
                is_codeshare=is_codeshare_route,
                operating_airlines=op_al_list,
            )

            # Track codeshare pairs: (marketing_carrier, operating_carrier)
            if is_codeshare_route:
                for op_al in op_al_list:
                    if op_al and op_al != airline:
                        codeshare_pairs.add((airline, op_al))

        # Store codeshare pairs as a graph-level attribute for downstream KG layers
        G.graph["codeshare_pairs"] = list(codeshare_pairs)

        # ── 4. Annotate airport nodes with computed hub metrics ───────────────
        for ap in list(G.nodes()):
            out_edges = list(G.out_edges(ap, data=True))
            in_edges  = list(G.in_edges(ap, data=True))

            out_freq      = sum(d.get("unique_flights", 0) for _, _, d in out_edges)
            in_freq       = sum(d.get("unique_flights", 0) for _, _, d in in_edges)
            dest_count    = len({v for _, v in G.out_edges(ap)})
            origin_count  = len({u for u, _ in G.in_edges(ap)})
            airline_set   = {d.get("airline", "") for _, _, d in out_edges}

            # Hub score: connectivity × frequency (log-scaled to avoid mega-hub dominance)
            connectivity  = (dest_count + origin_count) / 2
            freq_factor   = 1.0 + min(math.log1p(out_freq / 10), 5.0)
            hub_score     = round(connectivity * freq_factor, 1)

            G.nodes[ap].update({
                "out_freq":      out_freq,
                "in_freq":       in_freq,
                "dest_count":    dest_count,
                "origin_count":  origin_count,
                "airline_count": len(airline_set),
                "hub_score":     hub_score,
                "hub_tier":      _hub_tier(dest_count),
            })

        codeshare_count = len(G.graph.get("codeshare_pairs", []))
        logger.info(
            f"Knowledge graph built: {G.number_of_nodes():,} airports, "
            f"{G.number_of_edges():,} airline-routes, "
            f"{codeshare_count} codeshare pairs"
        )
        return G

    except Exception as exc:
        logger.error(f"Knowledge graph build failed: {exc}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def init_graph() -> bool:
    """
    Build the knowledge graph from DuckDB. Idempotent — safe to call multiple times.
    Returns True if the graph was built successfully.
    """
    global _graph, _graph_built
    if _graph_built:
        return _graph is not None

    with _graph_lock:
        if _graph_built:
            return _graph is not None

        logger.info("Building airline route knowledge graph …")
        _graph = _build_graph()
        _graph_built = True

        if _graph is not None:
            logger.info("Knowledge graph is ready.")
        else:
            logger.warning("Knowledge graph build failed — graph features disabled.")

        return _graph is not None


def get_graph() -> Optional[nx.MultiDiGraph]:
    """Return the cached graph. Returns None if not yet built or build failed."""
    return _graph


def is_ready() -> bool:
    """True if the graph was built successfully."""
    return _graph_built and _graph is not None


def rebuild_graph() -> bool:
    """Force a full rebuild (use after re-ingestion)."""
    global _graph, _graph_built
    with _graph_lock:
        _graph_built = False
        _graph = None
    return init_graph()
