"""
Unit tests for the rule engine — turnaround, curfew, spacing, rotation,
scoring, and the orchestrated run_all_rules function.
"""

import pytest
import pandas as pd
from datetime import datetime, date


# ─────────────────────────────────────────────────────────────────────────────
# Turnaround tests
# ─────────────────────────────────────────────────────────────────────────────

class TestTurnaround:
    def test_sufficient_wide_body(self, rules_config):
        from app.rules.turnaround import check_turnaround
        arr = datetime(2024, 3, 15, 10, 0)
        dep = datetime(2024, 3, 15, 12, 0)  # 120 min ground time
        result = check_turnaround(arr, dep, "B777", "DXB", rules_config)
        assert result["feasible"] is True
        assert result["metrics"]["ground_time_minutes"] == 120

    def test_insufficient_wide_body(self, rules_config):
        from app.rules.turnaround import check_turnaround
        arr = datetime(2024, 3, 15, 10, 0)
        dep = datetime(2024, 3, 15, 10, 30)  # 30 min — too short for B777
        result = check_turnaround(arr, dep, "B777", "DXB", rules_config)
        assert result["feasible"] is False
        assert len(result["violations"]) > 0

    def test_sufficient_narrow_body(self, rules_config):
        from app.rules.turnaround import check_turnaround
        arr = datetime(2024, 3, 15, 10, 0)
        dep = datetime(2024, 3, 15, 10, 50)  # 50 min — OK for A320
        result = check_turnaround(arr, dep, "A320", "DXB", rules_config)
        assert result["feasible"] is True

    def test_impossible_rotation_dep_before_arr(self, rules_config):
        from app.rules.turnaround import check_turnaround
        arr = datetime(2024, 3, 15, 14, 0)
        dep = datetime(2024, 3, 15, 12, 0)  # departure is BEFORE arrival
        result = check_turnaround(arr, dep, "A320", "DXB", rules_config)
        assert result["feasible"] is False

    def test_tight_turnaround_generates_warning(self, rules_config):
        from app.rules.turnaround import check_turnaround
        arr = datetime(2024, 3, 15, 10, 0)
        dep = datetime(2024, 3, 15, 10, 50)  # 50 min — 11% buffer on 45 min min
        result = check_turnaround(arr, dep, "A320", "DXB", rules_config)
        assert result["feasible"] is True
        # Should have a warning about tight turnaround
        assert len(result["warnings"]) > 0

    def test_missing_datetime(self, rules_config):
        from app.rules.turnaround import check_turnaround
        result = check_turnaround(None, None, "B777", "DXB", rules_config)
        assert result["feasible"] is False

    def test_regional_aircraft(self, rules_config):
        from app.rules.turnaround import check_turnaround
        arr = datetime(2024, 3, 15, 10, 0)
        dep = datetime(2024, 3, 15, 10, 35)  # 35 min — OK for regional
        result = check_turnaround(arr, dep, "E190", "DXB", rules_config)
        assert result["feasible"] is True


# ─────────────────────────────────────────────────────────────────────────────
# Curfew tests
# ─────────────────────────────────────────────────────────────────────────────

class TestCurfew:
    def test_lhr_curfew_violation_departure(self, rules_config):
        from app.rules.curfew import check_curfew
        # 23:30 violates LHR curfew (23:00–06:00)
        dep = datetime(2024, 3, 15, 23, 30)
        result = check_curfew("LHR", dep, None, rules_config)
        assert result["feasible"] is False
        assert any("curfew" in v.lower() for v in result["violations"])

    def test_lhr_no_curfew_daytime(self, rules_config):
        from app.rules.curfew import check_curfew
        dep = datetime(2024, 3, 15, 10, 0)  # 10:00 — no curfew
        result = check_curfew("LHR", dep, None, rules_config)
        assert result["feasible"] is True

    def test_dxb_no_curfew(self, rules_config):
        from app.rules.curfew import check_curfew
        dep = datetime(2024, 3, 15, 2, 0)  # 02:00 — DXB is 24/7
        result = check_curfew("DXB", dep, None, rules_config)
        assert result["feasible"] is True
        assert result["metrics"]["curfew_defined"] is False

    def test_lhr_curfew_arrival_violation(self, rules_config):
        from app.rules.curfew import check_curfew
        arr = datetime(2024, 3, 15, 4, 0)  # 04:00 — inside 23:00-06:00 curfew
        result = check_curfew("LHR", None, arr, rules_config)
        assert result["feasible"] is False

    def test_overnight_curfew_boundary(self, rules_config):
        from app.rules.curfew import check_curfew
        # 00:30 is inside LHR curfew (23:00–06:00)
        dep = datetime(2024, 3, 15, 0, 30)
        result = check_curfew("LHR", dep, None, rules_config)
        assert result["feasible"] is False


# ─────────────────────────────────────────────────────────────────────────────
# Spacing tests
# ─────────────────────────────────────────────────────────────────────────────

class TestSpacing:
    def test_adequate_spacing(self, rules_config, sample_flights_df):
        from app.rules.spacing import check_route_spacing
        # Existing flights at 08:00 and 14:00; propose 11:00 → 3hr gap each side
        proposed = datetime(2024, 3, 15, 11, 0)
        result = check_route_spacing(proposed, "DXB", "LHR", "EK", sample_flights_df, rules_config)
        assert result["feasible"] is True

    def test_too_close_spacing(self, rules_config, sample_flights_df):
        from app.rules.spacing import check_route_spacing
        # Existing at 08:00; propose 08:20 → only 20 min gap (< 60 min min)
        proposed = datetime(2024, 3, 15, 8, 20)
        result = check_route_spacing(proposed, "DXB", "LHR", "EK", sample_flights_df, rules_config)
        assert result["feasible"] is False

    def test_no_existing_flights(self, rules_config):
        from app.rules.spacing import check_route_spacing
        proposed = datetime(2024, 3, 15, 10, 0)
        result = check_route_spacing(proposed, "DXB", "SYD", "EK", pd.DataFrame(), rules_config)
        assert result["feasible"] is True


# ─────────────────────────────────────────────────────────────────────────────
# Connectivity tests
# ─────────────────────────────────────────────────────────────────────────────

class TestConnectivity:
    def test_feasible_connection(self, rules_config):
        from app.rules.connectivity import check_connection
        inb_arr = datetime(2024, 3, 15, 10, 0)
        out_dep = datetime(2024, 3, 15, 11, 30)  # 90 min — OK
        result = check_connection(inb_arr, out_dep, "DXB", True, rules_config)
        assert result["feasible"] is True

    def test_too_tight_connection(self, rules_config):
        from app.rules.connectivity import check_connection
        inb_arr = datetime(2024, 3, 15, 10, 0)
        out_dep = datetime(2024, 3, 15, 10, 20)  # 20 min — too tight
        result = check_connection(inb_arr, out_dep, "DXB", True, rules_config)
        assert result["feasible"] is False

    def test_too_long_connection(self, rules_config):
        from app.rules.connectivity import check_connection
        inb_arr = datetime(2024, 3, 15, 10, 0)
        out_dep = datetime(2024, 3, 15, 15, 0)   # 300 min — too long, warning
        result = check_connection(inb_arr, out_dep, "DXB", False, rules_config)
        assert len(result["warnings"]) > 0

    def test_negative_connection(self, rules_config):
        from app.rules.connectivity import check_connection
        inb_arr = datetime(2024, 3, 15, 14, 0)
        out_dep = datetime(2024, 3, 15, 12, 0)   # departs before arrival
        result = check_connection(inb_arr, out_dep, "DXB", True, rules_config)
        assert result["feasible"] is False


# ─────────────────────────────────────────────────────────────────────────────
# Scoring tests
# ─────────────────────────────────────────────────────────────────────────────

class TestScoring:
    def test_perfect_score_no_violations(self, rules_config):
        from app.rules.scoring import compute_feasibility_score
        rule_results = {
            "curfew":           {"feasible": True,  "violations": [], "warnings": []},
            "aircraft_overlap": {"feasible": True,  "violations": [], "warnings": []},
            "spacing":          {"feasible": True,  "violations": [], "warnings": []},
            "hub_bank":         {"feasible": True,  "violations": [], "warnings": []},
        }
        score, _ = compute_feasibility_score(rule_results, rules_config)
        assert score == 100

    def test_score_reduced_by_violation(self, rules_config):
        from app.rules.scoring import compute_feasibility_score
        rule_results = {
            "curfew": {"feasible": False, "violations": ["Curfew violation at LHR"], "warnings": []},
        }
        score, _ = compute_feasibility_score(rule_results, rules_config)
        assert score < 100
        assert score >= 0

    def test_confidence_high(self, rules_config):
        from app.rules.scoring import score_confidence
        assert score_confidence(85, 1.0, rules_config) == "High"

    def test_confidence_low_due_to_missing_data(self, rules_config):
        from app.rules.scoring import score_confidence
        assert score_confidence(90, 0.3, rules_config) == "Low"

    def test_confidence_medium(self, rules_config):
        from app.rules.scoring import score_confidence
        assert score_confidence(65, 1.0, rules_config) == "Medium"


# ─────────────────────────────────────────────────────────────────────────────
# Rule engine integration
# ─────────────────────────────────────────────────────────────────────────────

class TestRuleEngine:
    def test_run_all_rules_feasible(self, rules_config, sample_flight_dict, sample_flights_df):
        from app.rules.rule_engine import run_all_rules
        result = run_all_rules(sample_flight_dict, sample_flights_df, config=rules_config)
        assert "feasible" in result
        assert "feasibility_score" in result
        assert isinstance(result["feasibility_score"], int)
        assert 0 <= result["feasibility_score"] <= 100
        assert result["confidence"] in ("High", "Medium", "Low")

    def test_run_all_rules_curfew_infeasible(self, rules_config, sample_flights_df):
        from app.rules.rule_engine import run_all_rules
        # LHR departure at 23:30 → curfew violation
        flight = {
            "origin": "LHR", "destination": "DXB",
            "departure_local": datetime(2024, 3, 15, 23, 30),
            "arrival_local":   datetime(2024, 3, 16, 7, 0),
            "aircraft_type": "B777", "airline": "EK",
        }
        result = run_all_rules(flight, sample_flights_df, config=rules_config)
        assert result["feasible"] is False
        assert result["feasibility_score"] < 80

    def test_run_all_rules_empty_schedule(self, rules_config, sample_flight_dict):
        from app.rules.rule_engine import run_all_rules
        result = run_all_rules(sample_flight_dict, pd.DataFrame(), config=rules_config)
        assert "feasible" in result
        assert "warnings" in result
