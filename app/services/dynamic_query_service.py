"""
Dynamic query service — lets Gemini write and execute SQL against the schedule DB.

SAFETY CONTRACT
---------------
* Only SELECT statements are permitted.
* System / information-schema tables are blocked.
* Results are capped at MAX_ROWS rows to prevent token overflow.
* Queries time-out after QUERY_TIMEOUT_S seconds.
* No DDL, DML, or PRAGMA statements are allowed.

The primary use-case is to let the AI answer deep questions that no fixed tool
covers, e.g.: jet-leg analysis, timezone-based filtering, pax-type inference,
aircraft-family breakdowns, connection feasibility, hub bank analysis, etc.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List

from loguru import logger

from app.database.db import get_connection

MAX_ROWS        = 300
QUERY_TIMEOUT_S = 30

# ─────────────────────────────────────────────────────────────────────────────
# Aircraft metadata — used by schema helper AND by callers who want enrichment
# ─────────────────────────────────────────────────────────────────────────────

AIRCRAFT_META: Dict[str, Dict[str, Any]] = {
    # Wide-body (long/medium haul, premium cabins)
    "388": {"name": "Airbus A380-800",    "body": "Wide-body",   "seats_approx": 517, "has_premium": True,  "range_nm": 8200},
    "380": {"name": "Airbus A380",        "body": "Wide-body",   "seats_approx": 517, "has_premium": True,  "range_nm": 8200},
    "77W": {"name": "Boeing 777-300ER",   "body": "Wide-body",   "seats_approx": 396, "has_premium": True,  "range_nm": 7370},
    "77L": {"name": "Boeing 777-200LR",   "body": "Wide-body",   "seats_approx": 317, "has_premium": True,  "range_nm": 9395},
    "772": {"name": "Boeing 777-200",     "body": "Wide-body",   "seats_approx": 314, "has_premium": True,  "range_nm": 5240},
    "773": {"name": "Boeing 777-300",     "body": "Wide-body",   "seats_approx": 368, "has_premium": True,  "range_nm": 6005},
    "359": {"name": "Airbus A350-900",    "body": "Wide-body",   "seats_approx": 325, "has_premium": True,  "range_nm": 8100},
    "351": {"name": "Airbus A350-1000",   "body": "Wide-body",   "seats_approx": 369, "has_premium": True,  "range_nm": 8700},
    "789": {"name": "Boeing 787-9",       "body": "Wide-body",   "seats_approx": 296, "has_premium": True,  "range_nm": 7635},
    "788": {"name": "Boeing 787-8",       "body": "Wide-body",   "seats_approx": 248, "has_premium": True,  "range_nm": 7355},
    "78X": {"name": "Boeing 787-10",      "body": "Wide-body",   "seats_approx": 330, "has_premium": True,  "range_nm": 6430},
    "333": {"name": "Airbus A330-300",    "body": "Wide-body",   "seats_approx": 295, "has_premium": True,  "range_nm": 5835},
    "332": {"name": "Airbus A330-200",    "body": "Wide-body",   "seats_approx": 253, "has_premium": True,  "range_nm": 7250},
    "339": {"name": "Airbus A330-900neo", "body": "Wide-body",   "seats_approx": 287, "has_premium": True,  "range_nm": 8150},
    "346": {"name": "Airbus A340-600",    "body": "Wide-body",   "seats_approx": 369, "has_premium": True,  "range_nm": 7900},
    "744": {"name": "Boeing 747-400",     "body": "Wide-body",   "seats_approx": 416, "has_premium": True,  "range_nm": 7260},
    "74H": {"name": "Boeing 747-8",       "body": "Wide-body",   "seats_approx": 410, "has_premium": True,  "range_nm": 8000},
    # Narrow-body (short/medium haul, mostly economy)
    "32N": {"name": "Airbus A320neo",     "body": "Narrow-body", "seats_approx": 165, "has_premium": False, "range_nm": 3500},
    "32Q": {"name": "Airbus A320neo",     "body": "Narrow-body", "seats_approx": 165, "has_premium": False, "range_nm": 3500},
    "321": {"name": "Airbus A321",        "body": "Narrow-body", "seats_approx": 185, "has_premium": False, "range_nm": 3200},
    "31N": {"name": "Airbus A321neo",     "body": "Narrow-body", "seats_approx": 185, "has_premium": False, "range_nm": 4000},
    "320": {"name": "Airbus A320",        "body": "Narrow-body", "seats_approx": 156, "has_premium": False, "range_nm": 3300},
    "319": {"name": "Airbus A319",        "body": "Narrow-body", "seats_approx": 128, "has_premium": False, "range_nm": 3750},
    "318": {"name": "Airbus A318",        "body": "Narrow-body", "seats_approx": 107, "has_premium": False, "range_nm": 3100},
    "20N": {"name": "Airbus A320neo",     "body": "Narrow-body", "seats_approx": 165, "has_premium": False, "range_nm": 3500},
    "7M8": {"name": "Boeing 737 MAX 8",   "body": "Narrow-body", "seats_approx": 178, "has_premium": False, "range_nm": 3550},
    "7M9": {"name": "Boeing 737 MAX 9",   "body": "Narrow-body", "seats_approx": 193, "has_premium": False, "range_nm": 3550},
    "73H": {"name": "Boeing 737-800",     "body": "Narrow-body", "seats_approx": 162, "has_premium": False, "range_nm": 2935},
    "738": {"name": "Boeing 737-800",     "body": "Narrow-body", "seats_approx": 162, "has_premium": False, "range_nm": 2935},
    "739": {"name": "Boeing 737-900",     "body": "Narrow-body", "seats_approx": 177, "has_premium": False, "range_nm": 2950},
    "737": {"name": "Boeing 737",         "body": "Narrow-body", "seats_approx": 149, "has_premium": False, "range_nm": 2850},
    # Regional
    "E90": {"name": "Embraer E190",       "body": "Regional",    "seats_approx": 100, "has_premium": False, "range_nm": 2450},
    "E75": {"name": "Embraer E175",       "body": "Regional",    "seats_approx":  76, "has_premium": False, "range_nm": 2200},
    "E70": {"name": "Embraer E170",       "body": "Regional",    "seats_approx":  76, "has_premium": False, "range_nm": 2100},
    "AT7": {"name": "ATR 72",             "body": "Regional",    "seats_approx":  70, "has_premium": False, "range_nm": 900},
    "DH4": {"name": "Dash 8 Q400",        "body": "Regional",    "seats_approx":  78, "has_premium": False, "range_nm": 1530},
    "CR9": {"name": "CRJ-900",            "body": "Regional",    "seats_approx":  90, "has_premium": False, "range_nm": 1650},
    "CR7": {"name": "CRJ-700",            "body": "Regional",    "seats_approx":  70, "has_premium": False, "range_nm": 1620},
}

# Service type codes
SERVICE_TYPE_MEANING = {
    "J":  "Scheduled passenger service",
    "G":  "Positioning / ferry / non-revenue flight",
    "C":  "Charter passenger service",
    "F":  "Scheduled freight / cargo",
    "H":  "Charter freight",
    "YA": "Economy scheduled",
    "YC": "Economy charter",
    "CA": "Full-service / premium scheduled",
}

# Haul classification
def classify_haul(block_min: int) -> str:
    if block_min < 90:   return "Ultra-short (<90 min)"
    if block_min < 180:  return "Short-haul (90–180 min)"
    if block_min < 360:  return "Medium-haul (3–6 h)"
    if block_min < 720:  return "Long-haul (6–12 h)"
    return "Ultra-long-haul (>12 h)"

# Passenger profile heuristic
def infer_pax_profile(airline_type: str, body: str, block_min: int,
                      dep_hour: int) -> Dict[str, Any]:
    """
    Heuristic passenger profile based on carrier type, aircraft, flight time.
    Returns estimated pct of business vs leisure travelers.
    """
    is_lcc    = airline_type in ("Low-cost", "LCC")
    is_wb     = body == "Wide-body"
    is_longhl = block_min >= 360
    morning   = 5 <= dep_hour <= 9
    evening   = 17 <= dep_hour <= 21

    business_pct = 30
    if not is_lcc:
        business_pct += 20
    if is_longhl:
        business_pct += 10
    if morning or evening:
        business_pct += 15
    if is_wb:
        business_pct += 5
    business_pct = min(business_pct, 75)
    leisure_pct  = 100 - business_pct

    return {
        "business_pct_estimate": business_pct,
        "leisure_pct_estimate":  leisure_pct,
        "profile_note": (
            "LCC narrow-body leisure-heavy" if is_lcc and not is_wb
            else "Premium full-service with significant business share" if not is_lcc and is_wb
            else "Mixed — likely moderate leisure/business split"
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Table schema catalogue — what Gemini gets when it calls get_db_schema
# ─────────────────────────────────────────────────────────────────────────────

TABLE_CATALOGUE: Dict[str, Dict[str, Any]] = {
    "flights": {
        "description": (
            "Main schedule table. One row per unique flight × day-of-operation. "
            "1,665,502 rows covering one IATA schedule season (all global airlines)."
        ),
        "columns": {
            "airline":          "VARCHAR  — 2-letter IATA airline code (e.g. 'EK', 'AI', '6E')",
            "flight_number":    "VARCHAR  — full flight number (e.g. 'EK504', 'AI910')",
            "origin":           "VARCHAR  — 3-letter IATA origin airport code (e.g. 'DXB', 'BOM')",
            "destination":      "VARCHAR  — 3-letter IATA destination airport code",
            "departure_local":  "TIMESTAMP — local departure time (date part is a reference date, TIME part is the actual scheduled departure time)",
            "arrival_local":    "TIMESTAMP — local arrival time (same note as departure_local)",
            "departure_utc":    "TIMESTAMP — UTC departure time",
            "arrival_utc":      "TIMESTAMP — UTC arrival time",
            "day_of_operation": "INTEGER  — IATA day: 1=Mon 2=Tue 3=Wed 4=Thu 5=Fri 6=Sat 7=Sun",
            "aircraft_type":    "VARCHAR  — IATA 3-char aircraft code (e.g. '77W','388','32N','7M8'). See aircraft_types below.",
            "block_time":       "INTEGER  — flight duration in MINUTES (e.g. 195 = 3h15m)",
            "frequency":        "VARCHAR  — days string e.g. '1234567' (daily), '135' (Mon/Wed/Fri)",
            "effective_from":   "DATE     — first operating date of this flight",
            "effective_to":     "DATE     — last operating date of this flight",
            "service_type":     "VARCHAR  — 'J'=scheduled, 'G'=positioning/ferry/non-rev, 'C'=charter",
            "terminal_dep":     "VARCHAR  — departure terminal code (e.g. '3', 'T1', 'S')",
            "terminal_arr":     "VARCHAR  — arrival terminal code",
        },
        "sample_queries": {
            "jet_legs_for_route": (
                "-- Count individual flight legs on a route per day\n"
                "SELECT day_of_operation, COUNT(DISTINCT flight_number) AS legs\n"
                "FROM flights WHERE origin='DXB' AND destination='BOM'\n"
                "GROUP BY day_of_operation ORDER BY day_of_operation"
            ),
            "connections_via_hub": (
                "-- Find 1-stop itineraries DXB → HUB → BOM with 60-240 min connection\n"
                "SELECT f1.airline AS op1, f1.flight_number AS leg1, f1.destination AS hub,\n"
                "       f2.flight_number AS leg2, f1.arrival_local, f2.departure_local,\n"
                "       DATEDIFF('minute', f1.arrival_local, f2.departure_local) AS layover_min\n"
                "FROM flights f1 JOIN flights f2\n"
                "  ON f1.destination = f2.origin AND f1.day_of_operation = f2.day_of_operation\n"
                "WHERE f1.origin='DXB' AND f2.destination='BOM'\n"
                "  AND f1.service_type='J' AND f2.service_type='J'\n"
                "  AND DATEDIFF('minute', f1.arrival_local, f2.departure_local) BETWEEN 60 AND 240\n"
                "ORDER BY layover_min LIMIT 20"
            ),
            "timezone_analysis": (
                "-- Departure time buckets (local) for a route\n"
                "SELECT CASE WHEN HOUR(departure_local) < 6 THEN 'red-eye'\n"
                "            WHEN HOUR(departure_local) < 12 THEN 'morning'\n"
                "            WHEN HOUR(departure_local) < 18 THEN 'afternoon'\n"
                "            ELSE 'evening' END AS slot,\n"
                "       COUNT(DISTINCT flight_number) AS flights\n"
                "FROM flights WHERE origin='DXB' AND destination='BOM'\n"
                "GROUP BY slot ORDER BY slot"
            ),
            "aircraft_mix": (
                "-- Aircraft type distribution on a route\n"
                "SELECT aircraft_type, COUNT(DISTINCT flight_number) AS flights,\n"
                "       MIN(block_time) AS min_block, MAX(block_time) AS max_block\n"
                "FROM flights WHERE origin='DXB' AND destination='BOM'\n"
                "GROUP BY aircraft_type ORDER BY flights DESC"
            ),
            "airline_departure_spread": (
                "-- Each airline's departure times on a route\n"
                "SELECT airline, strftime(departure_local,'%H:%M') AS dep_time,\n"
                "       day_of_operation, aircraft_type, block_time\n"
                "FROM flights WHERE origin='DXB' AND destination='BOM'\n"
                "  AND service_type='J'\n"
                "ORDER BY airline, day_of_operation, dep_time"
            ),
        },
    },
    "workset_spill": {
        "description": (
            "SABRE SPILLDATA: market/itinerary level predicted demand. "
            "Each row = ONE ITINERARY option for a true passenger O&D market. "
            "4-segment demand model (HO/LO/HR/LR yield segments). "
            "Use market_origin/market_dest for the true O&D pair (NOT leg endpoints). "
            "Join to workset_base via baseIndex_l1/l2/l3 to reconstruct leg sequences. "
            "NOTE: Revenue data is NOT available — fare columns are relative indices only."
        ),
        "columns": {
            "market_origin":  "VARCHAR — true passenger origin airport (market O&D level)",
            "market_dest":    "VARCHAR — true passenger destination airport (market O&D level)",
            "dep_time":       "VARCHAR — departure time of first leg (HHMM string)",
            "day_of_week":    "INTEGER — 0=Mon … 6=Sun (0-based)",
            "dmd_HO":         "DOUBLE  — demand for High-yield Outbound segment",
            "dmd_LO":         "DOUBLE  — demand for Low-yield Outbound segment",
            "dmd_HR":         "DOUBLE  — demand for High-yield Return segment",
            "dmd_LR":         "DOUBLE  — demand for Low-yield Return segment",
            "spill_HO":       "DOUBLE  — spill for HO segment",
            "spill_LO":       "DOUBLE  — spill for LO segment",
            "spill_HR":       "DOUBLE  — spill for HR segment",
            "spill_LR":       "DOUBLE  — spill for LR segment",
            "traffic_HO":     "DOUBLE  — booked pax for HO segment",
            "traffic_LO":     "DOUBLE  — booked pax for LO segment",
            "traffic_HR":     "DOUBLE  — booked pax for HR segment",
            "traffic_LR":     "DOUBLE  — booked pax for LR segment",
            "total_demand":   "DOUBLE  — total demand across all 4 segments (dmd_HO+dmd_LO+dmd_HR+dmd_LR)",
            "total_pax":      "DOUBLE  — total booked pax across all 4 segments (use this for traffic)",
            "total_spill":    "DOUBLE  — total spill across all 4 segments",
            "jet_type":       "VARCHAR — aircraft type indicator (N=narrow, W=wide, R=regional)",
            "block_time":     "INTEGER — total itinerary elapsed time (minutes)",
            "stops":          "INTEGER — 0=nonstop (1 leg), 1=one-stop (2 legs), 2=two-stop (3 legs)",
            "mkt_share":      "DOUBLE  — this airline's market share fraction on the O&D (0-1); multiply by 100 for %",
            "airline":        "VARCHAR — 2-letter IATA code of marketing carrier",
            "is_codeshare":   "INTEGER — 0=operating carrier, 1=codeshare/interline",
            "baseIndex_l1":   "BIGINT  — BASEDATA record_id (workset_base.record_id) of leg 1",
            "baseIndex_l2":   "BIGINT  — BASEDATA record_id of leg 2 (0 if nonstop)",
            "baseIndex_l3":   "BIGINT  — BASEDATA record_id of leg 3 (0 if ≤1-stop)",
        },
        "notes": (
            "CRITICAL: market_origin/market_dest = true O&D, NOT leg endpoints. "
            "stops=0 → nonstop (1 leg via baseIndex_l1). "
            "stops=1 → one-stop connecting (2 legs via baseIndex_l1 + baseIndex_l2). "
            "Use total_pax for booked passengers, total_demand for demand (includes unmet demand). "
            "Revenue is NOT available in SPILLDATA — do not attempt revenue calculations. "
            "For market share: AVG(mkt_share)*100 or SUM(mkt_share)/COUNT(*)*100. "
            "To find all itineraries using a specific leg: "
            "WHERE baseIndex_l1=X OR baseIndex_l2=X OR baseIndex_l3=X. "
            "To get leg details: JOIN workset_base ON workset_base.record_id = baseIndex_l1."
        ),
    },
    "workset_base": {
        "description": (
            "SABRE BASEDATA: leg-level APM model predictions (primary operating carrier rows only). "
            "Each row = ONE LEG (single flight segment) on ONE day of week. "
            "mkt_ind<=1 filter already applied — no codeshare/thru duplicates. "
            "ALL metrics are MODEL PREDICTIONS, not actuals. "
            "record_id (baseIndex) uniquely identifies each leg instance."
        ),
        "columns": {
            "record_id":    "BIGINT  — unique leg identifier (baseIndex), used to join with workset_spill",
            "origin":       "VARCHAR — IATA departure airport (LEG origin, not market origin)",
            "dest":         "VARCHAR — IATA arrival airport (LEG destination, not market destination)",
            "flight_num":   "VARCHAR — flight number (e.g. '777', '4948')",
            "dep_time":     "VARCHAR — departure time as HHMM string",
            "arr_time":     "VARCHAR — arrival time as HHMM string",
            "block_time":   "INTEGER — block time in minutes",
            "distance_mi":  "INTEGER — great-circle distance in miles",
            "mkt_airline":  "VARCHAR — MARKETING airline IATA code (ticket-issuing carrier; use for flight designator)",
            "op_airline":   "VARCHAR — OPERATING airline IATA code (physically operates the aircraft)",
            "aircraft_type":"VARCHAR — IATA aircraft type code",
            "apm_cap":      "INTEGER — predicted seat capacity",
            "apm_dmd":      "DOUBLE  — predicted DEMAND per departure (≥ apm_pax when constrained)",
            "apm_pax":      "DOUBLE  — predicted TRAFFIC (passengers on board) per departure (local + flow)",
            "apm_lpax":     "DOUBLE  — predicted LOCAL-only passengers (single-leg journey) per departure",
            "apm_spill":    "DOUBLE  — predicted spilled passengers (unmet demand) per departure",
            "day_of_week":  "INTEGER — 0=Mon..6=Sun (0-based)",
            "mkt_ind":      "INTEGER — dedup flag: 0-1=primary row (already filtered), 2+=duplicates (excluded)",
            "dept_offset":  "INTEGER — UTC timezone offset at departure airport (minutes)",
            "arrv_offset":  "INTEGER — UTC timezone offset at arrival airport (minutes)",
        },
        "notes": (
            "CRITICAL: apm_dmd=demand (total market want), apm_pax=traffic (who boarded). "
            "apm_dmd >= apm_pax for constrained flights; both equal for unconstrained. "
            "apm_lpax = passengers whose ENTIRE journey is this single leg (local itin). "
            "Flow pax (predicted) = apm_pax - apm_lpax (passengers connecting via this leg). "
            "Load factor = SUM(apm_pax) / SUM(apm_cap) * 100 (aggregate first, then divide). "
            "Revenue data is NOT available in BASEDATA. "
            "To identify a flight: mkt_airline + flight_num (e.g. mkt_airline='AA' AND flight_num='100'). "
            "For leg uniqueness: each (origin, dest, mkt_airline, flight_num, day_of_week) combination is a unique leg. "
            "record_id is the baseIndex linking to workset_spill.baseIndex_l1/l2/l3. "
            "⚠ AGGREGATION RULE: workset_base has ONE ROW PER day_of_week per flight leg (e.g. a daily flight = 7 rows). "
            "To get PER-DEPARTURE metrics: SUM(apm_pax) / COUNT(DISTINCT day_of_week) or AVG(apm_pax). "
            "To get WEEKLY TOTALS: SUM(apm_pax) directly. "
            "The dashboard Flight View shows PER-DEPARTURE values. Always normalize by day count when comparing to the dashboard. "
            "workset_spill.day_of_week / workset_base.day_of_week: 0=Mon..6=Sun  "
            "Example: AVG daily pax = SUM(apm_pax)/COUNT(DISTINCT day_of_week) — NOT raw SUM."
        ),
    },
    "workset_mkt": {
        "description": (
            "SABRE O&D market size: weekly demand index per O&D pair. "
            "936,080 rows. Higher index = stronger O&D market."
        ),
        "columns": {
            "origin":        "VARCHAR — IATA origin",
            "dest":          "VARCHAR — IATA destination",
            "weekly_demand": "DOUBLE  — SABRE weekly demand index (pax units)",
        },
        "example": "DXB,BOM = 11761.5",
    },
    "workset_opp": {
        "description": (
            "Airport-level airline market share (SABRE model). "
            "19,549 rows. Shows which airlines dominate each airport."
        ),
        "columns": {
            "airport":    "VARCHAR — 3-letter IATA airport",
            "airline":    "VARCHAR — 2-letter IATA airline code",
            "mkt_share":  "DOUBLE  — fraction of traffic at that airport (sums to ≈1 per airport)",
        },
    },
    "workset_alliance": {
        "description": "Airline alliance memberships (411 entries).",
        "columns": {
            "alliance_name": "VARCHAR — e.g. 'EK GRP', 'Star Alliance', 'oneworld', 'SkyTeam'",
            "airline":       "VARCHAR — 2-letter IATA code",
            "adjust_pool":   "DOUBLE  — recapture pool adjustment factor",
        },
    },
    # ── Pre-aggregated dashboard tables (exact match with what tabs display) ───
    "dm_flight_report": {
        "description": (
            "Pre-aggregated FLIGHT VIEW dashboard table — EXACTLY what the Flight View tab shows. "
            "One row per unique flight (mkt_airline + flight_num + Dept Sta + Arvl Sta). "
            "All values are per-departure averages — use this table FIRST for flight-level queries."
        ),
        "columns": {
            "Flt Desg":          "VARCHAR — flight designator e.g. '6E  4948' (airline + space + flight_num)",
            "Dept Sta":          "VARCHAR — departure airport IATA code",
            "Arvl Sta":          "VARCHAR — arrival airport IATA code",
            "Freq":              "VARCHAR — operating days string e.g. '1.3.5..' (1=Mon..7=Sun dot=not operating)",
            "Dept Time":         "VARCHAR — departure time as HHMM string",
            "Arvl Time":         "VARCHAR — arrival time as HHMM string",
            "Elap Time":         "VARCHAR — elapsed block time formatted as HH:MM",
            "Subfleet":          "VARCHAR — IATA aircraft type code",
            "Seats":             "VARCHAR — seat capacity",
            "Distance(km)":      "VARCHAR — great-circle distance in kilometres",
            "Total Demand":      "VARCHAR — per-departure demand (matches Flight View 'Total Demand')",
            "Total Traffic":     "VARCHAR — per-departure total pax (matches Flight View 'Total Traffic')",
            "Lcl Demand (Mktd)": "VARCHAR — per-departure local/point-to-point pax",
            "Lcl Traffic":       "VARCHAR — per-departure local traffic",
            "Load Factor (%)":   "VARCHAR — load factor percentage (matches Flight View LF column)",
            "Pax Revenue($)":    "VARCHAR — empty string (revenue not in WORKSET204 BASEDATA)",
            "Op/Nonop Flight":   "VARCHAR — 'Y' = operating flight",
        },
        "notes": (
            "USE THIS TABLE for all flight-level queries — values match EXACTLY what users see in the Flight View tab. "
            "Filter with double-quoted column names (they contain spaces/special chars): "
            "WHERE \"Dept Sta\"='BLR' AND \"Arvl Sta\"='DEL'  "
            "OR use LIKE: WHERE \"Flt Desg\" LIKE '6E%'  "
            "Do NOT need get_db_schema before querying this table."
        ),
    },
    "dm_network_summary": {
        "description": (
            "Pre-aggregated NETWORK OVERVIEW dashboard table — EXACTLY what the Network tab shows. "
            "One row per host-airline O&D pair (origin + dest). "
            "Includes weekly capacity, demand, load factor, flow %. Query this for network/OD queries."
        ),
        "columns": {
            "orig":                                "VARCHAR — origin airport IATA code",
            "dest":                                "VARCHAR — destination airport IATA code",
            "weekly_departures":                   "BIGINT  — number of weekly departures",
            "weekly_pax_est":                      "VARCHAR — weekly total pax estimate",
            "apm_weekly_pax_est":                  "VARCHAR — APM weekly pax estimate",
            "market_weekly_demand":                "VARCHAR — weekly market demand estimate",
            "host_share_of_market_demand_pct_est": "VARCHAR — host airline % share of market demand",
            "load_factor_pct_est":                 "VARCHAR — load factor % (matches Network tab)",
            "flow_pdd_pct_est":                    "VARCHAR — connecting/flow pax as % of total",
            "abs_total_pax_diff_pct_est":          "VARCHAR — pax vs forecast difference %",
            "abs_plf_diff_pct_est":                "VARCHAR — LF vs forecast difference %",
        },
        "notes": (
            "USE THIS TABLE for network/OD route queries — matches exactly what Network tab shows. "
            "Example: SELECT orig, dest, weekly_departures, load_factor_pct_est, flow_pdd_pct_est "
            "FROM dm_network_summary ORDER BY CAST(weekly_departures AS INTEGER) DESC LIMIT 20"
        ),
    },
    "dm_market_summary": {
        "description": (
            "Pre-aggregated MARKET SUMMARY dashboard table — EXACTLY what the O&D Intelligence tab shows. "
            "One row per O&D pair × carrier. Includes market share, demand, traffic, revenue estimates. "
            "Query this table for competitive market share and O&D intelligence."
        ),
        "columns": {
            "orig":                           "VARCHAR — origin airport IATA code",
            "dest":                           "VARCHAR — destination airport IATA code",
            "carrier":                        "VARCHAR — 2-letter IATA airline code",
            "is_host_airline":                "VARCHAR — 'True' if this is the host airline, else 'False'",
            "nonstop_itinerary_count":        "VARCHAR — number of nonstop itinerary options",
            "single_connect_itinerary_count": "VARCHAR — number of 1-stop connecting itinerary options",
            "total_demand_est":               "DOUBLE  — per-departure demand estimate",
            "demand_share_pct_est":           "DOUBLE  — carrier's % share of O&D demand",
            "total_traffic_est":              "DOUBLE  — per-departure traffic (pax) estimate",
            "traffic_share_pct_est":          "DOUBLE  — carrier's % share of O&D traffic",
            "total_revenue_est":              "DOUBLE  — per-departure revenue estimate",
            "revenue_share_pct_est":          "DOUBLE  — carrier's % share of O&D revenue",
        },
        "notes": (
            "USE THIS TABLE for market share and competitive queries — matches O&D Intelligence tab. "
            "Example: SELECT carrier, demand_share_pct_est, traffic_share_pct_est, revenue_share_pct_est "
            "FROM dm_market_summary WHERE orig='BLR' AND dest='DEL' ORDER BY total_traffic_est DESC"
        ),
    },
}

# Convenience list of allowed table names (used for SQL safety check)
ALLOWED_TABLES = set(TABLE_CATALOGUE.keys())


# ─────────────────────────────────────────────────────────────────────────────
# Safety validator
# ─────────────────────────────────────────────────────────────────────────────

_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|COPY|EXPORT|ATTACH|DETACH|PRAGMA|VACUUM|INSTALL|LOAD)\b",
    re.IGNORECASE,
)
_SYSTEM_TABLES = re.compile(
    r"\b(information_schema|pg_catalog|sqlite_master|duckdb_tables|duckdb_columns)\b",
    re.IGNORECASE,
)


def _validate_sql(query: str) -> str | None:
    """
    Returns an error message string if the query is disallowed, else None.
    """
    stripped = query.strip()
    first_word = stripped.upper().split()[0] if stripped else ""
    if first_word not in ("SELECT", "WITH"):
        return "Only SELECT statements are permitted."
    if _FORBIDDEN.search(stripped):
        return "Forbidden keyword detected — only read operations are allowed."
    if _SYSTEM_TABLES.search(stripped):
        return "Access to system tables is not permitted."
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def get_db_schema() -> Dict[str, Any]:
    """
    Return the full schema catalogue, aircraft metadata, and service-type
    reference data so Gemini can write accurate queries.
    """
    return {
        "tables": TABLE_CATALOGUE,
        "aircraft_types": {
            code: {k: v for k, v in meta.items()}
            for code, meta in AIRCRAFT_META.items()
        },
        "service_type_codes": SERVICE_TYPE_MEANING,
        "day_of_week_note": (
            "flights.day_of_operation: 1=Mon..7=Sun  "
            "workset_spill.day_of_week / workset_base.day_of_week: 0=Mon..6=Sun  "
            "(subtract 1 when joining flights to workset tables on day)"
        ),
        "join_patterns": {
            "flights_to_base": (
                "JOIN workset_base wb ON wb.origin = f.origin "
                "AND wb.dest = f.destination "
                "AND wb.mkt_airline = f.airline "
                "AND wb.day_of_week = (f.day_of_operation - 1)"
            ),
            "base_to_spill_via_record_id": (
                "JOIN workset_spill ws ON ws.baseIndex_l1 = wb.record_id "
                "OR ws.baseIndex_l2 = wb.record_id "
                "-- Note: workset_spill links to workset_base via baseIndex_l1/l2/l3 = record_id"
            ),
        },
        "useful_duckdb_functions": {
            "time": [
                "HOUR(ts)  — extract hour (0-23)",
                "MINUTE(ts) — extract minute",
                "strftime(ts, '%H:%M') — format as HH:MM string",
                "DATEDIFF('minute', ts1, ts2) — difference in minutes",
                "ts + INTERVAL '60' MINUTE — add time",
                "EPOCH(ts)  — unix epoch seconds",
            ],
            "string": ["UPPER(s)", "LOWER(s)", "LIKE '%pattern%'"],
            "agg":    ["STRING_AGG(DISTINCT col, ',' ORDER BY col)", "COUNT(DISTINCT col)", "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY col)"],
        },
    }


def execute_sql(query: str, max_rows: int = MAX_ROWS) -> Dict[str, Any]:
    """
    Execute a safe SELECT query against the schedule DuckDB and return results.

    Returns dict with keys:
      columns    — list of column names
      rows       — list of row dicts (max MAX_ROWS)
      row_count  — number of rows returned
      truncated  — True if more rows existed but were cut off
      exec_ms    — execution time in milliseconds
      error      — present only on failure
    """
    err = _validate_sql(query)
    if err:
        logger.warning(f"execute_sql blocked: {err} | query={query[:120]}")
        return {"error": err, "query": query[:200]}

    # Inject LIMIT if not already present
    q_upper = query.upper()
    if "LIMIT" not in q_upper:
        query = query.rstrip(";").rstrip() + f" LIMIT {max_rows + 1}"

    t0 = time.perf_counter()
    try:
        conn  = get_connection()
        rel   = conn.execute(query)
        cols  = [d[0] for d in rel.description]
        raw   = rel.fetchmany(max_rows + 1)
        elapsed = int((time.perf_counter() - t0) * 1000)

        truncated = len(raw) > max_rows
        rows_out  = raw[:max_rows]

        # Serialise: convert non-JSON types to strings
        def _serial(v: Any) -> Any:
            if v is None:
                return None
            if isinstance(v, (int, float, bool, str)):
                return v
            return str(v)

        result_rows = [
            {cols[i]: _serial(cell) for i, cell in enumerate(row)}
            for row in rows_out
        ]

        logger.info(f"execute_sql: {len(result_rows)} rows in {elapsed}ms")
        return {
            "columns":   cols,
            "rows":      result_rows,
            "row_count": len(result_rows),
            "truncated": truncated,
            "exec_ms":   elapsed,
        }

    except Exception as exc:
        elapsed = int((time.perf_counter() - t0) * 1000)
        logger.warning(f"execute_sql error ({elapsed}ms): {exc} | query={query[:200]}")
        return {
            "error":   str(exc),
            "query":   query[:300],
            "exec_ms": elapsed,
            "hint":    "Check column names match the schema from get_db_schema(). Common issues: column not in table, wrong join key, wrong date format.",
        }


def get_aircraft_info(aircraft_type: str) -> Dict[str, Any]:
    """Return metadata for a given IATA aircraft type code."""
    code = (aircraft_type or "").strip().upper()
    if code in AIRCRAFT_META:
        meta = dict(AIRCRAFT_META[code])
        meta["haul_suitability"] = "Long-haul capable" if meta["range_nm"] >= 4000 else "Short/medium-haul"
        return {"code": code, **meta}
    return {
        "code": code,
        "name": f"Unknown aircraft ({code})",
        "body": "Unknown",
        "seats_approx": None,
        "has_premium": None,
        "range_nm": None,
    }
