"""
Route spacing rule.

Ensures that flights on the same route (or from the same airport) are not
scheduled too close together, creating market-cannibalisation risk and
reducing network utility.
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
import pandas as pd

from app.utils.time_utils import minutes_between_times


def _minutes_gap(dt1: datetime, dt2: datetime) -> int:
    """Absolute gap in minutes between two datetimes."""
    return abs(int((dt2 - dt1).total_seconds() / 60))


def check_route_spacing(
    proposed_dep: datetime,
    origin: str,
    destination: str,
    airline: str,
    existing_flights: pd.DataFrame,
    config: Dict,
) -> Dict[str, Any]:
    """
    Check whether a proposed departure is spaced sufficiently from existing
    flights on the same O&D pair.

    Returns standard rule dict.
    """
    violations: List[str] = []
    warnings:   List[str] = []
    conflicts: List[Dict] = []

    sp_cfg     = config.get("spacing", {})
    min_gap    = int(sp_cfg.get("minimum_minutes_same_route", 60))
    prefer_gap = int(sp_cfg.get("prefer_minutes_same_route", min_gap + 30))

    if existing_flights is None or existing_flights.empty:
        return {
            "feasible":   True,
            "violations": [],
            "warnings":   ["No existing flights on this route for spacing check."],
            "metrics":    {"route": f"{origin}-{destination}", "checked_against": 0},
        }

    same_route = existing_flights[
        (existing_flights["origin"].str.upper() == origin.upper()) &
        (existing_flights["destination"].str.upper() == destination.upper())
    ]

    for _, flt in same_route.iterrows():
        ex_dep = flt.get("departure_local")
        if ex_dep is None:
            continue
        if isinstance(ex_dep, str):
            try:
                ex_dep = datetime.strptime(ex_dep, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue

        gap = _minutes_gap(proposed_dep, ex_dep)
        flt_id = str(flt.get("flight_number", "?"))
        al = str(flt.get("airline", "?"))

        if gap < min_gap:
            violations.append(
                f"Spacing violation on {origin}–{destination}: proposed dep "
                f"{proposed_dep:%H:%M} is only {gap} min from {al}{flt_id} "
                f"({ex_dep:%H:%M}). Minimum: {min_gap} min."
            )
            conflicts.append({"flight": flt_id, "gap_minutes": gap, "severity": "violation"})
        elif gap < prefer_gap:
            warnings.append(
                f"Suboptimal spacing on {origin}–{destination}: {gap} min gap to "
                f"{al}{flt_id}. Preferred: {prefer_gap} min."
            )
            conflicts.append({"flight": flt_id, "gap_minutes": gap, "severity": "warning"})

    return {
        "feasible":   len(violations) == 0,
        "violations": violations,
        "warnings":   warnings,
        "metrics": {
            "route":            f"{origin}-{destination}",
            "proposed_dep":     proposed_dep.strftime("%H:%M"),
            "minimum_gap_min":  min_gap,
            "preferred_gap_min": prefer_gap,
            "checked_against":  len(same_route),
            "conflicts":        conflicts,
        },
    }


def check_airport_departure_spacing(
    proposed_dep: datetime,
    origin: str,
    airline: str,
    existing_flights: pd.DataFrame,
    config: Dict,
) -> Dict[str, Any]:
    """
    Check minimum spacing between any two departures from the same airport
    for the same airline (gate / slot constraint proxy).
    """
    violations: List[str] = []
    warnings:   List[str] = []

    sp_cfg  = config.get("spacing", {})
    min_gap = int(sp_cfg.get("minimum_minutes_same_airport", 15))

    if existing_flights is None or existing_flights.empty:
        return {
            "feasible": True, "violations": [], "warnings": [],
            "metrics": {"airport_gap_checked": 0},
        }

    same_airport = existing_flights[
        (existing_flights["origin"].str.upper() == origin.upper()) &
        (existing_flights["airline"].str.upper() == airline.upper())
    ]

    near_flights = []
    for _, flt in same_airport.iterrows():
        ex_dep = flt.get("departure_local")
        if ex_dep is None:
            continue
        if isinstance(ex_dep, str):
            try:
                ex_dep = datetime.strptime(ex_dep, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
        gap = _minutes_gap(proposed_dep, ex_dep)
        if gap < min_gap:
            near_flights.append(str(flt.get("flight_number", "?")))
            violations.append(
                f"Airport spacing at {origin}: {gap} min to {flt.get('flight_number')} "
                f"(minimum {min_gap} min)."
            )

    return {
        "feasible":   len(violations) == 0,
        "violations": violations,
        "warnings":   warnings,
        "metrics": {
            "airport": origin, "airline": airline,
            "minimum_gap_min": min_gap, "near_flights": near_flights,
        },
    }


def find_best_departure_window(
    origin: str,
    destination: str,
    airline: str,
    existing_flights: pd.DataFrame,
    config: Dict,
    window_start_hour: int = 6,
    window_end_hour:   int = 22,
    step_minutes:      int = 30,
) -> List[Dict[str, Any]]:
    """
    Suggest departure time slots on *origin*–*destination* that satisfy spacing
    constraints.  Returns list of candidate slots sorted by gap-to-nearest-flight
    (descending — biggest gap first).
    """
    from datetime import date

    sp_cfg  = config.get("spacing", {})
    min_gap = int(sp_cfg.get("minimum_minutes_same_route", 60))

    ref_date = date(2000, 1, 1)
    candidates = []

    current = datetime(ref_date.year, ref_date.month, ref_date.day, window_start_hour, 0)
    end_dt  = datetime(ref_date.year, ref_date.month, ref_date.day, window_end_hour, 0)

    same_route = (
        existing_flights[
            (existing_flights["origin"].str.upper() == origin.upper()) &
            (existing_flights["destination"].str.upper() == destination.upper())
        ]
        if existing_flights is not None and not existing_flights.empty
        else pd.DataFrame()
    )

    while current <= end_dt:
        min_dist = 9999
        for _, flt in same_route.iterrows():
            ex_dep = flt.get("departure_local")
            if ex_dep is not None:
                if isinstance(ex_dep, str):
                    try:
                        ex_dep = datetime.strptime(ex_dep, "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        ex_dep = None
                if ex_dep:
                    gap = _minutes_gap(current, ex_dep.replace(year=2000, month=1, day=1))
                    min_dist = min(min_dist, gap)

        if min_dist >= min_gap:
            candidates.append({
                "departure_time": current.strftime("%H:%M"),
                "gap_to_nearest_min": min_dist if min_dist < 9999 else None,
                "feasible": True,
            })
        current += timedelta(minutes=step_minutes)

    # Sort: largest gap first
    candidates.sort(key=lambda x: -(x["gap_to_nearest_min"] or 9999))
    return candidates[:10]
