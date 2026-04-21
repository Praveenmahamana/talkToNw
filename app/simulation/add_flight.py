"""
simulate_add_flight — deterministic feasibility simulation.

Evaluates whether a new flight can be added to the existing schedule
by running all rule-engine checks and computing composite scores.

Outputs:
  - verdict
  - feasibility_score (0–100)
  - network_value_score (0–100)
  - risks
  - alternatives (best departure windows)
  - evidence (all rule outputs)
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
import pandas as pd
from loguru import logger

from app.rules.rule_engine import run_all_rules, load_config
from app.rules.spacing import find_best_departure_window
from app.rules.scoring import score_confidence
from app.services.schedule_service import ScheduleService
from app.utils.time_utils import format_duration, get_airport_timezone, local_to_utc


def _enrich_with_utc(flight: Dict[str, Any]) -> Dict[str, Any]:
    """Add UTC datetimes from local times + timezone lookup if not supplied."""
    dep_local = flight.get("departure_local")
    arr_local = flight.get("arrival_local")
    origin    = flight.get("origin", "")
    dest      = flight.get("destination", "")

    if dep_local and not flight.get("departure_utc"):
        tz = get_airport_timezone(origin)
        if tz:
            flight["departure_utc"] = local_to_utc(dep_local, tz)

    if arr_local and not flight.get("arrival_utc"):
        tz = get_airport_timezone(dest)
        if tz:
            flight["arrival_utc"] = local_to_utc(arr_local, tz)

    return flight


def simulate_add_flight(
    proposed_flight: Dict[str, Any],
    hub: Optional[str] = None,
    config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """
    Full feasibility simulation for adding a new flight.

    Parameters
    ----------
    proposed_flight : dict with at minimum:
        - origin, destination
        - departure_local (datetime)
        - arrival_local   (datetime, optional)
        - aircraft_type   (optional)
        - airline         (optional)
        - block_time      (optional, minutes)
    hub : IATA code of the connecting hub (for connectivity scoring)
    config : override rules.yaml config

    Returns
    -------
    {
        "verdict":              str,
        "feasibility_score":    int (0–100),
        "network_value_score":  int (0–100),
        "confidence":           str,
        "feasible":             bool,
        "risks":                [str],
        "alternatives":         [dict],
        "evidence":             { rule: result, ... },
        "why_not":              str   (if infeasible),
        "best_window":          [dict],
    }
    """
    cfg = config or load_config()

    # Ensure we have UTC times
    proposed_flight = _enrich_with_utc(dict(proposed_flight))

    origin      = proposed_flight.get("origin", "")
    destination = proposed_flight.get("destination", "")
    airline     = proposed_flight.get("airline", "")
    dep_local   = proposed_flight.get("departure_local")

    # ── Fetch existing schedule ───────────────────────────────────────────────
    svc = ScheduleService()
    try:
        all_flights = svc.get_all_flights()
    except Exception as exc:
        logger.warning(f"Could not fetch existing flights: {exc}")
        all_flights = pd.DataFrame()

    # ── Run all rules ─────────────────────────────────────────────────────────
    logger.info(
        f"Simulating add flight: {airline} {origin}→{destination} "
        f"dep={dep_local.strftime('%H:%M') if dep_local else 'N/A'}"
    )
    rule_result = run_all_rules(proposed_flight, all_flights, hub=hub, config=cfg)

    # ── Build risks list ──────────────────────────────────────────────────────
    risks: List[str] = list(rule_result["violations"])

    # Cannibalization risk check
    if not all_flights.empty and origin and destination:
        same_route = all_flights[
            (all_flights["origin"].str.upper() == origin.upper()) &
            (all_flights["destination"].str.upper() == destination.upper())
        ]
        if len(same_route) >= 3:
            risks.append(
                f"High cannibalisation risk: {len(same_route)} existing flights already "
                f"serve {origin}–{destination}."
            )
        elif len(same_route) >= 1:
            risks.append(
                f"Moderate cannibalisation risk: {len(same_route)} existing flight(s) on "
                f"{origin}–{destination}."
            )

    # ── Suggest best departure windows ────────────────────────────────────────
    best_windows: List[Dict] = []
    if dep_local and origin and destination:
        best_windows = find_best_departure_window(
            origin, destination, airline, all_flights, cfg
        )

    # ── Verdict text ─────────────────────────────────────────────────────────
    fs = rule_result["feasibility_score"]
    nv = rule_result["network_value_score"]

    if rule_result["feasible"] and fs >= 70:
        verdict = f"FEASIBLE — Schedule addition is operationally viable (score: {fs}/100)."
    elif rule_result["feasible"] and fs >= 50:
        verdict = f"CONDITIONALLY FEASIBLE — Viable with caveats (score: {fs}/100). Review warnings."
    elif rule_result["feasible"]:
        verdict = f"MARGINALLY FEASIBLE — Low confidence (score: {fs}/100). Significant risks present."
    else:
        verdict = f"NOT FEASIBLE — {len(rule_result['violations'])} constraint violation(s) detected."

    # ── Why-not explanation ───────────────────────────────────────────────────
    why_not = ""
    if not rule_result["feasible"]:
        why_not = _build_why_not(rule_result, proposed_flight, best_windows)

    # ── Alternatives ─────────────────────────────────────────────────────────
    alternatives: List[str] = []
    if not rule_result["feasible"] and best_windows:
        top = best_windows[:3]
        alternatives = [
            f"Consider departure at {w['departure_time']} "
            f"(gap to nearest flight: {w.get('gap_to_nearest_min', 'N/A')} min)"
            for w in top
        ]

    return {
        "verdict":              verdict,
        "feasible":             rule_result["feasible"],
        "feasibility_score":    fs,
        "network_value_score":  nv,
        "confidence":           rule_result["confidence"],
        "violations":           rule_result["violations"],
        "warnings":             rule_result["warnings"],
        "risks":                risks,
        "alternatives":         alternatives,
        "why_not":              why_not,
        "best_window":          best_windows[:5],
        "evidence":             rule_result["rule_results"],
        "metrics":              rule_result["metrics"],
    }


def _build_why_not(
    rule_result: Dict[str, Any],
    proposed_flight: Dict[str, Any],
    best_windows: List[Dict],
) -> str:
    """Generate a human-readable explanation for infeasibility."""
    lines = ["Infeasibility reasons:"]
    for v in rule_result["violations"][:5]:
        lines.append(f"  • {v}")

    if best_windows:
        lines.append(
            f"\nSuggested alternative: depart at "
            f"{best_windows[0]['departure_time']} (largest gap in schedule)."
        )
    return "\n".join(lines)
