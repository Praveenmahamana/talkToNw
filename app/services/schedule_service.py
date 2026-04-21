"""
Schedule service — high-level data access layer.

Provides clean, typed access to the DuckDB schedule data for use by
the simulation, AI, and API layers.
"""

from datetime import datetime, date
from typing import Any, Dict, List, Optional
import pandas as pd
from loguru import logger

from app.database import queries as Q
from app.database.db import get_connection
from app.ingestion.loader import load_schedule_folder
from app.ingestion.normalizer import normalise
from app.database.queries import upsert_flights, log_ingestion


class ScheduleService:
    """Stateless service; each method hits DuckDB directly."""

    # ── Ingestion ─────────────────────────────────────────────────────────────

    def ingest_folder(self, folder_path: str) -> Dict[str, Any]:
        """
        Load all schedule files from *folder_path*, normalise, and persist.
        Returns an ingestion report.
        """
        logger.info(f"Ingesting schedules from: {folder_path}")
        raw_df, load_report = load_schedule_folder(folder_path)

        if raw_df.empty:
            return {
                "status": "warning",
                "message": "No data loaded from folder.",
                "report": load_report,
                "rows_inserted": 0,
            }

        norm_df, skipped = normalise(raw_df)

        rows_inserted = 0
        if not norm_df.empty:
            rows_inserted = upsert_flights(norm_df)

        for file_info in load_report.get("files", []):
            log_ingestion(
                file_info["name"],
                file_info["rows"],
                len(skipped),
                "; ".join(file_info.get("warnings", [])),
            )

        return {
            "status": "success",
            "rows_inserted": rows_inserted,
            "rows_skipped": len(skipped),
            "skip_reasons": skipped[:20],
            "report": load_report,
        }

    # ── Read helpers ─────────────────────────────────────────────────────────

    def get_all_flights(self) -> pd.DataFrame:
        return Q.get_all_flights()

    def search_flights(
        self,
        origin: Optional[str] = None,
        destination: Optional[str] = None,
        airline: Optional[str] = None,
        flight_number: Optional[str] = None,
        day_of_week: Optional[int] = None,   # 1=Mon … 7=Sun (IATA standard)
    ) -> pd.DataFrame:
        """Flexible flight search with optional filters."""
        conn = get_connection()
        clauses = []
        params  = []

        if origin:
            clauses.append("origin = ?")
            params.append(origin.upper())
        if destination:
            clauses.append("destination = ?")
            params.append(destination.upper())
        if airline:
            clauses.append("airline = ?")
            params.append(airline.upper())
        if flight_number:
            clauses.append("flight_number ILIKE ?")
            params.append(f"%{flight_number.upper()}%")
        if day_of_week is not None:
            clauses.append("day_of_operation = ?")
            params.append(int(day_of_week))

        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        sql   = f"SELECT * FROM flights {where} ORDER BY departure_local"
        return conn.execute(sql, params).df() if params else conn.execute(sql).df()

    def get_route_day_analysis(self, origin: str, destination: str, day_of_week: Optional[int] = None) -> Dict[str, Any]:
        """
        Return a full per-day analysis for a route:
          - weekly frequency breakdown (by day)
          - flight list (optionally filtered to one day)
          - market share
          - departure spread
        """
        from app.database import queries as Q
        breakdown_df = Q.get_route_day_breakdown(origin, destination)
        flights_df   = Q.get_route_detailed(origin, destination, day_of_week)

        DAY_NAMES = {1:"Monday",2:"Tuesday",3:"Wednesday",4:"Thursday",
                     5:"Friday",6:"Saturday",7:"Sunday"}

        # Weekly schedule grid
        weekly: Dict[str, Any] = {}
        for row in breakdown_df.to_dict("records"):
            day_num  = row.get("day_of_operation")
            day_name = DAY_NAMES.get(day_num, f"Day{day_num}")
            weekly[day_name] = {
                "day": day_num,
                "unique_flights": row.get("unique_flights", 0),
                "airlines": row.get("airlines", ""),
                "aircraft_types": row.get("aircraft_types", ""),
                "earliest_dep": row.get("earliest_dep", ""),
                "latest_dep": row.get("latest_dep", ""),
            }

        # Deduplicated flight list
        flight_records = []
        seen = set()
        for rec in flights_df.to_dict("records"):
            key = (rec.get("flight_number"), rec.get("dep_time"),
                   rec.get("day_of_operation"), rec.get("airline"))
            if key not in seen:
                seen.add(key)
                flight_records.append(rec)

        day_label = DAY_NAMES.get(day_of_week, "all days") if day_of_week else "all days"

        return {
            "route": f"{origin.upper()}-{destination.upper()}",
            "filter_day": day_label,
            "weekly_schedule": weekly,
            "operating_days": list(weekly.keys()),
            "flights_on_day": flight_records,
            "flight_count_on_day": len(flight_records),
            "total_weekly_unique_flights": sum(
                v["unique_flights"] for v in weekly.values()
            ),
        }

    def get_route_flights(self, origin: str, destination: str) -> pd.DataFrame:
        return Q.get_flights_by_route(origin, destination)

    def get_airport_schedule(self, airport: str) -> pd.DataFrame:
        return Q.get_airport_schedule(airport)

    def get_flights_for_overlap_check(
        self,
        airline: str,
        aircraft_type: str,
        dep_utc: datetime,
        arr_utc: datetime,
    ) -> pd.DataFrame:
        return Q.get_flights_overlapping_window(
            airline, aircraft_type,
            dep_utc.strftime("%Y-%m-%d %H:%M:%S"),
            arr_utc.strftime("%Y-%m-%d %H:%M:%S"),
        )

    def get_nearby_flights(
        self,
        origin: str,
        destination: str,
        dep_local: datetime,
        window_hours: int = 3,
    ) -> pd.DataFrame:
        """Return existing flights on same route within ±window_hours of proposed dep."""
        conn = get_connection()
        return conn.execute(
            """
            SELECT * FROM flights
            WHERE origin = ?
              AND destination = ?
              AND ABS(EPOCH(departure_local) - EPOCH(CAST(? AS TIMESTAMP))) < ?
            ORDER BY departure_local
            """,
            [
                origin.upper(),
                destination.upper(),
                dep_local.strftime("%Y-%m-%d %H:%M:%S"),
                window_hours * 3600,
            ],
        ).df()

    # ── Stats ────────────────────────────────────────────────────────────────

    def get_route_summary(self) -> pd.DataFrame:
        return Q.get_route_summary()

    def flight_count(self) -> int:
        conn = get_connection()
        return conn.execute("SELECT COUNT(*) FROM flights").fetchone()[0]

    def get_summary_stats(self) -> Dict[str, Any]:
        conn = get_connection()
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total_flights,
                COUNT(DISTINCT airline) AS airlines,
                COUNT(DISTINCT origin || destination) AS routes,
                COUNT(DISTINCT origin) AS origins,
                COUNT(DISTINCT destination) AS destinations
            FROM flights
            """
        ).fetchone()
        return {
            "total_flights": row[0],
            "airlines": row[1],
            "routes": row[2],
            "origins": row[3],
            "destinations": row[4],
        }
