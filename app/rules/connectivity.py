"""
Connectivity rule.

Evaluates whether passengers can make a connection between an inbound and
outbound flight at an intermediate hub, respecting Minimum Connection Times.
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
import pandas as pd
from loguru import logger

from app.utils.time_utils import minutes_between_times


def _get_mct(airport: str, config: Dict) -> int:
    """Return the Minimum Connection Time (minutes) for an airport."""
    conn_cfg = config.get("connectivity", {})
    extended = conn_cfg.get("extended_mct", {})
    if airport.upper() in extended:
        return int(extended[airport.upper()])
    return int(conn_cfg.get("minimum_connection_minutes", 45))


def check_connection(
    inbound_arrival: datetime,
    outbound_departure: datetime,
    connection_airport: str,
    is_international: bool,
    config: Dict,
) -> Dict[str, Any]:
    """
    Verify whether a connection from an arriving flight to a departing flight
    at *connection_airport* is feasible.

    Returns standard rule dict.
    """
    violations: List[str] = []
    warnings:   List[str] = []

    if inbound_arrival is None or outbound_departure is None:
        return {
            "feasible":   False,
            "violations": ["Missing arrival or departure datetime for connection check."],
            "warnings":   [],
            "metrics":    {},
        }

    conn_cfg = config.get("connectivity", {})
    mct = _get_mct(connection_airport, config)
    if is_international:
        mct = max(mct, int(conn_cfg.get("minimum_connection_minutes_international", 60)))

    max_connect = int(conn_cfg.get("maximum_connection_minutes", 240))

    connection_minutes = int(
        (outbound_departure - inbound_arrival).total_seconds() / 60
    )

    metrics = {
        "connection_airport":    connection_airport,
        "inbound_arrival":       inbound_arrival.strftime("%H:%M"),
        "outbound_departure":    outbound_departure.strftime("%H:%M"),
        "connection_minutes":    connection_minutes,
        "minimum_connection_minutes": mct,
        "maximum_connection_minutes": max_connect,
        "is_international":      is_international,
    }

    if connection_minutes < 0:
        violations.append(
            f"Outbound departs ({outbound_departure:%H:%M}) BEFORE inbound arrives "
            f"({inbound_arrival:%H:%M}) at {connection_airport}."
        )
    elif connection_minutes < mct:
        violations.append(
            f"Connection at {connection_airport} is too tight: "
            f"{connection_minutes} min available, {mct} min MCT required."
        )
    elif connection_minutes > max_connect:
        warnings.append(
            f"Connection at {connection_airport} is very long: "
            f"{connection_minutes} min (max preferred: {max_connect} min)."
        )
    elif connection_minutes < mct * 1.25:
        warnings.append(
            f"Connection at {connection_airport} is close to MCT: "
            f"{connection_minutes} min available (MCT {mct} min)."
        )

    metrics["buffer_minutes"] = connection_minutes - mct

    return {
        "feasible":   len(violations) == 0,
        "violations": violations,
        "warnings":   warnings,
        "metrics":    metrics,
    }


def find_connections(
    inbound_flights: pd.DataFrame,
    outbound_flights: pd.DataFrame,
    hub: str,
    config: Dict,
) -> List[Dict[str, Any]]:
    """
    Find all feasible connections between a set of arriving and departing flights
    at *hub*.  Returns a list of connection dicts with feasibility info.
    """
    results = []
    conn_cfg = config.get("connectivity", {})
    max_connect = int(conn_cfg.get("maximum_connection_minutes", 240))

    for _, inb in inbound_flights.iterrows():
        arr = inb.get("arrival_local")
        if arr is None:
            continue

        for _, outb in outbound_flights.iterrows():
            dep = outb.get("departure_local")
            if dep is None:
                continue

            gap = int((dep - arr).total_seconds() / 60) if dep > arr else -1
            if gap < 0 or gap > max_connect:
                continue

            result = check_connection(arr, dep, hub, True, config)
            results.append({
                "inbound":       inb.get("flight_number"),
                "outbound":      outb.get("flight_number"),
                "connection_gap": gap,
                "feasible":      result["feasible"],
                "violations":    result["violations"],
                "warnings":      result["warnings"],
            })

    return results


def count_connectivity_gain(
    hub: str,
    new_flight: Dict[str, Any],
    existing_flights: pd.DataFrame,
    config: Dict,
) -> Dict[str, Any]:
    """
    Count how many additional connections a new flight enables at *hub*.
    Returns counts of new inbound and outbound connections.
    """
    if existing_flights is None or existing_flights.empty:
        return {"new_connections": 0, "details": []}

    new_arr = new_flight.get("arrival_local")
    new_dep = new_flight.get("departure_local")
    results = []

    # New flight arrives at hub → connects to existing departures
    if new_flight.get("destination", "").upper() == hub.upper() and new_arr:
        outbounds = existing_flights[
            existing_flights["origin"].str.upper() == hub.upper()
        ]
        conns = find_connections(
            pd.DataFrame([new_flight]), outbounds, hub, config
        )
        results.extend([c for c in conns if c["feasible"]])

    # New flight departs from hub → connects from existing arrivals
    if new_flight.get("origin", "").upper() == hub.upper() and new_dep:
        inbounds = existing_flights[
            existing_flights["destination"].str.upper() == hub.upper()
        ]
        conns = find_connections(
            inbounds, pd.DataFrame([new_flight]), hub, config
        )
        results.extend([c for c in conns if c["feasible"]])

    return {
        "new_connections": len(results),
        "details": results[:10],  # cap to keep response manageable
    }
