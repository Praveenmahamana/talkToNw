"""
Turnaround feasibility rule.

Checks whether an aircraft has sufficient ground time between an arriving
and a departing flight at the same station.
"""

from datetime import datetime
from typing import Any, Dict, Optional
from app.utils.time_utils import calculate_block_time_minutes


def _get_min_turnaround(aircraft_type: str, config: Dict) -> int:
    """
    Return the minimum turnaround time (minutes) for a given aircraft type,
    using the rules.yaml config block:

        turnaround:
          minimum_minutes:
            default: 45
            B777: 90
            ...
          wide_body_codes: [...]
          regional_codes: [...]
    """
    ta_cfg   = config.get("turnaround", {})
    min_cfg  = ta_cfg.get("minimum_minutes", {})
    ac       = (aircraft_type or "").upper().strip()

    # 1. Exact match
    if ac in min_cfg:
        return int(min_cfg[ac])

    # 2. Prefix match against wide-body / regional code lists
    wb_codes = [c.upper() for c in ta_cfg.get("wide_body_codes", [])]
    rg_codes = [c.upper() for c in ta_cfg.get("regional_codes", [])]

    if any(ac.startswith(c) for c in wb_codes):
        return int(min_cfg.get("wide_body", min_cfg.get("default", 90)))

    if any(ac.startswith(c) for c in rg_codes):
        return int(min_cfg.get("regional", min_cfg.get("default", 30)))

    return int(min_cfg.get("narrow_body", min_cfg.get("default", 45)))


def check_turnaround(
    arrival_dt: datetime,
    departure_dt: datetime,
    aircraft_type: str,
    station: str,
    config: Dict,
) -> Dict[str, Any]:
    """
    Validate whether the ground time at *station* between *arrival_dt* and
    *departure_dt* meets the minimum turnaround standard.

    Returns the standard rule-engine dict:
        {
            "feasible":   bool,
            "violations": list[str],
            "warnings":   list[str],
            "metrics":    dict,
        }
    """
    violations: list = []
    warnings:   list = []

    if arrival_dt is None or departure_dt is None:
        return {
            "feasible":   False,
            "violations": ["Missing arrival or departure datetime — cannot evaluate turnaround."],
            "warnings":   [],
            "metrics":    {},
        }

    # Compute ground time as a signed integer (negative = impossible rotation)
    ground_minutes = int((departure_dt - arrival_dt).total_seconds() / 60)
    min_required   = _get_min_turnaround(aircraft_type, config)

    metrics = {
        "ground_time_minutes": ground_minutes,
        "minimum_required_minutes": min_required,
        "aircraft_type": aircraft_type,
        "station": station,
        "buffer_minutes": ground_minutes - min_required,
    }

    if ground_minutes < 0:
        violations.append(
            f"Departure at {departure_dt:%H:%M} is BEFORE arrival at {arrival_dt:%H:%M} "
            f"at {station} — impossible rotation."
        )
    elif ground_minutes < min_required:
        violations.append(
            f"Insufficient turnaround at {station}: {ground_minutes} min available, "
            f"{min_required} min required for {aircraft_type or 'unknown type'}."
        )
    elif ground_minutes < min_required * 1.25:
        warnings.append(
            f"Tight turnaround at {station}: {ground_minutes} min available "
            f"(minimum {min_required} min). Less than 25% buffer."
        )

    return {
        "feasible":   len(violations) == 0,
        "violations": violations,
        "warnings":   warnings,
        "metrics":    metrics,
    }


def check_turnaround_pair(
    inbound_flight: Dict[str, Any],
    outbound_flight: Dict[str, Any],
    config: Dict,
) -> Dict[str, Any]:
    """
    Convenience wrapper: evaluate turnaround between two flight dicts
    (each must have arrival_local/arrival_utc and departure_local/departure_utc keys).
    """
    station     = inbound_flight.get("destination") or outbound_flight.get("origin", "UNK")
    aircraft    = inbound_flight.get("aircraft_type") or outbound_flight.get("aircraft_type", "")
    arr_dt      = inbound_flight.get("arrival_local") or inbound_flight.get("arrival_utc")
    dep_dt      = outbound_flight.get("departure_local") or outbound_flight.get("departure_utc")

    return check_turnaround(arr_dt, dep_dt, aircraft, station, config)
