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
    has_pct = any(h in c for c in cl for h in ("pct","share","percent"))
    time_cols = {"hour","dep_hour","arr_hour","time_bucket","day_of_week","dow","day","dep_hour"}
    has_time = any(c in cl for c in time_cols)

    num_cols = [c for c in columns if _col_is_numeric(rows, c)]
    non_num  = [c for c in columns if not _col_is_numeric(rows, c)]

    if has_time and len(columns) >= 2:
        return "bar"
    if len(non_num) >= 1 and len(num_cols) >= 1:
        if has_pct and len(rows) <= 14:
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

def _sql_vizs(result: Dict) -> List[Dict]:
    columns = result.get("columns") or []
    rows    = result.get("rows") or []
    row_count = result.get("row_count", len(rows))
    if not columns or not rows:
        return []

    vizs: List[Dict] = []
    vizs.append({
        "type": "table",
        "title": f"Query Results ({row_count} rows)",
        "columns": columns,
        "rows": rows[:200],
        "row_count": row_count,
    })

    ct = _detect_chart_type(columns, rows)
    label_col, value_col = _find_label_value_cols(columns, rows)

    if ct in ("bar","horizontal_bar") and label_col and value_col:
        data = _bar_data(rows, label_col, value_col)
        if data["labels"]:
            title = f"{value_col.replace('_',' ').title()} by {label_col.replace('_',' ').title()}"
            vizs.append({"type": ct, "title": title, "label_col": label_col, "value_col": value_col, "data": data})
    elif ct == "pie" and label_col and value_col:
        slices = _pie_slices(rows, label_col, value_col)
        if slices:
            vizs.append({"type": "pie", "title": f"{value_col.replace('_',' ').title()} Distribution", "slices": slices})

    return vizs


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

    # De-duplicate by title
    seen: set = set()
    out: List[Dict] = []
    for v in all_vizs:
        t = v.get("title","")
        if t not in seen:
            seen.add(t)
            out.append(v)

    # Sort: kpi_row → local_flow_split → table → charts
    order = {"kpi_row": 0, "local_flow_split": 1, "table": 2, "bar": 3, "horizontal_bar": 4, "pie": 5}
    out.sort(key=lambda v: order.get(v.get("type",""), 6))
    return out
