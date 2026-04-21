"""
Intelligence Service — static reference data and derived analytics.

Provides:
  • Aircraft capacity / class mix by IATA type code
  • Airport terminal assignments by airline (where known)
  • Route haul classification
  • Competitor analysis on a given O&D pair
"""

from typing import Dict, List, Optional, Any
from loguru import logger

# ─────────────────────────────────────────────────────────────────────────────
# Aircraft Capacity Reference
# Sources: airline seat maps, CAPA, airline press releases
# Format: { iata_type: {total, first, business, premium_economy, economy, type, family} }
# ─────────────────────────────────────────────────────────────────────────────

AIRCRAFT_CAPACITY: Dict[str, Dict[str, Any]] = {
    # Airbus Widebody
    "388": {"total": 615, "first": 14, "business": 76, "premium_economy": 0,  "economy": 525, "type": "widebody",   "family": "A380"},
    "38F": {"total": 615, "first": 14, "business": 76, "premium_economy": 0,  "economy": 525, "type": "widebody",   "family": "A380"},
    "380": {"total": 555, "first": 0,  "business": 58, "premium_economy": 0,  "economy": 497, "type": "widebody",   "family": "A380"},
    "359": {"total": 358, "first": 8,  "business": 42, "premium_economy": 24, "economy": 284, "type": "widebody",   "family": "A350"},
    "351": {"total": 369, "first": 0,  "business": 44, "premium_economy": 24, "economy": 301, "type": "widebody",   "family": "A350"},
    "77W": {"total": 354, "first": 8,  "business": 42, "premium_economy": 0,  "economy": 304, "type": "widebody",   "family": "B777"},
    "773": {"total": 396, "first": 0,  "business": 48, "premium_economy": 0,  "economy": 348, "type": "widebody",   "family": "B777"},
    "772": {"total": 338, "first": 0,  "business": 49, "premium_economy": 0,  "economy": 289, "type": "widebody",   "family": "B777"},
    "77X": {"total": 426, "first": 0,  "business": 54, "premium_economy": 0,  "economy": 372, "type": "widebody",   "family": "B777X"},
    "789": {"total": 296, "first": 0,  "business": 42, "premium_economy": 21, "economy": 233, "type": "widebody",   "family": "B787"},
    "788": {"total": 256, "first": 0,  "business": 28, "premium_economy": 21, "economy": 207, "type": "widebody",   "family": "B787"},
    "781": {"total": 330, "first": 0,  "business": 40, "premium_economy": 35, "economy": 255, "type": "widebody",   "family": "B787"},
    "333": {"total": 277, "first": 0,  "business": 40, "premium_economy": 0,  "economy": 237, "type": "widebody",   "family": "A330"},
    "332": {"total": 268, "first": 0,  "business": 36, "premium_economy": 0,  "economy": 232, "type": "widebody",   "family": "A330"},
    "339": {"total": 287, "first": 0,  "business": 32, "premium_economy": 21, "economy": 234, "type": "widebody",   "family": "A330neo"},
    # Narrowbody — Boeing
    "73H": {"total": 174, "first": 0,  "business": 12, "premium_economy": 0,  "economy": 162, "type": "narrowbody", "family": "B737-800"},
    "738": {"total": 162, "first": 0,  "business": 8,  "premium_economy": 0,  "economy": 154, "type": "narrowbody", "family": "B737-800"},
    "739": {"total": 177, "first": 0,  "business": 12, "premium_economy": 0,  "economy": 165, "type": "narrowbody", "family": "B737-900"},
    "7M8": {"total": 178, "first": 0,  "business": 12, "premium_economy": 0,  "economy": 166, "type": "narrowbody", "family": "B737 MAX 8"},
    "7M9": {"total": 189, "first": 0,  "business": 16, "premium_economy": 0,  "economy": 173, "type": "narrowbody", "family": "B737 MAX 9"},
    # Narrowbody — Airbus
    "32A": {"total": 186, "first": 0,  "business": 12, "premium_economy": 0,  "economy": 174, "type": "narrowbody", "family": "A320neo"},
    "320": {"total": 180, "first": 0,  "business": 12, "premium_economy": 0,  "economy": 168, "type": "narrowbody", "family": "A320"},
    "321": {"total": 220, "first": 0,  "business": 16, "premium_economy": 0,  "economy": 204, "type": "narrowbody", "family": "A321"},
    "32B": {"total": 220, "first": 0,  "business": 16, "premium_economy": 0,  "economy": 204, "type": "narrowbody", "family": "A321neo"},
    "319": {"total": 144, "first": 0,  "business": 12, "premium_economy": 0,  "economy": 132, "type": "narrowbody", "family": "A319"},
    # Regional
    "AT7": {"total":  70, "first": 0,  "business": 0,  "premium_economy": 0,  "economy":  70, "type": "turboprop",  "family": "ATR 72"},
    "AT4": {"total":  48, "first": 0,  "business": 0,  "premium_economy": 0,  "economy":  48, "type": "turboprop",  "family": "ATR 42"},
    "E90": {"total":  94, "first": 0,  "business": 8,  "premium_economy": 0,  "economy":  86, "type": "regional",   "family": "E190"},
    "E95": {"total":  98, "first": 0,  "business": 8,  "premium_economy": 0,  "economy":  90, "type": "regional",   "family": "E195"},
    "CR9": {"total":  90, "first": 0,  "business": 9,  "premium_economy": 0,  "economy":  81, "type": "regional",   "family": "CRJ-900"},
}

# ─────────────────────────────────────────────────────────────────────────────
# Airport Terminal Map  (airport → airline → terminal name / number)
# Where SSIM terminal field is blank, fall back to this static map.
# Sources: airport / airline official pages (as of 2024)
# ─────────────────────────────────────────────────────────────────────────────

AIRPORT_TERMINAL_MAP: Dict[str, Dict[str, str]] = {
    "DXB": {
        "EK": "T3",   "FZ": "T2",   "AC": "T1",   "UA": "T3",
        "QR": "T1",   "EY": "T1",   "WY": "T1",   "LH": "T1",
        "BA": "T1",   "AF": "T1",   "KL": "T1",   "TK": "T1",
        "_default": "T1",
    },
    "BOM": {
        "EK": "T2",   "FZ": "T2",   "AI": "T2",   "QR": "T2",
        "EY": "T2",   "BA": "T2",   "LH": "T2",   "9W": "T2",
        "_default": "T2",
    },
    "LHR": {
        "EK": "T3",   "QR": "T4",   "BA": "T5",   "AA": "T3",
        "IB": "T5",   "LH": "T2",   "AC": "T2",   "UA": "T2",
        "AF": "T2",   "KL": "T4",   "VS": "T3",   "FZ": "T3",
        "_default": "T3",
    },
    "CDG": {
        "EK": "2C",   "AF": "2E",   "KL": "2E",   "BA": "2A",
        "QR": "2E",   "EY": "2C",   "LH": "2C",
        "_default": "2E",
    },
    "JFK": {
        "EK": "T4",   "BA": "T7",   "AA": "T8",   "QR": "T8",
        "AF": "T1",   "KL": "T4",   "LH": "T1",   "AC": "T8",
        "_default": "T4",
    },
    "SIN": {
        "EK": "T1",   "FZ": "T3",   "SQ": "T3",   "QR": "T1",
        "_default": "T1",
    },
    "BKK": {
        "EK": "S",    "FZ": "S",    "TG": "S",    "QR": "S",
        "_default": "S",
    },
    "DEL": {
        "EK": "T3",   "FZ": "T3",   "AI": "T3",   "QR": "T3",
        "EY": "T3",   "BA": "T3",
        "_default": "T3",
    },
    "SYD": {
        "EK": "T1",   "QF": "T3",   "JQ": "T3",
        "_default": "T1",
    },
    "MEL": {
        "EK": "T2",   "QF": "T1",
        "_default": "T2",
    },
    "MAN": {
        "EK": "T1",   "FZ": "T2",
        "_default": "T1",
    },
    "GLA": {
        "EK": "T1",   "FZ": "T2",
        "_default": "T1",
    },
    "KHI": {
        "EK": "T2",   "FZ": "T1",
        "_default": "T2",
    },
    "LHE": {
        "EK": "T2",   "FZ": "T2",
        "_default": "T2",
    },
    "ISB": {
        "EK": "NBIA", "FZ": "NBIA",
        "_default": "NBIA",
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Haul classification by block time
# ─────────────────────────────────────────────────────────────────────────────

def classify_haul(block_minutes: Optional[int]) -> str:
    if block_minutes is None:
        return "unknown"
    if block_minutes <= 180:
        return "short-haul"
    if block_minutes <= 360:
        return "medium-haul"
    return "long-haul"


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def get_terminal_info(airport: str, airline: str) -> Dict[str, Any]:
    """
    Return terminal information for an airline at an airport.
    Uses AIRPORT_TERMINAL_MAP (static reference).
    Returns a dict with keys: airport, airline, terminal, source.
    """
    airport = airport.upper().strip()
    airline = airline.upper().strip()

    if airport in AIRPORT_TERMINAL_MAP:
        by_airline = AIRPORT_TERMINAL_MAP[airport]
        terminal = by_airline.get(airline) or by_airline.get("_default", "Unknown")
        return {
            "airport":  airport,
            "airline":  airline,
            "terminal": terminal,
            "source":   "reference_data",
            "note":     "Terminal data from static reference — verify with airport authority for operational planning.",
        }
    return {
        "airport":  airport,
        "airline":  airline,
        "terminal": "Unknown",
        "source":   "not_available",
        "note":     f"No terminal data available for {airport}.",
    }


def get_terminals_for_route(origin: str, destination: str, airline: str) -> Dict[str, Any]:
    """Return departure and arrival terminal info for a route + airline."""
    dep_info = get_terminal_info(origin, airline)
    arr_info = get_terminal_info(destination, airline)
    return {
        "origin":            origin.upper(),
        "destination":       destination.upper(),
        "airline":           airline.upper(),
        "departure_terminal": dep_info["terminal"],
        "arrival_terminal":   arr_info["terminal"],
        "dep_source":        dep_info["source"],
        "arr_source":        arr_info["source"],
        "note":              dep_info.get("note", ""),
    }


def get_pax_capacity_info(aircraft_type: str) -> Dict[str, Any]:
    """
    Return seating capacity breakdown for a given IATA aircraft type code.
    Includes total seats, class mix, haul suitability.
    """
    code = aircraft_type.upper().strip()
    cap = AIRCRAFT_CAPACITY.get(code)
    if not cap:
        return {
            "aircraft_type":     code,
            "found":             False,
            "note":              f"No capacity data for aircraft type '{code}'. Common codes: 388=A380, 77W=B777, 789=B787, 73H=B737-800.",
        }

    classes = []
    if cap.get("first",    0) > 0: classes.append(f"First: {cap['first']}")
    if cap.get("business", 0) > 0: classes.append(f"Business: {cap['business']}")
    if cap.get("premium_economy", 0) > 0: classes.append(f"Prem Economy: {cap['premium_economy']}")
    if cap.get("economy",  0) > 0: classes.append(f"Economy: {cap['economy']}")

    return {
        "aircraft_type":        code,
        "found":                True,
        "family":               cap["family"],
        "aircraft_category":    cap["type"],
        "total_seats":          cap["total"],
        "first_class":          cap.get("first", 0),
        "business_class":       cap.get("business", 0),
        "premium_economy":      cap.get("premium_economy", 0),
        "economy_class":        cap.get("economy", 0),
        "class_mix":            " | ".join(classes),
        "multi_class":          cap.get("first", 0) > 0 or cap.get("business", 0) > 0,
    }


def get_competitor_analysis(origin: str, destination: str) -> Dict[str, Any]:
    """
    Analyse all airlines on a given O&D route from the schedule database.
    Returns per-airline flight counts, weekly frequency, aircraft mix,
    departure time spread, and market share.
    """
    from app.database.db import get_connection
    import math

    origin      = origin.upper().strip()
    destination = destination.upper().strip()

    try:
        conn = get_connection()
        rows = conn.execute("""
            SELECT
                airline,
                COUNT(DISTINCT flight_number)           AS unique_flights,
                COUNT(*)                                AS total_ops,
                STRING_AGG(DISTINCT aircraft_type, ', '
                    ORDER BY aircraft_type)             AS aircraft_types,
                MIN(strftime(departure_local, '%H:%M')) AS earliest_dep,
                MAX(strftime(departure_local, '%H:%M')) AS latest_dep,
                AVG(block_time)                         AS avg_block_min,
                STRING_AGG(DISTINCT service_type, ', '
                    ORDER BY service_type)              AS service_types
            FROM flights
            WHERE origin = ? AND destination = ?
              AND service_type != 'G'
            GROUP BY airline
            ORDER BY total_ops DESC
        """, [origin, destination]).fetchall()

        if not rows:
            return {
                "origin":      origin,
                "destination": destination,
                "found":       False,
                "note":        f"No scheduled flights found on {origin}→{destination}.",
            }

        total_ops = sum(r[2] for r in rows)
        airlines  = []

        for r in rows:
            al_code    = r[0]
            uniq       = int(r[1] or 0)
            ops        = int(r[2] or 0)
            ac_types   = r[3] or "N/A"
            earliest   = r[4] or "N/A"
            latest     = r[5] or "N/A"
            avg_blk    = r[6]
            svc_types  = r[7] or "J"
            share_pct  = round(ops / total_ops * 100, 1) if total_ops else 0

            # Get capacity info for primary aircraft type (first in list)
            primary_ac = ac_types.split(",")[0].strip()
            cap        = get_pax_capacity_info(primary_ac)
            seats      = cap.get("total_seats", "N/A") if cap.get("found") else "N/A"
            ac_cat     = cap.get("aircraft_category", "N/A") if cap.get("found") else "N/A"

            airlines.append({
                "airline":              al_code,
                "unique_flight_numbers": uniq,
                "weekly_operations":    ops,
                "market_share_pct":     share_pct,
                "aircraft_types":       ac_types,
                "primary_aircraft":     primary_ac,
                "seat_capacity":        seats,
                "aircraft_category":    ac_cat,
                "earliest_departure":   earliest,
                "latest_departure":     latest,
                "avg_block_min":        round(avg_blk, 0) if avg_blk and not math.isnan(avg_blk) else None,
                "haul_type":            classify_haul(int(avg_blk) if avg_blk and not math.isnan(avg_blk) else None),
                "service_types":        svc_types,
            })

        # Market leader
        leader = airlines[0]["airline"] if airlines else "N/A"

        return {
            "origin":           origin,
            "destination":      destination,
            "found":            True,
            "total_airlines":   len(airlines),
            "market_leader":    leader,
            "total_operations": total_ops,
            "airlines":         airlines,
            "note":             "Operations = expanded weekly frequency (one row per operating day). Market share is by frequency.",
        }

    except Exception as exc:
        logger.warning(f"get_competitor_analysis failed: {exc}")
        return {
            "origin":      origin,
            "destination": destination,
            "found":       False,
            "error":       str(exc),
        }


def get_nonops_flights(origin: Optional[str] = None, destination: Optional[str] = None,
                       airline: Optional[str] = None) -> Dict[str, Any]:
    """
    Return non-revenue / positioning (service_type='G') flights.
    These represent ferry / positioning operations not sold to passengers.
    """
    from app.database.db import get_connection

    try:
        conn   = get_connection()
        params = ["G"]
        where  = ["service_type = ?"]
        if origin:
            where.append("origin = ?");      params.append(origin.upper())
        if destination:
            where.append("destination = ?"); params.append(destination.upper())
        if airline:
            where.append("airline = ?");     params.append(airline.upper())

        sql = f"""
            SELECT airline, flight_number, origin, destination,
                   departure_local, arrival_local, aircraft_type, frequency,
                   effective_from, effective_to
            FROM flights
            WHERE {' AND '.join(where)}
            ORDER BY airline, departure_local
            LIMIT 100
        """
        rows = conn.execute(sql, params).fetchall()
        cols = ["airline","flight_number","origin","destination",
                "departure_local","arrival_local","aircraft_type",
                "frequency","effective_from","effective_to"]
        flights = [dict(zip(cols, r)) for r in rows]
        # stringify datetimes
        for f in flights:
            for k, v in f.items():
                if hasattr(v, "isoformat"):
                    f[k] = v.isoformat()

        return {
            "service_type":   "G (Non-Revenue/Positioning)",
            "count":          len(flights),
            "flights":        flights,
            "note":           "These are ferry/positioning operations (SSIM service_type='G'), not sold to passengers.",
        }
    except Exception as exc:
        logger.warning(f"get_nonops_flights failed: {exc}")
        return {"error": str(exc), "flights": []}
