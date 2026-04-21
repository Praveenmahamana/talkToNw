"""
simulate_retime_flight — deterministic retime feasibility simulation.

Compares the current flight timing against a proposed new timing and
measures the delta across all rule-engine dimensions.
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
import pandas as pd
from loguru import logger

from app.rules.rule_engine import run_all_rules, load_config
from app.rules.connectivity import count_connectivity_gain
from app.rules.scoring import score_confidence
from app.services.schedule_service import ScheduleService
from app.utils.time_utils import format_duration, get_airport_timezone, local_to_utc


def _apply_retime(flight: Dict[str, Any], new_departure: datetime) -> Dict[str, Any]:
    """Return a copy of *flight* with updated departure and adjusted arrival."""
    retimed = dict(flight)
    old_dep = flight.get("departure_local")
    old_arr = flight.get("arrival_local")

    retimed["departure_local"] = new_departure

    # Prefer the explicit block_time field; fall back to datetime difference
    explicit_block = flight.get("block_time")
    if explicit_block and isinstance(explicit_block, (int, float)) and explicit_block > 0:
        block = int(explicit_block)
    elif old_dep and old_arr and isinstance(old_dep, datetime) and isinstance(old_arr, datetime):
        block = int((old_arr - old_dep).total_seconds() / 60)
    else:
        block = None

    if block:
        retimed["arrival_local"] = new_departure + timedelta(minutes=block)

    # Update UTC
    origin = retimed.get("origin", "")
    dest   = retimed.get("destination", "")
    if retimed.get("departure_local"):
        tz = get_airport_timezone(origin)
        if tz:
            retimed["departure_utc"] = local_to_utc(retimed["departure_local"], tz)
    if retimed.get("arrival_local"):
        tz = get_airport_timezone(dest)
        if tz:
            retimed["arrival_utc"] = local_to_utc(retimed["arrival_local"], tz)

    return retimed


def simulate_retime_flight(
    current_flight: Dict[str, Any],
    proposed_departure: datetime,
    hub: Optional[str] = None,
    config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """
    Evaluate the impact of retiming an existing flight.

    Parameters
    ----------
    current_flight     : dict of the existing flight (from DB or API)
    proposed_departure : new departure datetime (local)
    hub                : IATA hub code for connectivity delta
    config             : override rules config

    Returns
    -------
    {
        "verdict":                  str,
        "feasible":                 bool,
        "feasibility_score":        int,
        "network_value_score":      int,
        "confidence":               str,
        "current_timing":           dict,
        "proposed_timing":          dict,
        "delta":                    dict,
        "violations":               [str],
        "warnings":                 [str],
        "connectivity_change":      dict,
        "conflicts":                [str],
        "turnaround_impact":        dict,
        "evidence":                 dict,
    }
    """
    cfg = config or load_config()
    svc = ScheduleService()

    try:
        all_flights = svc.get_all_flights()
    except Exception as exc:
        logger.warning(f"Could not fetch existing flights: {exc}")
        all_flights = pd.DataFrame()

    current_dep = current_flight.get("departure_local")
    current_arr = current_flight.get("arrival_local")
    origin      = current_flight.get("origin", "")
    destination = current_flight.get("destination", "")
    flt_num     = current_flight.get("flight_number", "")

    # Remove current flight from the pool (we're retiming it, not adding a new one)
    if not all_flights.empty and flt_num:
        pool = all_flights[all_flights["flight_number"] != flt_num].copy()
    else:
        pool = all_flights

    # ── Build the retimed flight ──────────────────────────────────────────────
    retimed_flight = _apply_retime(current_flight, proposed_departure)

    logger.info(
        f"Simulating retime of {flt_num} ({origin}→{destination}): "
        f"{current_dep.strftime('%H:%M') if current_dep else '?'} → "
        f"{proposed_departure.strftime('%H:%M')}"
    )

    # ── Run rules against both timings ────────────────────────────────────────
    current_rule_result = run_all_rules(current_flight, pool, hub=hub, config=cfg)
    retimed_rule_result = run_all_rules(retimed_flight, pool, hub=hub, config=cfg)

    # ── Delta computation ────────────────────────────────────────────────────
    shift_minutes = 0
    if current_dep and isinstance(current_dep, datetime):
        shift_minutes = int((proposed_departure - current_dep).total_seconds() / 60)

    block_time = current_flight.get("block_time", 0) or 0
    proposed_arr = retimed_flight.get("arrival_local")

    delta = {
        "departure_shift_minutes": shift_minutes,
        "departure_shift_direction": "later" if shift_minutes > 0 else ("earlier" if shift_minutes < 0 else "unchanged"),
        "feasibility_score_change": retimed_rule_result["feasibility_score"] - current_rule_result["feasibility_score"],
        "network_value_change":     retimed_rule_result["network_value_score"] - current_rule_result["network_value_score"],
        "new_violations":    [v for v in retimed_rule_result["violations"] if v not in current_rule_result["violations"]],
        "resolved_violations": [v for v in current_rule_result["violations"] if v not in retimed_rule_result["violations"]],
    }

    # ── Connectivity delta ────────────────────────────────────────────────────
    effective_hub = hub or origin or destination
    current_conn = count_connectivity_gain(effective_hub, current_flight, pool, cfg) if not pool.empty else {"new_connections": 0}
    retimed_conn = count_connectivity_gain(effective_hub, retimed_flight, pool, cfg) if not pool.empty else {"new_connections": 0}

    connectivity_change = {
        "current_connections": current_conn.get("new_connections", 0),
        "proposed_connections": retimed_conn.get("new_connections", 0),
        "delta": retimed_conn.get("new_connections", 0) - current_conn.get("new_connections", 0),
    }

    # ── Verdict ───────────────────────────────────────────────────────────────
    fs    = retimed_rule_result["feasibility_score"]
    delta_fs = delta["feasibility_score_change"]

    if retimed_rule_result["feasible"] and delta_fs >= 0:
        verdict = f"RETIME RECOMMENDED — Score improves by {delta_fs:+d} pts (→{fs}/100)."
    elif retimed_rule_result["feasible"] and delta_fs < 0:
        verdict = (
            f"RETIME FEASIBLE BUT WORSE — Score drops by {abs(delta_fs)} pts (→{fs}/100). "
            f"Review trade-offs before proceeding."
        )
    else:
        verdict = (
            f"RETIME NOT FEASIBLE — {len(retimed_rule_result['violations'])} "
            f"violation(s) at proposed time."
        )

    return {
        "verdict":                verdict,
        "feasible":               retimed_rule_result["feasible"],
        "feasibility_score":      fs,
        "network_value_score":    retimed_rule_result["network_value_score"],
        "confidence":             retimed_rule_result["confidence"],
        "current_timing": {
            "departure": current_dep.strftime("%H:%M") if current_dep else None,
            "arrival":   current_arr.strftime("%H:%M") if current_arr else None,
            "feasibility_score": current_rule_result["feasibility_score"],
        },
        "proposed_timing": {
            "departure": proposed_departure.strftime("%H:%M"),
            "arrival":   proposed_arr.strftime("%H:%M") if proposed_arr else None,
            "feasibility_score": fs,
        },
        "delta":                  delta,
        "violations":             retimed_rule_result["violations"],
        "warnings":               retimed_rule_result["warnings"],
        "connectivity_change":    connectivity_change,
        "conflicts":              retimed_rule_result.get("violations", []),
        "turnaround_impact":      retimed_rule_result.get("rule_results", {}).get("turnaround", {}),
        "evidence":               retimed_rule_result["rule_results"],
    }
