"""
Itinerary service.

Builds multi-leg itineraries (connections) from the schedule data and
evaluates connection quality.
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
import pandas as pd
from loguru import logger

from app.database.db import get_connection
from app.rules.connectivity import check_connection
from app.rules.rule_engine import load_config
from app.utils.time_utils import format_duration


class ItineraryService:

    def find_itineraries(
        self,
        origin: str,
        destination: str,
        max_stops: int = 1,
        min_connection_minutes: Optional[int] = None,
        max_connection_minutes: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Find single or double-stop itineraries between origin and destination.
        Returns direct flights and connecting options.
        """
        cfg = load_config()
        conn = get_connection()
        cfg_conn = cfg.get("connectivity", {})

        min_conn = min_connection_minutes or cfg_conn.get("minimum_connection_minutes", 45)
        max_conn = max_connection_minutes or cfg_conn.get("maximum_connection_minutes", 240)

        results: Dict[str, Any] = {
            "origin": origin.upper(),
            "destination": destination.upper(),
            "direct_flights": [],
            "connecting_itineraries": [],
        }

        # ── Direct flights ───────────────────────────────────────────────────
        direct_df = conn.execute(
            "SELECT * FROM flights WHERE origin = ? AND destination = ? ORDER BY departure_local",
            [origin.upper(), destination.upper()],
        ).df()

        for _, flt in direct_df.iterrows():
            results["direct_flights"].append({
                "flight_number": flt.get("flight_number"),
                "airline":       flt.get("airline"),
                "departure":     str(flt.get("departure_local"))[:16] if flt.get("departure_local") else None,
                "arrival":       str(flt.get("arrival_local"))[:16] if flt.get("arrival_local") else None,
                "block_time":    format_duration(int(flt["block_time"])) if flt.get("block_time") else None,
                "aircraft_type": flt.get("aircraft_type"),
                "stops": 0,
            })

        # ── 1-stop connections ───────────────────────────────────────────────
        if max_stops >= 1:
            # Find all flights from origin to any hub
            leg1_df = conn.execute(
                "SELECT * FROM flights WHERE origin = ? ORDER BY departure_local",
                [origin.upper()],
            ).df()

            for _, leg1 in leg1_df.iterrows():
                via = leg1.get("destination", "")
                if not via or via.upper() == destination.upper():
                    continue

                arr1 = leg1.get("arrival_local")
                if arr1 is None:
                    continue

                # Find leg 2: from via to destination with valid connection
                leg2_df = conn.execute(
                    "SELECT * FROM flights WHERE origin = ? AND destination = ? ORDER BY departure_local",
                    [via.upper(), destination.upper()],
                ).df()

                for _, leg2 in leg2_df.iterrows():
                    dep2 = leg2.get("departure_local")
                    if dep2 is None:
                        continue

                    conn_min = int((dep2 - arr1).total_seconds() / 60) if dep2 > arr1 else -1
                    if conn_min < min_conn or conn_min > max_conn:
                        continue

                    conn_check = check_connection(arr1, dep2, via, True, cfg)

                    # Total journey
                    blk1 = leg1.get("block_time") or 0
                    blk2 = leg2.get("block_time") or 0
                    total_travel = blk1 + conn_min + blk2 if blk1 and blk2 else None

                    results["connecting_itineraries"].append({
                        "via": via,
                        "stops": 1,
                        "leg1": {
                            "flight_number": leg1.get("flight_number"),
                            "airline":       leg1.get("airline"),
                            "departure":     str(leg1.get("departure_local"))[:16],
                            "arrival":       str(arr1)[:16],
                        },
                        "leg2": {
                            "flight_number": leg2.get("flight_number"),
                            "airline":       leg2.get("airline"),
                            "departure":     str(dep2)[:16],
                            "arrival":       str(leg2.get("arrival_local"))[:16],
                        },
                        "connection_minutes": conn_min,
                        "total_travel_minutes": total_travel,
                        "connection_feasible": conn_check["feasible"],
                        "connection_warnings": conn_check["warnings"],
                    })

        # Sort connecting itineraries by total travel time
        results["connecting_itineraries"].sort(
            key=lambda x: x.get("total_travel_minutes") or 9999
        )

        return results

    def evaluate_connection(
        self,
        inbound_flight_number: str,
        outbound_flight_number: str,
        connection_airport: str,
    ) -> Dict[str, Any]:
        """Evaluate a specific flight-to-flight connection."""
        cfg  = load_config()
        conn = get_connection()

        inb_df = conn.execute(
            "SELECT * FROM flights WHERE flight_number = ? LIMIT 1",
            [inbound_flight_number.upper()],
        ).df()
        out_df = conn.execute(
            "SELECT * FROM flights WHERE flight_number = ? LIMIT 1",
            [outbound_flight_number.upper()],
        ).df()

        if inb_df.empty or out_df.empty:
            return {
                "feasible": False,
                "error": "One or both flight numbers not found in schedule.",
            }

        inb = inb_df.iloc[0]
        out = out_df.iloc[0]

        arr  = inb.get("arrival_local")
        dep  = out.get("departure_local")
        if arr is None or dep is None:
            return {"feasible": False, "error": "Missing arrival or departure times."}

        result = check_connection(arr, dep, connection_airport, True, cfg)
        result["inbound_flight"]  = inbound_flight_number
        result["outbound_flight"] = outbound_flight_number
        return result
