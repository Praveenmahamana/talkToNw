"""Reusable SQL query functions against the DuckDB flights table."""

from typing import List, Optional
import pandas as pd
from app.database.db import fetchdf, execute
from loguru import logger


# ─────────────────────────────────────────────────────────────────────────────
# Generic helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_all_flights() -> pd.DataFrame:
    return fetchdf("SELECT * FROM flights ORDER BY departure_local")


def get_flights_by_route(origin: str, destination: str) -> pd.DataFrame:
    return fetchdf(
        "SELECT * FROM flights WHERE origin = ? AND destination = ? ORDER BY departure_local",
        [origin.upper(), destination.upper()],
    )


def get_flights_by_airline(airline: str) -> pd.DataFrame:
    return fetchdf(
        "SELECT * FROM flights WHERE airline = ? ORDER BY departure_local",
        [airline.upper()],
    )


def get_flight_by_number(flight_number: str) -> pd.DataFrame:
    return fetchdf(
        "SELECT * FROM flights WHERE flight_number = ? ORDER BY departure_local",
        [flight_number.upper()],
    )


def get_flights_by_airport(airport: str) -> pd.DataFrame:
    ap = airport.upper()
    return fetchdf(
        "SELECT * FROM flights WHERE origin = ? OR destination = ? ORDER BY departure_local",
        [ap, ap],
    )


def get_departures_from(airport: str, airline: Optional[str] = None) -> pd.DataFrame:
    ap = airport.upper()
    if airline:
        return fetchdf(
            "SELECT * FROM flights WHERE origin = ? AND airline = ? ORDER BY departure_local",
            [ap, airline.upper()],
        )
    return fetchdf(
        "SELECT * FROM flights WHERE origin = ? ORDER BY departure_local", [ap]
    )


def get_arrivals_at(airport: str, airline: Optional[str] = None) -> pd.DataFrame:
    ap = airport.upper()
    if airline:
        return fetchdf(
            "SELECT * FROM flights WHERE destination = ? AND airline = ? ORDER BY arrival_local",
            [ap, airline.upper()],
        )
    return fetchdf(
        "SELECT * FROM flights WHERE destination = ? ORDER BY arrival_local", [ap]
    )


def get_summary_stats() -> dict:
    """Return high-level statistics about the loaded schedule for the dashboard."""
    try:
        from app.database.db import get_connection
        conn = get_connection()
        row = conn.execute("""
            SELECT
                COUNT(DISTINCT flight_number)                       AS unique_flights,
                COUNT(DISTINCT airline)                             AS airlines,
                COUNT(DISTINCT origin || '-' || destination)        AS routes,
                COUNT(DISTINCT origin)                              AS origins,
                COUNT(DISTINCT destination)                         AS destinations,
                COUNT(DISTINCT aircraft_type)                       AS aircraft_types,
                ROUND(AVG(block_time), 0)                           AS avg_block_min,
                MIN(effective_from)                                 AS season_start,
                MAX(effective_to)                                   AS season_end,
                STRING_AGG(DISTINCT airline, ', ' ORDER BY airline) AS airline_codes
            FROM flights
        """).fetchone()

        # Daily average: total unique flights / 7 days
        daily_avg = round((row[0] or 0) / 7, 1) if row[0] else 0

        return {
            "unique_flights":   int(row[0] or 0),
            "airlines":         int(row[1] or 0),
            "routes":           int(row[2] or 0),
            "origins":          int(row[3] or 0),
            "destinations":     int(row[4] or 0),
            "aircraft_types":   int(row[5] or 0),
            "avg_block_min":    int(row[6] or 0),
            "season_start":     str(row[7]) if row[7] else None,
            "season_end":       str(row[8]) if row[8] else None,
            "airline_codes":    row[9] or "",
            "daily_avg":        daily_avg,
        }
    except Exception as exc:
        logger.warning(f"get_summary_stats failed: {exc}")
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Aircraft rotation queries
# ─────────────────────────────────────────────────────────────────────────────

def get_aircraft_schedule(aircraft_type: str, airline: str) -> pd.DataFrame:
    """All flights for a given aircraft type / airline combination."""
    return fetchdf(
        "SELECT * FROM flights WHERE aircraft_type = ? AND airline = ? ORDER BY departure_local",
        [aircraft_type.upper(), airline.upper()],
    )


def get_flights_overlapping_window(
    airline: str,
    aircraft_type: str,
    dep_utc_str: str,
    arr_utc_str: str,
) -> pd.DataFrame:
    """Find flights that overlap a proposed time window for conflict detection."""
    return fetchdf(
        """
        SELECT * FROM flights
        WHERE airline = ?
          AND aircraft_type = ?
          AND departure_utc < CAST(? AS TIMESTAMP)
          AND arrival_utc   > CAST(? AS TIMESTAMP)
        """,
        [airline.upper(), aircraft_type.upper(), arr_utc_str, dep_utc_str],
    )


# ─────────────────────────────────────────────────────────────────────────────
# Analytics queries
# ─────────────────────────────────────────────────────────────────────────────

def get_route_summary() -> pd.DataFrame:
    return fetchdf(
        """
        SELECT
            origin,
            destination,
            COUNT(*) AS total_flights,
            COUNT(DISTINCT airline) AS airlines,
            MIN(departure_local) AS first_departure,
            MAX(departure_local) AS last_departure,
            AVG(block_time) AS avg_block_minutes
        FROM flights
        GROUP BY origin, destination
        ORDER BY total_flights DESC
        """
    )


def get_airport_schedule(airport: str) -> pd.DataFrame:
    ap = airport.upper()
    return fetchdf(
        """
        SELECT
            flight_number,
            airline,
            CASE WHEN origin = ? THEN 'DEP' ELSE 'ARR' END AS movement,
            CASE WHEN origin = ? THEN destination ELSE origin END AS counterpart,
            CASE WHEN origin = ? THEN departure_local ELSE arrival_local END AS local_time,
            aircraft_type,
            block_time
        FROM flights
        WHERE origin = ? OR destination = ?
        ORDER BY local_time
        """,
        [ap, ap, ap, ap, ap],
    )


def get_route_day_breakdown(origin: str, destination: str) -> pd.DataFrame:
    """Per-day-of-week frequency breakdown for a route."""
    return fetchdf(
        """
        SELECT
            day_of_operation,
            COUNT(DISTINCT flight_number) AS unique_flights,
            COUNT(*) AS total_rows,
            STRING_AGG(DISTINCT airline, ', ') AS airlines,
            STRING_AGG(DISTINCT aircraft_type, ', ') AS aircraft_types,
            MIN(STRFTIME(departure_local, '%H:%M')) AS earliest_dep,
            MAX(STRFTIME(departure_local, '%H:%M')) AS latest_dep
        FROM flights
        WHERE origin = ? AND destination = ?
        GROUP BY day_of_operation
        ORDER BY day_of_operation
        """,
        [origin.upper(), destination.upper()],
    )


def get_route_detailed(origin: str, destination: str, day_of_operation: Optional[int] = None) -> pd.DataFrame:
    """All flights for a route, optionally filtered to a specific day (1=Mon … 7=Sun)."""
    if day_of_operation is not None:
        return fetchdf(
            """
            SELECT flight_number, airline, origin, destination,
                   STRFTIME(departure_local, '%H:%M') AS dep_time,
                   STRFTIME(arrival_local, '%H:%M') AS arr_time,
                   aircraft_type, block_time, day_of_operation, frequency
            FROM flights
            WHERE origin = ? AND destination = ? AND day_of_operation = ?
            ORDER BY departure_local
            """,
            [origin.upper(), destination.upper(), day_of_operation],
        )
    return fetchdf(
        """
        SELECT flight_number, airline, origin, destination,
               STRFTIME(departure_local, '%H:%M') AS dep_time,
               STRFTIME(arrival_local, '%H:%M') AS arr_time,
               aircraft_type, block_time, day_of_operation, frequency
        FROM flights
        WHERE origin = ? AND destination = ?
        ORDER BY day_of_operation, departure_local
        """,
        [origin.upper(), destination.upper()],
    )


def get_daily_flight_count(airline: Optional[str] = None) -> pd.DataFrame:
    if airline:
        return fetchdf(
            "SELECT day_of_operation, COUNT(*) AS flights FROM flights WHERE airline = ? GROUP BY day_of_operation ORDER BY day_of_operation",
            [airline.upper()],
        )
    return fetchdf(
        "SELECT day_of_operation, COUNT(*) AS flights FROM flights GROUP BY day_of_operation ORDER BY day_of_operation"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Write helpers
# ─────────────────────────────────────────────────────────────────────────────

def clear_flights() -> int:
    """Delete ALL flight records from the DB. Returns the count of deleted rows."""
    from app.database.db import get_connection
    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM flights").fetchone()[0]
    conn.execute("DELETE FROM flights")
    logger.info(f"clear_flights: removed {count} existing flight records.")
    return count


def upsert_flights(df: pd.DataFrame) -> int:
    """
    Insert-or-replace flights from a DataFrame.
    Uses explicit column names to handle schema migrations safely.
    Returns the number of rows inserted.
    """
    if df.empty:
        return 0

    from app.database.db import get_connection
    conn = get_connection()

    # Only include columns that exist in both the DataFrame and the DB table
    db_cols = {r[0] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name='flights'").fetchall()}
    df_cols  = [c for c in df.columns if c in db_cols]

    staging = df[df_cols].copy()
    conn.register("_staging", staging)
    col_list = ", ".join(f'"{c}"' for c in df_cols)
    conn.execute(f"INSERT OR REPLACE INTO flights ({col_list}) SELECT {col_list} FROM _staging")
    conn.unregister("_staging")
    logger.info(f"Upserted {len(df)} flight records.")
    return len(df)


def log_ingestion(
    file_name: str,
    rows_loaded: int,
    rows_skipped: int,
    errors: str = "",
) -> None:
    execute(
        "INSERT INTO ingestion_log (file_name, rows_loaded, rows_skipped, errors) VALUES (?, ?, ?, ?)",
        [file_name, rows_loaded, rows_skipped, errors],
    )


def log_query(user_query: str, intent: str, tools_called: str, response_time: float) -> None:
    execute(
        "INSERT INTO query_log (user_query, intent, tools_called, response_time) VALUES (?, ?, ?, ?)",
        [user_query, intent, tools_called, response_time],
    )
