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
            "SABRE simulation data: per-flight per-day demand, spill, recapture, market share. "
            "4,658,296 rows. Join to flights on (origin, dest, dep_time/airline)."
        ),
        "columns": {
            "origin":       "VARCHAR — 3-letter IATA origin",
            "dest":         "VARCHAR — 3-letter IATA destination",
            "dep_time":     "INTEGER — departure time as HHMM integer (e.g. 835 = 08:35)",
            "day_of_week":  "INTEGER — 0=Mon … 6=Sun  (NOTE: 0-based, unlike flights table which is 1-based)",
            "cap_total":    "INTEGER — total seat capacity",
            "cap_biz":      "INTEGER — business class seats",
            "lf_pax":       "DOUBLE  — demand load (local demand units)",
            "spill_pax":    "DOUBLE  — passengers spilled (could not board due to capacity)",
            "spill_rev":    "DOUBLE  — revenue spilled",
            "recap_pax":    "DOUBLE  — spilled passengers recaptured on other flights",
            "recap_rev":    "DOUBLE  — revenue recaptured",
            "total_lf_pax": "DOUBLE  — total system load factor (pax)",
            "mkt_share":    "DOUBLE  — this airline's market share on the route (fraction 0-1, sums to 1.0 per route)",
            "airline":      "VARCHAR — 2-letter IATA code",
            "is_codeshare": "INTEGER — 0=operating carrier, 1=codeshare",
            "flight_id":    "VARCHAR — flight identifier",
            "service_type": "VARCHAR — service type (YA=economy sched, YC=economy charter, etc.)",
            "block_time":   "INTEGER — block time in minutes",
            "stops":        "INTEGER — number of intermediate stops",
        },
        "notes": (
            "day_of_week is 0=Mon..6=Sun (subtract 1 from flights.day_of_operation to match). "
            "mkt_share sums to exactly 1.0 per (origin, dest) across operating carriers. "
            "cap_total=0 for codeshare rows — use only operating rows (is_codeshare=0) for capacity analysis."
        ),
    },
    "workset_base": {
        "description": (
            "SABRE BASEDATA: per-flight capacity, demand, distance, yield. "
            "1,665,502 rows. Covers all scheduled flights in the season."
        ),
        "columns": {
            "origin":       "VARCHAR — IATA origin",
            "dest":         "VARCHAR — IATA destination",
            "flight_num":   "VARCHAR — flight number",
            "dep_time":     "INTEGER — HHMM integer",
            "arr_time":     "INTEGER — HHMM integer",
            "block_time":   "INTEGER — minutes",
            "distance_mi":  "DOUBLE  — great-circle distance in miles",
            "op_airline":   "VARCHAR — operating airline code",
            "aircraft_type":"VARCHAR — IATA aircraft type code",
            "cap_total":    "INTEGER — total seats",
            "booked_pax":   "DOUBLE  — booked passengers (SABRE model)",
            "demand_pax":   "DOUBLE  — unconstrained demand (SABRE model)",
            "spill_pax":    "DOUBLE  — spilled passengers",
            "day_of_week":  "INTEGER — 0=Mon..6=Sun",
            "stops":        "INTEGER — number of stops",
            "mct_dep":      "INTEGER — minimum connect time at departure (minutes)",
            "mct_arr":      "INTEGER — minimum connect time at arrival (minutes)",
        },
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
    if not stripped.upper().startswith("SELECT"):
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
            "flights_to_spill": (
                "JOIN workset_spill ws ON ws.origin = f.origin "
                "AND ws.dest = f.destination "
                "AND ws.airline = f.airline "
                "AND ws.day_of_week = (f.day_of_operation - 1)"
            ),
            "flights_to_base": (
                "JOIN workset_base wb ON wb.origin = f.origin "
                "AND wb.dest = f.destination "
                "AND wb.op_airline = f.airline "
                "AND wb.day_of_week = (f.day_of_operation - 1)"
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
