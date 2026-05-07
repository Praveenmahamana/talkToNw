"""
Workset Knowledge Graph Builder
================================
Builds a full spec-compliant knowledge graph from BASEDATA (workset_base)
and SPILLDATA (workset_spill) per the domain definitions in app/domain_definitions.py.

Node types:   AIRPORT | LEG | MARKET | ITINERARY
Edge types:   DEPARTS_ON | ARRIVES_AT | HAS_ITINERARY | USES_LEG | FLOW_TO | FLOW_THROUGH | MARKET_FLOW

Generates all 7 output tables:
  1. itinerary_table
  2. od_leg_contribution_matrix
  3. leg_flow_summary
  4. market_summary
  5. airport_board_deboard_summary
  6. graph_nodes
  7. graph_edges

This module is the authoritative implementation of the OD→Leg Contribution
Matrix algorithm and KG Flow Edges (⚡) spec.

Column-name mapping (DuckDB → spec):
  workset_base:  record_id → baseIndex, dest → destin, flight_num → flt_num
  workset_spill: market_origin → origin, market_dest → destin
                 max 3 legs (baseIndex_l1, l2, l3) in this dataset
"""

from __future__ import annotations

import threading
import math
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd
import networkx as nx
from loguru import logger

from app.domain_definitions import (
    build_base_lookup,
    get_itinerary_base_indexes,
    choose_traffic,
    build_od_leg_contribution_matrix,
    build_airport_board_deboard_summary,
    build_graph_edges,
    TRAFFIC_COLUMNS,
)
from app.services.workset_service import AIRPORT_INFO


# ─────────────────────────────────────────────────────────────────────────────
# Singletons
# ─────────────────────────────────────────────────────────────────────────────

_lock   = threading.Lock()
_built  = False
_error: Optional[str] = None

# 7 output tables
_itinerary_table:              Optional[pd.DataFrame] = None
_od_leg_contribution_matrix:   Optional[pd.DataFrame] = None
_leg_flow_summary:             Optional[pd.DataFrame] = None
_market_summary:               Optional[pd.DataFrame] = None
_airport_board_deboard_summary: Optional[pd.DataFrame] = None
_graph_nodes:                  Optional[pd.DataFrame] = None
_graph_edges_df:               Optional[pd.DataFrame] = None

# Full NetworkX property graph
_wkg: Optional[nx.MultiDiGraph] = None

# ─────────────────────────────────────────────────────────────────────────────
# Live build-progress tracking  (polled by SSE endpoint)
# ─────────────────────────────────────────────────────────────────────────────

import time as _time

_build_progress: Dict[str, Any] = {
    "phase":       "idle",   # idle | building | od_matrix | derived | graph_nodes | graph_edges | networkx | done | error
    "phase_label": "Not started",
    "percent":     0,
    "base_legs":   0,
    "spill_rows":  0,
    "od_rows":     0,
    "market_count":  0,
    "node_count":    0,
    "edge_count":    0,
    "flow_edges":    0,
    "airport_count": 0,
    "error_msg":   None,
    "ts":          _time.time(),
}

# Visualization chunks queued for the SSE endpoint to stream to the splash canvas.
# Each entry: {"type": "airports"|"edges", "data": [...]}
# Appended by the build thread; popped one-at-a-time by the SSE async generator.
# Python list.pop(0) is GIL-safe for single-consumer, single-producer usage.
_viz_chunks: List[Dict[str, Any]] = []


def _emit(phase: str, label: str, pct: int, **extra):
    _build_progress.update({"phase": phase, "phase_label": label, "percent": pct,
                            "ts": _time.time(), **extra})
    logger.info(f"workset_graph [%{pct:3d}] {label}")


def get_build_progress() -> Dict[str, Any]:
    """Return a snapshot of the current build progress (thread-safe copy)."""
    return dict(_build_progress)


def pop_viz_chunk() -> Optional[Dict[str, Any]]:
    """Pop the oldest queued visualization chunk (GIL-safe single-consumer pop)."""
    if _viz_chunks:
        return _viz_chunks.pop(0)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# DuckDB → domain DataFrame bridge
# ─────────────────────────────────────────────────────────────────────────────

def _load_basedata() -> pd.DataFrame:
    """
    Load workset_base from DuckDB and rename columns to domain spec names.
    workset_base per-day rows aggregated to per-flight averages to avoid
    inflating the OD contribution by day_of_week multiplicity.

    Opens a DEDICATED connection using the same read_only=False mode as the
    singleton — DuckDB allows multiple connections within the same process
    as long as they share the same configuration.
    """
    import duckdb
    from app.database.db import get_db_path
    db_path = get_db_path()

    conn = None
    try:
        conn = duckdb.connect(database=db_path, read_only=False)
        # Aggregate per unique flight leg (record_id) across all operating days
        df = conn.execute("""
            SELECT
                record_id                       AS baseIndex,
                origin,
                dest                            AS destin,
                flight_num                      AS flt_num,
                dep_time                        AS dept_time,
                arr_time                        AS arrv_time,
                AVG(CAST(apm_cap  AS FLOAT))    AS apm_cap,
                AVG(CAST(apm_dmd  AS FLOAT))    AS apm_dmd,
                AVG(CAST(apm_pax  AS FLOAT))    AS apm_pax,
                AVG(CAST(apm_lpax AS FLOAT))    AS apm_lpax,
                AVG(CAST(apm_spill AS FLOAT))   AS apm_spill,
                mkt_airline                     AS flt_airline
            FROM workset_base
            WHERE record_id IS NOT NULL
            GROUP BY record_id, origin, dest, flight_num, dep_time, arr_time, mkt_airline
        """).df()
        logger.info(f"workset_graph: loaded {len(df):,} baseIndex legs from workset_base")
        return df
    except Exception as exc:
        logger.warning(f"workset_graph: could not load workset_base — {exc}")
        return pd.DataFrame()
    finally:
        if conn:
            try: conn.close()
            except Exception: pass


def _load_spilldata() -> pd.DataFrame:
    """
    Load workset_spill from DuckDB and rename columns to domain spec names.
    workset_spill rows are per-itinerary-per-day — aggregate to unique itineraries.

    Opens a DEDICATED connection using the same read_only=False mode as the
    singleton — DuckDB allows multiple connections within the same process
    as long as they share the same configuration.
    """
    import duckdb
    from app.database.db import get_db_path
    db_path = get_db_path()

    conn = None
    try:
        conn = duckdb.connect(database=db_path, read_only=False)
        # Aggregate per unique itinerary (market_origin, market_dest, baseIndex_l1/l2/l3, day)
        # Summing traffic across all days gives weekly totals.
        df = conn.execute("""
            SELECT
                market_origin               AS origin,
                market_dest                 AS destin,
                baseIndex_l1,
                baseIndex_l2,
                baseIndex_l3,
                stops,
                airline,
                is_codeshare,
                SUM(COALESCE(traffic_HO,0)) AS traffic_HO,
                SUM(COALESCE(traffic_LO,0)) AS traffic_LO,
                SUM(COALESCE(traffic_HR,0)) AS traffic_HR,
                SUM(COALESCE(traffic_LR,0)) AS traffic_LR,
                SUM(COALESCE(dmd_HO,0))     AS dmd_HO,
                SUM(COALESCE(dmd_LO,0))     AS dmd_LO,
                SUM(COALESCE(dmd_HR,0))     AS dmd_HR,
                SUM(COALESCE(dmd_LR,0))     AS dmd_LR,
                SUM(COALESCE(spill_HO,0))   AS spill_HO,
                SUM(COALESCE(spill_LO,0))   AS spill_LO,
                SUM(COALESCE(spill_HR,0))   AS spill_HR,
                SUM(COALESCE(spill_LR,0))   AS spill_LR,
                SUM(COALESCE(total_demand,0))AS total_demand,
                SUM(COALESCE(total_pax,0))  AS total_pax,
                SUM(COALESCE(total_spill,0))AS total_spill
            FROM workset_spill
            WHERE market_origin IS NOT NULL
              AND market_dest   IS NOT NULL
              AND is_codeshare  = 0
            GROUP BY market_origin, market_dest, baseIndex_l1, baseIndex_l2,
                     baseIndex_l3, stops, airline, is_codeshare
        """).df()

        logger.info(f"workset_graph: loaded {len(df):,} unique itineraries from workset_spill")
        return df
    except Exception as exc:
        logger.warning(f"workset_graph: could not load workset_spill — {exc}")
        return pd.DataFrame()
    finally:
        if conn:
            try: conn.close()
            except Exception: pass


# ─────────────────────────────────────────────────────────────────────────────
# Derived table builders
# ─────────────────────────────────────────────────────────────────────────────

def _build_itinerary_table(od_matrix: pd.DataFrame) -> pd.DataFrame:
    """Roll up od_leg_contribution_matrix to one row per (market_od, route)."""
    if od_matrix.empty:
        return pd.DataFrame()
    return (
        od_matrix
        .groupby(["market_od", "market_origin", "market_destin", "route", "itin_type"], as_index=False)
        .agg(
            num_legs=("leg_seq", "max"),
            traffic=("traffic", "first"),
        )
    )


def _build_leg_flow_summary(od_matrix: pd.DataFrame) -> pd.DataFrame:
    """Aggregate contributions per leg across all contributing market ODs."""
    if od_matrix.empty:
        return pd.DataFrame()
    local_mask = od_matrix["itin_type"] == "LOCAL"
    flow_mask  = od_matrix["itin_type"] == "FLOW"

    agg = od_matrix.groupby(["baseIndex", "leg_od", "flt_num"], as_index=False).agg(
        total_od_contribution=("traffic", "sum"),
        contributing_markets=("market_od", "nunique"),
    )
    local_agg = (
        od_matrix[local_mask]
        .groupby("baseIndex", as_index=False)["traffic"].sum()
        .rename(columns={"traffic": "local_contribution"})
    )
    flow_agg = (
        od_matrix[flow_mask]
        .groupby("baseIndex", as_index=False)["traffic"].sum()
        .rename(columns={"traffic": "flow_contribution"})
    )

    result = agg.merge(local_agg, on="baseIndex", how="left") \
                .merge(flow_agg,  on="baseIndex", how="left")
    result["local_contribution"] = result["local_contribution"].fillna(0)
    result["flow_contribution"]  = result["flow_contribution"].fillna(0)
    return result


def _build_market_summary(od_matrix: pd.DataFrame, spilldata: pd.DataFrame) -> pd.DataFrame:
    """Market-level totals."""
    if od_matrix.empty:
        return pd.DataFrame()

    itin_counts = (
        od_matrix.drop_duplicates(["market_od", "route"])
        .groupby("market_od", as_index=False)
        .agg(
            itinerary_count=("route", "count"),
            local_itin_count=("itin_type", lambda x: (x == "LOCAL").sum()),
            flow_itin_count=("itin_type",  lambda x: (x == "FLOW").sum()),
        )
    )
    traffic_totals = (
        od_matrix.drop_duplicates(["market_od", "route"])
        .groupby("market_od", as_index=False)
        .agg(total_traffic=("traffic", "sum"))
    )
    return itin_counts.merge(traffic_totals, on="market_od", how="left")


def _build_graph_nodes(
    od_matrix: pd.DataFrame,
    base_lookup: Dict[int, Dict],
    basedata: pd.DataFrame,
) -> pd.DataFrame:
    """Build graph_nodes table with all 4 node types."""
    rows: List[Dict] = []

    # AIRPORT nodes
    airports: Set[str] = set()
    if not od_matrix.empty:
        airports.update(od_matrix["leg_origin"].dropna())
        airports.update(od_matrix["leg_destin"].dropna())
    for ap in airports:
        info = AIRPORT_INFO.get(ap, {})
        rows.append({
            "node_id":   ap,
            "node_type": "AIRPORT",
            "label":     f"{ap} — {info.get('city', ap)}",
            "properties": str({
                "airport_code": ap,
                "city":    info.get("city", ap),
                "country": info.get("country", ""),
                "region":  info.get("utc", ""),
            }),
        })

    # LEG nodes
    for bi, leg in base_lookup.items():
        rows.append({
            "node_id":   f"LEG_{bi}",
            "node_type": "LEG",
            "label":     f"LEG {bi}: {leg['leg_od']} {leg.get('flt_num','')}",
            "properties": str({
                "baseIndex":         bi,
                "origin":            leg["origin"],
                "destin":            leg["destin"],
                "flt_num":           leg.get("flt_num"),
                "dept_time":         str(leg.get("dept_time")),
                "arrv_time":         str(leg.get("arrv_time")),
                "apm_cap_pred":      leg.get("apm_cap"),
                "apm_pax_pred":      leg.get("apm_pax"),
                "apm_lpax_pred":     leg.get("apm_lpax"),
                "apm_flow_pax_pred": leg.get("apm_flow_pax"),
            }),
        })

    if od_matrix.empty:
        return pd.DataFrame(rows)

    # MARKET nodes
    for market_od, grp in od_matrix.drop_duplicates(["market_od", "route"]).groupby("market_od"):
        origin, destin = grp["market_origin"].iloc[0], grp["market_destin"].iloc[0]
        rows.append({
            "node_id":   f"MKT_{origin}_{destin}",
            "node_type": "MARKET",
            "label":     f"Market {market_od}",
            "properties": str({
                "origin":       origin,
                "destin":       destin,
                "market_od":    market_od,
                "total_traffic": round(grp["traffic"].sum(), 1),
            }),
        })

    # ITINERARY nodes
    for (market_od, route), grp in od_matrix.drop_duplicates(["market_od", "route", "itin_type"]).groupby(["market_od", "route"]):
        itin_type = grp["itin_type"].iloc[0]
        num_legs  = int(grp["leg_seq"].max())
        origin    = grp["market_origin"].iloc[0]
        destin    = grp["market_destin"].iloc[0]
        node_id   = "ITIN_" + route.replace("->", "_")
        rows.append({
            "node_id":   node_id,
            "node_type": "ITINERARY",
            "label":     f"Itin {route}",
            "properties": str({
                "market_od": market_od,
                "route":     route,
                "itin_type": itin_type,
                "num_legs":  num_legs,
                "traffic":   round(float(grp["traffic"].iloc[0]), 1),
            }),
        })

    return pd.DataFrame(rows)


def _build_full_graph_edges(od_matrix: pd.DataFrame, base_lookup: Dict) -> pd.DataFrame:
    """Combine structural edges + flow edges into one graph_edges DataFrame. Vectorized."""
    _NULL = {"market_od": None, "route": None, "traffic": None, "seq": None}

    # ── DEPARTS_ON / ARRIVES_AT (AIRPORT ↔ LEG) ─────────────────────────────
    # Build from base_lookup — one DEPARTS_ON + one ARRIVES_AT per leg
    if base_lookup:
        bl_df = pd.DataFrame(base_lookup.values())
        departs = pd.DataFrame({
            "source":    bl_df["origin"],
            "target":    "LEG_" + bl_df["baseIndex"].astype(str),
            "edge_type": "DEPARTS_ON",
            **_NULL,
        })
        arrives = pd.DataFrame({
            "source":    "LEG_" + bl_df["baseIndex"].astype(str),
            "target":    bl_df["destin"],
            "edge_type": "ARRIVES_AT",
            **_NULL,
        })
        structural = pd.concat([departs, arrives], ignore_index=True)
    else:
        structural = pd.DataFrame(columns=["source", "target", "edge_type", "market_od", "route", "traffic", "seq"])

    if od_matrix.empty:
        return structural

    # ── HAS_ITINERARY (MARKET → ITINERARY) ──────────────────────────────────
    itin_uniq = od_matrix.drop_duplicates(["market_od", "route"])[
        ["market_od", "route", "market_origin", "market_destin"]
    ].copy()
    itin_uniq["mkt_id"]  = "MKT_"  + itin_uniq["market_origin"] + "_" + itin_uniq["market_destin"]
    itin_uniq["itin_id"] = "ITIN_" + itin_uniq["route"].str.replace("->", "_", regex=False)
    has_itin = itin_uniq[["mkt_id", "itin_id", "market_od", "route"]].rename(
        columns={"mkt_id": "source", "itin_id": "target"}
    )
    has_itin["edge_type"] = "HAS_ITINERARY"
    has_itin["traffic"]   = None
    has_itin["seq"]       = None

    # ── USES_LEG (ITINERARY → LEG) ──────────────────────────────────────────
    uses = od_matrix[["market_od", "route", "baseIndex", "traffic", "leg_seq"]].copy()
    uses["source"]    = "ITIN_" + uses["route"].str.replace("->", "_", regex=False)
    uses["target"]    = "LEG_"  + uses["baseIndex"].astype(str)
    uses["edge_type"] = "USES_LEG"
    uses_leg = uses[["source", "target", "edge_type", "market_od", "route", "traffic", "leg_seq"]].rename(
        columns={"leg_seq": "seq"}
    )

    # ── FLOW_THROUGH / FLOW_TO / MARKET_FLOW (from domain function) ─────────
    flow_df = build_graph_edges(od_matrix)
    flow_df["seq"] = None

    return pd.concat([structural, has_itin, uses_leg, flow_df], ignore_index=True)


def _build_graph_nodes(
    od_matrix: pd.DataFrame,
    base_lookup: Dict[int, Dict],
    basedata: pd.DataFrame,
) -> pd.DataFrame:
    """Build graph_nodes table with all 4 node types. Vectorized."""
    frames: List[pd.DataFrame] = []

    # ── AIRPORT nodes ────────────────────────────────────────────────────────
    if not od_matrix.empty:
        airports = pd.unique(
            pd.concat([od_matrix["leg_origin"], od_matrix["leg_destin"]]).dropna()
        )
    else:
        airports = []
    ap_rows = [
        {
            "node_id":    ap,
            "node_type":  "AIRPORT",
            "label":      f"{ap} — {AIRPORT_INFO.get(ap, {}).get('city', ap)}",
            "properties": str({
                "airport_code": ap,
                "city":    AIRPORT_INFO.get(ap, {}).get("city", ap),
                "country": AIRPORT_INFO.get(ap, {}).get("country", ""),
                "region":  AIRPORT_INFO.get(ap, {}).get("utc", ""),
            }),
        }
        for ap in airports
    ]
    if ap_rows:
        frames.append(pd.DataFrame(ap_rows))

    # ── LEG nodes ────────────────────────────────────────────────────────────
    if base_lookup:
        bl = pd.DataFrame(base_lookup.values())
        leg_df = pd.DataFrame({
            "node_id":   "LEG_" + bl["baseIndex"].astype(str),
            "node_type": "LEG",
            "label":     "LEG " + bl["baseIndex"].astype(str) + ": " + bl["leg_od"].astype(str),
            "properties": bl.apply(lambda r: str({
                "baseIndex": r["baseIndex"],
                "origin":    r["origin"],
                "destin":    r["destin"],
                "flt_num":   r.get("flt_num"),
            }), axis=1),
        })
        frames.append(leg_df)

    if od_matrix.empty:
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    # ── MARKET nodes ─────────────────────────────────────────────────────────
    mkt_uniq = od_matrix.drop_duplicates("market_od")[
        ["market_od", "market_origin", "market_destin"]
    ].copy()
    mkt_traffic = (
        od_matrix.drop_duplicates(["market_od", "route"])
        .groupby("market_od")["traffic"].sum()
        .reset_index()
        .rename(columns={"traffic": "total_traffic"})
    )
    mkt_uniq = mkt_uniq.merge(mkt_traffic, on="market_od", how="left")
    mkt_df = pd.DataFrame({
        "node_id":   "MKT_" + mkt_uniq["market_origin"] + "_" + mkt_uniq["market_destin"],
        "node_type": "MARKET",
        "label":     "Market " + mkt_uniq["market_od"],
        "properties": mkt_uniq.apply(lambda r: str({
            "origin": r["market_origin"], "destin": r["market_destin"],
            "market_od": r["market_od"], "total_traffic": round(r.get("total_traffic", 0), 1),
        }), axis=1),
    })
    frames.append(mkt_df)

    # ── ITINERARY nodes ──────────────────────────────────────────────────────
    itin_uniq = od_matrix.drop_duplicates(["market_od", "route"])[
        ["market_od", "route", "itin_type", "market_origin", "market_destin", "traffic"]
    ].copy()
    n_legs = (
        od_matrix.groupby(["market_od", "route"])["leg_seq"].max()
        .reset_index().rename(columns={"leg_seq": "num_legs"})
    )
    itin_uniq = itin_uniq.merge(n_legs, on=["market_od", "route"], how="left")
    itin_df = pd.DataFrame({
        "node_id":   "ITIN_" + itin_uniq["route"].str.replace("->", "_", regex=False),
        "node_type": "ITINERARY",
        "label":     "Itin " + itin_uniq["route"],
        "properties": itin_uniq.apply(lambda r: str({
            "market_od": r["market_od"], "route": r["route"],
            "itin_type": r["itin_type"], "num_legs": int(r.get("num_legs", 1)),
            "traffic": round(float(r["traffic"]), 1),
        }), axis=1),
    })
    frames.append(itin_df)

    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

def _build_networkx_graph(
    graph_nodes_df: pd.DataFrame,
    graph_edges_df: pd.DataFrame,
) -> nx.MultiDiGraph:
    """Construct NetworkX property graph from node/edge DataFrames.
    Only includes AIRPORT and MARKET nodes + flow edges for graph-level queries;
    LEG/ITINERARY nodes are too numerous for efficient in-memory traversal.
    """
    G: nx.MultiDiGraph = nx.MultiDiGraph()

    # Only add AIRPORT + MARKET nodes to keep the graph tractable
    slim_nodes = graph_nodes_df[
        graph_nodes_df["node_type"].isin(["AIRPORT", "MARKET"])
    ]
    for row in slim_nodes.itertuples(index=False):
        G.add_node(
            row.node_id,
            node_type=row.node_type,
            label=getattr(row, "label", ""),
        )

    # Only add FLOW_THROUGH + MARKET_FLOW edges (airport-to-airport)
    slim_edges = graph_edges_df[
        graph_edges_df["edge_type"].isin(["FLOW_THROUGH", "MARKET_FLOW"])
    ]
    for row in slim_edges.itertuples(index=False):
        G.add_edge(
            row.source, row.target,
            key=row.edge_type,
            edge_type=row.edge_type,
            market_od=getattr(row, "market_od", None),
            traffic=getattr(row, "traffic", None),
        )

    return G


# ─────────────────────────────────────────────────────────────────────────────
# Main build entry point
# ─────────────────────────────────────────────────────────────────────────────

def _build(basedata: pd.DataFrame, spilldata: pd.DataFrame) -> bool:
    """
    Core build logic.  All DuckDB reads happen BEFORE this call (in the calling
    thread), so this function is purely CPU/pandas — no DB access.
    """
    global _itinerary_table, _od_leg_contribution_matrix, _leg_flow_summary
    global _market_summary, _airport_board_deboard_summary
    global _graph_nodes, _graph_edges_df, _wkg, _error

    try:
        _emit("building", "Loading base leg data…", 5,
              base_legs=len(basedata), spill_rows=len(spilldata))

        if basedata.empty:
            _error = "workset_base not loaded — run workset ingestion first"
            _emit("error", _error, 0, error_msg=_error)
            logger.warning(f"workset_graph: {_error}")
            return False

        base_lookup = build_base_lookup(basedata)
        _emit("building", f"Base lookup ready — {len(base_lookup):,} legs", 8,
              base_legs=len(base_lookup))

        if spilldata.empty:
            _error = "workset_spill not loaded — run workset ingestion first"
            _emit("error", _error, 0, error_msg=_error)
            logger.warning(f"workset_graph: {_error}")
            return False

        # ── 3. OD→Leg contribution matrix ───────────────────────────────────
        _emit("od_matrix",
              f"Building OD→Leg contribution matrix… ({len(spilldata):,} itineraries × {len(base_lookup):,} legs)",
              10)
        od_matrix = build_od_leg_contribution_matrix(
            spilldata, basedata,
            traffic_cols=TRAFFIC_COLUMNS["non_cabin"],  # HO/LO/HR/LR
        )
        _emit("od_matrix", f"OD→Leg matrix complete — {len(od_matrix):,} rows", 45,
              od_rows=len(od_matrix))

        # ── 4. Derived tables ───────────────────────────────────────────────
        _emit("derived", "Building itinerary & market summary tables…", 50)
        _itinerary_table            = _build_itinerary_table(od_matrix)
        _emit("derived", f"Itinerary table done — {len(_itinerary_table):,} rows", 55)
        _leg_flow_summary           = _build_leg_flow_summary(od_matrix)
        _market_summary             = _build_market_summary(od_matrix, spilldata)
        _airport_board_deboard_summary = build_airport_board_deboard_summary(od_matrix)

        _emit("derived",
              f"Derived tables — markets={len(_market_summary):,}  airports={len(_airport_board_deboard_summary):,}",
              60, market_count=len(_market_summary),
              airport_count=len(_airport_board_deboard_summary))

        # ── 5. Graph nodes + edges DataFrames ───────────────────────────────
        _od_leg_contribution_matrix = od_matrix
        _emit("graph_nodes", f"Building graph nodes (AIRPORT/LEG/MARKET/ITINERARY)…", 63)
        _graph_nodes    = _build_graph_nodes(od_matrix, base_lookup, basedata)
        _emit("graph_nodes", f"Graph nodes — {len(_graph_nodes):,} nodes", 70,
              node_count=len(_graph_nodes))

        _emit("graph_edges", "Building graph edges (FLOW_THROUGH/MARKET_FLOW/USES_LEG…)…", 72)
        _graph_edges_df = _build_full_graph_edges(od_matrix, base_lookup)

        flow_ct = int((_graph_edges_df["edge_type"] == "FLOW_THROUGH").sum()) if not _graph_edges_df.empty else 0
        _emit("graph_edges",
              f"Graph edges — {len(_graph_edges_df):,} total  ({flow_ct:,} FLOW_THROUGH)", 82,
              edge_count=len(_graph_edges_df), flow_edges=flow_ct)

        # ── Queue visualization chunks for SSE → splash canvas ───────────────
        # Dynamically take 25% of each node type in the workset
        if _airport_board_deboard_summary is not None and not _airport_board_deboard_summary.empty:
            BATCH = 120
            total_airports = len(_airport_board_deboard_summary)
            n_airports     = max(5, int(total_airports * 0.25))   # 25% of workset airports

            top_airports = (
                _airport_board_deboard_summary
                .assign(_mv=lambda d: d["boarded"] + d["deboarded"])
                .nlargest(n_airports, "_mv")["station"]
                .tolist()
            )
            top_set = set(top_airports)
            logger.info(f"viz-chunks: {n_airports}/{total_airports} airports (25% of workset)")

            # 1. Airport nodes
            _viz_chunks.append({"type": "airports", "data": [{"iata": a} for a in top_airports]})

            # 2. FLOW_THROUGH edges between selected airports
            if not _graph_edges_df.empty:
                ft_df = (
                    _graph_edges_df[
                        (_graph_edges_df["edge_type"] == "FLOW_THROUGH") &
                        (_graph_edges_df["source"].isin(top_set)) &
                        (_graph_edges_df["target"].isin(top_set))
                    ]
                    .groupby(["source", "target", "edge_type"], as_index=False)
                    .agg(traffic=("traffic", "sum"))
                )
                records = ft_df.rename(columns={"source": "src", "target": "dst"}).to_dict("records")
                for i in range(0, len(records), BATCH):
                    _viz_chunks.append({"type": "edges", "data": records[i: i + BATCH]})

            if not od_matrix.empty:
                # 3. LEG nodes — 25% of unique legs in workset, from legs within selected airports
                total_legs = int(od_matrix["baseIndex"].nunique())
                n_legs     = min(max(10, int(total_legs * 0.25)), 600)  # cap at 600 for D3 perf
                top_legs = (
                    od_matrix[
                        od_matrix["leg_origin"].isin(top_set) &
                        od_matrix["leg_destin"].isin(top_set)
                    ]
                    .groupby(["baseIndex", "leg_origin", "leg_destin", "flt_num"], as_index=False)
                    .agg(total_pax=("traffic", "sum"))
                    .nlargest(n_legs, "total_pax")
                )
                logger.info(f"viz-chunks: {len(top_legs)}/{total_legs} legs (25% of workset, capped 600)")
                leg_records = [
                    {
                        "id": f"LEG_{int(r.baseIndex)}",
                        "baseIndex": int(r.baseIndex),
                        "origin": r.leg_origin,
                        "destin": r.leg_destin,
                        "flt_num": str(r.flt_num),
                        "pax": round(float(r.total_pax), 0),
                    }
                    for r in top_legs.itertuples()
                ]
                if leg_records:
                    _viz_chunks.append({"type": "leg_nodes", "data": leg_records})

                # 4. MARKET nodes — 25% of unique markets in workset
                total_markets = int(od_matrix["market_od"].nunique())
                n_markets     = min(max(5, int(total_markets * 0.25)), 300)
                mkt_traffic = (
                    od_matrix
                    .groupby(["market_od", "market_origin", "market_destin"], as_index=False)
                    .agg(total_traffic=("traffic", "sum"))
                    .nlargest(n_markets, "total_traffic")
                )
                logger.info(f"viz-chunks: {len(mkt_traffic)}/{total_markets} markets (25% of workset, capped 300)")
                mkt_records = [
                    {
                        "id": f"MKT_{r.market_origin}_{r.market_destin}",
                        "market_od": r.market_od,
                        "origin": r.market_origin,
                        "destin": r.market_destin,
                        "traffic": round(float(r.total_traffic), 0),
                    }
                    for r in mkt_traffic.itertuples()
                ]
                if mkt_records:
                    _viz_chunks.append({"type": "market_nodes", "data": mkt_records})

                # 5. ITINERARY nodes — 25% of unique itineraries in workset
                total_itins = int(od_matrix.drop_duplicates(["market_od", "route"]).shape[0])
                n_itins     = min(max(10, int(total_itins * 0.25)), 500)
                top_mkt_ods = {r["market_od"] for r in mkt_records}
                itin_df = (
                    od_matrix[od_matrix["market_od"].isin(top_mkt_ods)]
                    .drop_duplicates(["market_od", "route"])
                    .nlargest(n_itins, "traffic")
                )
                logger.info(f"viz-chunks: {len(itin_df)}/{total_itins} itineraries (25% of workset, capped 500)")
                itin_records = [
                    {
                        "id": "ITIN_" + r.route.replace("->", "_"),
                        "market_od": r.market_od,
                        "route": r.route,
                        "itin_type": r.itin_type,
                        "market_origin": r.market_origin,
                        "market_destin": r.market_destin,
                        "traffic": round(float(r.traffic), 0),
                    }
                    for r in itin_df.itertuples()
                ]
                if itin_records:
                    _viz_chunks.append({"type": "itin_nodes", "data": itin_records})

                # 6. Structural edges (DEPARTS_ON / ARRIVES_AT / HAS_ITINERARY / USES_LEG)
                #    scoped to the sampled nodes only
                if not _graph_edges_df.empty:
                    leg_ids  = {r["id"] for r in leg_records}
                    mkt_ids  = {r["id"] for r in mkt_records}
                    itin_ids = {r["id"] for r in itin_records}
                    all_ids  = leg_ids | mkt_ids | itin_ids | top_set
                    struct = _graph_edges_df[
                        _graph_edges_df["edge_type"].isin(
                            ["DEPARTS_ON", "ARRIVES_AT", "HAS_ITINERARY", "USES_LEG"]
                        ) &
                        _graph_edges_df["source"].isin(all_ids) &
                        _graph_edges_df["target"].isin(all_ids)
                    ]
                    if not struct.empty:
                        s_recs = struct[["source", "target", "edge_type"]].to_dict("records")
                        logger.info(f"viz-chunks: {len(s_recs)} structural edges")
                        for i in range(0, len(s_recs), BATCH):
                            _viz_chunks.append({"type": "struct_edges", "data": s_recs[i: i + BATCH]})

        # ── 6. NetworkX property graph ──────────────────────────────────────
        _emit("networkx", "Building NetworkX property graph…", 85)
        _wkg = _build_networkx_graph(_graph_nodes, _graph_edges_df)
        _emit("networkx",
              f"NetworkX — {_wkg.number_of_nodes():,} nodes, {_wkg.number_of_edges():,} edges", 95)

        _error = None
        _emit("done",
              f"Knowledge Graph ready ⚡  {len(_graph_nodes):,} nodes | {len(_graph_edges_df):,} edges | "
              f"{len(_market_summary):,} markets | {len(od_matrix):,} OD rows", 100,
              node_count=len(_graph_nodes), edge_count=len(_graph_edges_df),
              market_count=len(_market_summary), od_rows=len(od_matrix),
              flow_edges=flow_ct)
        return True

    except Exception as exc:
        _error = str(exc)
        _emit("error", f"Build failed: {exc}", 0, error_msg=str(exc))
        logger.error(f"workset_graph build failed: {exc}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def init_workset_graph(
    basedata: Optional[pd.DataFrame] = None,
    spilldata: Optional[pd.DataFrame] = None,
    force: bool = False,
) -> bool:
    """
    Build the workset knowledge graph. Thread-safe and idempotent.

    Pass ``basedata`` and ``spilldata`` as pre-loaded DataFrames (already
    column-mapped to domain spec names) to avoid a DuckDB re-read inside the
    background thread — DuckDB connections are not thread-safe.

    If DataFrames are omitted, the function falls back to reading from DuckDB
    directly (safe only when called from the main thread).

    Pass force=True to rebuild (e.g. after fresh data ingestion).
    Returns True on success.
    """
    global _built
    if _built and not force:
        return _wkg is not None

    with _lock:
        if _built and not force:
            return _wkg is not None
        _built = False

        _emit("building", "Initialising KG build — loading workset DataFrames…", 2)

        # Load from DB only if DataFrames weren't pre-supplied
        if basedata is None:
            basedata = _load_basedata()
        if spilldata is None:
            spilldata = _load_spilldata()

        _emit("building",
              f"DataFrames loaded — {len(basedata):,} legs, {len(spilldata):,} itineraries", 5,
              base_legs=len(basedata), spill_rows=len(spilldata))

        success = _build(basedata, spilldata)
        _built = True
        return success


def is_ready() -> bool:
    return _built and _wkg is not None


def get_workset_graph() -> Optional[nx.MultiDiGraph]:
    return _wkg


def get_status() -> Dict[str, Any]:
    return {
        "ready":         is_ready(),
        "error":         _error,
        "nodes":         _wkg.number_of_nodes() if _wkg else 0,
        "edges":         _wkg.number_of_edges() if _wkg else 0,
        "od_matrix_rows":  len(_od_leg_contribution_matrix) if _od_leg_contribution_matrix is not None else 0,
        "itinerary_count": len(_itinerary_table)            if _itinerary_table is not None else 0,
        "market_count":    len(_market_summary)             if _market_summary is not None else 0,
        "leg_count":       len(_leg_flow_summary)           if _leg_flow_summary is not None else 0,
        "airport_count":   len(_airport_board_deboard_summary) if _airport_board_deboard_summary is not None else 0,
    }


# ── Table accessors ──────────────────────────────────────────────────────────

def get_od_leg_contribution_matrix() -> Optional[pd.DataFrame]:
    return _od_leg_contribution_matrix

def get_itinerary_table() -> Optional[pd.DataFrame]:
    return _itinerary_table

def get_leg_flow_summary() -> Optional[pd.DataFrame]:
    return _leg_flow_summary

def get_market_summary() -> Optional[pd.DataFrame]:
    return _market_summary

def get_airport_board_deboard_summary() -> Optional[pd.DataFrame]:
    return _airport_board_deboard_summary

def get_graph_nodes() -> Optional[pd.DataFrame]:
    return _graph_nodes

def get_graph_edges() -> Optional[pd.DataFrame]:
    return _graph_edges_df


# ── Targeted query helpers (used by API endpoints) ───────────────────────────

def get_od_flow_detail(origin: str, destin: str) -> Dict[str, Any]:
    """
    Return all itineraries + leg contributions for a specific market OD.
    Used by the /flow API endpoint.
    """
    if not is_ready():
        return {"error": "Workset graph not ready", "ready": False}

    od = f"{origin.upper()}->{destin.upper()}"
    matrix = _od_leg_contribution_matrix

    od_rows = matrix[matrix["market_od"] == od] if matrix is not None else pd.DataFrame()
    if od_rows.empty:
        return {"market_od": od, "found": False, "itineraries": []}

    itineraries = []
    for route, grp in od_rows.groupby("route"):
        legs = []
        for _, leg in grp.sort_values("leg_seq").iterrows():
            legs.append({
                "seq":          int(leg["leg_seq"]),
                "baseIndex":    int(leg["baseIndex"]),
                "leg_od":       leg["leg_od"],
                "flt_num":      leg.get("flt_num"),
                "traffic":      round(float(leg["traffic"]), 1),
            })
        itineraries.append({
            "route":      route,
            "itin_type":  grp["itin_type"].iloc[0],
            "num_legs":   int(grp["leg_seq"].max()),
            "traffic":    round(float(grp["traffic"].iloc[0]), 1),
            "legs":       legs,
        })

    # Board/deboard for this OD
    bd = _airport_board_deboard_summary
    flow_edges = _graph_edges_df[
        (_graph_edges_df["market_od"] == od) &
        (_graph_edges_df["edge_type"].isin(["FLOW_THROUGH", "FLOW_TO", "MARKET_FLOW"]))
    ] if _graph_edges_df is not None else pd.DataFrame()

    return {
        "market_od":    od,
        "found":        True,
        "itinerary_count": len(itineraries),
        "total_traffic":   round(sum(i["traffic"] for i in itineraries), 1),
        "itineraries":  sorted(itineraries, key=lambda x: x["traffic"], reverse=True),
        "flow_edges":   flow_edges.to_dict("records") if not flow_edges.empty else [],
    }


def get_leg_flow_detail(base_index: int) -> Dict[str, Any]:
    """
    Return all market ODs that contribute to a specific leg.
    Used by the /leg-flow API endpoint.
    """
    if not is_ready():
        return {"error": "Workset graph not ready", "ready": False}

    matrix = _od_leg_contribution_matrix
    if matrix is None:
        return {"error": "Matrix not built"}

    leg_rows = matrix[matrix["baseIndex"] == base_index]
    if leg_rows.empty:
        return {"baseIndex": base_index, "found": False}

    sample = leg_rows.iloc[0]
    return {
        "baseIndex":      base_index,
        "leg_od":         sample["leg_od"],
        "flt_num":        sample.get("flt_num"),
        "found":          True,
        "apm_pax_pred":   round(float(sample["apm_leg_pax_pred"]), 1) if pd.notna(sample.get("apm_leg_pax_pred")) else None,
        "apm_flow_pax_pred": round(float(sample["apm_leg_flow_pax_pred"]), 1) if pd.notna(sample.get("apm_leg_flow_pax_pred")) else None,
        "contributing_markets": int(leg_rows["market_od"].nunique()),
        "total_contribution":   round(float(leg_rows["traffic"].sum()), 1),
        "local_contribution":   round(float(leg_rows[leg_rows["itin_type"] == "LOCAL"]["traffic"].sum()), 1),
        "flow_contribution":    round(float(leg_rows[leg_rows["itin_type"] == "FLOW"]["traffic"].sum()), 1),
        "top_markets": (
            leg_rows.groupby("market_od")["traffic"].sum()
            .sort_values(ascending=False)
            .head(20)
            .reset_index()
            .rename(columns={"traffic": "contribution"})
            .round(1)
            .to_dict("records")
        ),
    }
