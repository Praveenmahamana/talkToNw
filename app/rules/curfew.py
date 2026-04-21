"""
Airport curfew rule.

Validates that a proposed departure or arrival does not fall within a
restricted operating-hours window at a curfewed airport.
"""

from datetime import datetime, time
from typing import Any, Dict, List, Optional
from app.utils.time_utils import parse_time, is_within_curfew


def _get_curfew(airport: str, config: Dict) -> Optional[Dict]:
    """Return the curfew config block for *airport*, or None."""
    curfew_cfg = config.get("curfew", {}).get("airports", {})
    return curfew_cfg.get(airport.upper())


def check_curfew(
    airport: str,
    departure_local: Optional[datetime],
    arrival_local: Optional[datetime],
    config: Dict,
) -> Dict[str, Any]:
    """
    Check whether a departure or arrival at *airport* violates a curfew.

    Parameters
    ----------
    airport          : IATA airport code
    departure_local  : scheduled departure local datetime (or None)
    arrival_local    : scheduled arrival local datetime   (or None)
    config           : loaded rules.yaml as a dict

    Returns
    -------
    Standard rule dict: feasible, violations, warnings, metrics.
    """
    violations: List[str] = []
    warnings:   List[str] = []

    curfew = _get_curfew(airport, config)

    if curfew is None:
        return {
            "feasible":   True,
            "violations": [],
            "warnings":   [f"No curfew data for {airport} — assumed 24/7 operations."],
            "metrics":    {"airport": airport, "curfew_defined": False},
        }

    c_start = parse_time(curfew["start"])
    c_end   = parse_time(curfew["end"])

    if c_start is None or c_end is None:
        warnings.append(f"Curfew config for {airport} has invalid times; skipping check.")
        return {
            "feasible":   True,
            "violations": violations,
            "warnings":   warnings,
            "metrics":    {"airport": airport, "curfew_defined": True, "parse_error": True},
        }

    metrics: Dict[str, Any] = {
        "airport": airport,
        "curfew_start": curfew["start"],
        "curfew_end": curfew["end"],
        "curfew_defined": True,
    }

    # Evaluate departure
    if departure_local is not None:
        dep_time = departure_local.time()
        if is_within_curfew(dep_time, c_start, c_end):
            violations.append(
                f"Departure at {dep_time.strftime('%H:%M')} violates curfew at {airport} "
                f"({curfew['start']} – {curfew['end']} local)."
            )
        metrics["departure_local"] = departure_local.strftime("%H:%M")

    # Evaluate arrival
    if arrival_local is not None:
        arr_time = arrival_local.time()
        if is_within_curfew(arr_time, c_start, c_end):
            violations.append(
                f"Arrival at {arr_time.strftime('%H:%M')} violates curfew at {airport} "
                f"({curfew['start']} – {curfew['end']} local)."
            )
        metrics["arrival_local"] = arrival_local.strftime("%H:%M")

    # Warn if within 30 minutes of curfew boundary
    def _near_boundary(t: time, threshold_mins: int = 30) -> bool:
        from app.utils.time_utils import minutes_between_times
        to_start = minutes_between_times(t, c_start)
        from_end = minutes_between_times(c_end, t)
        return min(to_start, from_end) < threshold_mins

    if len(violations) == 0:
        for label, dt in [("Departure", departure_local), ("Arrival", arrival_local)]:
            if dt is not None and _near_boundary(dt.time()):
                warnings.append(
                    f"{label} at {dt.time().strftime('%H:%M')} is within 30 min of "
                    f"curfew boundary at {airport}."
                )

    return {
        "feasible":   len(violations) == 0,
        "violations": violations,
        "warnings":   warnings,
        "metrics":    metrics,
    }


def check_curfew_flight(flight: Dict[str, Any], config: Dict) -> Dict[str, Any]:
    """
    Evaluate curfew for both origin (departure) and destination (arrival) of a flight.
    Returns a merged result covering both airports.
    """
    origin_result = check_curfew(
        flight.get("origin", ""),
        flight.get("departure_local"),
        None,
        config,
    )
    dest_result = check_curfew(
        flight.get("destination", ""),
        None,
        flight.get("arrival_local"),
        config,
    )

    violations = origin_result["violations"] + dest_result["violations"]
    warnings   = origin_result["warnings"]   + dest_result["warnings"]

    return {
        "feasible":   len(violations) == 0,
        "violations": violations,
        "warnings":   warnings,
        "metrics": {
            "origin":      origin_result["metrics"],
            "destination": dest_result["metrics"],
        },
    }
