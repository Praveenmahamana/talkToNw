"""
Central rule engine orchestrator.

Runs all deterministic rules against a flight proposal and aggregates
results into a unified structured response.

Design contract
---------------
* NEVER calls any AI service
* All outputs are deterministic and reproducible
* Returns a typed dict consumable by the simulation and AI layers
"""

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml
import pandas as pd
from loguru import logger

from app.rules.turnaround   import check_turnaround
from app.rules.curfew       import check_curfew_flight
from app.rules.rotation     import check_aircraft_overlap
from app.rules.connectivity import count_connectivity_gain
from app.rules.spacing      import check_route_spacing, check_airport_departure_spacing
from app.rules.scoring      import (
    check_hub_bank_alignment,
    compute_feasibility_score,
    compute_network_value_score,
    score_confidence,
)


# ─────────────────────────────────────────────────────────────────────────────
# Config loading
# ─────────────────────────────────────────────────────────────────────────────

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "rules.yaml"
_config_cache: Optional[Dict] = None


def load_config(path: Optional[str] = None) -> Dict:
    """Load and cache rules.yaml.  Thread-safe for read-only access."""
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    cfg_path = Path(path) if path else _CONFIG_PATH
    with open(cfg_path, "r", encoding="utf-8") as fh:
        _config_cache = yaml.safe_load(fh)
    logger.info(f"Rules config loaded from {cfg_path}")
    return _config_cache


def reload_config() -> Dict:
    """Force-reload config from disk."""
    global _config_cache
    _config_cache = None
    return load_config()


# ─────────────────────────────────────────────────────────────────────────────
# Main rule runner
# ─────────────────────────────────────────────────────────────────────────────

def run_all_rules(
    proposed_flight: Dict[str, Any],
    existing_flights: pd.DataFrame,
    hub: Optional[str] = None,
    config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """
    Execute all deterministic rules for *proposed_flight* against the
    *existing_flights* schedule.

    Parameters
    ----------
    proposed_flight : dict with keys matching the flights table schema
    existing_flights: DataFrame from the database
    hub             : optional hub IATA code for connectivity / bank checks
    config          : override rules config (defaults to rules.yaml)

    Returns
    -------
    {
        "feasible":           bool,
        "violations":         [str, ...],
        "warnings":           [str, ...],
        "rule_results":       { rule_name: rule_dict, ... },
        "feasibility_score":  0–100,
        "network_value_score":0–100,
        "confidence":         "High" | "Medium" | "Low",
        "metrics":            { ... },
    }
    """
    cfg = config or load_config()
    all_violations: List[str] = []
    all_warnings:   List[str] = []
    rule_results:   Dict[str, Dict] = {}

    origin      = proposed_flight.get("origin", "")
    destination = proposed_flight.get("destination", "")
    airline     = proposed_flight.get("airline", "")
    ac_type     = proposed_flight.get("aircraft_type", "")
    dep_local   = proposed_flight.get("departure_local")
    arr_local   = proposed_flight.get("arrival_local")
    dep_utc     = proposed_flight.get("departure_utc")
    arr_utc     = proposed_flight.get("arrival_utc")

    # Track data completeness for confidence scoring
    filled_fields = sum(1 for v in [origin, destination, airline, ac_type, dep_local, arr_local]
                        if v is not None and v != "")
    data_completeness = filled_fields / 6.0

    # ── 1. Curfew ─────────────────────────────────────────────────────────────
    logger.debug(f"Running curfew check for {origin}–{destination}")
    curfew_result = check_curfew_flight(proposed_flight, cfg)
    rule_results["curfew"] = curfew_result
    all_violations.extend(curfew_result["violations"])
    all_warnings.extend(curfew_result["warnings"])

    # ── 2. Aircraft overlap ───────────────────────────────────────────────────
    if dep_utc and arr_utc and ac_type:
        logger.debug("Running aircraft overlap check")
        same_type = (
            existing_flights[
                (existing_flights["aircraft_type"].str.upper() == ac_type.upper()) &
                (existing_flights["airline"].str.upper() == airline.upper())
            ]
            if existing_flights is not None and not existing_flights.empty
            else pd.DataFrame()
        )
        overlap_result = check_aircraft_overlap(
            airline, ac_type, dep_utc, arr_utc, same_type,
            turnaround_buffer_minutes=45,
        )
        rule_results["aircraft_overlap"] = overlap_result
        all_violations.extend(overlap_result["violations"])
        all_warnings.extend(overlap_result["warnings"])
    else:
        rule_results["aircraft_overlap"] = {
            "feasible": True,
            "violations": [],
            "warnings": ["Aircraft type or UTC times not provided — overlap check skipped."],
            "metrics": {},
        }
        all_warnings.append("Aircraft overlap check skipped: missing aircraft_type or UTC times.")

    # ── 3. Route spacing ──────────────────────────────────────────────────────
    if dep_local and origin and destination:
        logger.debug("Running route spacing check")
        spacing_result = check_route_spacing(
            dep_local, origin, destination, airline, existing_flights, cfg
        )
        rule_results["spacing"] = spacing_result
        all_violations.extend(spacing_result["violations"])
        all_warnings.extend(spacing_result["warnings"])

        # Also check airport departure spacing
        airport_sp_result = check_airport_departure_spacing(
            dep_local, origin, airline, existing_flights, cfg
        )
        rule_results["airport_spacing"] = airport_sp_result
        all_violations.extend(airport_sp_result["violations"])
        all_warnings.extend(airport_sp_result["warnings"])
    else:
        rule_results["spacing"] = {
            "feasible": True, "violations": [],
            "warnings": ["Spacing check skipped: missing departure or airports."],
            "metrics": {},
        }

    # ── 4. Hub bank alignment ─────────────────────────────────────────────────
    logger.debug("Running hub bank alignment check")
    bank_result = check_hub_bank_alignment(proposed_flight, cfg)
    rule_results["hub_bank"] = bank_result
    all_violations.extend(bank_result["violations"])
    all_warnings.extend(bank_result["warnings"])

    # ── 5. Connectivity gain (informational — not a gate) ─────────────────────
    effective_hub = hub or origin or destination
    conn_gain = {"new_connections": 0, "details": []}
    if existing_flights is not None and not existing_flights.empty and effective_hub:
        logger.debug("Running connectivity gain analysis")
        conn_gain = count_connectivity_gain(
            effective_hub, proposed_flight, existing_flights, cfg
        )
    rule_results["connectivity"] = {
        "feasible": True,
        "violations": [],
        "warnings": [],
        "metrics": conn_gain,
    }

    # ── 6. Turnaround (if preceding flight info is given) ─────────────────────
    preceding = proposed_flight.get("_preceding_flight")
    if preceding:
        logger.debug("Running turnaround check against preceding flight")
        arr_of_prev  = preceding.get("arrival_local") or preceding.get("arrival_utc")
        dep_proposed = dep_local or dep_utc
        ta_result = check_turnaround(
            arr_of_prev, dep_proposed, ac_type,
            proposed_flight.get("origin", ""), cfg,
        )
        rule_results["turnaround"] = ta_result
        all_violations.extend(ta_result["violations"])
        all_warnings.extend(ta_result["warnings"])

    # ── Aggregate scores ──────────────────────────────────────────────────────
    feasibility_score, penalty_log = compute_feasibility_score(rule_results, cfg)
    network_score = compute_network_value_score(
        origin, destination,
        rule_results.get("connectivity", {}),
        bank_result,
        rule_results.get("spacing", {}),
        cfg,
    )
    confidence = score_confidence(feasibility_score, data_completeness, cfg)

    overall_feasible = len(all_violations) == 0

    logger.info(
        f"Rule engine complete: feasible={overall_feasible}, "
        f"score={feasibility_score}, network={network_score}, confidence={confidence}"
    )

    return {
        "feasible":            overall_feasible,
        "violations":          all_violations,
        "warnings":            all_warnings,
        "rule_results":        rule_results,
        "feasibility_score":   feasibility_score,
        "network_value_score": network_score,
        "confidence":          confidence,
        "metrics": {
            "data_completeness":  round(data_completeness, 2),
            "penalty_log":        penalty_log,
            "connectivity_gain":  conn_gain.get("new_connections", 0),
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Quick-check helpers (called directly by API / AI tools)
# ─────────────────────────────────────────────────────────────────────────────

def check_turnaround_standalone(
    arrival_dt: datetime,
    departure_dt: datetime,
    aircraft_type: str,
    station: str,
) -> Dict[str, Any]:
    cfg = load_config()
    return check_turnaround(arrival_dt, departure_dt, aircraft_type, station, cfg)


def check_airport_constraints_standalone(
    airport: str,
    departure_local: Optional[datetime],
    arrival_local: Optional[datetime],
) -> Dict[str, Any]:
    cfg = load_config()
    from app.rules.curfew import check_curfew
    return check_curfew(airport, departure_local, arrival_local, cfg)
