"""
Normalise raw schedule DataFrames into the canonical flights schema.

The normaliser:
  1. Detects column aliases (DEP, ARR, ORG, DST, …)
  2. Coerces types
  3. Derives UTC timestamps from local time + UTC offset or IANA timezone lookup
  4. Generates a stable row ID
  5. Returns a clean DataFrame ready for database insertion
"""

import hashlib
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import pandas as pd
from loguru import logger

from app.utils.time_utils import (
    parse_time, parse_date, local_to_utc, get_airport_timezone,
    calculate_block_time_minutes, is_overnight_flight, frequency_to_days,
)

# ─────────────────────────────────────────────────────────────────────────────
# Column alias maps  (lowercase → canonical name)
# ─────────────────────────────────────────────────────────────────────────────

_ALIASES: Dict[str, str] = {
    # airline / carrier
    "airline":          "airline",  "carrier":      "airline",
    "al":               "airline",  "iata_carrier": "airline",
    # flight number
    "flight_number":    "flight_number", "flt":      "flight_number",
    "flight_no":        "flight_number", "flightno": "flight_number",
    "flt_no":           "flight_number", "flt_num":  "flight_number",
    "flight":           "flight_number",
    # origin
    "origin":           "origin",   "org":          "origin",
    "dep":              "origin",   "dep_station":  "origin",
    "from":             "origin",   "departure_airport": "origin",
    "board_point":      "origin",
    # destination
    "destination":      "destination", "dst":        "destination",
    "dest":             "destination", "arr":        "destination",
    "arr_station":      "destination", "to":         "destination",
    "arrival_airport":  "destination", "off_point":  "destination",
    # departure time
    "departure_local":  "departure_local", "dep_time": "departure_local",
    "std":              "departure_local",  "etd":      "departure_local",
    "departure_time":   "departure_local",  "sched_dep": "departure_local",
    # arrival time
    "arrival_local":    "arrival_local",   "arr_time": "arrival_local",
    "sta":              "arrival_local",    "eta":      "arrival_local",
    "arrival_time":     "arrival_local",    "sched_arr": "arrival_local",
    # aircraft
    "aircraft_type":    "aircraft_type",   "ac_type":  "aircraft_type",
    "aircraft":         "aircraft_type",   "equip":    "aircraft_type",
    "equipment":        "aircraft_type",   "acft":     "aircraft_type",
    # block time
    "block_time":       "block_time",      "blk":      "block_time",
    "elapsed":          "block_time",      "duration": "block_time",
    "block_hours":      "block_time",
    # frequency / DOW
    "frequency":        "frequency",       "freq":     "frequency",
    "days_of_operation":"frequency",       "days":     "frequency",
    "day_of_week":      "frequency",
    # effective dates
    "effective_from":   "effective_from",  "eff_from": "effective_from",
    "start_date":       "effective_from",  "valid_from": "effective_from",
    "period_from":      "effective_from",
    "effective_to":     "effective_to",    "eff_to":   "effective_to",
    "end_date":         "effective_to",    "valid_to": "effective_to",
    "period_to":        "effective_to",
    # utc offsets (from SSIM loader)
    "utc_offset_dep":   "utc_offset_dep",
    "utc_offset_arr":   "utc_offset_arr",
    # SSIM enrichment fields
    "service_type":     "service_type",
    "terminal_dep":     "terminal_dep",
    "terminal_arr":     "terminal_arr",
}

REQUIRED_CANONICAL = {"origin", "destination", "departure_local"}

# ─────────────────────────────────────────────────────────────────────────────
# Column detection
# ─────────────────────────────────────────────────────────────────────────────

def _detect_columns(df: pd.DataFrame) -> Dict[str, str]:
    """
    Map DataFrame columns → canonical names using the alias dictionary.
    Returns {canonical_name: actual_col_name}.
    """
    mapping: Dict[str, str] = {}
    for col in df.columns:
        key = col.strip().lower().replace(" ", "_").replace("-", "_")
        canonical = _ALIASES.get(key)
        if canonical and canonical not in mapping:
            mapping[canonical] = col
    return mapping


# ─────────────────────────────────────────────────────────────────────────────
# Row-level helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe(row: pd.Series, col: Optional[str]) -> str:
    if col is None or col not in row:
        return ""
    val = row[col]
    if pd.isna(val):
        return ""
    return str(val).strip()


def _make_id(airline: str, flight_num: str, origin: str, dest: str, dep_str: str, day: int) -> str:
    key = f"{airline}_{flight_num}_{origin}_{dest}_{dep_str}_{day}"
    return hashlib.md5(key.encode()).hexdigest()[:16]


def _parse_block_time(raw: str) -> Optional[int]:
    """Parse block time: accept minutes (int), or HH:MM string."""
    raw = raw.strip()
    if not raw:
        return None
    if ":" in raw:
        parts = raw.split(":")
        try:
            return int(parts[0]) * 60 + int(parts[1])
        except ValueError:
            return None
    try:
        val = float(raw)
        # Heuristic: if < 20 it is likely in hours, not minutes
        return int(val * 60) if val < 20 else int(val)
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Main normaliser
# ─────────────────────────────────────────────────────────────────────────────

def normalise(raw_df: pd.DataFrame) -> Tuple[pd.DataFrame, List[str]]:
    """
    Convert a raw schedule DataFrame into the canonical flights schema.

    Returns:
        normalised_df  – rows conforming to the flights table schema
        skipped_rows   – list of human-readable skip reasons
    """
    if raw_df.empty:
        return pd.DataFrame(), []

    col_map = _detect_columns(raw_df)
    skipped: List[str] = []
    rows_out: List[Dict] = []

    missing_req = REQUIRED_CANONICAL - col_map.keys()
    if missing_req:
        msg = f"Required columns missing from source: {missing_req}. Skipping entire file."
        logger.warning(msg)
        return pd.DataFrame(), [msg]

    logger.info(f"Column mapping detected: {col_map}")

    # Reference date for time-only rows (today is fine for schedule data)
    ref_date = datetime.utcnow().date()

    for idx, row in raw_df.iterrows():
        origin  = _safe(row, col_map.get("origin")).upper()
        dest    = _safe(row, col_map.get("destination")).upper()
        dep_raw = _safe(row, col_map.get("departure_local"))
        arr_raw = _safe(row, col_map.get("arrival_local"))

        if not origin or not dest:
            skipped.append(f"Row {idx}: blank origin or destination.")
            continue
        if not dep_raw:
            skipped.append(f"Row {idx} ({origin}-{dest}): blank departure time.")
            continue

        # ── Airline / flight number ──────────────────────────────────────────
        airline_raw = _safe(row, col_map.get("airline")).upper()
        flight_num  = _safe(row, col_map.get("flight_number")).upper()

        # Extract two-letter airline prefix from flight number if not explicit
        if not airline_raw and flight_num:
            airline_raw = re.sub(r'\d', '', flight_num)[:2]

        # ── Effective dates ──────────────────────────────────────────────────
        eff_from_raw = _safe(row, col_map.get("effective_from"))
        eff_to_raw   = _safe(row, col_map.get("effective_to"))
        eff_from = parse_date(eff_from_raw) if eff_from_raw else ref_date
        eff_to   = parse_date(eff_to_raw)   if eff_to_raw   else ref_date

        # ── Frequency / DOW ──────────────────────────────────────────────────
        freq_raw = _safe(row, col_map.get("frequency"))
        if not freq_raw:
            freq_raw = "1234567"  # assume daily if unspecified

        days = frequency_to_days(freq_raw)
        if not days:
            days = list(range(1, 8))

        # ── Parse departure / arrival times ──────────────────────────────────
        dep_time = parse_time(dep_raw)
        arr_time = parse_time(arr_raw) if arr_raw else None

        if dep_time is None:
            skipped.append(f"Row {idx} ({origin}-{dest}): unparseable dep time '{dep_raw}'.")
            continue

        # ── Block time ───────────────────────────────────────────────────────
        blk_raw   = _safe(row, col_map.get("block_time"))
        block_min = _parse_block_time(blk_raw) if blk_raw else None
        if block_min is None and arr_time is not None:
            base = datetime.combine(ref_date, dep_time)
            arr_dt = datetime.combine(ref_date, arr_time)
            if arr_dt < base:
                arr_dt += timedelta(days=1)
            block_min = int((arr_dt - base).total_seconds() / 60)

        # ── UTC offsets (SSIM files carry them explicitly) ───────────────────
        utc_off_dep = 0
        utc_off_arr = 0
        if "utc_offset_dep" in col_map:
            try:
                utc_off_dep = int(_safe(row, col_map["utc_offset_dep"]) or 0)
            except ValueError:
                pass
        if "utc_offset_arr" in col_map:
            try:
                utc_off_arr = int(_safe(row, col_map["utc_offset_arr"]) or 0)
            except ValueError:
                pass

        # ── Expand by day-of-operation ───────────────────────────────────────
        for dow in days:
            dep_local_dt = datetime.combine(ref_date, dep_time)
            arr_local_dt = (
                datetime.combine(ref_date, arr_time)
                + (timedelta(days=1) if arr_time and arr_time < dep_time else timedelta())
                if arr_time else None
            )

            # Derive UTC datetimes
            dep_utc_dt: Optional[datetime] = None
            arr_utc_dt: Optional[datetime] = None

            if utc_off_dep:
                dep_utc_dt = dep_local_dt - timedelta(minutes=utc_off_dep)
            else:
                tz_dep = get_airport_timezone(origin)
                if tz_dep:
                    dep_utc_dt = local_to_utc(dep_local_dt, tz_dep)

            if arr_local_dt:
                if utc_off_arr:
                    arr_utc_dt = arr_local_dt - timedelta(minutes=utc_off_arr)
                else:
                    tz_arr = get_airport_timezone(dest)
                    if tz_arr:
                        arr_utc_dt = local_to_utc(arr_local_dt, tz_arr)

            row_id = _make_id(airline_raw, flight_num, origin, dest, dep_raw, dow)

            rows_out.append({
                "id":               row_id,
                "airline":          airline_raw,
                "flight_number":    flight_num,
                "origin":           origin,
                "destination":      dest,
                "departure_local":  dep_local_dt,
                "arrival_local":    arr_local_dt,
                "departure_utc":    dep_utc_dt,
                "arrival_utc":      arr_utc_dt,
                "day_of_operation": dow,
                "aircraft_type":    _safe(row, col_map.get("aircraft_type")).upper() or None,
                "block_time":       block_min,
                "frequency":        freq_raw,
                "effective_from":   eff_from,
                "effective_to":     eff_to,
                "service_type":     _safe(row, col_map.get("service_type")) or "J",
                "terminal_dep":     _safe(row, col_map.get("terminal_dep")) or None,
                "terminal_arr":     _safe(row, col_map.get("terminal_arr")) or None,
                "source_file":      _safe(row, "source_file") if "source_file" in row.index else "",
                "load_timestamp":   datetime.utcnow(),
            })

    result_df = pd.DataFrame(rows_out) if rows_out else pd.DataFrame()
    logger.info(
        f"Normalised {len(result_df)} rows from {len(raw_df)} raw rows. "
        f"Skipped: {len(skipped)}."
    )
    return result_df, skipped


import re  # noqa: E402 — ensure re is available for the regex above
