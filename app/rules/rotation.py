"""
Aircraft rotation / overlap rule.

Detects when a proposed (or existing) flight schedule causes an aircraft
to be assigned to two flights simultaneously — an impossible rotation.
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
import pandas as pd
from loguru import logger


def _to_dt(val) -> Optional[datetime]:
    if isinstance(val, datetime):
        return val
    if isinstance(val, str) and val:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(val, fmt)
            except ValueError:
                continue
    return None


def check_aircraft_overlap(
    airline: str,
    aircraft_type: str,
    proposed_dep: datetime,
    proposed_arr: datetime,
    existing_flights: pd.DataFrame,
    turnaround_buffer_minutes: int = 0,
) -> Dict[str, Any]:
    """
    Check whether a proposed flight (dep→arr for a given aircraft_type/airline)
    conflicts with any flight already in *existing_flights*.

    Parameters
    ----------
    airline, aircraft_type : identifiers for the rotation chain
    proposed_dep/arr       : UTC datetimes for the proposed flight
    existing_flights       : DataFrame with columns [flight_number, departure_utc,
                             arrival_utc, origin, destination]
    turnaround_buffer_minutes : add this buffer around existing flights

    Returns
    -------
    Standard rule dict.
    """
    violations: List[str] = []
    warnings:   List[str] = []

    if proposed_dep is None or proposed_arr is None:
        return {
            "feasible":   False,
            "violations": ["Proposed flight has no UTC departure/arrival — cannot check overlap."],
            "warnings":   [],
            "metrics":    {},
        }

    if existing_flights is None or existing_flights.empty:
        return {
            "feasible":   True,
            "violations": [],
            "warnings":   ["No existing flights found for overlap check."],
            "metrics":    {"overlapping_flights": 0},
        }

    buffer = timedelta(minutes=turnaround_buffer_minutes)
    overlap_list = []

    for _, flt in existing_flights.iterrows():
        ex_dep = _to_dt(flt.get("departure_utc"))
        ex_arr = _to_dt(flt.get("arrival_utc"))
        if ex_dep is None or ex_arr is None:
            continue

        # Check overlap: [ex_dep - buffer, ex_arr + buffer] ∩ [proposed_dep, proposed_arr]
        if (proposed_dep < ex_arr + buffer) and (proposed_arr > ex_dep - buffer):
            flt_id = str(flt.get("flight_number", "UNKNOWN"))
            overlap_list.append({
                "flight_number":  flt_id,
                "existing_dep":   ex_dep.strftime("%Y-%m-%d %H:%M"),
                "existing_arr":   ex_arr.strftime("%Y-%m-%d %H:%M"),
                "proposed_dep":   proposed_dep.strftime("%Y-%m-%d %H:%M"),
                "proposed_arr":   proposed_arr.strftime("%Y-%m-%d %H:%M"),
                "overlap_minutes": int(
                    (min(proposed_arr, ex_arr) - max(proposed_dep, ex_dep)).total_seconds() / 60
                ),
            })
            violations.append(
                f"Aircraft conflict: proposed {proposed_dep:%H:%M}–{proposed_arr:%H:%M} "
                f"overlaps {flt_id} ({ex_dep:%H:%M}–{ex_arr:%H:%M})."
            )

    return {
        "feasible":   len(violations) == 0,
        "violations": violations,
        "warnings":   warnings,
        "metrics": {
            "overlapping_flights": len(overlap_list),
            "conflicts": overlap_list,
            "aircraft_type": aircraft_type,
            "airline": airline,
        },
    }


def validate_rotation_chain(
    flights: pd.DataFrame,
    config: Dict,
) -> Dict[str, Any]:
    """
    Validate that an ordered sequence of flights forms a feasible rotation:
    - Aircraft must be at the correct station before each departure
    - Ground time must meet turnaround minimum

    *flights* must be sorted by departure_local ascending and represent a
    single aircraft's schedule for one day.
    """
    from app.rules.turnaround import check_turnaround

    violations: List[str] = []
    warnings:   List[str] = []
    metrics: Dict[str, Any] = {"legs": []}

    if flights.empty or len(flights) < 2:
        return {"feasible": True, "violations": [], "warnings": [], "metrics": metrics}

    prev = None
    for _, flt in flights.iterrows():
        if prev is not None:
            # Station check
            if prev.get("destination") != flt.get("origin"):
                violations.append(
                    f"Rotation break: {prev['flight_number']} arrives at "
                    f"{prev['destination']} but next flight departs from {flt['origin']}."
                )

            # Turnaround check
            ta_result = check_turnaround(
                arrival_dt   = _to_dt(prev.get("arrival_local")) or _to_dt(prev.get("arrival_utc")),
                departure_dt = _to_dt(flt.get("departure_local")) or _to_dt(flt.get("departure_utc")),
                aircraft_type= flt.get("aircraft_type", ""),
                station      = flt.get("origin", ""),
                config       = config,
            )
            violations.extend(ta_result["violations"])
            warnings.extend(ta_result["warnings"])
            metrics["legs"].append(ta_result["metrics"])

        prev = flt

    return {
        "feasible":   len(violations) == 0,
        "violations": violations,
        "warnings":   warnings,
        "metrics":    metrics,
    }
