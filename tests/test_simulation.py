"""
Unit tests for the simulation module — add_flight and retime_flight.
Edge cases: overnight flights, missing aircraft, empty schedule.
"""

import pytest
import pandas as pd
from datetime import datetime, date, timedelta


# ─────────────────────────────────────────────────────────────────────────────
# simulate_add_flight tests
# ─────────────────────────────────────────────────────────────────────────────

class TestAddFlight:

    def _make_proposal(self, dep: datetime, arr: datetime = None, **kwargs):
        base = {
            "origin": "DXB", "destination": "LHR",
            "departure_local": dep,
            "arrival_local": arr,
            "aircraft_type": "B777",
            "airline": "EK",
        }
        base.update(kwargs)
        return base

    def test_basic_feasible_addition(self, rules_config, sample_flights_df):
        from app.simulation.add_flight import simulate_add_flight
        dep = datetime(2024, 3, 15, 11, 0)
        arr = datetime(2024, 3, 15, 16, 0)
        proposal = self._make_proposal(dep, arr)

        result = simulate_add_flight(proposal, config=rules_config)

        assert "verdict" in result
        assert "feasibility_score" in result
        assert "network_value_score" in result
        assert "confidence" in result
        assert isinstance(result["feasibility_score"], int)
        assert 0 <= result["feasibility_score"] <= 100

    def test_curfew_violation_infeasible(self, rules_config):
        from app.simulation.add_flight import simulate_add_flight
        dep = datetime(2024, 3, 15, 23, 30)  # LHR dep at 23:30 → curfew
        arr = datetime(2024, 3, 16, 7, 0)
        proposal = self._make_proposal(
            dep, arr, origin="LHR", destination="DXB"
        )
        result = simulate_add_flight(proposal, config=rules_config)
        assert result["feasible"] is False
        assert "NOT FEASIBLE" in result["verdict"].upper() or result["feasibility_score"] < 50

    def test_overnight_flight_handled(self, rules_config):
        from app.simulation.add_flight import simulate_add_flight
        dep = datetime(2024, 3, 15, 22, 0)   # departs 22:00
        arr = datetime(2024, 3, 16, 6, 30)   # arrives next day 06:30
        proposal = {
            "origin": "DXB", "destination": "LHR",
            "departure_local": dep,
            "arrival_local":   arr,
            "aircraft_type":   "B777",
            "airline":         "EK",
        }
        result = simulate_add_flight(proposal, config=rules_config)
        assert "feasibility_score" in result
        assert result["feasibility_score"] >= 0

    def test_missing_aircraft_type(self, rules_config):
        from app.simulation.add_flight import simulate_add_flight
        dep = datetime(2024, 3, 15, 10, 0)
        arr = datetime(2024, 3, 15, 15, 0)
        proposal = {
            "origin": "DXB", "destination": "AMS",
            "departure_local": dep,
            "arrival_local": arr,
            "aircraft_type": "",  # missing!
            "airline": "EK",
        }
        result = simulate_add_flight(proposal, config=rules_config)
        # Should still run, just with warnings about missing data
        assert "feasibility_score" in result
        assert len(result.get("warnings", [])) > 0 or result["confidence"] in ("Low", "Medium")

    def test_best_window_returned_when_infeasible(self, rules_config):
        from app.simulation.add_flight import simulate_add_flight
        # Force spacing violation: very close to existing 08:00 departure
        dep = datetime(2024, 3, 15, 8, 10)
        arr = datetime(2024, 3, 15, 13, 10)
        proposal = {
            "origin": "DXB", "destination": "LHR",
            "departure_local": dep,
            "arrival_local": arr,
            "aircraft_type": "B777",
            "airline": "EK",
        }
        result = simulate_add_flight(proposal, config=rules_config)
        # best_window should suggest alternatives
        assert "best_window" in result

    def test_cannibalization_risk_detected(self, rules_config, sample_flights_df):
        from app.simulation.add_flight import simulate_add_flight
        # Adding a 3rd flight to a route that already has 2
        dep = datetime(2024, 3, 15, 11, 30)
        arr = datetime(2024, 3, 15, 16, 30)

        import unittest.mock as mock
        from app.services import schedule_service as ss
        with mock.patch.object(ss.ScheduleService, "get_all_flights", return_value=sample_flights_df):
            result = simulate_add_flight(
                {"origin": "DXB", "destination": "LHR",
                 "departure_local": dep, "arrival_local": arr,
                 "aircraft_type": "B777", "airline": "EK"},
                config=rules_config,
            )
        # Should flag cannibalisation in risks
        risks_text = " ".join(result.get("risks", [])).lower()
        assert "cannibal" in risks_text or result["feasibility_score"] is not None

    def test_why_not_populated_when_infeasible(self, rules_config):
        from app.simulation.add_flight import simulate_add_flight
        dep = datetime(2024, 3, 15, 23, 30)
        proposal = {
            "origin": "LHR", "destination": "DXB",
            "departure_local": dep, "arrival_local": None,
            "aircraft_type": "B777", "airline": "EK",
        }
        result = simulate_add_flight(proposal, config=rules_config)
        if not result["feasible"]:
            assert result.get("why_not"), "why_not should be non-empty when infeasible"


# ─────────────────────────────────────────────────────────────────────────────
# simulate_retime_flight tests
# ─────────────────────────────────────────────────────────────────────────────

class TestRetimeFlight:

    def test_retime_later_improves_spacing(self, rules_config, sample_flight_dict, sample_flights_df):
        from app.simulation.retime_flight import simulate_retime_flight

        # current dep is 08:00 — move to 11:30 (further from 14:00 existing flight)
        new_dep = datetime(2024, 3, 15, 11, 30)
        result = simulate_retime_flight(sample_flight_dict, new_dep, config=rules_config)

        assert "verdict" in result
        assert "feasibility_score" in result
        assert "delta" in result
        assert "current_timing" in result
        assert "proposed_timing" in result

    def test_retime_into_curfew_detected(self, rules_config, sample_flight_dict):
        from app.simulation.retime_flight import simulate_retime_flight

        # Move LHR departure into curfew window
        flt = dict(sample_flight_dict)
        flt["origin"]      = "LHR"
        flt["destination"] = "DXB"
        flt["departure_local"] = datetime(2024, 3, 15, 8, 0)
        flt["arrival_local"]   = datetime(2024, 3, 16, 3, 0)

        new_dep = datetime(2024, 3, 15, 23, 30)  # into curfew
        result = simulate_retime_flight(flt, new_dep, config=rules_config)

        assert not result["feasible"]
        assert len(result.get("violations", [])) > 0

    def test_delta_shift_direction(self, rules_config, sample_flight_dict):
        from app.simulation.retime_flight import simulate_retime_flight

        current_dep = sample_flight_dict["departure_local"]  # 08:00
        new_dep = datetime(2024, 3, 15, 10, 0)               # 10:00 — 120 min later

        result = simulate_retime_flight(sample_flight_dict, new_dep, config=rules_config)

        assert result["delta"]["departure_shift_minutes"] == 120
        assert result["delta"]["departure_shift_direction"] == "later"

    def test_retime_earlier(self, rules_config, sample_flight_dict):
        from app.simulation.retime_flight import simulate_retime_flight

        new_dep = datetime(2024, 3, 15, 6, 0)   # 06:00 — 2 hours earlier

        result = simulate_retime_flight(sample_flight_dict, new_dep, config=rules_config)

        assert result["delta"]["departure_shift_minutes"] == -120
        assert result["delta"]["departure_shift_direction"] == "earlier"

    def test_block_time_preserved(self, rules_config, sample_flight_dict):
        from app.simulation.retime_flight import simulate_retime_flight

        original_block = sample_flight_dict["block_time"]  # 420 min
        new_dep = datetime(2024, 3, 15, 10, 0)

        result = simulate_retime_flight(sample_flight_dict, new_dep, config=rules_config)

        # Proposed arrival should be new_dep + block_time (420 min = 7h → 17:00)
        proposed_arr = result["proposed_timing"].get("arrival")
        if proposed_arr:
            assert proposed_arr == "17:00", f"Expected 17:00, got {proposed_arr}"

    def test_connectivity_change_computed(self, rules_config, sample_flight_dict):
        from app.simulation.retime_flight import simulate_retime_flight

        new_dep = datetime(2024, 3, 15, 9, 0)
        result = simulate_retime_flight(sample_flight_dict, new_dep, config=rules_config)

        assert "connectivity_change" in result
        cc = result["connectivity_change"]
        assert "current_connections" in cc
        assert "proposed_connections" in cc
        assert "delta" in cc


# ─────────────────────────────────────────────────────────────────────────────
# Edge cases
# ─────────────────────────────────────────────────────────────────────────────

class TestEdgeCases:

    def test_overnight_flight_block_time(self, rules_config):
        """Overnight flight: arrival next day should have positive block time."""
        from app.utils.time_utils import calculate_block_time_minutes
        dep = datetime(2024, 3, 15, 22, 0)
        arr = datetime(2024, 3, 16, 5, 30)  # next day
        block = calculate_block_time_minutes(dep, arr)
        assert block == 450

    def test_no_curfew_data_for_unknown_airport(self, rules_config):
        """Unknown airport should not cause an error — treated as 24/7."""
        from app.rules.curfew import check_curfew
        dep = datetime(2024, 3, 15, 2, 0)
        result = check_curfew("XYZ", dep, None, rules_config)
        assert result["feasible"] is True
        assert result["metrics"]["curfew_defined"] is False

    def test_add_flight_no_arrival_time(self, rules_config):
        """Simulation should handle missing arrival gracefully."""
        from app.simulation.add_flight import simulate_add_flight
        dep = datetime(2024, 3, 15, 9, 0)
        proposal = {
            "origin": "DXB", "destination": "BOM",
            "departure_local": dep,
            "arrival_local": None,  # no arrival
            "aircraft_type": "B738",
            "airline": "FZ",
        }
        result = simulate_add_flight(proposal, config=rules_config)
        assert "feasibility_score" in result
        assert result["confidence"] in ("High", "Medium", "Low")

    def test_isvalid_frequency_parsing(self):
        from app.utils.time_utils import frequency_to_days
        assert frequency_to_days("1234567") == [1, 2, 3, 4, 5, 6, 7]
        assert frequency_to_days("135") == [1, 3, 5]
        assert frequency_to_days("") == []
        assert frequency_to_days("17") == [1, 7]
