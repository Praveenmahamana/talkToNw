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

