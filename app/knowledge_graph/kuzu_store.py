"""
Kuzu Embeddable Graph Database — property graph persistence + Cypher traversals.

Category: Graph Databases (awesome-knowledge-graph)
Library:  Kuzu (https://kuzudb.com) — embeddable, no server required

Schema:
  NODE  Airport (code, city, country, utc_offset, hub_tier, hub_score,
                 dest_count, airline_count, out_freq)
  NODE  Airline (code, name, carrier_type, alliance)
  REL   Route(FROM Airport TO Airport, airline_code, weekly_flights,
               avg_block_min, aircraft_types)
  REL   Operates(FROM Airline TO Airport)   ← airline present at airport

Provides Cypher traversal queries that the NetworkX layer cannot express:
  - Variable-length path finding (1–3 stops)
  - Pattern matching across airline + airport nodes
  - Aggregation over property graph traversals
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

try:
    import kuzu
    KUZU_AVAILABLE = True
except ImportError:
    KUZU_AVAILABLE = False
    logger.warning("kuzu not installed — graph database disabled.")

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

_KUZU_DIR = Path(os.environ.get("SCHEDAI_DB_PATH", "data/output/schedules.duckdb")).parent / "airline.kuzu"

# ─────────────────────────────────────────────────────────────────────────────
# Singleton
# ─────────────────────────────────────────────────────────────────────────────

_db: Optional[Any] = None
_conn: Optional[Any] = None
_kuzu_lock = threading.Lock()
_kuzu_built = False


# ─────────────────────────────────────────────────────────────────────────────
# Schema creation
# ─────────────────────────────────────────────────────────────────────────────

def _create_schema(conn: Any) -> None:
    """Create node and relationship tables."""
    conn.execute("""
        CREATE NODE TABLE Airport(
            code         STRING,
            city         STRING,
            country      STRING,
            utc_offset   STRING,
            hub_tier     STRING,
            hub_score    DOUBLE,
            dest_count   INT64,
            airline_count INT64,
            out_freq     INT64,
            PRIMARY KEY(code)
        )
    """)
    conn.execute("""
        CREATE NODE TABLE Airline(
            code         STRING,
            name         STRING,
            carrier_type STRING,
            alliance     STRING,
            PRIMARY KEY(code)
        )
    """)
    conn.execute("""
        CREATE REL TABLE Route(
            FROM Airport TO Airport,
            airline_code    STRING,
            weekly_flights  INT64,
            avg_block_min   INT64,
            aircraft_types  STRING
        )
    """)
    conn.execute("""
        CREATE REL TABLE Operates(
            FROM Airline TO Airport
        )
    """)


# ─────────────────────────────────────────────────────────────────────────────
# Bulk loader
# ─────────────────────────────────────────────────────────────────────────────

def _load_data(conn: Any) -> None:
    """Populate Kuzu from the NetworkX graph using pandas DataFrames."""
    import pandas as pd
    from app.knowledge_graph.graph_builder import get_graph
    from app.services.workset_service import AIRPORT_INFO, AIRLINE_NAMES, CARRIER_TYPE
    from app.knowledge_graph.rdf_store import ALLIANCE_MAP

    G = get_graph()
    if G is None:
        raise RuntimeError("NetworkX graph not ready — cannot load Kuzu.")

    # ── Airport nodes ─────────────────────────────────────────────────────────
    ap_rows = []
    for ap, data in G.nodes(data=True):
        info = AIRPORT_INFO.get(ap, {})
        ap_rows.append({
            "code":          ap,
            "city":          info.get("city", ap),
            "country":       info.get("country", ""),
            "utc_offset":    info.get("utc", "unknown"),
            "hub_tier":      data.get("hub_tier", "Point-to-point"),
            "hub_score":     float(data.get("hub_score", 0.0)),
            "dest_count":    int(data.get("dest_count", 0)),
            "airline_count": int(data.get("airline_count", 0)),
            "out_freq":      int(data.get("out_freq", 0)),
        })
    ap_df = pd.DataFrame(ap_rows)
    conn.execute("COPY Airport FROM ap_df")
    logger.debug(f"Kuzu: loaded {len(ap_rows)} airports")

    # ── Airline nodes ─────────────────────────────────────────────────────────
    airlines_seen = {d.get("airline") for _, _, d in G.edges(data=True) if d.get("airline")}
    al_rows = [
        {
            "code":         al,
            "name":         AIRLINE_NAMES.get(al, al),
            "carrier_type": CARRIER_TYPE.get(al, "Full-service"),
            "alliance":     ALLIANCE_MAP.get(al, ""),
        }
        for al in airlines_seen
    ]
    al_df = pd.DataFrame(al_rows)
    conn.execute("COPY Airline FROM al_df")
    logger.debug(f"Kuzu: loaded {len(al_rows)} airlines")

    # ── Route edges ───────────────────────────────────────────────────────────
    rt_rows = []
    for origin, dest, data in G.edges(data=True):
        al = data.get("airline", "")
        if not al:
            continue
        rt_rows.append({
            "from":           origin,
            "to":             dest,
            "airline_code":   al,
            "weekly_flights": int(data.get("unique_flights", 0)),
            "avg_block_min":  int(data.get("avg_block_min", 0)),
            "aircraft_types": ",".join(data.get("aircraft_types", []) or []),
        })
    rt_df = pd.DataFrame(rt_rows)
    conn.execute("COPY Route FROM rt_df")
    logger.debug(f"Kuzu: loaded {len(rt_rows)} route edges")

    # ── Operates edges (airline → airport) ───────────────────────────────────
    ops_map: Dict[str, set] = {}
    for _, v, data in G.edges(data=True):
        al = data.get("airline", "")
        if al:
            ops_map.setdefault(al, set()).add(v)
    for u, _, data in G.edges(data=True):
        al = data.get("airline", "")
        if al:
            ops_map.setdefault(al, set()).add(u)

    ops_rows = [
        {"from": al, "to": ap}
        for al, aps in ops_map.items()
        for ap in aps
    ]
    ops_df = pd.DataFrame(ops_rows)
    conn.execute("COPY Operates FROM ops_df")
    logger.debug(f"Kuzu: loaded {len(ops_rows)} operates edges")


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def init_kuzu() -> bool:
    """Build the Kuzu graph database. Idempotent."""
    global _db, _conn, _kuzu_built
    if _kuzu_built:
        return _conn is not None
    if not KUZU_AVAILABLE:
        return False

    with _kuzu_lock:
        if _kuzu_built:
            return _conn is not None
        try:
            db_path = str(_KUZU_DIR)
            # Always rebuild fresh to stay in sync with DuckDB
            if Path(db_path).exists():
                shutil.rmtree(db_path, ignore_errors=True)
            # Ensure PARENT directory exists; Kuzu creates db_path itself
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

            logger.info(f"Building Kuzu graph database at {db_path} …")
            _db   = kuzu.Database(db_path)
            _conn = kuzu.Connection(_db)
            _create_schema(_conn)
            _load_data(_conn)
            _kuzu_built = True
            logger.info("Kuzu graph database ready.")
        except Exception as exc:
            logger.error(f"Kuzu init failed: {exc}")
            _db = _conn = None
            _kuzu_built = True  # mark as attempted to prevent retry loops
    return _conn is not None


def get_conn() -> Optional[Any]:
    return _conn


def is_kuzu_ready() -> bool:
    return _kuzu_built and _conn is not None


def rebuild_kuzu() -> bool:
    global _db, _conn, _kuzu_built
    with _kuzu_lock:
        _kuzu_built = False
        _db = _conn = None
    return init_kuzu()


def cypher_query(query: str) -> List[Dict[str, Any]]:
    """
    Execute a Cypher query and return results as a list of dicts.
    Column names are inferred from the RETURN clause aliases where possible.
    """
    if not is_kuzu_ready():
        return [{"error": "Kuzu graph database not ready."}]
    try:
        result = _conn.execute(query)
        df = result.get_as_df()
        return df.to_dict("records")
    except Exception as exc:
        return [{"error": str(exc), "query_fragment": query[:200]}]


# ─────────────────────────────────────────────────────────────────────────────
# Pre-built Cypher queries
# ─────────────────────────────────────────────────────────────────────────────

def find_direct_routes(origin: str, dest: str) -> List[Dict[str, Any]]:
    """Return all direct routes origin→dest with airline details."""
    return cypher_query(f"""
        MATCH (a:Airport {{code: '{origin}'}})-[r:Route]->(b:Airport {{code: '{dest}'}})
        RETURN r.airline_code AS airline,
               r.weekly_flights AS weekly_flights,
               r.avg_block_min  AS avg_block_min,
               r.aircraft_types AS aircraft_types
        ORDER BY r.weekly_flights DESC
    """)


def find_paths_via_hub(origin: str, dest: str, max_hops: int = 2) -> List[Dict[str, Any]]:
    """
    Find paths between two airports via up to max_hops intermediate stops.
    Uses Kuzu's variable-length path matching.
    """
    if max_hops < 1 or max_hops > 3:
        max_hops = 2
    return cypher_query(f"""
        MATCH (a:Airport {{code: '{origin}'}})-[:Route*1..{max_hops}]->(b:Airport {{code: '{dest}'}})
        RETURN a.code AS origin, b.code AS dest
        LIMIT 10
    """)


def find_connecting_airports(origin: str, dest: str) -> List[Dict[str, Any]]:
    """
    Find airports that have direct routes to both origin and dest
    (classic 1-stop hub connectivity).
    """
    return cypher_query(f"""
        MATCH (a:Airport {{code: '{origin}'}})-[:Route]->(h:Airport)<-[:Route]-(b:Airport {{code: '{dest}'}})
        RETURN DISTINCT h.code AS hub, h.hub_tier AS tier, h.hub_score AS score, h.city AS city
        ORDER BY h.hub_score DESC
        LIMIT 10
    """)


def find_hub_neighbors(airport: str, top_n: int = 20) -> List[Dict[str, Any]]:
    """Return top outbound neighbors of an airport by weekly frequency."""
    return cypher_query(f"""
        MATCH (a:Airport {{code: '{airport}'}})-[r:Route]->(b:Airport)
        RETURN b.code AS dest, b.city AS dest_city, b.hub_tier AS dest_tier,
               SUM(r.weekly_flights) AS weekly_flights
        ORDER BY weekly_flights DESC
        LIMIT {top_n}
    """)


def find_airline_hubs(airline: str) -> List[Dict[str, Any]]:
    """Return primary hub airports for an airline (most departures)."""
    return cypher_query(f"""
        MATCH (al:Airline {{code: '{airline}'}})-[:Operates]->(a:Airport)
        MATCH (a)-[r:Route]->(:Airport)
        WHERE r.airline_code = '{airline}'
        RETURN a.code AS airport, a.city AS city, a.hub_tier AS tier,
               SUM(r.weekly_flights) AS departures
        ORDER BY departures DESC
        LIMIT 5
    """)


def find_airlines_at_airport(airport: str) -> List[Dict[str, Any]]:
    """Return all airlines operating at an airport with departure counts."""
    return cypher_query(f"""
        MATCH (al:Airline)-[:Operates]->(a:Airport {{code: '{airport}'}})
        RETURN al.code AS airline, al.name AS name, al.carrier_type AS type,
               al.alliance AS alliance
        ORDER BY al.name
        LIMIT 30
    """)


def find_routes_between_hubs(hub1: str, hub2: str) -> List[Dict[str, Any]]:
    """Return all direct routes between two hub airports (both directions)."""
    return cypher_query(f"""
        MATCH (a:Airport {{code: '{hub1}'}})-[r:Route]->(b:Airport {{code: '{hub2}'}})
        RETURN r.airline_code AS airline, r.weekly_flights AS weekly, r.avg_block_min AS block_min
        UNION ALL
        MATCH (a:Airport {{code: '{hub2}'}})-[r:Route]->(b:Airport {{code: '{hub1}'}})
        RETURN r.airline_code AS airline, r.weekly_flights AS weekly, r.avg_block_min AS block_min
        ORDER BY weekly DESC
    """)
