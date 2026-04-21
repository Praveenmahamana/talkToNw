"""
Hub bank alignment and composite scoring rule.

Scores a proposed flight (or existing flight) against hub bank windows,
utilisation targets, and computes a composite feasibility/network-value score.
"""

from datetime import datetime, time
from typing import Any, Dict, List, Optional, Tuple
from app.utils.time_utils import parse_time, minutes_between_times


# ─────────────────────────────────────────────────────────────────────────────
# Hub bank alignment
# ─────────────────────────────────────────────────────────────────────────────

def _time_in_window(t: time, window: List[str]) -> bool:
    """Return True if *t* falls within [start, end] (handles midnight crossing)."""
    if len(window) < 2:
        return False
    start = parse_time(window[0])
    end   = parse_time(window[1])
    if start is None or end is None:
        return False
    if start <= end:
        return start <= t <= end
    return t >= start or t <= end


def _best_bank_match(t: time, banks: List[Dict]) -> Tuple[Optional[str], bool]:
    """Return (bank_name, matched) for the best matching bank for time *t*."""
    for bank in banks:
        arr_win = bank.get("arrival_window")
        dep_win = bank.get("departure_window")
        if arr_win and _time_in_window(t, arr_win):
            return bank.get("name"), True
        if dep_win and _time_in_window(t, dep_win):
            return bank.get("name"), True
    return None, False


def check_hub_bank_alignment(
    flight: Dict[str, Any],
    config: Dict,
) -> Dict[str, Any]:
    """
    Score a flight's hub-bank alignment.

    Returns standard rule dict with a 0–100 `bank_score` metric.
    """
    violations: List[str] = []
    warnings:   List[str] = []

    hub_cfg = config.get("hub_banks", {})
    origin  = (flight.get("origin") or "").upper()
    dest    = (flight.get("destination") or "").upper()

    dep_dt = flight.get("departure_local")
    arr_dt = flight.get("arrival_local")
    dep_t  = dep_dt.time() if isinstance(dep_dt, datetime) else None
    arr_t  = arr_dt.time() if isinstance(arr_dt, datetime) else None

    score   = 100  # start perfect, deduct
    matched = False

    for hub, hub_data in hub_cfg.items():
        banks = hub_data.get("banks", [])
        hub_up = hub.upper()

        if origin == hub_up and dep_t:
            bank_name, ok = _best_bank_match(dep_t, banks)
            if ok:
                matched = True
            else:
                score -= 15
                warnings.append(
                    f"Departure from hub {hub} at {dep_t.strftime('%H:%M')} "
                    f"does not align with any bank window."
                )

        if dest == hub_up and arr_t:
            bank_name, ok = _best_bank_match(arr_t, banks)
            if ok:
                matched = True
            else:
                score -= 15
                warnings.append(
                    f"Arrival at hub {hub} at {arr_t.strftime('%H:%M')} "
                    f"does not align with any bank window."
                )

    return {
        "feasible":   len(violations) == 0,
        "violations": violations,
        "warnings":   warnings,
        "metrics": {
            "bank_score": max(0, score),
            "bank_matched": matched,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Composite feasibility scoring
# ─────────────────────────────────────────────────────────────────────────────

def compute_feasibility_score(rule_results: Dict[str, Dict], config: Dict) -> int:
    """
    Aggregate individual rule results into a single 0–100 feasibility score.

    *rule_results* is a dict like:
        {
            "turnaround": { "feasible": True, "violations": [], ... },
            "curfew":     { ... },
            ...
        }
    """
    scoring_cfg  = config.get("scoring", {})
    weights      = scoring_cfg.get("weights", {})
    penalties    = scoring_cfg.get("penalties", {})

    score = 100.0
    penalty_log: List[str] = []

    for rule, result in rule_results.items():
        if not isinstance(result, dict):
            continue
        if not result.get("feasible", True):
            violations = result.get("violations", [])
            penalty = penalties.get(f"{rule}_violation", 20)
            total_penalty = min(penalty * len(violations), penalty * 2)  # cap double violations
            score -= total_penalty
            penalty_log.append(f"{rule}: -{total_penalty} ({len(violations)} violation(s))")

        # Per-rule deductions for warnings
        for _w in result.get("warnings", []):
            score -= 2
            penalty_log.append(f"{rule}: -2 (warning)")

    return max(0, min(100, int(score))), penalty_log


def compute_network_value_score(
    origin: str,
    destination: str,
    connectivity_result: Dict,
    bank_result: Dict,
    spacing_result: Dict,
    config: Dict,
) -> int:
    """
    Compute a 0–100 network value score based on:
      - New connections enabled
      - Hub bank alignment
      - Spacing quality
    """
    score = 50  # baseline

    # Connectivity contribution
    conn_metrics = connectivity_result.get("metrics", {}) if connectivity_result else {}
    new_conns    = conn_metrics.get("new_connections", 0)
    score       += min(new_conns * 3, 30)  # up to +30 for connections

    # Bank alignment
    bank_metrics = bank_result.get("metrics", {}) if bank_result else {}
    bank_score   = bank_metrics.get("bank_score", 50)
    score       += int((bank_score - 50) * 0.2)  # ±10

    # Spacing quality: big gap = less cannibalisation
    sp_metrics = spacing_result.get("metrics", {}) if spacing_result else {}
    conflicts  = sp_metrics.get("conflicts", [])
    if conflicts:
        score -= len(conflicts) * 5
    else:
        score += 10  # no spacing issues → good

    return max(0, min(100, score))


def score_confidence(feasibility_score: int, data_completeness: float, config: Dict) -> str:
    """
    Return 'High', 'Medium', or 'Low' confidence based on score and data completeness.
    *data_completeness* is 0.0–1.0 (fraction of expected fields present).
    """
    thresholds = config.get("scoring", {}).get("thresholds", {})
    high_thr   = int(thresholds.get("high_confidence", 80))
    med_thr    = int(thresholds.get("medium_confidence", 60))

    effective_score = feasibility_score * data_completeness

    if data_completeness < 0.5:
        return "Low"
    if effective_score >= high_thr:
        return "High"
    if effective_score >= med_thr:
        return "Medium"
    return "Low"
