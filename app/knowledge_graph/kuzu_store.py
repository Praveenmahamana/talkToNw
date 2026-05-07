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
            code           STRING,
            city           STRING,
            country        STRING,
            utc_offset     STRING,
            hub_tier       STRING,
            hub_score      DOUBLE,
            dest_count     INT64,
            airline_count  INT64,
            out_freq       INT64,
            strategic_role STRING,
            description    STRING,
            PRIMARY KEY(code)
        )
    """)
    conn.execute("""
        CREATE NODE TABLE Airline(
            code             STRING,
            name             STRING,
            carrier_type     STRING,
            carrier_subtype  STRING,
            alliance         STRING,
            description      STRING,
            PRIMARY KEY(code)
        )
    """)
    conn.execute("""
        CREATE NODE TABLE Alliance(
            name  STRING,
            PRIMARY KEY(name)
        )
    """)
    conn.execute("""
        CREATE NODE TABLE AircraftType(
            code         STRING,
            total_routes INT64,
            total_freq   INT64,
            operators    INT64,
            PRIMARY KEY(code)
        )
    """)
    conn.execute("""
        CREATE REL TABLE Route(
            FROM Airport TO Airport,
            airline_code       STRING,
            weekly_flights     INT64,
            avg_block_min      INT64,
            aircraft_types     STRING,
            is_codeshare       BOOLEAN,
            operating_airline  STRING,
            marketing_airline  STRING
        )
    """)
    conn.execute("""
        CREATE REL TABLE Operates(
            FROM Airline TO Airport
        )
    """)
    conn.execute("""
        CREATE REL TABLE MemberOf(
            FROM Airline TO Alliance
        )
    """)
    conn.execute("""
        CREATE REL TABLE UsesAircraft(
            FROM Airline TO AircraftType
        )
    """)
    conn.execute("""
        CREATE REL TABLE CodeshareRelation(
            FROM Airline TO Airline
        )
    """)


# ─────────────────────────────────────────────────────────────────────────────
# Bulk loader
# ─────────────────────────────────────────────────────────────────────────────

def _load_data(conn: Any) -> None:
    """Populate Kuzu from the NetworkX graph and entity taxonomy."""
    import pandas as pd
    from app.knowledge_graph.graph_builder import get_graph
    from app.services.workset_service import AIRPORT_INFO, AIRLINE_NAMES, CARRIER_TYPE
    from app.knowledge_graph.rdf_store import ALLIANCE_MAP

    G = get_graph()
    if G is None:
        raise RuntimeError("NetworkX graph not ready — cannot load Kuzu.")

    # ── Fetch entity taxonomy (LM-enriched carrier + aircraft data) ───────────
    taxonomy: dict = {}
    try:
        from app.knowledge_graph.entity_enrichment import build_entity_taxonomy
        taxonomy = build_entity_taxonomy(G)
    except Exception as exc:
        logger.warning(f"Kuzu: entity taxonomy unavailable ({exc}) — loading base data only.")

    lm_carriers       = taxonomy.get("lm_enrichment", {}).get("carriers", {})
    lm_airports_map   = taxonomy.get("airport_enrichment", {}).get("airports", {})
    taxonomy_aircraft = taxonomy.get("aircraft_nodes", [])
    taxonomy_ac_edges = taxonomy.get("aircraft_edges", [])

    # ── Airport nodes ─────────────────────────────────────────────────────────
    ap_rows = []
    for ap, data in G.nodes(data=True):
        info     = AIRPORT_INFO.get(ap, {})
        lm_ap    = lm_airports_map.get(ap, {})
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
            # LM-enriched fields (empty string if not enriched)
            "strategic_role": data.get("strategic_role", lm_ap.get("strategic_role", "")),
            "description":    data.get("description",    lm_ap.get("description", "")),
        })
    ap_df = pd.DataFrame(ap_rows)
    conn.execute("COPY Airport FROM ap_df")
    logger.debug(f"Kuzu: loaded {len(ap_rows)} airports")

    # ── Airline nodes ─────────────────────────────────────────────────────────
    airlines_seen = {d.get("airline") for _, _, d in G.edges(data=True) if d.get("airline")}
    al_rows = []
    for al in airlines_seen:
        lm_info = lm_carriers.get(al, {})
        al_rows.append({
            "code":            al,
            "name":            AIRLINE_NAMES.get(al, al),
            "carrier_type":    CARRIER_TYPE.get(al, "Full-service"),
            # LM-enriched fields
            "carrier_subtype": lm_info.get("type", ""),
            "alliance":        ALLIANCE_MAP.get(al, lm_info.get("alliance", "")),
            "description":     lm_info.get("description", ""),
        })
    al_df = pd.DataFrame(al_rows)
    conn.execute("COPY Airline FROM al_df")
    logger.debug(f"Kuzu: loaded {len(al_rows)} airlines")

    # ── Alliance nodes ────────────────────────────────────────────────────────
    all_alliances: set = set(ALLIANCE_MAP.values())
    for info in lm_carriers.values():
        aln = info.get("alliance", "")
        if aln and aln not in ("None", ""):
            all_alliances.add(aln)
    aln_rows = [{"name": aln} for aln in sorted(all_alliances) if aln]
    if aln_rows:
        aln_df = pd.DataFrame(aln_rows)
        conn.execute("COPY Alliance FROM aln_df")
        logger.debug(f"Kuzu: loaded {len(aln_rows)} alliances")

    # ── AircraftType nodes ────────────────────────────────────────────────────
    if taxonomy_aircraft:
        ac_rows = [
            {
                "code":         n["label"],
                "total_routes": int(n.get("routes", 0)),
                "total_freq":   int(n.get("total_freq", 0)),
                "operators":    int(n.get("operators", 0)),
            }
            for n in taxonomy_aircraft
        ]
        ac_df = pd.DataFrame(ac_rows)
        conn.execute("COPY AircraftType FROM ac_df")
        logger.debug(f"Kuzu: loaded {len(ac_rows)} aircraft types")

    # ── Route edges ───────────────────────────────────────────────────────────
    rt_rows = []
    for origin, dest, data in G.edges(data=True):
        al = data.get("airline", "")
        if not al:
            continue
        rt_rows.append({
            "from":              origin,
            "to":                dest,
            "airline_code":      al,
            "weekly_flights":    int(data.get("unique_flights", 0)),
            "avg_block_min":     int(data.get("avg_block_min", 0)),
            "aircraft_types":    ",".join(data.get("aircraft_types", []) or []),
            "is_codeshare":      bool(data.get("is_codeshare", False)),
            "operating_airline": str(data.get("operating_airline", al)),
            "marketing_airline": str(data.get("marketing_airline", al)),
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

    # ── MemberOf edges (airline → alliance) ──────────────────────────────────
    alliance_names_set = {r["name"] for r in aln_rows} if aln_rows else set()
    member_rows = []
    for al in airlines_seen:
        aln = ALLIANCE_MAP.get(al) or lm_carriers.get(al, {}).get("alliance", "")
        if aln and aln not in ("None", "") and aln in alliance_names_set:
            member_rows.append({"from": al, "to": aln})
    if member_rows:
        member_df = pd.DataFrame(member_rows)
        conn.execute("COPY MemberOf FROM member_df")
        logger.debug(f"Kuzu: loaded {len(member_rows)} member_of edges")

    # ── UsesAircraft edges (airline → aircraft type) ──────────────────────────
    if taxonomy_aircraft and taxonomy_ac_edges:
        ac_codes_set = {n["label"] for n in taxonomy_aircraft}
        ua_rows = []
        seen_pairs: set = set()
        for e in taxonomy_ac_edges:
            al = e["source"].removeprefix("C_")
            ac = e["target"].removeprefix("AC_")
            pair = (al, ac)
            if al in airlines_seen and ac in ac_codes_set and pair not in seen_pairs:
                ua_rows.append({"from": al, "to": ac})
                seen_pairs.add(pair)
        if ua_rows:
            ua_df = pd.DataFrame(ua_rows)
            conn.execute("COPY UsesAircraft FROM ua_df")
            logger.debug(f"Kuzu: loaded {len(ua_rows)} uses_aircraft edges")

    # ── CodeshareRelation edges (marketing airline → operating airline) ────────
    codeshare_pairs = G.graph.get("codeshare_pairs", [])
    airlines_in_kuzu = {row["code"] for row in al_rows}
    cs_rows = []
    seen_cs: set = set()
    for mkt_al, op_al in codeshare_pairs:
        pair = (mkt_al, op_al)
        if mkt_al in airlines_in_kuzu and op_al in airlines_in_kuzu and pair not in seen_cs:
            cs_rows.append({"from": mkt_al, "to": op_al})
            seen_cs.add(pair)
    if cs_rows:
        cs_df = pd.DataFrame(cs_rows)
        conn.execute("COPY CodeshareRelation FROM cs_df")
        logger.debug(f"Kuzu: loaded {len(cs_rows)} codeshare_relation edges")


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
            # Try to delete stale DB; on Windows the dir may be locked — handle gracefully
            dir_existed = Path(db_path).exists()
            if dir_existed:
                shutil.rmtree(db_path, ignore_errors=True)
            # Ensure PARENT directory exists; Kuzu creates db_path itself
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

            dir_still_exists = Path(db_path).exists()
            logger.info(f"Building Kuzu graph database at {db_path} …")
            _db   = kuzu.Database(db_path)
            _conn = kuzu.Connection(_db)

            if dir_still_exists and dir_existed:
                # Deletion failed (Windows file lock) — reuse existing schema + data
                logger.info("Kuzu DB directory could not be removed; reusing existing catalog.")
            else:
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


def find_airlines_in_alliance(alliance: str) -> List[Dict[str, Any]]:
    """Return all airlines that are members of a given alliance."""
    return cypher_query(f"""
        MATCH (al:Airline)-[:MemberOf]->(aln:Alliance {{name: '{alliance}'}})
        RETURN al.code AS airline, al.name AS name,
               al.carrier_type AS carrier_type, al.carrier_subtype AS subtype
        ORDER BY al.name
    """)


def find_aircraft_operators(aircraft_type: str) -> List[Dict[str, Any]]:
    """Return all airlines that operate a given aircraft type."""
    return cypher_query(f"""
        MATCH (al:Airline)-[:UsesAircraft]->(ac:AircraftType {{code: '{aircraft_type}'}})
        RETURN al.code AS airline, al.name AS name, al.carrier_type AS carrier_type
        ORDER BY al.name
    """)


def find_gateway_airports() -> List[Dict[str, Any]]:
    """Return airports classified as Gateway or Hub by LLM enrichment."""
    return cypher_query("""
        MATCH (a:Airport)
        WHERE a.strategic_role IN ['Gateway', 'Hub']
        RETURN a.code AS airport, a.city AS city, a.country AS country,
               a.strategic_role AS role, a.description AS description,
               a.hub_tier AS tier, a.hub_score AS score
        ORDER BY a.hub_score DESC
        LIMIT 30
    """)


def find_codeshare_partners(airline: str) -> List[Dict[str, Any]]:
    """Return airlines that have a codeshare relationship with the given airline."""
    return cypher_query(f"""
        MATCH (al:Airline {{code: '{airline}'}})-[:CodeshareRelation]->(partner:Airline)
        RETURN partner.code AS partner_code, partner.name AS partner_name,
               partner.carrier_type AS carrier_type, partner.alliance AS alliance
        ORDER BY partner.name
    """)


def find_codeshare_routes(airline: str) -> List[Dict[str, Any]]:
    """Return all codeshare routes where this airline is marketing or operating carrier."""
    return cypher_query(f"""
        MATCH (o:Airport)-[r:Route]->(d:Airport)
        WHERE r.is_codeshare = true
          AND (r.marketing_airline = '{airline}' OR r.operating_airline = '{airline}')
        RETURN o.code AS origin, d.code AS dest,
               r.marketing_airline AS marketing, r.operating_airline AS operating,
               r.weekly_flights AS weekly_flights
        ORDER BY o.code, d.code
        LIMIT 50
    """)
