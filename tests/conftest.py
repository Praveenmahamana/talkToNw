"""
Pytest configuration and shared fixtures for the airline schedule test suite.
"""

import pytest
import pandas as pd
from datetime import datetime, date
from pathlib import Path
import tempfile
import csv
import os


# ─────────────────────────────────────────────────────────────────────────────
# Rules config fixture
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def rules_config():
    """Load the actual rules.yaml config for tests."""
    import yaml
    cfg_path = Path(__file__).parent.parent / "app" / "config" / "rules.yaml"
    with open(cfg_path, "r") as fh:
        return yaml.safe_load(fh)


# ─────────────────────────────────────────────────────────────────────────────
# In-memory DuckDB fixture
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="function")
def fresh_db():
    """
    Set up a fresh in-memory DuckDB for each test, then tear it down.
    Returns the connection object.
    """
    from app.database import db as db_module
    from app.database.models import init_db

    # Force a new connection for each test
    db_module._connection = None
    db_module._DB_PATH = ":memory:"
    conn = db_module.get_connection()
    init_db()
    yield conn
    db_module.close_connection()


# ─────────────────────────────────────────────────────────────────────────────
# Sample flight fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def sample_flight_dict():
    """A minimal flight dict for simulation tests."""
    return {
        "id":               "test001",
        "airline":          "EK",
        "flight_number":    "EK500",
        "origin":           "DXB",
        "destination":      "LHR",
        "departure_local":  datetime(2024, 3, 15, 8, 0),
        "arrival_local":    datetime(2024, 3, 15, 13, 0),
        "departure_utc":    datetime(2024, 3, 15, 4, 0),
        "arrival_utc":      datetime(2024, 3, 15, 13, 0),
        "day_of_operation": 5,
        "aircraft_type":    "B777",
        "block_time":       420,
        "frequency":        "12345",
        "effective_from":   date(2024, 3, 1),
        "effective_to":     date(2024, 10, 31),
    }


@pytest.fixture
def sample_flights_df():
    """A small DataFrame of existing schedule flights."""
    data = [
        {
            "id": "f001", "airline": "EK", "flight_number": "EK100",
            "origin": "DXB", "destination": "LHR",
            "departure_local": datetime(2024, 3, 15, 8, 0),
            "arrival_local":   datetime(2024, 3, 15, 13, 0),
            "departure_utc":   datetime(2024, 3, 15, 4, 0),
            "arrival_utc":     datetime(2024, 3, 15, 13, 0),
            "day_of_operation": 1, "aircraft_type": "B777",
            "block_time": 420, "frequency": "1234567",
        },
        {
            "id": "f002", "airline": "EK", "flight_number": "EK102",
            "origin": "DXB", "destination": "LHR",
            "departure_local": datetime(2024, 3, 15, 14, 0),
            "arrival_local":   datetime(2024, 3, 15, 19, 0),
            "departure_utc":   datetime(2024, 3, 15, 10, 0),
            "arrival_utc":     datetime(2024, 3, 15, 19, 0),
            "day_of_operation": 1, "aircraft_type": "A380",
            "block_time": 420, "frequency": "1234567",
        },
        {
            "id": "f003", "airline": "EK", "flight_number": "EK200",
            "origin": "LHR", "destination": "DXB",
            "departure_local": datetime(2024, 3, 15, 21, 0),
            "arrival_local":   datetime(2024, 3, 16, 7, 0),
            "departure_utc":   datetime(2024, 3, 15, 21, 0),
            "arrival_utc":     datetime(2024, 3, 16, 3, 0),
            "day_of_operation": 1, "aircraft_type": "B777",
            "block_time": 420, "frequency": "1234567",
        },
    ]
    return pd.DataFrame(data)


@pytest.fixture
def sample_csv_folder():
    """Create a temp folder with a sample CSV schedule file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        csv_path = Path(tmpdir) / "test_schedule.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "airline", "flight_number", "origin", "destination",
                "departure_local", "arrival_local", "aircraft_type",
                "effective_from", "effective_to", "frequency"
            ])
            writer.writeheader()
            writer.writerow({
                "airline": "EK", "flight_number": "EK500",
                "origin": "DXB", "destination": "LHR",
                "departure_local": "08:00", "arrival_local": "13:00",
                "aircraft_type": "B777",
                "effective_from": "2024-01-01", "effective_to": "2024-12-31",
                "frequency": "1234567",
            })
            writer.writerow({
                "airline": "FZ", "flight_number": "FZ001",
                "origin": "DXB", "destination": "CMB",
                "departure_local": "09:30", "arrival_local": "14:00",
                "aircraft_type": "B738",
                "effective_from": "2024-01-01", "effective_to": "2024-12-31",
                "frequency": "135",
            })
        yield tmpdir
