"""
Visualization extraction service.
Parses agent tool_results and converts them into frontend-renderable viz specs
matching insightsDB table patterns: Market Summary, Itinerary Report, O&D local/flow,
Flight View, QSI-style analysis.
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional

PIE_COLORS = [
    "#2065d1","#0ea5e9","#10b981","#f59e0b","#ef4444",
    "#8b5cf6","#ec4899","#14b8a6","#f97316","#06b6d4",
    "#84cc16","#6366f1","#a78bfa","#34d399","#fb923c",
]

# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────

def _is_numeric(val) -> bool:
    try:
        float(val)
        return True
    except (TypeError, ValueError):
        return False


def _col_is_numeric(rows: List[Dict], col: str) -> bool:
    vals = [r.get(col) for r in rows[:20] if r.get(col) is not None]
    if not vals:
        return False
    return sum(1 for v in vals if _is_numeric(v)) > len(vals) * 0.7


def _find_label_value_cols(columns: List[str], rows: List[Dict]):
    """Return (label_col, value_col) best suited for a bar/pie chart."""
    label_hints = {
        "airline","carrier","airport","origin","orig","dest","destination",
        "category","type","name","aircraft","code","od","route","segment",
        "hour","dep_hour","arr_hour","time_bucket","day","dow","terminal","alliance",
    }
    pct_hints = ("pct","share","percent","ratio","rate")

    cl = [c.lower() for c in columns]
    time_first = any(c in cl[:1] for c in ("hour","dep_hour","arr_hour","time_bucket","day_of_week","dow","day"))

    label_candidates = [c for c in columns if any(h in c.lower() for h in label_hints)]
    pct_cols = [c for c in columns if any(h in c.lower() for h in pct_hints) and _col_is_numeric(rows, c)]
    num_cols = [c for c in columns if _col_is_numeric(rows, c)]

    if time_first:
        label_col = columns[0]
        value_col = next((c for c in columns[1:] if _col_is_numeric(rows, c)), None)
    else:
        label_col = label_candidates[0] if label_candidates else (columns[0] if columns else None)
        value_col = pct_cols[0] if pct_cols else (
            next((c for c in num_cols if c != label_col), None)
        )
    return label_col, value_col


import logging
logger = logging.getLogger(__name__)


def _detect_chart_type(columns: List[str], rows: List[Dict]) -> str:
    cl = [c.lower() for c in columns]
    # "share" columns sum to ~100% across labels → pie makes sense
    # "pct/percent/avg" columns are per-entity comparisons → bar is better
    has_share = any("share" in c and "pct" not in c for c in cl) or any("share_pct" == c or c.endswith("_share") for c in cl)
    has_pct   = any(h in c for c in cl for h in ("pct","percent"))
    time_cols = {"hour","dep_hour","arr_hour","time_bucket","day_of_week","dow","day","dep_hour"}
    has_time = any(c in cl for c in time_cols)

    num_cols = [c for c in columns if _col_is_numeric(rows, c)]
    non_num  = [c for c in columns if not _col_is_numeric(rows, c)]

    if has_time and len(columns) >= 2:
        return "bar"
    if len(non_num) >= 1 and len(num_cols) >= 1:
        # Only use pie when values genuinely represent parts of a whole (share/proportion)
        # Avg/LF/rate columns are comparisons — use horizontal bar instead
        if has_share and not has_pct and len(rows) <= 14:
            return "pie"
        if len(rows) <= 20:
            return "bar" if len(rows) <= 10 else "horizontal_bar"
    if len(columns) == 2 and _col_is_numeric(rows, columns[1]):
        return "bar"
    return "table"


def _bar_data(rows: List[Dict], label_col: str, value_col: str, max_rows: int = 25) -> Dict:
    labels, values = [], []
    for r in rows[:max_rows]:
        lbl = str(r.get(label_col, ""))
        try:
            val = round(float(r.get(value_col) or 0), 2)
        except (TypeError, ValueError):
            val = 0.0
        labels.append(lbl)
        values.append(val)
    return {"labels": labels, "values": values}


def _pie_slices(rows: List[Dict], label_col: str, value_col: str, max_rows: int = 14) -> List[Dict]:
    slices = []
    for r in rows[:max_rows]:
        lbl = str(r.get(label_col, ""))
        try:
            val = round(float(r.get(value_col) or 0), 2)
        except (TypeError, ValueError):
            val = 0.0
        if val > 0:
            slices.append({"label": lbl, "value": val})
    return slices


def _fmt(v, digits=1):
    """Format a number for display."""
    try:
        return round(float(v or 0), digits)
    except (TypeError, ValueError):
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# Per-tool converters
# ─────────────────────────────────────────────────────────────────────────────

_ITIN_COL_HINTS = {
    "stops", "stop_count", "num_stops", "via", "hub", "connect_point",
    "layover", "layover_min", "connection_minutes", "conn_min",
    "leg1", "leg2", "leg_1", "leg_2", "flt_desg", "seg1", "seg2",
    "itin", "itinerary", "connect_time", "mct", "minimum_connect",
    # insightsDB workset column names
    "flt desg (seg1)", "flt desg (seg2)", "flt desg (seg3)",
    "connect point 1", "connect point 2",
}


def _is_itin_columns(columns: List[str]) -> bool:
    """Heuristic: true when the column set looks like an itinerary result."""
    cl = {c.lower().strip() for c in columns}
    return bool(cl & _ITIN_COL_HINTS)


def _sql_vizs(result: Dict) -> List[Dict]:
    columns = result.get("columns") or []
    rows    = result.get("rows") or []
    row_count = result.get("row_count", len(rows))
    chart_hint = (result.get("chart_type") or "").strip().lower()
    if not columns or not rows:
        return []

    # Auto-detect itinerary tables and apply insightsDB table_style
    table_style = "itinerary_report" if _is_itin_columns(columns) else ""

    vizs: List[Dict] = []
    tbl: Dict = {
        "type": "table",
        "title": f"Query Results ({row_count} rows)",
        "columns": columns,
        "rows": rows[:200],
        "row_count": row_count,
    }
    if table_style:
        tbl["table_style"] = table_style
    vizs.append(tbl)

    # ── Radar: multi-metric comparison across entities ─────────────────────
    if chart_hint == "radar" or (chart_hint == "" and _should_radar(columns, rows)):
        radar = _radar_data(columns, rows)
        if radar:
            vizs.append(radar)
            return vizs

    # ── Heatmap: two-axis grid (e.g. hour × day) ──────────────────────────
    if chart_hint == "heatmap" or (chart_hint == "" and _should_heatmap(columns, rows)):
        hm = _heatmap_data(columns, rows)
        if hm:
            vizs.append(hm)
            return vizs

    # ── Table-only if explicitly requested ────────────────────────────────
    if chart_hint == "table":
        return vizs

    # ── Bar / Pie auto-detection (or honoured hint) ───────────────────────
    ct = chart_hint if chart_hint in ("bar", "horizontal_bar", "pie") else _detect_chart_type(columns, rows)
    label_col, value_col = _find_label_value_cols(columns, rows)

    if ct in ("bar", "horizontal_bar") and label_col and value_col:
        data = _bar_data(rows, label_col, value_col)
        if data["labels"]:
            vc = value_col.replace('_',' ').replace(' pct',' %').title()
            lc = label_col.replace('_',' ').title()
            vizs.append({"type": ct, "title": f"{vc} by {lc}",
                         "label_col": label_col, "value_col": value_col, "data": data})
    elif ct == "pie" and label_col and value_col:
        slices = _pie_slices(rows, label_col, value_col)
        if slices:
            vc = value_col.replace('_',' ').replace(' pct',' %').title()
            vizs.append({"type": "pie", "title": f"{vc} Share", "slices": slices})

    return vizs


# ── Radar helpers ──────────────────────────────────────────────────────────────

def _should_radar(columns: List[str], rows: List[Dict]) -> bool:
    """True when result looks like a multi-airline × multi-metric comparison."""
    if len(rows) < 2 or len(rows) > 12:
        return False
    num_cols = [c for c in columns if _col_is_numeric(rows, c)]
    non_num  = [c for c in columns if not _col_is_numeric(rows, c)]
    # Radar: 1 label col + 4+ numeric metric cols (airline comparison pattern)
    return len(non_num) == 1 and len(num_cols) >= 4


def _radar_data(columns: List[str], rows: List[Dict]) -> Optional[Dict]:
    num_cols = [c for c in columns if _col_is_numeric(rows, c)]
    non_num  = [c for c in columns if not _col_is_numeric(rows, c)]
    if not non_num or len(num_cols) < 3:
        return None
    label_col = non_num[0]
    # Normalize each metric 0-100 for radar readability
    col_maxes = {}
    for col in num_cols:
        vals = [float(r.get(col) or 0) for r in rows]
        col_maxes[col] = max(vals) if vals else 1
    datasets = []
    for row in rows[:10]:
        label = str(row.get(label_col, ""))
        data  = [
            round(float(row.get(col) or 0) / max(col_maxes[col], 0.001) * 100, 1)
            for col in num_cols
        ]
        datasets.append({"label": label, "data": data})
    axes = [c.replace('_',' ').replace(' pct',' %').title() for c in num_cols]
    return {
        "type": "radar",
        "title": f"Multi-Metric Comparison · {label_col.replace('_',' ').title()}",
        "labels": axes,
        "datasets": datasets,
        "note": "Values normalized 0–100 per metric (100 = max observed)",
    }


# ── Heatmap helpers ────────────────────────────────────────────────────────────

def _should_heatmap(columns: List[str], rows: List[Dict]) -> bool:
    """True when the query looks like a time × day departure-count heatmap."""
    cl = [c.lower() for c in columns]
    has_time = any(h in c for c in cl for h in ("hour","time_bucket","dep_hour"))
    has_day  = any(h in c for c in cl for h in ("day","dow","day_of_week"))
    num_cols = [c for c in columns if _col_is_numeric(rows, c)]
    return has_time and has_day and len(num_cols) >= 1 and len(rows) >= 4


def _heatmap_data(columns: List[str], rows: List[Dict]) -> Optional[Dict]:
    cl = [c.lower() for c in columns]
    # Find row-axis col (day), col-axis col (hour/time), value col
    day_col  = next((columns[i] for i, c in enumerate(cl) if any(h in c for h in ("day","dow","day_of_week"))), None)
    time_col = next((columns[i] for i, c in enumerate(cl) if any(h in c for h in ("hour","time_bucket","dep_hour"))), None)
    num_cols = [c for c in columns if _col_is_numeric(rows, c)]
    if not (day_col and time_col and num_cols):
        return None
    val_col = num_cols[0]
    # Build pivot: rows=days, cols=time buckets
    days  = sorted(set(str(r.get(day_col, "")) for r in rows))
    times = sorted(set(str(r.get(time_col, "")) for r in rows))
    lookup = {(str(r.get(day_col,"")), str(r.get(time_col,""))): float(r.get(val_col) or 0) for r in rows}
    matrix = [[lookup.get((d, t), 0) for t in times] for d in days]
    return {
        "type": "heatmap",
        "title": f"{val_col.replace('_',' ').title()} · {day_col.replace('_',' ').title()} × {time_col.replace('_',' ').title()}",
        "row_labels": days,
        "col_labels": times,
        "matrix": matrix,
    }


def _route_analysis_vizs(result: Dict) -> List[Dict]:
    """Build vizs from get_route_analysis result (insightsDB Flight View pattern)."""
    vizs: List[Dict] = []
    origin = (result.get("origin") or "").upper()
    dest   = (result.get("destination") or "").upper()
    route  = f"{origin}-{dest}" if origin and dest else result.get("route","")

    # KPI row
    kpis = []
    total_f = result.get("total_nonstop_flights") or result.get("total_weekly_flights") or result.get("total_flights")
    airlines_count = result.get("airlines_count") or len(result.get("airlines_on_route") or [])
    avg_dep = result.get("avg_departure_hour")
    dow_dist = result.get("day_of_week_distribution") or {}

    if total_f:
        kpis.append({"label": "Total Flights", "value": str(int(total_f)), "tone": "blue"})
    if airlines_count:
        kpis.append({"label": "Airlines", "value": str(int(airlines_count)), "tone": "teal"})
    if avg_dep:
        kpis.append({"label": "Avg Dep Hour", "value": f"{float(avg_dep):.1f}h", "tone": "amber"})
    if kpis:
        vizs.append({"type": "kpi_row", "title": f"Route Snapshot · {route}", "cards": kpis})

    # Flights-on-day table (insightsDB Flight View)
    flights = result.get("flights_on_day") or result.get("flights") or []
    if flights:
        flt_cols = []
        col_keys = ["airline","flight_number","departure_local","arrival_local",
                    "aircraft_type","frequency","day_of_operation","block_time","service_type"]
        for k in col_keys:
            if any(k in f for f in flights):
                flt_cols.append(k)
        if not flt_cols and flights:
            flt_cols = list(flights[0].keys())[:10]
        vizs.append({
            "type": "table",
            "title": f"Flight Schedule · {route}",
            "columns": flt_cols,
            "rows": flights[:100],
            "row_count": len(flights),
            "table_style": "flight_view",
        })

    # Day-of-week distribution bar
    if dow_dist:
        day_labels = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
        labels = [day_labels[int(k)-1] if str(k).isdigit() and 1<=int(k)<=7 else str(k) for k in sorted(dow_dist.keys())]
        values = [int(dow_dist[k]) for k in sorted(dow_dist.keys())]
        if any(v > 0 for v in values):
            vizs.append({"type": "bar", "title": f"Flights by Day · {route}",
                         "label_col": "day", "value_col": "flights",
                         "data": {"labels": labels, "values": values}})

    # Airlines bar
    airlines = result.get("airlines_on_route") or result.get("airlines") or []
    if airlines and isinstance(airlines[0], dict):
        al_labels = [a.get("airline","") for a in airlines[:15]]
        al_values = [int(a.get("flights", a.get("total_flights", 0))) for a in airlines[:15]]
        if al_labels:
            vizs.append({"type": "horizontal_bar", "title": f"Flights per Airline · {route}",
                         "label_col": "airline", "value_col": "flights",
                         "data": {"labels": al_labels, "values": al_values}})

    return vizs


def _route_intel_vizs(result: Dict) -> List[Dict]:
    """Build vizs from get_route_intelligence — insightsDB Market Summary + O&D View pattern."""
    vizs: List[Dict] = []
    route   = result.get("route", "")
    airlines = result.get("airlines") or []
    spill   = result.get("spill_analysis") or {}
    buckets = result.get("departure_time_buckets") or {}

    # ── KPI row ──────────────────────────────────────────────────────────────
    kpis: List[Dict] = []
    total_f = result.get("total_nonstop_flights") or result.get("total_flights")
    weekly  = result.get("weekly_frequency")
    demand  = result.get("demand_index")
    avg_blk = result.get("avg_block_time_min")

    if total_f:
        kpis.append({"label": "Total Flights", "value": str(int(total_f)), "tone": "blue"})
    if weekly:
        kpis.append({"label": "Weekly Freq",   "value": str(int(weekly)),  "tone": "teal"})
    if demand:
        kpis.append({"label": "Demand Index",  "value": f"{float(demand):,.0f}", "tone": "amber"})
    if avg_blk:
        kpis.append({"label": "Avg Block",     "value": f"{int(float(avg_blk))}m", "tone": "indigo"})
    if spill:
        hs = spill.get("host_market_share_pct")
        if hs is not None:
            kpis.append({"label": "Host Share", "value": f"{float(hs):.1f}%", "tone": "purple"})
        local_pax = spill.get("local_pax") or spill.get("total_local_pax")
        flow_pax  = spill.get("flow_pax")  or spill.get("total_flow_pax")
        if local_pax:
            kpis.append({"label": "Local Pax",  "value": f"{float(local_pax):,.0f}", "tone": "green"})
        if flow_pax:
            kpis.append({"label": "Flow Pax",   "value": f"{float(flow_pax):,.0f}",  "tone": "slate"})

    if kpis:
        vizs.append({"type": "kpi_row", "title": f"Route Intelligence · {route}", "cards": kpis})

    # ── Market Summary table (insightsDB pattern) ─────────────────────────────
    if airlines:
        al_cols = ["airline"]
        al_rows: List[Dict] = []
        for a in airlines[:25]:
            # workset_service uses "code" not "airline"; support both
            al_code = a.get("code") or a.get("airline", "")
            row: Dict = {"airline": al_code}
            # Map actual workset_service field names → display names
            for src_k, dst_k in [
                # workset_service actual keys
                ("weekly_flight_days", "flights"),
                ("unique_dep_times",   "dep_slots"),
                ("avg_seats_per_flight","avg_seats"),
                ("weekly_seat_capacity","weekly_seats"),
                ("market_share_pct",   "mkt_share%"),
                ("weekly_spill",       "spill"),
                ("weekly_recap",       "recap"),
                ("avg_lf",             "avg_lf%"),
                ("carrier_type",       "type"),
                ("alliance",           "alliance"),
                ("aircraft_types",     "aircraft"),
                # fallback legacy keys (in case tool returns different format)
                ("nstop_freq","nstops"), ("thru_freq","thrus"), ("connect_freq","cncts"),
                ("total_flights","flights"), ("seat_capacity","seats"),
                ("local_pax","local_pax"), ("flow_pax","flow_pax"),
            ]:
                v = a.get(src_k)
                if v is not None:
                    if dst_k not in row:  # first mapping wins
                        if isinstance(v, list):
                            row[dst_k] = ", ".join(str(x) for x in v[:4])
                        elif isinstance(v, float):
                            row[dst_k] = round(v, 1)
                        else:
                            row[dst_k] = v
                        if dst_k not in al_cols:
                            al_cols.append(dst_k)
            al_rows.append(row)

        # Ensure mkt_share% exists — compute from flights if missing
        if "mkt_share%" not in al_cols and "flights" in al_cols:
            total_flights = sum(int(r.get("flights") or 0) for r in al_rows) or 1
            for r in al_rows:
                r["mkt_share%"] = round(int(r.get("flights") or 0) / total_flights * 100, 1)
            al_cols.append("mkt_share%")

        # Rebuild cols in insightsDB-style order
        ordered = ["airline"]
        for c in ["flights","dep_slots","avg_seats","weekly_seats","mkt_share%",
                  "spill","recap","avg_lf%","local_pax","flow_pax",
                  "type","alliance","aircraft","nstops","thrus","cncts","seats"]:
            if c in al_cols and c not in ordered:
                ordered.append(c)

        vizs.append({
            "type": "table",
            "title": f"Market Summary · {route}",
            "columns": ordered,
            "rows": al_rows,
            "row_count": len(al_rows),
            "table_style": "market_summary",
        })

        # Market share pie
        if "mkt_share%" in al_cols:
            slices = [{"label": r["airline"], "value": r.get("mkt_share%", 0)} for r in al_rows if r.get("mkt_share%", 0) > 0]
            if slices:
                vizs.append({"type": "pie", "title": f"Market Share · {route}", "slices": slices})

        # Flights per airline horizontal bar
        bar_values = [int(r.get("flights") or r.get("nstops") or 0) for r in al_rows[:15]]
        bar_labels  = [r["airline"] for r in al_rows[:15]]
        if bar_labels and any(v > 0 for v in bar_values):
            vizs.append({"type": "horizontal_bar", "title": f"Flights per Airline · {route}",
                         "label_col": "airline", "value_col": "flights",
                         "data": {"labels": bar_labels, "values": bar_values}})

    # ── Local vs Flow split (O&D View pattern) ────────────────────────────────
    local_pax = spill.get("local_pax") or spill.get("total_local_pax")
    flow_pax  = spill.get("flow_pax")  or spill.get("total_flow_pax")
    local_rev = spill.get("local_revenue")
    flow_rev  = spill.get("flow_revenue")
    connecting_routes = result.get("connecting_routes") or spill.get("connecting_routes") or []

    if local_pax is not None or flow_pax is not None:
        total_pax = (float(local_pax or 0) + float(flow_pax or 0)) or 1
        total_rev = (float(local_rev or 0) + float(flow_rev or 0)) or 1
        local_pct = round(float(local_pax or 0) / total_pax * 100, 1)
        flow_pct  = round(float(flow_pax  or 0) / total_pax * 100, 1)
        local_rev_pct = round(float(local_rev or 0) / total_rev * 100, 1) if local_rev else None
        flow_rev_pct  = round(float(flow_rev  or 0) / total_rev * 100, 1) if flow_rev  else None
        # Parse O&D from route string e.g. "DXB-BOM"
        _parts = route.split("-") if route else []
        _origin = _parts[0].strip() if len(_parts) >= 1 else ""
        _dest   = _parts[1].strip() if len(_parts) >= 2 else ""
        vizs.append({
            "type": "local_flow_split",
            "title": f"Demand Mix · {route}",
            "origin": _origin,
            "destination": _dest,
            "local_pax": float(local_pax or 0),
            "flow_pax":  float(flow_pax  or 0),
            "local_pct": local_pct,
            "flow_pct":  flow_pct,
            "local_rev": float(local_rev or 0) if local_rev else None,
            "flow_rev":  float(flow_rev  or 0) if flow_rev  else None,
            "local_rev_pct": local_rev_pct,
            "flow_rev_pct":  flow_rev_pct,
            "connecting_routes": connecting_routes,
        })

    # ── Departure time distribution ───────────────────────────────────────────
    if buckets:
        labels = sorted(buckets.keys())
        values = [int(buckets[k]) for k in labels]
        if any(v > 0 for v in values):
            vizs.append({"type": "bar", "title": f"Departures by Time of Day · {route}",
                         "label_col": "time", "value_col": "flights",
                         "data": {"labels": labels, "values": values}})

    # ── Connecting routes table ───────────────────────────────────────────────
    if connecting_routes:
        cn_cols = []
        if isinstance(connecting_routes[0], dict):
            for k in ["via","direction","airline","weekly_freq","connect_time_min","frequency","demand","traffic"]:
                if any(k in r for r in connecting_routes):
                    cn_cols.append(k)
            if not cn_cols:
                cn_cols = list(connecting_routes[0].keys())[:8]
            vizs.append({
                "type": "table",
                "title": f"Connecting Routes · {route}",
                "columns": cn_cols,
                "rows": connecting_routes[:50],
                "row_count": len(connecting_routes),
                "table_style": "itinerary_report",
            })

    return vizs


def _competitor_analysis_vizs(result: Dict) -> List[Dict]:
    """insightsDB-style competitor/market table + charts."""
    vizs: List[Dict] = []
    origin = result.get("origin", "")
    dest   = result.get("destination", "")
    airlines = result.get("airlines") or []
    if not airlines:
        return vizs

    route = f"{origin}-{dest}"

    # KPI
    total_ops  = result.get("total_operations", 0)
    n_airlines = result.get("total_airlines", len(airlines))
    leader     = result.get("market_leader", "")
    kpis = [
        {"label": "Airlines",      "value": str(n_airlines), "tone": "blue"},
        {"label": "Total Ops",     "value": str(total_ops),  "tone": "teal"},
        {"label": "Market Leader", "value": leader,           "tone": "amber"},
    ]
    vizs.append({"type": "kpi_row", "title": f"Competitor Overview · {route}", "cards": kpis})

    # Market summary table (insightsDB pattern with nstops, cncts, demand, traffic, revenue cols)
    al_cols_order = ["airline","weekly_operations","market_share_pct","seat_capacity",
                     "aircraft_types","aircraft_category","earliest_departure","latest_departure"]
    al_rows = []
    for a in airlines:
        row = {}
        for c in al_cols_order:
            v = a.get(c)
            if v is not None:
                row[c] = round(float(v), 1) if isinstance(v, float) else v
        row["airline"] = a.get("airline","")
        al_rows.append(row)

    vizs.append({
        "type": "table",
        "title": f"Airline Comparison · {route}",
        "columns": ["airline"] + [c for c in al_cols_order if c != "airline" and any(c in r for r in al_rows)],
        "rows": al_rows,
        "row_count": len(al_rows),
        "table_style": "market_summary",
    })

    # Market share pie
    slices = [{"label": a.get("airline",""), "value": a.get("market_share_pct", 0)}
              for a in airlines if a.get("market_share_pct", 0) > 0]
    if slices:
        vizs.append({"type": "pie", "title": f"Market Share · {route}", "slices": slices})

    # Weekly ops bar
    data = {"labels": [a.get("airline","") for a in airlines],
            "values": [a.get("weekly_operations", 0) for a in airlines]}
    if data["labels"]:
        vizs.append({"type": "horizontal_bar", "title": f"Weekly Operations · {route}",
                     "label_col": "airline", "value_col": "weekly_operations", "data": data})

    return vizs


def _search_schedule_vizs(result: Dict) -> List[Dict]:
    """insightsDB Flight View pattern for search_schedule results."""
    vizs: List[Dict] = []
    flights = result.get("flights") or []
    if not flights:
        return vizs
    cols_order = ["airline","flight_number","origin","destination",
                  "departure_local","arrival_local","aircraft_type",
                  "frequency","day_of_operation","block_time","service_type"]
    cols = [c for c in cols_order if any(c in f for f in flights)]
    if not cols:
        cols = list(flights[0].keys())[:10]
    vizs.append({
        "type": "table",
        "title": f"Flight Search ({result.get('count', len(flights))} results)",
        "columns": cols,
        "rows": flights[:200],
        "row_count": result.get("count", len(flights)),
        "table_style": "flight_view",
    })
    return vizs


def _generic_table_viz(result: Dict, title_prefix: str) -> List[Dict]:
    for key in ("flights","schedules","data","results","rows","records"):
        items = result.get(key)
        if items and isinstance(items, list) and items:
            cols = list(items[0].keys())
            return [{
                "type": "table",
                "title": f"{title_prefix} ({result.get('count', len(items))} rows)",
                "columns": cols,
                "rows": items[:200],
                "row_count": result.get("count", len(items)),
            }]
    return []


def _find_path_vizs(result: Dict) -> List[Dict]:
    """Flatten find_path path options into an insightsDB-style itinerary table."""
    paths = result.get("paths") or []
    if not paths:
        return []
    origin = result.get("origin", "")
    dest   = result.get("destination", "")
    rows: List[Dict] = []
    for p in paths:
        stops = int(p.get("stops", 0))
        legs  = p.get("legs") or []
        row: Dict = {
            "stops":      stops,
            "total_time": f"{int(p.get('total_block_min', 0) or 0) // 60}h {int(p.get('total_block_min', 0) or 0) % 60}m",
            "path":       " → ".join(p.get("path") or [origin, dest]),
        }
        if legs:
            row["airline"]   = legs[0].get("airline", "")
            row["leg1"]      = f"{legs[0]['from']}→{legs[0]['to']}"
            row["blk1_min"]  = legs[0].get("block_min", "")
        if len(legs) >= 2:
            row["via"]       = legs[0].get("to", "")
            row["leg2"]      = f"{legs[1]['from']}→{legs[1]['to']}"
            row["blk2_min"]  = legs[1].get("block_min", "")
        if len(legs) >= 3:
            row["leg3"]      = f"{legs[2]['from']}→{legs[2]['to']}"
            row["blk3_min"]  = legs[2].get("block_min", "")
        rows.append(row)

    # Build columns in a logical order
    base_cols = ["stops", "total_time", "airline", "path"]
    extra = ["leg1", "blk1_min", "via", "leg2", "blk2_min", "leg3", "blk3_min"]
    cols = base_cols + [c for c in extra if any(c in r for r in rows)]

    return [{
        "type": "table",
        "title": f"Routing Options · {origin}–{dest}",
        "columns": cols,
        "rows": rows,
        "row_count": len(rows),
        "table_style": "itinerary_report",
    }]


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def extract_visualizations(tool_results: List[Dict]) -> List[Dict]:
    """
    Parse agent tool_results → list of viz specs for the frontend.
    Each spec: {type, title, ...type-specific fields}.
    """
    all_vizs: List[Dict] = []

    for tr in (tool_results or []):
        tool = tr.get("tool", "")
        result = {k: v for k, v in tr.items() if k != "tool"}

        if tool == "execute_sql":
            all_vizs.extend(_sql_vizs(result))
        elif tool in ("get_route_intelligence",):
            all_vizs.extend(_route_intel_vizs(result))

        elif tool in ("get_route_analysis", "get_route_summary"):
            all_vizs.extend(_route_analysis_vizs(result))

        elif tool == "get_competitor_analysis":
            all_vizs.extend(_competitor_analysis_vizs(result))

        elif tool == "search_schedule":
            all_vizs.extend(_search_schedule_vizs(result))

        elif tool == "get_airport_overview":
            all_vizs.extend(_generic_table_viz(result, "Airport Overview"))

        elif tool == "get_itin_report":
            orig = result.get("origin", "")
            dest = result.get("destination", "")
            carrier = result.get("carrier_filter", "all")
            title = f"Itinerary View: {orig}→{dest}" + (f" ({carrier})" if carrier != "all" else "")
            rows = result.get("rows", [])
            if rows:
                # Show key columns only to keep table readable
                display_cols = [
                    "Dept Arp", "Arvl Arp", "Flt Desg (Seg1)", "Connect Point 1",
                    "Flt Desg (Seg2)", "Stops", "Freq", "Dept Time", "Arvl Time",
                    "Elap Time", "Total Demand", "Total Traffic",
                ]
                trimmed = [{c: r.get(c, "") for c in display_cols} for r in rows[:200]]
                all_vizs.append({
                    "type": "table",
                    "title": f"{title} ({len(rows)} itins)",
                    "columns": display_cols,
                    "rows": trimmed,
                    "row_count": len(rows),
                })

    # De-duplicate by title
    seen: set = set()
    out: List[Dict] = []
    for v in all_vizs:
        t = v.get("title","")
        if t not in seen:
            seen.add(t)
            out.append(v)

    # Sort: kpi_row → local_flow_split → table → charts
    order = {"kpi_row": 0, "local_flow_split": 1, "table": 2, "bar": 3, "horizontal_bar": 4, "pie": 5, "radar": 3, "heatmap": 3}
    out.sort(key=lambda v: order.get(v.get("type",""), 6))
    return out



# ─────────────────────────────────────────────────────────────────────────────
# AI Chart Suggestion  — asks Gemini to pick the best chart for any data set
# ─────────────────────────────────────────────────────────────────────────────

# Columns that are never meaningful to visualise
_JUNK_COL_PATTERNS = {
    "id", "uuid", "key", "hash", "index", "idx", "row_num", "rownum",
    "record_id", "flight_id", "created_at", "updated_at", "timestamp",
    "effective_from", "effective_to", "utc_offset_dep", "utc_offset_arr",
    "terminal_dep", "terminal_arr", "day_of_operation",
    "baseindex_l1", "baseindex_l2", "baseindex", "spill_index",
}

# Column name fragments → semantic type  (checked in order)
_COL_SEMANTIC_RULES: List[tuple] = [
    # airport/hub nodes — most important for network charts
    (("origin", "dest", "airport", "dep_arp", "arr_arp", "connect_point",
      "connection_point", "via_point", "hub", "gateway", "from_airport", "to_airport"), "airport_code"),
    # airline / carrier
    (("mkt_airline", "op_airline", "carrier", "airline", "aln"), "airline_code"),
    # flight identifiers — informational only, not a metric
    (("flight_number", "flt_num", "flt_no", "flt"), "flight_number"),
    # explicit percentage / share columns
    (("pct", "percent", "share", "rate", "ratio", "lf", "load_factor"), "percentage"),
    # volume / count metrics — good for chart values
    (("pax", "seats", "freq", "flights", "count", "total", "sum",
      "revenue", "yield", "capacity", "demand", "spill"), "numeric_metric"),
]

def _is_junk_col(col: str) -> bool:
    cl = col.lower().strip()
    return (
        cl in _JUNK_COL_PATTERNS
        or cl.endswith("_id")
        or cl.endswith("_key")
        or cl.endswith("_hash")
        or cl.startswith("__")
        or cl in ("id", "pk", "fk")
    )

def _col_profile(col: str, rows: List[Dict]) -> dict:
    """
    Return a rich profile dict for a column:
      type: semantic type string
      cardinality: number of unique non-null values
      constant: True if all values are identical (useless for charts)
      flag_numeric: True if it looks like stops/boolean (≤4 unique tiny ints)
      sample: short list of example values
    """
    sample_all = [r.get(col) for r in rows if r.get(col) is not None]
    sample = sample_all[:30]
    unique_vals = list(dict.fromkeys(str(v) for v in sample_all))  # ordered unique

    cardinality = len(unique_vals)
    constant = (cardinality <= 1)

    cl = col.lower().strip()

    # Semantic type via rules
    sem_type = "categorical"
    for keywords, stype in _COL_SEMANTIC_RULES:
        if any(kw in cl for kw in keywords):
            sem_type = stype
            break
    else:
        # fallback: numeric detection
        num_count = sum(1 for v in sample if _is_numeric(v))
        if num_count > len(sample) * 0.7:
            sem_type = "numeric"

    # Detect "flag" numerics: numeric column with ≤4 unique tiny integer values
    # (e.g. stops=0/1/2, codeshare=0/1) — these are dimensions, NOT metrics
    flag_numeric = False
    if sem_type in ("numeric", "numeric_metric"):
        try:
            int_vals = {int(float(v)) for v in unique_vals if _is_numeric(v)}
            if len(int_vals) <= 4 and all(0 <= x <= 10 for x in int_vals):
                flag_numeric = True
                sem_type = "flag_or_category"
        except Exception:
            pass

    return {
        "type": sem_type,
        "cardinality": cardinality,
        "constant": constant,
        "flag_numeric": flag_numeric,
        "sample": unique_vals[:5],
    }

def _select_chart_columns(col_profiles: dict) -> dict:
    """
    Pre-select the best columns for common chart mappings so Gemini has strong hints.
    Returns a hints dict the prompt can include.
    """
    airport_cols = [c for c, p in col_profiles.items()
                    if p["type"] == "airport_code" and not p["constant"]]
    airline_cols = [c for c, p in col_profiles.items()
                    if p["type"] == "airline_code" and not p["constant"]]
    metric_cols  = [c for c, p in col_profiles.items()
                    if p["type"] in ("numeric_metric", "numeric", "percentage")
                    and not p["constant"] and not p["flag_numeric"]]

    hints = {}
    # Network: need 2 airport cols (or airport+airline)
    if len(airport_cols) >= 2:
        hints["suggested_chart"] = "network"
        hints["source_hint"] = airport_cols[0]
        hints["target_hint"] = airport_cols[1]
        hints["weight_hint"] = metric_cols[0] if metric_cols else None
    elif len(airport_cols) == 1 and airline_cols:
        hints["suggested_chart"] = "network"
        hints["source_hint"] = airline_cols[0]
        hints["target_hint"] = airport_cols[0]
        hints["weight_hint"] = metric_cols[0] if metric_cols else None
    # Bubble: need 2+ metrics
    elif len(metric_cols) >= 3:
        entity = (airport_cols + airline_cols + [None])[0]
        hints["suggested_chart"] = "bubble"
        hints["label_hint"] = entity
        hints["x_hint"] = metric_cols[0]
        hints["y_hint"] = metric_cols[1]
        hints["r_hint"] = metric_cols[2]
    elif len(metric_cols) >= 1 and (airport_cols or airline_cols):
        entity = (airport_cols + airline_cols)[0]
        hints["suggested_chart"] = "bar"
        hints["label_hint"] = entity
        hints["value_hint"] = metric_cols[0]

    return hints


_CHART_SUGGEST_SYSTEM = (
    "You are a senior data visualization expert embedded in an airline network intelligence platform. "
    "You deeply understand airline scheduling data: IATA airport codes, airline codes, flight numbers, "
    "passenger volumes, seat capacity, load factors, market share, route frequencies, connecting itineraries. "
    "Your ONLY output is a single valid JSON object — no markdown, no prose, nothing else."
)

_CHART_SUGGEST_PROMPT = """\
CONTEXT
-------
User question: {query}
AI answer summary: {answer_snippet}

AVAILABLE COLUMNS (IDs and constants already removed)
------------------------------------------------------
{col_profiles}

COLUMN HINTS (pre-computed based on column semantics — trust these strongly):
{hints_text}

SAMPLE DATA ({n_rows} rows):
{rows_json}

RULES — read carefully before responding:
1. CONSTANT columns (cardinality=1, same value in every row) are USELESS for charts. Never use them as axes.
2. FLAG/ENUM columns (flag_or_category type, e.g. stops=0/1/2, codeshare=0/1) are dimension groupings, NOT metrics. Never use them as a y-axis value or weight.
3. airport_code columns are the best source/target nodes for network charts.
4. numeric_metric columns (pax, seats, flights, freq, revenue) are the best values/weights.
5. If the query involves connecting flights, routing, ODs, flow, or markets — STRONGLY prefer "network".
6. A bar chart of "stops by origin" or "stops by market_origin" is WRONG and USELESS. Never do this.
7. Only use "bar" if there is NO origin+destination pair available.

Chart type reference:
  "network"  — source_col (airport/airline) → target_col (airport/airline), weight_col = traffic metric
  "bubble"   — x_col, y_col, r_col all numeric_metric; label_col = airport or airline code
  "bar"      — label_col = category (airport/airline); value_col = one numeric_metric
  "pie"      — only for share/pct data summing to 100%
  "scatter"  — exactly 2 continuous metrics, no natural size

Return ONLY valid JSON (omit unused keys):
{{
  "chart_type": "network|bubble|bar|pie|scatter",
  "title": "<specific, insightful title — name the actual airports/airlines/metric>",
  "source_col": "<(network) origin or source airport/airline column>",
  "target_col": "<(network) destination or target airport/airline column>",
  "weight_col": "<(network) traffic volume column>",
  "label_col": "<(bubble/bar/scatter) entity identifier>",
  "x_col":     "<(bubble/scatter) numeric metric for x>",
  "y_col":     "<(bubble/scatter) numeric metric for y>",
  "r_col":     "<(bubble) numeric metric for size>",
  "value_col": "<(bar/pie) numeric metric>"
}}
"""


def suggest_chart_spec(
    query: str,
    answer: str,
    columns: List[str],
    rows: List[Dict],
) -> Optional[Dict]:
    """
    Ask Gemini to pick the best chart type for the given data.
    Pre-filters junk/constant/flag columns, sends rich profiles + pre-computed hints.
    """
    import json as _json
    from app.ai.vertex_client import generate_content, is_available, extract_text

    if not is_available() or not columns or not rows:
        return None

    # ── Strip junk columns ────────────────────────────────────────────────────
    clean_cols = [c for c in columns if not _is_junk_col(c)]
    if not clean_cols:
        clean_cols = columns

    # ── Build rich profiles ───────────────────────────────────────────────────
    profiles: dict = {c: _col_profile(c, rows) for c in clean_cols}

    # Drop constant columns (zero chart value), but keep at least 2 cols
    useful_cols = [c for c in clean_cols if not profiles[c]["constant"]]
    if len(useful_cols) >= 2:
        clean_cols = useful_cols
        profiles = {c: profiles[c] for c in clean_cols}

    # ── Pre-select column hints ───────────────────────────────────────────────
    hints = _select_chart_columns(profiles)

    # Format profiles for prompt
    col_profiles_lines = []
    for c in clean_cols:
        p = profiles[c]
        flags = ""
        if p["flag_numeric"]:
            flags += "  ⚠ FLAG/ENUM — do NOT use as metric"
        if p["constant"]:
            flags += "  ⚠ CONSTANT — useless for charts"
        col_profiles_lines.append(
            f"  {c!r:32s} type={p['type']:20s} cardinality={p['cardinality']:4d}"
            f"  e.g. {', '.join(p['sample'][:4])}{flags}"
        )
    col_profiles_str = "\n".join(col_profiles_lines)

    hints_lines = []
    if hints:
        hints_lines.append(f"  Recommended chart type: {hints.get('suggested_chart','?').upper()}")
        for k, v in hints.items():
            if k != "suggested_chart" and v:
                hints_lines.append(f"  {k}: {v}")
    hints_text = "\n".join(hints_lines) if hints_lines else "  (no strong pre-selection — use column profiles above)"

    # ── Build clean sample rows ───────────────────────────────────────────────
    clean_rows = [{c: r.get(c) for c in clean_cols} for r in rows[:15]]
    try:
        rows_json = _json.dumps(clean_rows, default=str)
        if len(rows_json) > 3000:
            rows_json = _json.dumps(clean_rows[:6], default=str)
    except Exception:
        rows_json = str(clean_rows)[:2000]

    prompt_text = _CHART_SUGGEST_PROMPT.format(
        query=query[:300],
        answer_snippet=answer[:500],
        col_profiles=col_profiles_str,
        hints_text=hints_text,
        n_rows=len(clean_rows),
        rows_json=rows_json,
    )

    try:
        resp = generate_content(
            contents=[{"role": "user", "parts": [{"text": prompt_text}]}],
            system_instruction=_CHART_SUGGEST_SYSTEM,
            temperature=0.0,
        )
        raw = extract_text(resp)
        if not raw:
            return None
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip().rstrip("```").strip()
        spec = _json.loads(raw)
        if not isinstance(spec, dict) or "chart_type" not in spec:
            return None

        # Validate chosen columns exist; clear invalid ones
        valid = set(clean_cols)
        for key in ("x_col","y_col","r_col","label_col","source_col","target_col","weight_col","value_col"):
            if key in spec and spec[key]:
                if spec[key] not in valid:
                    match = next((c for c in clean_cols if c.lower() == str(spec[key]).lower()), None)
                    spec[key] = match

        # Attach data
        spec["columns"] = clean_cols
        spec["rows"] = [{c: r.get(c) for c in clean_cols} for r in rows[:80]]
        return spec
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Summary Charts — generate D3/Plotly chart specs from a full chat response
# ─────────────────────────────────────────────────────────────────────────────

_SUMMARY_CHARTS_SYSTEM = """\
You are an expert data visualization scientist for an airline intelligence platform.

Analyze the AI assistant's response and generate a JSON array of chart specifications
that visually represent ALL quantitative insights in the response.

OUTPUT: Only a valid JSON array. Use [] if no meaningful numeric data exists.

Each chart spec object:
{
  "id": "c1",                    // unique string ID
  "type": "<chart_type>",        // see types below
  "title": "<specific title>",   // data-driven, name the entities/metrics
  "subtitle": "<context>",       // optional, ≤60 chars
  "width": "half"|"full",        // full = spans 2 columns in grid
  "note": "<1-2 sentence insight>",  // optional
  "data": [...],                 // REQUIRED array of uniform objects with REAL values

  // Field mappings (vary by type):
  "x": "<field>",        // bar, line, area, scatter
  "y": "<field>",        // bar, line, area, scatter (or array for grouped/stacked)
  "group": "<field>",    // bar-grouped, bar-stacked, area-stacked, streamgraph
  "size": "<field>",     // bubble
  "label": "<field>",    // pie, donut, treemap, sunburst, radial-bar
  "value": "<field>",    // pie, donut, treemap, sunburst, radial-bar, waterfall, funnel
  "source": "<field>",   // sankey, chord, force
  "target": "<field>",   // sankey, chord, force
  "weight": "<field>",   // sankey, chord, force
  "rows": "<field>",     // heatmap, calendar
  "cols": "<field>",     // heatmap
  "val": "<field>",      // heatmap, calendar
  "bins": 10,            // histogram
  "categories": ["<f1>","<f2>"]  // parallel: list of numeric field names
}

CHART TYPES (from D3 Observable gallery):
  bar            — vertical bar (category → numeric value)
  bar-horizontal — horizontal bar (better for long labels, rankings)
  bar-grouped    — multiple series side-by-side (needs group field)
  bar-stacked    — stacked bars, absolute or percent
  line           — line chart (ordered x, numeric y)
  area           — filled area chart
  area-stacked   — stacked area (multiple series, needs group field)
  streamgraph    — stream graph (smooth stacked area)
  scatter        — scatter plot (2 numeric axes)
  bubble         — bubble chart (x, y, size all numeric)
  pie            — pie chart (use only for ≤6 slices)
  donut          — donut chart (preferred over pie)
  radar          — radar/spider chart (multi-metric entity comparison)
  histogram      — frequency distribution (one numeric field in data as "x")
  box            — box plot (category + numeric: "x" and "y")
  heatmap        — 2D heat map matrix
  treemap        — hierarchical rectangles (label + value + optional parent)
  sunburst       — hierarchical rings
  sankey         — flow diagram (source → target → weight)
  chord          — chord/relationship diagram (bidirectional flows)
  force          — force-directed network graph
  waterfall      — cumulative gain/loss chart
  funnel         — conversion funnel (label + value, ordered)
  parallel       — parallel coordinates (categories list + data)
  radial-bar     — radial/coxcomb bar
  bump           — bump/slope chart (rank changes over time)
  calendar       — calendar heat map (date + value)
  arc-diagram    — arc diagram: nodes on line, curved arcs above; use for airline route connections, partnerships (source, target, optional weight)
  chord-directed — directed chord with asymmetric flows and arrows; for itinerary flows O→connect→D (source, target, weight)
  radial-tree    — radial tidy tree for hierarchies; airline hub trees (id, parent, value, label)
  force-tree     — force-directed hierarchy tree; hub-spoke networks (id, parent, value, label)
  sequence-sunburst — multi-level click-through sunburst; for airline→region→route→FN breakdowns (path as "A/B/C", value)

RULES:
1. ONLY use exact numeric values from the response — NEVER fabricate numbers
2. Each chart reveals a DISTINCT, non-overlapping insight
3. Choose the most informative chart type for each insight:
   - Rankings → bar-horizontal
   - Time trends → line or area
   - Part-of-whole (≤8 slices) → donut or treemap
   - Flows between nodes → sankey or chord
   - Distribution → histogram or box
   - Correlations → scatter or bubble
   - Hierarchy → treemap or sunburst
   - Multi-metric comparison → radar or parallel
4. Use "full" width for: sankey, chord, force, parallel, heatmap, or >10 data points in bar
5. Sort data descending by value (rankings) or chronologically (time series)
6. Generate ALL charts meaningful from the data — no artificial limit
7. Use simple snake_case keys in data objects (no spaces)
8. Tool results (structured rows) should produce richer, more detailed charts
9. CRITICAL — field name integrity: every value you put in "x", "y", "label", "value",
   "source", "target", "weight", "group", "size", "rows", "cols", "val" MUST exactly
   match a key present in every object inside "data". Mismatches produce blank charts.
10. Minimum data: do NOT generate a chart with fewer than 2 data rows (5 for histogram,
    3 for box/bump, 7 for calendar, 2 for sankey/chord/force).
11. All numeric values in data must be real numbers (int or float), never null or empty string.
12. If you are not confident about the exact values for a chart, omit that chart entirely.
13. AIRLINE DATA PATTERNS — apply these chart types when the data warrants:
    - Thru/connecting flights: sankey (origin→connecting city→destination, weight=freq/seats)
    - Itinerary flows: chord-directed (show asymmetric flows between cities)
    - Route networks: arc-diagram (cities as nodes on line, arcs = routes)
    - Hub hierarchies: radial-tree or force-tree (airline→hub→spoke structure)
    - Multi-level breakdowns: sequence-sunburst (drill-down path sequences)
    - Market share by region/airline: treemap or stacked-bar with 2 groupings
    - Schedule frequency patterns: heatmap (day-of-week vs time-of-day)
    - Capacity comparisons: bubble (x=frequency, y=seats, size=load factor)
14. DETAIL MAXIMIZATION: Include as much detail as the data supports. For D3 charts prefer
    data with 5-15 nodes/paths. For Sankey include all intermediate nodes. For trees include
    all hierarchy levels. For sequence-sunburst include at least 3 levels.
15. When tool_results contain flight/schedule data: extract connecting city information for
    sankey; extract O&D pairs for chord-directed; extract airline/route hierarchies for trees.
"""


# ─────────────────────────────────────────────────────────────────────────────
# DEFAULT SUMMARY CHARTS — built directly from workset DB data (no LLM)
# ─────────────────────────────────────────────────────────────────────────────

def generate_default_summary_charts() -> List[Dict]:
    """
    Build 7-8 rich default charts from workset_base + workset_spill data.
    All queries are direct SQL — no LLM involved.
    Every chart is filtered / scoped to the Host airline.
    Returns list of chart specs in the same format as generate_summary_charts().
    """
    import duckdb
    from app.database.db import get_db_path
    db_path = get_db_path()

    def _db() -> duckdb.DuckDBPyConnection:
        return duckdb.connect(database=db_path, read_only=False)

    def _q(sql: str) -> List[Dict]:
        conn = _db()
        try:
            return conn.execute(sql).df().to_dict(orient="records")
        except Exception as exc:
            logger.warning(f"default chart query failed: {exc}")
            return []
        finally:
            try: conn.close()
            except Exception: pass

    # ── Detect host airline ──────────────────────────────────────────────────
    # 1. Try workset_profile.json (most reliable — written by workset_service at load)
    host: str = ""
    try:
        from app.services.workset_service import _get_workset_dir
        _wd = _get_workset_dir()
        _profile_path = _wd / "dashboard_output" / "workset_profile.json"
        if _profile_path.exists():
            import json as _json
            _prof = _json.loads(_profile_path.read_text())
            host = (_prof.get("host_airline") or "").upper().strip()
    except Exception:
        pass

    # 2. Fallback: most-represented airline in workset_base (host dominates the workset)
    if not host:
        fb = _q("SELECT mkt_airline AS h, COUNT(*) AS c FROM workset_base "
                "WHERE mkt_airline IS NOT NULL GROUP BY mkt_airline ORDER BY c DESC LIMIT 1")
        host = (fb[0]["h"] or "").upper().strip() if fb else "HOST"

    # Sanitise — only safe alphanumeric codes (max 4 chars)
    host = "".join(c for c in host if c.isalnum())[:4]
    logger.info(f"generate_default_summary_charts: host={host!r}")

    charts: List[Dict] = []

    # ── 1. SANKEY — Host: Origin → Via-Airline → Destination ────────────────
    try:
        rows = _q(f"""
            WITH top AS (
                SELECT market_origin AS origin, market_dest AS destin, airline,
                       SUM(total_pax) AS pax
                FROM workset_spill
                WHERE total_pax > 0
                  AND market_origin IS NOT NULL AND market_dest IS NOT NULL
                  AND UPPER(TRIM(airline)) = '{host}'
                  AND is_codeshare = 0
                GROUP BY market_origin, market_dest, airline
                ORDER BY pax DESC
                LIMIT 25
            )
            SELECT
                origin                                      AS source,
                COALESCE(airline,'?') || ' (' || origin || ')' AS via,
                destin                                      AS target,
                pax                                         AS value
            FROM top
        """)
        # Build two-stage links: origin→via, via→dest
        links = []
        for r in rows:
            links.append({"source": r["source"], "target": r["via"],  "value": r["value"]})
            links.append({"source": r["via"],    "target": r["target"], "value": r["value"]})
        if len(links) >= 4:
            charts.append({
                "id": "def_sankey", "type": "sankey", "width": "full",
                "title": f"{host} Passenger Flow: Origin → Airline → Destination",
                "subtitle": f"Top 25 O&D markets — {host} workset traffic",
                "source": "source", "target": "target", "weight": "value",
                "data": links,
            })
    except Exception as e:
        logger.warning(f"default sankey failed: {e}")

    # ── 2. CHORD DIRECTED — Host: Airport-to-airport O&D flow matrix ─────────
    try:
        rows = _q(f"""
            WITH top_airports AS (
                SELECT market_origin AS ap FROM workset_spill
                WHERE total_pax > 0 AND is_codeshare = 0
                  AND UPPER(TRIM(airline)) = '{host}'
                GROUP BY market_origin ORDER BY SUM(total_pax) DESC LIMIT 14
            ),
            flows AS (
                SELECT s.market_origin AS origin, s.market_dest AS destin,
                       SUM(s.total_pax) AS flow
                FROM workset_spill s
                JOIN top_airports ta ON s.market_origin = ta.ap
                WHERE s.total_pax > 0 AND s.is_codeshare = 0
                  AND UPPER(TRIM(s.airline)) = '{host}'
                  AND s.market_dest IN (SELECT ap FROM top_airports)
                  AND s.market_origin <> s.market_dest
                GROUP BY s.market_origin, s.market_dest
            )
            SELECT origin, destin, flow FROM flows ORDER BY flow DESC LIMIT 100
        """)
        if len(rows) >= 4:
            charts.append({
                "id": "def_chord", "type": "chord-directed",
                "title": f"{host} Directed O&D Flow Between Top Airports",
                "subtitle": f"Top 14 {host} airports by passenger volume",
                "source": "origin", "target": "destin", "weight": "flow",
                "data": rows,
            })
    except Exception as e:
        logger.warning(f"default chord failed: {e}")

    # ── 3. ICICLE — Host itinerary stop-type breakdown ────────────────────────
    try:
        rows = _q(f"""
            WITH base AS (
                SELECT
                    CASE stops
                        WHEN 0 THEN 'Nonstop'
                        WHEN 1 THEN '1-Stop'
                        ELSE       '2+ Stops'
                    END AS stop_type,
                    market_origin || '-' || market_dest AS market,
                    ROUND(SUM(total_pax)) AS traffic
                FROM workset_spill
                WHERE total_pax > 0 AND is_codeshare = 0
                  AND UPPER(TRIM(airline)) = '{host}'
                GROUP BY stop_type, market_origin, market_dest
            ),
            ranked AS (
                SELECT *,
                       ROW_NUMBER() OVER (PARTITION BY stop_type ORDER BY traffic DESC) AS rn
                FROM base
            )
            SELECT stop_type, market, traffic
            FROM ranked WHERE rn <= 40
            ORDER BY stop_type, traffic DESC
        """)
        totals_rows = _q(f"""
            SELECT
                CASE stops
                    WHEN 0 THEN 'Nonstop'
                    WHEN 1 THEN '1-Stop'
                    ELSE       '2+ Stops'
                END AS stop_type,
                ROUND(SUM(total_pax)) AS traffic
            FROM workset_spill
            WHERE total_pax > 0 AND is_codeshare = 0
              AND UPPER(TRIM(airline)) = '{host}'
            GROUP BY stop_type
        """)
        if len(rows) >= 6:
            stop_totals = {r["stop_type"]: float(r["traffic"] or 0) for r in totals_rows}
            total = sum(stop_totals.values())

            tm_data = [{"label": f"{host} Traffic", "parent": "", "value": round(total)}]
            for st, tot in sorted(stop_totals.items(), key=lambda x: -x[1]):
                tm_data.append({"label": st, "parent": f"{host} Traffic", "value": round(tot)})
            for r in rows:
                tm_data.append({
                    "label": r["market"],
                    "parent": r["stop_type"],
                    "value": int(float(r["traffic"] or 0)),
                })

            charts.append({
                "id": "def_itinerary", "type": "icicle",
                "title": f"{host} Itinerary Path Breakdown",
                "subtitle": f"{host} traffic by stop type → top O&D markets (size = passengers)",
                "label": "label", "parent": "parent", "value": "value",
                "data": tm_data,
            })
    except Exception as e:
        logger.warning(f"default itinerary treemap failed: {e}")

    # ── 4. HEATMAP — Host flight frequency by day-of-week × departure hour ───
    try:
        rows = _q(f"""
            SELECT
                CASE day_of_week
                    WHEN 0 THEN 'Sun' WHEN 1 THEN 'Mon' WHEN 2 THEN 'Tue'
                    WHEN 3 THEN 'Wed' WHEN 4 THEN 'Thu' WHEN 5 THEN 'Fri'
                    ELSE 'Sat' END AS day,
                CAST(SUBSTR(LPAD(CAST(dep_time AS VARCHAR), 4, '0'), 1, 2) AS INTEGER) AS hour,
                COUNT(*) AS freq
            FROM workset_base
            WHERE day_of_week IS NOT NULL AND dep_time IS NOT NULL
              AND mkt_ind <= 1
              AND UPPER(TRIM(mkt_airline)) = '{host}'
            GROUP BY day_of_week, hour
            ORDER BY day_of_week, hour
        """)
        if len(rows) >= 10:
            charts.append({
                "id": "def_heatmap", "type": "heatmap",
                "title": f"{host} Flight Frequency by Day & Departure Hour",
                "subtitle": f"{host} departure density across week — schedule pattern",
                "rows": "day", "cols": "hour", "val": "freq",
                "data": rows,
            })
    except Exception as e:
        logger.warning(f"default heatmap failed: {e}")

    # ── 5. TREEMAP — Seat capacity comparison (host + top competitors) ────────
    try:
        rows = _q(f"""
            SELECT
                mkt_airline AS airline,
                ROUND(SUM(apm_cap)) AS capacity,
                ROUND(SUM(apm_pax)) AS traffic
            FROM workset_base
            WHERE mkt_airline IS NOT NULL AND mkt_ind <= 1
            GROUP BY mkt_airline
            HAVING capacity > 0
            ORDER BY
                CASE WHEN UPPER(TRIM(mkt_airline)) = '{host}' THEN 0 ELSE 1 END,
                capacity DESC
            LIMIT 25
        """)
        if len(rows) >= 3:
            data = [{"label": "All Airlines", "parent": "", "value": sum(r["capacity"] for r in rows)}]
            for r in rows:
                data.append({"label": r["airline"], "parent": "All Airlines", "value": r["capacity"]})
            charts.append({
                "id": "def_treemap", "type": "treemap",
                "title": f"Seat Capacity: {host} vs Competitors",
                "subtitle": f"Total scheduled seats — {host} shown first",
                "label": "label", "parent": "parent", "value": "value",
                "data": data,
            })
    except Exception as e:
        logger.warning(f"default treemap failed: {e}")

    # ── 6. RADIAL TIDY TREE — Host + top competitors: airline → routes ────────
    try:
        rows = _q(f"""
            WITH airline_routes AS (
                SELECT mkt_airline AS airline,
                       origin || '-' || dest AS route,
                       COUNT(*) AS freq
                FROM workset_base
                WHERE mkt_ind <= 1 AND mkt_airline IS NOT NULL
                GROUP BY mkt_airline, origin, dest
            ),
            host_entry AS (
                SELECT airline FROM airline_routes
                WHERE UPPER(TRIM(airline)) = '{host}'
                LIMIT 1
            ),
            top_competitors AS (
                SELECT airline FROM (
                    SELECT airline, SUM(freq) AS f FROM airline_routes
                    WHERE UPPER(TRIM(airline)) != '{host}'
                    GROUP BY airline ORDER BY f DESC LIMIT 9
                ) sub
            ),
            top_airlines AS (
                SELECT airline FROM host_entry
                UNION ALL
                SELECT airline FROM top_competitors
            ),
            ranked AS (
                SELECT ar.*, ROW_NUMBER() OVER (PARTITION BY ar.airline ORDER BY ar.freq DESC) AS rn
                FROM airline_routes ar
                JOIN top_airlines ta ON ar.airline = ta.airline
            )
            SELECT airline, route, freq FROM ranked WHERE rn <= 5
            ORDER BY
                CASE WHEN UPPER(TRIM(airline)) = '{host}' THEN 0 ELSE 1 END,
                airline, freq DESC
        """)
        if len(rows) >= 6:
            tree_data = [{"id": "Network", "parent": "", "label": "All Routes"}]
            airlines_seen = set()
            for r in rows:
                aln = r["airline"]
                if aln not in airlines_seen:
                    tree_data.append({"id": aln, "parent": "Network", "label": aln})
                    airlines_seen.add(aln)
                tree_data.append({"id": f"{aln}-{r['route']}", "parent": aln, "label": r["route"]})
            charts.append({
                "id": "def_radial_tree", "type": "radial-tree", "width": "full",
                "title": f"Airline Route Hierarchy — {host} + Top Competitors",
                "subtitle": f"{host} routes (first branch) vs top 9 competitors",
                "id_field": "id", "parent": "parent", "label": "label",
                "data": tree_data,
            })
    except Exception as e:
        logger.warning(f"default radial tree failed: {e}")

    # ── 7. FORCE TREE — Host airport connectivity network ─────────────────────
    try:
        rows = _q(f"""
            WITH top_airports AS (
                SELECT origin AS ap, COUNT(DISTINCT dest) AS routes
                FROM workset_base
                WHERE mkt_ind <= 1 AND UPPER(TRIM(mkt_airline)) = '{host}'
                GROUP BY origin ORDER BY routes DESC LIMIT 12
            ),
            connections AS (
                SELECT b.origin, b.dest, COUNT(*) AS freq
                FROM workset_base b
                JOIN top_airports ta ON b.origin = ta.ap
                WHERE b.mkt_ind <= 1 AND UPPER(TRIM(b.mkt_airline)) = '{host}'
                  AND b.dest IN (SELECT ap FROM top_airports)
                  AND b.origin <> b.dest
                GROUP BY b.origin, b.dest
                ORDER BY freq DESC
                LIMIT 50
            )
            SELECT origin, dest FROM connections
        """)
        if len(rows) >= 5:
            airports = set()
            for r in rows:
                airports.add(r["origin"]); airports.add(r["dest"])
            from collections import Counter
            hub_counts = Counter()
            for r in rows:
                hub_counts[r["origin"]] += 1
                hub_counts[r["dest"]] += 1
            hub = hub_counts.most_common(1)[0][0]
            tree_data = [{"id": hub, "parent": "", "label": hub}]
            added = {hub}
            for r in rows:
                for ap in [r["origin"], r["dest"]]:
                    if ap not in added:
                        tree_data.append({"id": ap, "parent": hub, "label": ap})
                        added.add(ap)
            charts.append({
                "id": "def_force_tree", "type": "force-tree", "width": "full",
                "title": f"{host} Airport Connectivity Network",
                "subtitle": f"Top 12 {host} airports — route interconnection",
                "id_field": "id", "parent": "parent", "label": "label",
                "data": tree_data,
            })
    except Exception as e:
        logger.warning(f"default force tree failed: {e}")

    # ── 8. BAR — Host top O&D markets by demand ───────────────────────────────
    try:
        rows = _q(f"""
            SELECT
                market_origin || ' → ' || market_dest AS market,
                ROUND(SUM(total_demand)) AS demand,
                ROUND(SUM(total_pax)) AS traffic,
                ROUND(SUM(total_spill)) AS spill
            FROM workset_spill
            WHERE total_demand > 0 AND is_codeshare = 0
              AND UPPER(TRIM(airline)) = '{host}'
            GROUP BY market_origin, market_dest
            ORDER BY demand DESC
            LIMIT 20
        """)
        if len(rows) >= 4:
            charts.append({
                "id": "def_top_markets", "type": "bar",
                "title": f"{host} Top 20 O&D Markets by Demand",
                "subtitle": f"Total weekly demand — {host} workset data",
                "x": "market", "y": "demand",
                "data": rows,
            })
    except Exception as e:
        logger.warning(f"default bar failed: {e}")

    # ── 9. ARC DIAGRAM — Host spill flows ─────────────────────────────────────
    try:
        rows = _q(f"""
            SELECT market_origin AS source, market_dest AS target,
                   SUM(total_spill) AS spill
            FROM workset_spill
            WHERE total_spill > 0 AND is_codeshare = 0
              AND UPPER(TRIM(airline)) = '{host}'
            GROUP BY market_origin, market_dest
            ORDER BY spill DESC
            LIMIT 40
        """)
        if len(rows) >= 6:
            charts.append({
                "id": "def_arc_spill", "type": "arc-diagram",
                "title": f"{host} Passenger Spill Flows Between Markets",
                "subtitle": f"{host} spilled demand arcs — thicker = more spill",
                "source": "source", "target": "target", "weight": "spill",
                "data": rows,
            })
    except Exception as e:
        logger.warning(f"default arc failed: {e}")

    logger.info(f"generate_default_summary_charts: built {len(charts)} charts (host={host!r})")
    return charts


def generate_summary_charts(
    query: str,
    response_text: str,
    tool_results: List[Dict],
) -> List[Dict]:
    """
    Ask the LLM to generate chart specs from a chat response.
    Returns a list of chart spec dicts (may be empty list).
    """
    import json as _json
    from app.ai.vertex_client import generate_content, is_available, extract_text

    if not is_available():
        return []

    # Build tool_results summary for context
    tool_summary_parts = []
    for tr in (tool_results or [])[:6]:
        name = tr.get("function_name") or tr.get("tool") or "tool"
        result = tr.get("result") or {}
        rows = tr.get("rows") or result.get("rows", [])
        cols = tr.get("columns") or result.get("columns", [])
        if rows and cols:
            sample = _json.dumps(rows[:25], default=str)
            tool_summary_parts.append(
                f"\nTool '{name}': {len(rows)} rows, columns={cols}\nSample rows: {sample[:2000]}"
            )

    tool_block = "\n\nSTRUCTURED DATA FROM TOOLS:" + "".join(tool_summary_parts) if tool_summary_parts else ""

    user_prompt = (
        f"USER QUERY: {query[:400]}\n\n"
        f"AI RESPONSE:\n{response_text[:3500]}"
        f"{tool_block}\n\n"
        "Generate chart specifications for ALL quantitative insights above.\n"
        "Output ONLY a valid JSON array. Use [] if no meaningful numeric data."
    )

    try:
        resp = generate_content(
            contents=[{"role": "user", "parts": [{"text": user_prompt}]}],
            system_instruction=_SUMMARY_CHARTS_SYSTEM,
            temperature=0.0,
        )
        raw = extract_text(resp)
        if not raw:
            return []

        raw = raw.strip()
        # Strip markdown code fences
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip().rstrip("`").strip()

        # Find JSON array bounds
        start = raw.find("[")
        end = raw.rfind("]")
        if start == -1 or end == -1:
            return []
        raw = raw[start:end + 1]

        specs = _json.loads(raw)
        if not isinstance(specs, list):
            return []

        # Validate and assign IDs — enforce field-mapping integrity
        _CHART_REQUIRED = {
            'bar': ('x', 'y'), 'bar-horizontal': ('x', 'y'),
            'bar-grouped': ('x', 'y'), 'bar-stacked': ('x', 'y'),
            'line': ('x', 'y'), 'area': ('x', 'y'),
            'area-stacked': ('x', 'y'), 'streamgraph': ('x', 'y'),
            'scatter': ('x', 'y'), 'bubble': ('x', 'y', 'size'),
            'pie': ('label', 'value'), 'donut': ('label', 'value'),
            'radar': ('label',), 'histogram': ('x',), 'box': ('x', 'y'),
            'heatmap': ('rows', 'cols', 'val'),
            'treemap': ('label', 'value'), 'sunburst': ('label', 'value'),
            'sankey': ('source', 'target'), 'chord': ('source', 'target'),
            'force': ('source', 'target'),
            'waterfall': ('x', 'y'), 'funnel': ('label', 'value'),
            'radial-bar': ('label', 'value'), 'bump': ('x', 'y', 'group'),
            'calendar': ('rows', 'val'),
            'arc-diagram': ('source', 'target'),
            'chord-directed': ('source', 'target', 'weight'),
            'radial-tree': ('id', 'parent'),
            'force-tree': ('id', 'parent'),
            'sequence-sunburst': ('path', 'value'),
        }
        _CHART_MIN_ROWS = {
            'histogram': 4, 'box': 3, 'chord': 2, 'sankey': 2,
            'force': 2, 'bump': 3, 'calendar': 7,
            'arc-diagram': 2, 'chord-directed': 2,
            'radial-tree': 3, 'force-tree': 3, 'sequence-sunburst': 3,
        }
        valid = []
        for i, s in enumerate(specs):
            if not isinstance(s, dict):
                continue
            chart_type = str(s.get("type", "")).lower()
            if not chart_type:
                continue
            data = s.get("data")
            if not isinstance(data, list) or len(data) == 0:
                continue
            # Minimum rows check
            if len(data) < _CHART_MIN_ROWS.get(chart_type, 1):
                continue
            # Collect actual keys present across data rows
            data_keys: set = set()
            for row in data:
                if isinstance(row, dict):
                    data_keys.update(row.keys())
            if not data_keys:
                continue
            # Validate required field mappings
            required = _CHART_REQUIRED.get(chart_type, ())
            bad = False
            for field_key in required:
                mapped_col = s.get(field_key)
                if not mapped_col:
                    bad = True; break
                if mapped_col not in data_keys:
                    # Try case-insensitive match
                    match = next((k for k in data_keys
                                  if k.lower() == str(mapped_col).lower()), None)
                    if match:
                        s[field_key] = match
                    else:
                        bad = True; break
            if bad:
                continue
            if not s.get("id"):
                s["id"] = f"c{i + 1}"
            valid.append(s)

        # AI final look: curate and reorder by visual impact
        if len(valid) > 1:
            valid = _curate_summary_specs(valid, query)

        return valid

    except Exception as exc:
        logger.warning(f"generate_summary_charts error: {exc}")
        return []


def _curate_summary_specs(specs: List[Dict], query: str) -> List[Dict]:
    """
    Second LLM pass: review validated chart specs, remove redundant/low-value charts,
    and return them ordered by visual impact. If curation fails, returns specs unchanged.
    """
    import json as _json
    from app.ai.vertex_client import generate_content, is_available, extract_text

    if not is_available() or not specs:
        return specs

    # Only send id + type + title to keep the prompt tiny
    slim = [{"id": s.get("id"), "type": s.get("type"), "title": s.get("title", "")} for s in specs]

    prompt = (
        f'USER QUERY: "{query[:300]}"\n\n'
        f"CHART CANDIDATES (already validated, data is real):\n{_json.dumps(slim, indent=2)}\n\n"
        "Your task:\n"
        "1. Remove charts that are REDUNDANT (same insight shown twice in different forms).\n"
        "2. Remove charts that are NOT MEANINGFULLY INFORMATIVE for the query.\n"
        "3. Order remaining charts from HIGHEST to LOWEST visual impact.\n"
        "4. Keep ALL charts that reveal distinct, useful insights — do NOT cap the count.\n\n"
        "Output ONLY a JSON array of the IDs you want to KEEP, in order. Example: [\"c1\",\"c3\",\"c2\"]\n"
        "Return ALL IDs if everything is distinct and informative."
    )
    try:
        resp = generate_content(
            contents=[{"role": "user", "parts": [{"text": prompt}]}],
            system_instruction="You are a senior data-visualization curator. Be concise and decisive.",
            temperature=0.0,
        )
        raw = extract_text(resp) or ""
        raw = raw.strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"): raw = raw[4:]
        raw = raw.strip().rstrip("`").strip()
        start, end = raw.find("["), raw.rfind("]")
        if start == -1 or end == -1:
            return specs
        kept_ids = _json.loads(raw[start:end + 1])
        if not isinstance(kept_ids, list) or not kept_ids:
            return specs
        # Reorder specs to match curator order; include any that weren't mentioned
        id_map = {s.get("id"): s for s in specs}
        curated = [id_map[cid] for cid in kept_ids if cid in id_map]
        # Append any spec not mentioned by curator (safety net)
        mentioned = set(kept_ids)
        curated += [s for s in specs if s.get("id") not in mentioned]
        return curated
    except Exception as exc:
        logger.warning(f"_curate_summary_specs error (returning original): {exc}")
        return specs


# ─────────────────────────────────────────────────────────────────────────────
# Summary Chat
# ─────────────────────────────────────────────────────────────────────────────

_SUMMARY_CHAT_SYSTEM = """\
You are an expert data visualization assistant for an airline intelligence dashboard.
The user is viewing a set of charts in the Summary tab and wants to modify them via chat.

You can:
1. ADD a new chart  — action="add", put full spec in "charts" array
2. REPLACE a chart  — action="replace", put full updated spec (same id) in "charts"
3. REMOVE a chart   — action="remove", put its id in "remove_ids"
4. MODIFY a chart   — action="modify", put full updated spec (same id) in "charts"
5. ANSWER a question — action="answer", leave "charts" and "remove_ids" empty

OUTPUT FORMAT — always return a single valid JSON object:
{
  "reply": "One-sentence explanation of what changed (or why nothing changed)",
  "action": "add"|"replace"|"remove"|"modify"|"answer",
  "charts": [...],
  "remove_ids": [...]
}

CRITICAL RULES:
1. When action is add/replace/modify: the "charts" array MUST contain COMPLETE chart specs,
   including the FULL "data" array. Copy the existing chart's data verbatim and apply only
   the changes the user requested. Never return charts with an empty "data" array.
2. NEVER fabricate numeric values not present in the current chart data or original context.
3. If the requested information (e.g. competitor data) does not exist in any current chart
   or the original response context, set action="answer" and explain what's missing — do NOT
   pretend to have modified a chart.
4. When changing chart type: copy all existing data, change "type" and adjust field mappings.
5. When user says "remove/delete X": put its id in remove_ids, leave "charts" empty.
6. Field mappings (x, y, label, value, source, target, etc.) must exactly match keys in data.
7. Respond with ONLY the JSON object — no markdown fences, no extra text.
"""


def summary_chat(
    message: str,
    current_charts: List[Dict],
    context_query: str,
    context_response: str,
) -> Dict:
    """
    Chat interface for modifying Summary tab charts.
    Returns {reply, action, charts (new/modified specs), remove_ids}.
    """
    import json as _json
    from app.ai.vertex_client import generate_content, is_available, extract_text

    if not is_available():
        return {"reply": "AI is not available.", "action": "answer", "charts": [], "remove_ids": []}

    # Send full chart specs — the LLM must echo back complete data arrays when modifying
    charts_json = _json.dumps(current_charts, default=str)
    # Cap total prompt at ~12k chars, prioritising chart data over response text
    ctx_response_trunc = context_response[:600] if len(charts_json) > 8000 else context_response[:1200]
    user_prompt = (
        f"ORIGINAL QUERY: {context_query[:300]}\n\n"
        f"ORIGINAL RESPONSE SUMMARY: {ctx_response_trunc}\n\n"
        f"CURRENT CHARTS (complete specs with data):\n{charts_json}\n\n"
        f"USER REQUEST: {message}"
    )

    try:
        resp = generate_content(
            contents=[{"role": "user", "parts": [{"text": user_prompt}]}],
            system_instruction=_SUMMARY_CHAT_SYSTEM,
            temperature=0.1,
        )
        raw = extract_text(resp)
        if not raw:
            return {"reply": "No response from AI.", "action": "answer", "charts": [], "remove_ids": []}

        raw = raw.strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip().rstrip("`").strip()

        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1:
            return {"reply": raw[:200], "action": "answer", "charts": [], "remove_ids": []}

        result = _json.loads(raw[start:end + 1])
        return {
            "reply": result.get("reply", "Done."),
            "action": result.get("action", "answer"),
            "charts": result.get("charts", []) if isinstance(result.get("charts"), list) else [],
            "remove_ids": result.get("remove_ids", []) if isinstance(result.get("remove_ids"), list) else [],
        }

    except Exception as exc:
        logger.warning(f"summary_chat error: {exc}")
        return {"reply": f"Error: {exc}", "action": "answer", "charts": [], "remove_ids": []}


def add_context_to_response(
    query: str,
    response: str,
    contexts: List[Dict],
) -> str:
    """
    Regenerate an assistant response incorporating user-added context items.
    contexts: [{type: str, instruction: str}, ...]

    Strategy:
      - If any context requests new entities (airlines, routes, competitors) that
        need fresh DB data, build an augmented query and re-run through the agent.
      - Otherwise, synthesise additions with a strong LLM prompt.
    Returns the regenerated response text.
    """
    from app.ai.vertex_client import generate_content, is_available, extract_text

    if not contexts:
        return response

    # ── Detect if re-querying gives better results ───────────────────────────
    # "Add X competitor", "include Y airline", "show Z route" → real DB data needed
    _NEW_ENTITY_PATTERNS = [
        r'\badd\s+([A-Z]{2,3})\b',          # "add BA", "add UA"
        r'\binclude\s+([A-Z]{2,3})\b',
        r'\bshow\s+([A-Z]{2,3})\b',
        r'\bcompetitor\b',
        r'\badd\s+\w+\s+airline\b',
        r'\badd\s+\w+\s+carrier\b',
        r'\badd\s+route\b',
    ]
    import re as _re
    needs_requery = any(
        _re.search(pat, c.get('instruction', ''), _re.IGNORECASE)
        for c in contexts
        for pat in _NEW_ENTITY_PATTERNS
    )

    if needs_requery and is_available():
        # Build an augmented query combining original + context instructions
        additions = '; '.join(c.get('instruction', '') for c in contexts)
        augmented_query = (
            f"{query}. Additionally: {additions}. "
            "Include all entities and data explicitly requested above."
        )
        try:
            from app.api.routes import _agent
            result = _agent.query(augmented_query)
            raw = result.get('answer', '')
            if raw and len(raw) > 100:
                return raw.replace('[NAV:', '[_NAV:').strip()  # strip nav markers
        except Exception as exc:
            logger.warning(f"add_context re-query failed: {exc}")
            # fall through to LLM synthesis below

    if not is_available():
        return response

    ctx_lines = '\n'.join(
        f"  [{c.get('type', 'general')}] {c.get('instruction', '')}"
        for c in contexts
    )

    system = """\
You are an expert airline intelligence assistant. You will regenerate a response with \
user-requested additions using BOTH the information already in the response AND your \
airline industry domain knowledge.

RULES — you MUST follow ALL of them:
1. Preserve the original markdown structure (headings, tables, bullets, bold).
2. Extend tables with new rows/columns as requested — populate every cell with real \
   airline data you know (routes, frequencies, seat counts, hubs, IATA codes). Never \
   leave a cell empty or write "N/A" when you can infer the value.
3. When asked to add a competitor airline, add its relevant routes/data in the same \
   format as existing entries — use your training knowledge of published schedules.
4. Add new textual sections at the most logical position; do not remove original content.
5. Do NOT hedge with phrases like "I don't have exact data" — synthesise from domain \
   knowledge if needed, and clearly note when a figure is an estimate.
6. Output ONLY the regenerated response — no preamble, no wrapper sentence.
"""

    user_prompt = (
        f"ORIGINAL QUERY:\n{query}\n\n"
        f"PREVIOUS RESPONSE:\n{response}\n\n"
        f"ADDITIONS TO INCORPORATE (you MUST include ALL of these):\n{ctx_lines}\n\n"
        "Regenerated response (complete, with all additions):"
    )

    try:
        resp = generate_content(
            contents=[{"role": "user", "parts": [{"text": user_prompt}]}],
            system_instruction=system,
            temperature=0.2,
        )
        regen = extract_text(resp)
        return regen.strip() if regen else response
    except Exception as exc:
        logger.warning(f"add_context_to_response error: {exc}")
        return response
