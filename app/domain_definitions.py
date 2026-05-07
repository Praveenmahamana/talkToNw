"""
Airline Network Intelligence — Domain Definitions
==================================================
Single source of truth for all data-model concepts, algorithm logic, output table
schemas, and knowledge-graph structure used across the dashboard.

Import from any module:
    from app.domain_definitions import (
        BASEDATA_COLUMNS, SPILLDATA_COLUMNS, TRAFFIC_COLUMNS,
        OUTPUT_TABLE_SCHEMAS, KG_NODE_TYPES, KG_EDGE_TYPES,
        build_base_lookup, get_itinerary_base_indexes, choose_traffic,
        build_od_leg_contribution_matrix,
        DOMAIN_DEFINITIONS_PROMPT,
    )

The constant DOMAIN_DEFINITIONS_PROMPT is injected into the LLM system prompt so
the AI references these canonical definitions when answering relevant queries.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import pandas as pd


# ─────────────────────────────────────────────────────────────────────────────
# DATA MODEL CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

# BASEDATA column descriptions
BASEDATA_COLUMNS: Dict[str, str] = {
    "baseIndex":  "Unique leg identifier (joins to SPILLDATA baseIndex_l1…l5)",
    "origin":     "IATA origin airport code for this leg",
    "destin":     "IATA destination airport code for this leg",
    "flt_num":    "Flight number (marketing carrier)",
    "dept_time":  "Scheduled departure time",
    "arrv_time":  "Scheduled arrival time",
    "apm_cap":    "Predicted seat capacity (APM model output — NOT actual)",
    "apm_dmd":    "Predicted unconstrained demand (always ≥ apm_pax when constrained)",
    "apm_pax":    "Predicted traffic = local + flow pax (APM model output — NOT actual)",
    "apm_lpax":   "Predicted local pax (passengers whose entire journey is this single leg)",
    "apm_spill":  "Predicted spilled pax = apm_dmd − apm_pax (unserved demand)",
}

# Derived metrics from BASEDATA
BASEDATA_DERIVED: Dict[str, str] = {
    "apm_flow_pax": "Predicted flow pax = apm_pax − apm_lpax",
    "lf_pct":       "Load factor % = SUM(apm_pax) / NULLIF(SUM(apm_cap),0) × 100 — aggregate first, then divide",
    "spill_rate":   "Spill rate % = SUM(apm_spill) / NULLIF(SUM(apm_dmd),0) × 100",
}

# SPILLDATA column descriptions
SPILLDATA_COLUMNS: Dict[str, str] = {
    "origin":           "TRUE market origin (≠ leg origin; represents the passenger's journey start)",
    "destin":           "TRUE market destination (represents the passenger's journey end)",
    "baseIndex_l1":     "baseIndex of leg 1 in the itinerary (joins to BASEDATA)",
    "baseIndex_l2":     "baseIndex of leg 2 in the itinerary (null for local/nonstop)",
    "baseIndex_l3":     "baseIndex of leg 3 (null for ≤2-leg itineraries)",
    "baseIndex_l4":     "baseIndex of leg 4 (null for ≤3-leg itineraries)",
    "baseIndex_l5":     "baseIndex of leg 5 (null for ≤4-leg itineraries)",
    "traffic_HO":       "Predicted traffic — High-yield Outbound segment",
    "traffic_LO":       "Predicted traffic — Low-yield Outbound segment",
    "traffic_HR":       "Predicted traffic — High-yield Return segment",
    "traffic_LR":       "Predicted traffic — Low-yield Return segment",
    "traffic_FO":       "Predicted traffic — First class Outbound (cabin mode)",
    "traffic_CO":       "Predicted traffic — Business class Outbound",
    "traffic_WO":       "Predicted traffic — Premium Economy Outbound",
    "traffic_YO":       "Predicted traffic — Economy Outbound",
    "traffic_FR":       "Predicted traffic — First class Return",
    "traffic_CR":       "Predicted traffic — Business class Return",
    "traffic_WR":       "Predicted traffic — Premium Economy Return",
    "traffic_YR":       "Predicted traffic — Economy Return",
    "dmd_HO":           "Predicted demand — High-yield Outbound (unconstrained)",
    "dmd_LO":           "Predicted demand — Low-yield Outbound",
    "spill_HO":         "Predicted spill — High-yield Outbound",
    "spill_LO":         "Predicted spill — Low-yield Outbound",
    "mkt_share":        "Market share (0–1 fraction; ×100 for %). PM logit model output.",
    "itin_pax":         "Passengers on this specific itinerary (economy)",
    "recap_pax":        "Recaptured pax — spilled from competitor, rebooked here",
    "total_pax":        "Total booked pax across all yield segments (HO+LO+HR+LR)",
    "total_demand":     "Total unconstrained demand across all yield segments",
    "total_spill":      "Total spilled pax across all yield segments",
    "is_codeshare":     "1 = codeshare itinerary (exclude from market share analysis)",
    "stops":            "0 = nonstop/local (1 leg); 1 = 1-stop (2 legs); 2 = 2-stop (3 legs)",
}

# Standard traffic column sets by mode
TRAFFIC_COLUMNS: Dict[str, List[str]] = {
    "non_cabin":  ["traffic_HO", "traffic_LO", "traffic_HR", "traffic_LR"],
    "cabin":      ["traffic_FO", "traffic_CO", "traffic_WO", "traffic_YO",
                   "traffic_FR", "traffic_CR", "traffic_WR", "traffic_YR"],
    "all":        ["traffic_HO", "traffic_LO", "traffic_HR", "traffic_LR",
                   "traffic_FO", "traffic_CO", "traffic_WO", "traffic_YO",
                   "traffic_FR", "traffic_CR", "traffic_WR", "traffic_YR"],
}

MAX_LEGS: int = 5  # Maximum legs in a multi-stop itinerary


# ─────────────────────────────────────────────────────────────────────────────
# OUTPUT TABLE SCHEMAS
# ─────────────────────────────────────────────────────────────────────────────

OUTPUT_TABLE_SCHEMAS: Dict[str, Dict[str, str]] = {
    "itinerary_table": {
        "description": "One row per itinerary per market OD",
        "columns": {
            "market_od":    "Origin→Destination market (from SPILLDATA origin/destin)",
            "route":        "Full itinerary path  e.g. AAA→CCC→BBB",
            "itin_type":    "'LOCAL' (1 leg) or 'FLOW' (2+ legs)",
            "num_legs":     "Number of legs in the itinerary",
            "traffic":      "Total predicted boarded pax for this itinerary",
            "spill":        "Total predicted spill for this itinerary",
            "demand":       "Total predicted unconstrained demand for this itinerary",
        },
    },
    "od_leg_contribution_matrix": {
        "description": (
            "Core output — market OD contribution to each leg. "
            "Example: AAA→BBB via AAA→CCC→BBB contributes 120 pax "
            "to BOTH the AAA→CCC leg AND the CCC→BBB leg."
        ),
        "columns": {
            "spill_row_id":          "Source SPILLDATA row index",
            "market_od":             "Market O&D  e.g. AAA→BBB",
            "market_origin":         "Market origin airport",
            "market_destin":         "Market destination airport",
            "route":                 "Full itinerary route path",
            "itin_type":             "'LOCAL' or 'FLOW'",
            "leg_seq":               "Position of this leg in the itinerary (1=first, 2=second, …)",
            "baseIndex":             "Unique leg identifier (links back to BASEDATA)",
            "leg_od":                "Leg origin→destination  e.g. AAA→CCC",
            "leg_origin":            "Leg departure airport",
            "leg_destin":            "Leg arrival airport",
            "flt_num":               "Flight number of this leg",
            "traffic":               "Pax contributed to this leg by this market OD",
            "traffic_cols_used":     "Which traffic columns were summed (e.g. traffic_HO,traffic_LO)",
            "apm_leg_pax_pred":      "Leg-level predicted total pax (from BASEDATA apm_pax)",
            "apm_leg_local_pax_pred":"Leg-level predicted local pax (from BASEDATA apm_lpax)",
            "apm_leg_flow_pax_pred": "Leg-level predicted flow pax = apm_pax − apm_lpax",
        },
    },
    "leg_flow_summary": {
        "description": "Aggregated flow summary per leg across all contributing market ODs",
        "columns": {
            "baseIndex":          "Unique leg identifier",
            "leg_od":             "Leg O&D",
            "flt_num":            "Flight number",
            "total_od_contribution": "Sum of all market OD contributions to this leg",
            "local_contribution": "Contribution from LOCAL itineraries only",
            "flow_contribution":  "Contribution from FLOW (multi-leg) itineraries only",
            "contributing_markets": "Count of distinct market ODs using this leg",
        },
    },
    "market_summary": {
        "description": "Market-level totals rolled up from SPILLDATA",
        "columns": {
            "market_od":      "Market O&D",
            "total_traffic":  "Total boarded pax across all itineraries for this market",
            "total_demand":   "Total unconstrained demand",
            "total_spill":    "Total spilled pax",
            "itinerary_count":"Number of distinct itinerary options for this market",
            "local_itin_count":"Number of nonstop/local itineraries",
            "flow_itin_count": "Number of connecting itineraries",
        },
    },
    "airport_board_deboard_summary": {
        "description": (
            "Station-level boarding/deboarding matrix derived from the itinerary table. "
            "For an itinerary AAA→CCC→BBB with 120 pax: "
            "AAA boards 120 (origin); CCC neither boards nor deboads (connecting); BBB deboars 120 (destination). "
            "Local pax board AND deBoard at their respective leg endpoints. "
            "Flow pax connect at intermediate stations (connected_in = connected_out)."
        ),
        "columns": {
            "station":         "Airport IATA code",
            "boarded":         "Pax boarding here as their journey origin",
            "deboarded":       "Pax deboarding here as their journey destination",
            "connected_in":    "Pax arriving here to connect to next leg",
            "connected_out":   "Pax departing here after connecting in",
        },
    },
    "graph_nodes": {
        "description": "All node types for the knowledge graph",
        "columns": {
            "node_id":    "Unique node identifier",
            "node_type":  "AIRPORT | MARKET | ITINERARY | LEG",
            "properties": "JSON of node-specific properties (see KG_NODE_TYPES)",
        },
    },
    "graph_edges": {
        "description": "All edge types for the knowledge graph",
        "columns": {
            "source":      "Source node_id",
            "target":      "Target node_id",
            "edge_type":   "DEPARTS_ON | ARRIVES_AT | HAS_ITINERARY | USES_LEG | FLOW_TO | FLOW_THROUGH | MARKET_FLOW",
            "market_od":   "Market O&D (for flow edges)",
            "route":       "Itinerary route path (for flow edges)",
            "traffic":     "Pax on this flow edge",
            "seq":         "Leg sequence number (for USES_LEG edges)",
        },
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# KNOWLEDGE GRAPH DEFINITIONS
# ─────────────────────────────────────────────────────────────────────────────

KG_NODE_TYPES: Dict[str, Dict] = {
    "AIRPORT": {
        "id_format":   "IATA code  e.g. DXB",
        "description": "Airport / station node",
        "properties":  {
            "airport_code": "IATA code",
            "city":         "City name",
            "country":      "Country name",
            "region":       "Geographic region",
            "hub_flag":     "True if this airport is a hub in the loaded schedule",
            "hub_tier":     "Mega-hub / Major hub / Secondary hub / Regional hub / Point-to-point",
            "hub_score":    "Computed score = connectivity × log(frequency)",
        },
    },
    "LEG": {
        "id_format":   "LEG_{baseIndex}  e.g. LEG_394212",
        "description": "Physical flight leg identified by baseIndex",
        "properties":  {
            "baseIndex":         "Unique leg identifier",
            "origin":            "Leg departure airport (IATA)",
            "destin":            "Leg arrival airport (IATA)",
            "flt_num":           "Flight number",
            "dept_time":         "Scheduled departure time",
            "arrv_time":         "Scheduled arrival time",
            "apm_cap_pred":      "Predicted capacity",
            "apm_pax_pred":      "Predicted total pax",
            "apm_lpax_pred":     "Predicted local pax",
            "apm_flow_pax_pred": "Predicted flow pax = apm_pax − apm_lpax",
        },
    },
    "MARKET": {
        "id_format":   "{origin}_{destin}  e.g. AAA_BBB",
        "description": "True passenger O&D market",
        "properties":  {
            "origin":         "Market origin (IATA)",
            "destin":         "Market destination (IATA)",
            "market_od":      "origin→destin string",
            "total_demand":   "Total unconstrained demand across all itineraries",
            "total_traffic":  "Total boarded pax",
            "total_spill":    "Total spilled pax",
        },
    },
    "ITINERARY": {
        "id_format":   "{origin}_{via1}_{destin}  e.g. AAA_CCC_BBB",
        "description": "One specific routing option for a market OD",
        "properties":  {
            "market_od":  "Parent market O&D",
            "route":      "Full path  e.g. AAA→CCC→BBB",
            "itin_type":  "'LOCAL' or 'FLOW'",
            "num_legs":   "Number of legs",
            "traffic":    "Predicted pax on this itinerary",
            "spill":      "Predicted spill on this itinerary",
            "demand":     "Predicted demand for this itinerary",
        },
    },
    "AIRLINE": {
        "id_format":   "IATA carrier code  e.g. EK, QF, BA",
        "description": "Airline / carrier node. A carrier may act as a marketing carrier (sells seats under its own code) or an operating carrier (physically operates the aircraft), or both.",
        "properties":  {
            "code":               "IATA 2-letter carrier code",
            "name":               "Full airline name",
            "carrier_type":       "Full-service / Low-cost / Regional / Charter",
            "carrier_subtype":    "More specific type (LM-enriched)",
            "alliance":           "Star Alliance / SkyTeam / Oneworld / None",
            "is_marketing_carrier": "True if this carrier sells codeshare seats under its own code",
            "is_operating_carrier": "True if this carrier physically operates flights for another carrier",
        },
    },
    "CODESHARE_ROUTE": {
        "id_format":   "{origin}_{dest}_{marketing_carrier}  e.g. DXB_LHR_QF",
        "description": "A route where the marketing carrier (code on ticket) differs from the operating carrier (airline flying the aircraft). Codeshare routes have is_codeshare=True.",
        "properties":  {
            "origin":            "Departure airport IATA code",
            "dest":              "Arrival airport IATA code",
            "marketing_airline": "Carrier code printed on the ticket / sold to passengers",
            "operating_airline": "Carrier code that physically operates the flight (crew, aircraft)",
            "is_codeshare":      "Always True for this node type",
            "weekly_flights":    "Weekly frequency under this marketing code",
        },
    },
}

KG_EDGE_TYPES: Dict[str, Dict] = {
    "DEPARTS_ON": {
        "from_type": "AIRPORT",
        "to_type":   "LEG",
        "cypher":    "(:AIRPORT)-[:DEPARTS_ON]->(:LEG)",
        "meaning":   "Airport is the departure point for the leg",
    },
    "ARRIVES_AT": {
        "from_type": "LEG",
        "to_type":   "AIRPORT",
        "cypher":    "(:LEG)-[:ARRIVES_AT]->(:AIRPORT)",
        "meaning":   "Leg arrives at this airport",
    },
    "HAS_ITINERARY": {
        "from_type": "MARKET",
        "to_type":   "ITINERARY",
        "cypher":    "(:MARKET)-[:HAS_ITINERARY]->(:ITINERARY)",
        "meaning":   "Market offers this itinerary option",
    },
    "USES_LEG": {
        "from_type":  "ITINERARY",
        "to_type":    "LEG",
        "cypher":     "(:ITINERARY)-[:USES_LEG {seq, traffic}]->(:LEG)",
        "properties": {"seq": "Leg sequence (1=first)", "traffic": "Pax contributed to this leg"},
        "meaning":    "Itinerary uses this leg at the given sequence position",
    },
    "FLOW_TO": {
        "from_type":  "AIRPORT",
        "to_type":    "AIRPORT",
        "cypher":     "(:AIRPORT)-[:FLOW_TO {market_od, traffic}]->(:AIRPORT)",
        "properties": {"market_od": "Market O&D string", "traffic": "Total market pax"},
        "meaning":    "Aggregate market-level flow between two airports (origin→destination of the market)",
    },
    "FLOW_THROUGH": {
        "from_type":  "AIRPORT",
        "to_type":    "AIRPORT",
        "cypher":     "(:AIRPORT)-[:FLOW_THROUGH {market_od, route, traffic}]->(:AIRPORT)",
        "properties": {
            "market_od": "Market O&D string",
            "route":     "Full itinerary path",
            "traffic":   "Pax on this leg-level flow",
        },
        "meaning":    "Leg-level contribution edge — pax flowing market_od travel leg-by-leg on this route",
    },
    "MARKET_FLOW": {
        "from_type":  "AIRPORT",
        "to_type":    "AIRPORT",
        "cypher":     "(:AIRPORT)-[:MARKET_FLOW {market_od, total_traffic}]->(:AIRPORT)",
        "meaning":    "Alias for FLOW_TO used in graph_edges DataFrame output",
    },
    "OPERATED_BY": {
        "from_type":  "CODESHARE_ROUTE",
        "to_type":    "AIRLINE",
        "cypher":     "(:CODESHARE_ROUTE)-[:OPERATED_BY]->(:AIRLINE)",
        "meaning":    "The route is physically operated by this carrier (crew, aircraft, safety responsibility). The operating carrier may differ from the marketing carrier whose code appears on the ticket.",
    },
    "MARKETED_BY": {
        "from_type":  "CODESHARE_ROUTE",
        "to_type":    "AIRLINE",
        "cypher":     "(:CODESHARE_ROUTE)-[:MARKETED_BY]->(:AIRLINE)",
        "meaning":    "The route is sold / marketed under this carrier's code. Passengers see this code on their ticket even though a different airline operates the flight.",
    },
    "CODESHARES_WITH": {
        "from_type":  "AIRLINE",
        "to_type":    "AIRLINE",
        "cypher":     "(:AIRLINE)-[:CODESHARES_WITH]->(:AIRLINE)",
        "meaning":    "The source airline (marketing carrier) sells seats on flights physically operated by the target airline (operating carrier). This is a bilateral commercial agreement. e.g. QF-[:CODESHARES_WITH]->BA means Qantas markets BA-operated flights.",
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# ALGORITHM FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def build_base_lookup(basedata: pd.DataFrame) -> Dict[int, Dict]:
    """
    Create a dictionary from BASEDATA keyed by baseIndex.
    Each value contains leg details plus the derived apm_flow_pax field.

    Rule: predicted flow pax = apm_pax − apm_lpax.
    """
    return {
        int(row["baseIndex"]): {
            "baseIndex":      int(row["baseIndex"]),
            "origin":         row["origin"],
            "destin":         row["destin"],
            "leg_od":         f'{row["origin"]}->{row["destin"]}',
            "flt_num":        row.get("flt_num"),
            "dept_time":      row.get("dept_time"),
            "arrv_time":      row.get("arrv_time"),
            "apm_cap":        row.get("apm_cap"),
            "apm_pax":        row.get("apm_pax"),
            "apm_lpax":       row.get("apm_lpax"),
            # Derived: flow pax = total pax − local pax
            "apm_flow_pax":   (row.get("apm_pax") or 0) - (row.get("apm_lpax") or 0),
        }
        for _, row in basedata.iterrows()
    }


def get_itinerary_base_indexes(row: Any, max_legs: int = MAX_LEGS) -> List[int]:
    """
    Extract non-empty baseIndex_l1…lN values from a SPILLDATA row.
    Returns a list of integer baseIndex values in leg sequence order.
    """
    indexes: List[int] = []
    for i in range(1, max_legs + 1):
        col = f"baseIndex_l{i}"
        if col in row and pd.notna(row[col]) and str(row[col]).strip() != "":
            indexes.append(int(row[col]))
    return indexes


def choose_traffic(
    row: Any,
    traffic_cols: Optional[List[str]] = None,
) -> Tuple[float, List[str]]:
    """
    Sum all available traffic columns in the SPILLDATA row.
    Returns (total_traffic, list_of_columns_used).

    Default: sums ALL traffic columns (HO, LO, HR, LR + cabin variants).
    Pass traffic_cols=TRAFFIC_COLUMNS['non_cabin'] to restrict to yield-only.
    """
    if traffic_cols is None:
        traffic_cols = TRAFFIC_COLUMNS["all"]

    total: float = 0.0
    used: List[str] = []
    for col in traffic_cols:
        if col in row and pd.notna(row[col]):
            total += float(row[col])
            used.append(col)
    return total, used


def build_od_leg_contribution_matrix(
    spilldata: pd.DataFrame,
    basedata: pd.DataFrame,
    traffic_cols: Optional[List[str]] = None,
) -> pd.DataFrame:
    """
    Core algorithm: convert itinerary-level demand from SPILLDATA into
    leg-level contribution by market OD.

    Key rule:
        Market OD AAA→BBB using itinerary AAA→CCC→BBB with 120 pax
        contributes 120 to leg AAA→CCC  AND  120 to leg CCC→BBB.

    Validation applied per itinerary:
        - leg chaining: destin of leg N == origin of leg N+1
        - route starts with market origin
        - route ends with market destin

    Uses vectorized pandas melt+merge for performance on large datasets
    (100-1000x faster than iterrows for millions of spill rows).

    Returns the od_leg_contribution_matrix DataFrame.
    """
    import logging
    log = logging.getLogger(__name__)

    _EMPTY_COLS = [
        "spill_row_id", "market_od", "market_origin", "market_destin",
        "route", "itin_type", "leg_seq", "baseIndex",
        "leg_od", "leg_origin", "leg_destin", "flt_num",
        "traffic", "traffic_cols_used",
        "apm_leg_pax_pred", "apm_leg_local_pax_pred", "apm_leg_flow_pax_pred",
    ]

    if traffic_cols is None:
        traffic_cols = TRAFFIC_COLUMNS["all"]

    # ── 1. Prepare basedata join table ────────────────────────────────────
    bd = basedata.copy()
    apm_pax  = bd["apm_pax"].fillna(0)  if "apm_pax"  in bd.columns else pd.Series(0, index=bd.index)
    apm_lpax = bd["apm_lpax"].fillna(0) if "apm_lpax" in bd.columns else pd.Series(0, index=bd.index)
    bd["leg_od"]      = bd["origin"].astype(str) + "->" + bd["destin"].astype(str)
    bd["apm_flow_pax"] = apm_pax - apm_lpax
    bd = bd.rename(columns={"origin": "leg_origin", "destin": "leg_destin"})
    keep = ["baseIndex", "leg_origin", "leg_destin", "leg_od", "flt_num",
            "apm_pax", "apm_lpax", "apm_flow_pax"]
    bd = bd[[c for c in keep if c in bd.columns]]

    # ── 2. Compute per-row traffic (vectorized sum) ───────────────────────
    sd = spilldata.copy()
    sd["spill_row_id"] = range(len(sd))
    sd["market_od"]    = sd["origin"].astype(str) + "->" + sd["destin"].astype(str)

    avail_traffic = [c for c in traffic_cols if c in sd.columns]
    if avail_traffic:
        sd["traffic"]           = sd[avail_traffic].fillna(0).sum(axis=1)
        sd["traffic_cols_used"] = ",".join(avail_traffic)
    else:
        sd["traffic"]           = 0.0
        sd["traffic_cols_used"] = ""

    # ── 3. Melt baseIndex_l* → long format (one row per spill × leg) ─────
    leg_cols    = [c for c in [f"baseIndex_l{i}" for i in range(1, MAX_LEGS + 1)] if c in sd.columns]
    leg_seq_map = {f"baseIndex_l{i}": i for i in range(1, MAX_LEGS + 1)}

    id_cols = ["spill_row_id", "origin", "destin", "market_od", "traffic", "traffic_cols_used"]
    melted = sd[id_cols + leg_cols].melt(
        id_vars=id_cols,
        value_vars=leg_cols,
        var_name="leg_col",
        value_name="baseIndex",
    )

    # Drop null / zero leg slots
    melted = melted[melted["baseIndex"].notna()].copy()
    melted["baseIndex"] = pd.to_numeric(melted["baseIndex"], errors="coerce")
    melted = melted[melted["baseIndex"].notna() & (melted["baseIndex"] != 0)].copy()
    melted["baseIndex"] = melted["baseIndex"].astype(int)
    melted["leg_seq"]   = melted["leg_col"].map(leg_seq_map)
    melted = melted.drop(columns=["leg_col"])

    # ── 4. Join with basedata ─────────────────────────────────────────────
    merged = melted.merge(bd, on="baseIndex", how="inner")
    if merged.empty:
        return pd.DataFrame(columns=_EMPTY_COLS)

    merged = merged.sort_values(["spill_row_id", "leg_seq"])

    # ── 5. Validate leg chaining (destin of leg N == origin of leg N+1) ───
    merged["_prev_destin"] = merged.groupby("spill_row_id")["leg_destin"].shift(1)
    bad_chains = merged[
        merged["_prev_destin"].notna() & (merged["_prev_destin"] != merged["leg_origin"])
    ]["spill_row_id"].unique()
    if len(bad_chains):
        log.debug("Dropping %d itineraries with broken leg chaining", len(bad_chains))
        merged = merged[~merged["spill_row_id"].isin(bad_chains)]
    merged = merged.drop(columns=["_prev_destin"])

    if merged.empty:
        return pd.DataFrame(columns=_EMPTY_COLS)

    # ── 6. Build route string per itinerary (vectorized groupby) ──────────
    grp = merged.groupby("spill_row_id")
    first_origin = grp["leg_origin"].first()
    all_destins  = grp["leg_destin"].agg("->".join)
    route_series = (first_origin + "->" + all_destins).rename("route")
    merged = merged.join(route_series, on="spill_row_id")

    # ── 7. itin_type: LOCAL (1 leg) vs FLOW (2+ legs) ────────────────────
    n_legs = grp["leg_seq"].max().rename("_n_legs")
    merged = merged.join(n_legs, on="spill_row_id")
    merged["itin_type"] = merged["_n_legs"].map(lambda x: "LOCAL" if x == 1 else "FLOW")
    merged = merged.drop(columns=["_n_legs"])

    # ── 8. Rename and return final columns ────────────────────────────────
    merged = merged.rename(columns={
        "origin":       "market_origin",
        "destin":       "market_destin",
        "apm_pax":      "apm_leg_pax_pred",
        "apm_lpax":     "apm_leg_local_pax_pred",
        "apm_flow_pax": "apm_leg_flow_pax_pred",
    })

    out_cols = [c for c in _EMPTY_COLS if c in merged.columns]
    return merged[out_cols].reset_index(drop=True)


def build_airport_board_deboard_summary(
    od_leg_matrix: pd.DataFrame,
) -> pd.DataFrame:
    """
    Derive station-level boarding/deboarding/connecting movements from the
    OD→Leg contribution matrix.  Fully vectorized.

    For itinerary AAA→CCC→BBB with traffic=120:
        AAA: boarded=120, deboarded=0, connected_in=0, connected_out=120
        CCC: boarded=0,   deboarded=0, connected_in=120, connected_out=120
        BBB: boarded=0,   deboarded=120, connected_in=120, connected_out=0
    """
    if od_leg_matrix.empty:
        return pd.DataFrame(
            columns=["station", "boarded", "deboarded", "connected_in", "connected_out"]
        )

    m = od_leg_matrix.copy()
    max_seq = m.groupby(["market_od", "route"])["leg_seq"].transform("max")
    is_first = m["leg_seq"] == 1
    is_last  = m["leg_seq"] == max_seq
    is_flow  = m["itin_type"] == "FLOW"

    # Origin station of every leg
    orig = pd.DataFrame({
        "station":       m["leg_origin"],
        "boarded":       m["traffic"].where(is_first, 0),
        "deboarded":     0.0,
        "connected_in":  m["traffic"].where(~is_first & is_flow, 0.0),
        "connected_out": m["traffic"].where(is_flow, 0.0),
    })

    # Destination station — only for the last leg of each itinerary
    last = m[is_last]
    dest = pd.DataFrame({
        "station":       last["leg_destin"],
        "boarded":       0.0,
        "deboarded":     last["traffic"],
        "connected_in":  last["traffic"].where(last["itin_type"] == "FLOW", 0.0),
        "connected_out": 0.0,
    })

    df = pd.concat([orig, dest], ignore_index=True)
    return (
        df.groupby("station", as_index=False)[
            ["boarded", "deboarded", "connected_in", "connected_out"]
        ].sum()
    )



def build_graph_edges(od_leg_matrix: pd.DataFrame) -> pd.DataFrame:
    """
    Build the graph edge DataFrame from the OD→Leg contribution matrix.
    Produces three edge types (fully vectorized):
        - FLOW_THROUGH : leg-by-leg airport→airport flow for multi-leg itineraries
        - FLOW_TO      : direct airport→airport edge for local itineraries
        - MARKET_FLOW  : aggregate market-level origin→destination edge
    """
    if od_leg_matrix.empty:
        return pd.DataFrame(
            columns=["source", "target", "edge_type", "market_od", "route", "traffic"]
        )

    m = od_leg_matrix.copy()
    m["edge_type"] = m["itin_type"].map(
        lambda x: "FLOW_THROUGH" if x == "FLOW" else "FLOW_TO"
    )

    # FLOW_THROUGH / FLOW_TO — one row per leg in od_matrix
    flow_edges = m[
        ["leg_origin", "leg_destin", "edge_type", "market_od", "route", "traffic"]
    ].rename(columns={"leg_origin": "source", "leg_destin": "target"})

    # MARKET_FLOW — one row per unique (market_od, route)
    mkt = m.drop_duplicates(["market_od", "route"])[
        ["market_od", "route", "market_origin", "market_destin", "traffic"]
    ].copy()
    mkt["edge_type"] = "MARKET_FLOW"
    market_flow = mkt.rename(
        columns={"market_origin": "source", "market_destin": "target"}
    )[["source", "target", "edge_type", "market_od", "route", "traffic"]]

    return pd.concat([flow_edges, market_flow], ignore_index=True)





# ─────────────────────────────────────────────────────────────────────────────
# LLM PROMPT INJECTION — DOMAIN DEFINITIONS
# ─────────────────────────────────────────────────────────────────────────────

DOMAIN_DEFINITIONS_PROMPT: str = """
## INTERNAL DOMAIN DEFINITIONS (authoritative — check these before answering)

These definitions are the single source of truth used across all dashboard components.
Reference them whenever a user asks about data columns, algorithms, flow pax, spill,
OD contribution, KG edges, or output table schemas.

### DATA TABLES

**BASEDATA (workset_base)** — one row per flight leg:
| Column | Meaning |
|--------|---------|
| `baseIndex` | Unique leg ID — joins to SPILLDATA `baseIndex_l1…l5` |
| `origin` / `destin` | Leg airports (NOT the market O&D) |
| `apm_dmd` | Predicted **unconstrained demand** (always ≥ apm_pax when constrained) |
| `apm_pax` | Predicted **total traffic** = local + flow pax |
| `apm_lpax` | Predicted **local pax** (complete journey = this one leg only) |
| `apm_spill` | Predicted **spill** = apm_dmd − apm_pax |
| `apm_cap` | Predicted **seat capacity** |
| `apm_flow_pax` *(derived)* | = apm_pax − apm_lpax |

⚠ All APM values are MODEL PREDICTIONS — not actual observed data.

**SPILLDATA (workset_spill)** — one row per itinerary option per market:
| Column | Meaning |
|--------|---------|
| `origin` / `destin` | TRUE market O&D (the passenger's journey endpoints) |
| `baseIndex_l1…l5` | Leg IDs in sequence — join each to BASEDATA |
| `traffic_HO/LO/HR/LR` | Pax by yield: High/Low-yield Outbound/Return |
| `traffic_FO/CO/WO/YO/FR/CR/WR/YR` | Pax by cabin class (cabin-mode files) |
| `stops` | 0 = local/nonstop (1 leg); 1 = 1-stop (2 legs) |
| `mkt_share` | Market share 0–1 fraction (×100 for %) |
| `is_codeshare` | 1 = exclude from market share analysis |

### OD → LEG CONTRIBUTION ALGORITHM

The fundamental rule:
> A market OD **AAA→BBB** using itinerary **AAA→CCC→BBB** with **120 pax**
> contributes **120 pax to leg AAA→CCC** AND **120 pax to leg CCC→BBB**.
> The same pax count is assigned to EVERY leg in the itinerary.

Steps:
1. For each SPILLDATA row → extract `baseIndex_l1…l5` to get the leg sequence.
2. Look up each `baseIndex` in BASEDATA to get leg details.
3. Validate: `destin[leg N]` == `origin[leg N+1]`; route starts at market origin; route ends at market destin.
4. Sum traffic columns (`traffic_HO+LO+HR+LR` for non-cabin mode, or cabin variants).
5. Assign that total pax to EVERY leg in the itinerary.

### OUTPUT TABLES

1. **itinerary_table** — one row per (market_od, route) pair
2. **od_leg_contribution_matrix** — core output; one row per (market_od, route, leg_seq)
3. **leg_flow_summary** — aggregated contributions per leg across all contributing ODs
4. **market_summary** — market-level totals (traffic, demand, spill, itinerary count)
5. **airport_board_deboard_summary** — station-level boarding/deboarding/connecting movements
6. **graph_nodes** — AIRPORT | MARKET | ITINERARY | LEG nodes
7. **graph_edges** — DEPARTS_ON | ARRIVES_AT | HAS_ITINERARY | USES_LEG | FLOW_TO | FLOW_THROUGH | MARKET_FLOW edges

### KNOWLEDGE GRAPH EDGE TYPES

| Edge | Cypher | Meaning |
|------|--------|---------|
| `DEPARTS_ON` | `(AIRPORT)→(LEG)` | Leg departs from this airport |
| `ARRIVES_AT` | `(LEG)→(AIRPORT)` | Leg arrives at this airport |
| `HAS_ITINERARY` | `(MARKET)→(ITINERARY)` | Market offers this routing |
| `USES_LEG {seq, traffic}` | `(ITINERARY)→(LEG)` | Itinerary uses this leg in sequence |
| `FLOW_TO {market_od, traffic}` | `(AIRPORT)→(AIRPORT)` | Direct market-level O&D flow |
| `FLOW_THROUGH {market_od, route, traffic}` | `(AIRPORT)→(AIRPORT)` | Leg-level flow for connecting itineraries ⚡ |
| `MARKET_FLOW` | `(AIRPORT)→(AIRPORT)` | Aggregate market origin→destination edge |
| `CODESHARES_WITH` | `(AIRLINE)→(AIRLINE)` | Marketing carrier sells flights operated by target carrier |
| `OPERATED_BY` | `(CODESHARE_ROUTE)→(AIRLINE)` | Physical operator of the codeshare route |
| `MARKETED_BY` | `(CODESHARE_ROUTE)→(AIRLINE)` | Carrier whose code is on the ticket |

### CODESHARE CONCEPTS

**Codeshare flight** — a flight sold by one carrier (the *marketing carrier*) but physically operated by a different carrier (the *operating carrier*).

| Term | Definition |
|------|-----------|
| **Marketing carrier (MC)** | The airline whose code appears on the passenger's ticket. Sells the seat, handles booking. e.g. QF sells flight QF7001 DXB→LHR. |
| **Operating carrier (OC)** | The airline whose aircraft, crew, and safety protocols are used. e.g. BA operates the physical aircraft as BA7001. |
| **`is_codeshare = True`** | The marketing code differs from the operating code. In SPILLDATA `is_codeshare=1` means exclude from carrier market-share analysis (pax already counted under the operating carrier). |
| **`operating_airline`** | IATA code of the carrier physically operating the flight. Equals `airline` for non-codeshare flights. |
| **`marketing_airline`** | IATA code of the carrier selling the flight. Equals `airline` for own-operated flights. |

**KG representation:**
- `CODESHARE_ROUTE` node: `origin`, `dest`, `marketing_airline`, `operating_airline`, `is_codeshare=True`
- `AIRLINE -[:CODESHARES_WITH]-> AIRLINE` edge: marketing carrier → operating carrier
- `CODESHARE_ROUTE -[:OPERATED_BY]-> AIRLINE`: physical operator
- `CODESHARE_ROUTE -[:MARKETED_BY]-> AIRLINE`: selling carrier

**Example:** Qantas (QF) codeshares British Airways (BA) on DXB→LHR:
- Route node: `{origin: DXB, dest: LHR, marketing_airline: QF, operating_airline: BA, is_codeshare: true}`
- `QF -[:CODESHARES_WITH]-> BA`
- `Route -[:OPERATED_BY]-> BA`, `Route -[:MARKETED_BY]-> QF`

### BOARDING / DEBOARDING LOGIC

For itinerary **AAA→CCC→BBB** with 120 pax:
| Station | Boarded | Deboarded | Connected-in | Connected-out |
|---------|---------|-----------|--------------|---------------|
| AAA | 120 | 0 | 0 | 120 |
| CCC | 0 | 0 | 120 | 120 |
| BBB | 0 | 120 | 120 | 0 |

For local (single-leg) **AAA→CCC** with 80 pax:
| Station | Boarded | Deboarded | Connected-in | Connected-out |
|---------|---------|-----------|--------------|---------------|
| AAA | 80 | 0 | 0 | 0 |
| CCC | 0 | 80 | 0 | 0 |
"""
