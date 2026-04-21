"""
Route analysis service.

Provides analytics on routes, frequencies, market coverage, and gaps
in the current schedule.
"""

from typing import Any, Dict, List, Optional
import pandas as pd
from loguru import logger

from app.database import queries as Q
from app.database.db import get_connection
from app.utils.time_utils import format_duration


class RouteAnalysisService:

    def get_route_summary(self, origin: Optional[str] = None, destination: Optional[str] = None) -> Dict[str, Any]:
        """Return a summary of all routes or a specific O&D pair."""
        if origin and destination:
            df = Q.get_flights_by_route(origin, destination)
            if df.empty:
                return {"route": f"{origin}-{destination}", "found": False, "flights": []}
            return {
                "route": f"{origin}-{destination}",
                "found": True,
                "total_flights": len(df),
                "airlines": sorted(df["airline"].dropna().unique().tolist()),
                "aircraft_types": sorted(df["aircraft_type"].dropna().unique().tolist()),
                "avg_block_time": format_duration(int(df["block_time"].dropna().mean())) if "block_time" in df.columns else None,
                "departures": df[["flight_number", "airline", "departure_local", "aircraft_type"]].head(20).to_dict("records"),
            }
        summary_df = Q.get_route_summary()
        return {"routes": summary_df.to_dict("records")}

    def get_frequency_analysis(self, origin: str, destination: str) -> Dict[str, Any]:
        """Analyse frequency distribution across the week for an O&D pair."""
        df = Q.get_flights_by_route(origin, destination)
        if df.empty:
            return {"route": f"{origin}-{destination}", "frequency": {}, "total": 0}

        day_labels = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}
        freq = {}
        if "day_of_operation" in df.columns:
            counts = df["day_of_operation"].value_counts().to_dict()
            freq = {day_labels.get(int(k), str(k)): int(v) for k, v in counts.items()}

        return {
            "route": f"{origin}-{destination}",
            "total_flights": len(df),
            "frequency_by_day": freq,
            "airlines": sorted(df["airline"].dropna().unique().tolist()),
        }

    def get_market_share(self, origin: str, destination: str) -> Dict[str, Any]:
        """Compute airline market share by flight count on an O&D pair."""
        df = Q.get_flights_by_route(origin, destination)
        if df.empty:
            return {"route": f"{origin}-{destination}", "market_share": {}}
        total = len(df)
        share = (
            df.groupby("airline").size().reset_index(name="count")
        )
        share["share_pct"] = (share["count"] / total * 100).round(1)
        return {
            "route": f"{origin}-{destination}",
            "total_flights": total,
            "market_share": share.to_dict("records"),
        }

    def get_departure_spread(self, origin: str, destination: str) -> Dict[str, Any]:
        """Return departure time distribution for an O&D pair."""
        df = Q.get_flights_by_route(origin, destination)
        if df.empty:
            return {"route": f"{origin}-{destination}", "departures": []}

        deps = []
        for _, row in df.iterrows():
            dep = row.get("departure_local")
            if dep is not None:
                t_str = dep.strftime("%H:%M") if hasattr(dep, "strftime") else str(dep)[:5]
                deps.append({
                    "time": t_str,
                    "flight": row.get("flight_number"),
                    "airline": row.get("airline"),
                })

        deps.sort(key=lambda x: x["time"])
        return {
            "route": f"{origin}-{destination}",
            "departures": deps,
            "count": len(deps),
        }

    def find_schedule_gaps(
        self,
        origin: str,
        destination: str,
        min_gap_hours: int = 4,
    ) -> List[Dict[str, Any]]:
        """
        Find time gaps in the departure schedule on a route.
        Returns gaps larger than *min_gap_hours*.
        """
        df = Q.get_flights_by_route(origin, destination)
        if df.empty or "departure_local" not in df.columns:
            return []

        deps = sorted([
            d for d in df["departure_local"].dropna()
            if hasattr(d, "strftime")
        ])

        gaps = []
        for i in range(1, len(deps)):
            gap_min = int((deps[i] - deps[i - 1]).total_seconds() / 60)
            if gap_min >= min_gap_hours * 60:
                gaps.append({
                    "gap_start": deps[i - 1].strftime("%H:%M"),
                    "gap_end":   deps[i].strftime("%H:%M"),
                    "gap_hours": round(gap_min / 60, 1),
                })

        return gaps

    def get_hub_connectivity_matrix(self, hub: str) -> Dict[str, Any]:
        """Return a summary of inbound vs outbound connectivity at a hub."""
        inbound  = Q.get_arrivals_at(hub)
        outbound = Q.get_departures_from(hub)

        return {
            "hub": hub,
            "inbound_flights": len(inbound),
            "outbound_flights": len(outbound),
            "inbound_origins": sorted(inbound["origin"].dropna().unique().tolist()) if not inbound.empty else [],
            "outbound_destinations": sorted(outbound["destination"].dropna().unique().tolist()) if not outbound.empty else [],
        }
