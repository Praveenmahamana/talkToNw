"""
Load airline schedule files from a directory.

Supported formats:
  • CSV  (.csv)
  • TSV / fixed-width text (.txt, .dat)
  • SSIM  (.ssim)  — IATA Standard Schedules Information Manual, Type-3 records
"""

import re
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
import pandas as pd
from loguru import logger

# ─────────────────────────────────────────────────────────────────────────────
# SSIM fixed-width field positions for Type-3 (flight leg) records
# Reference: IATA SSIM Chapter 2
# ─────────────────────────────────────────────────────────────────────────────
SSIM3_FIELDS = {
    "record_type":          (0,  1),
    "operational_suffix":   (1,  2),
    "airline":              (2,  5),
    "flight_number":        (5,  9),
    "itinerary_variation":  (9, 11),
    "leg_sequence":         (11, 13),
    "service_type":         (13, 14),
    "period_from":          (14, 21),   # DDMMMYY
    "period_to":            (21, 28),   # DDMMMYY
    "days_of_operation":    (28, 35),
    "frequency_rate":       (35, 36),
    "dep_station":          (36, 39),
    "dep_time_passenger":   (39, 43),
    "dep_time_aircraft":    (43, 47),
    "utc_variation_dep":    (47, 52),
    # col 53 (index 52): PAX meal code at dep — skipped
    "terminal_dep":         (53, 54),   # PAX terminal at dep station
    "arr_station":          (54, 57),
    "arr_time_passenger":   (57, 61),
    "arr_time_aircraft":    (61, 65),
    "utc_variation_arr":    (65, 70),
    # col 71 (index 70): PAX meal code at arr — skipped
    "terminal_arr":         (71, 72),   # PAX terminal at arr station
    "aircraft_type":        (72, 75),
}

SSIM_MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4,
    "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8,
    "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _parse_ssim_date(s: str) -> Optional[str]:
    """Convert DDMMMYY (e.g. 01JAN24) → YYYY-MM-DD string."""
    s = s.strip()
    if len(s) < 7:
        return None
    try:
        day = int(s[0:2])
        mon = SSIM_MONTH_MAP.get(s[2:5].upper())
        yr  = int(s[5:7])
        year = 2000 + yr if yr < 80 else 1900 + yr
        if mon:
            return f"{year:04d}-{mon:02d}-{day:02d}"
    except (ValueError, IndexError):
        pass
    return None


def _parse_utc_offset(s: str) -> int:
    """
    Parse a UTC offset like +0530 or -0100 to total signed minutes.
    Returns 0 if unparseable.
    """
    s = s.strip()
    if not s or s[0] not in ("+", "-"):
        return 0
    try:
        sign = 1 if s[0] == "+" else -1
        hours = int(s[1:3])
        mins  = int(s[3:5]) if len(s) >= 5 else 0
        return sign * (hours * 60 + mins)
    except (ValueError, IndexError):
        return 0


def load_ssim_file(path: Path) -> Tuple[pd.DataFrame, List[str]]:
    """
    Parse an SSIM file and return (DataFrame of raw fields, list of warning strings).
    Each row corresponds to one Type-3 (flight leg) record.
    """
    records: List[Dict[str, Any]] = []
    warnings: List[str] = []

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.rstrip("\n")
            if len(line) < 2:
                continue
            rec_type = line[0]
            if rec_type != "3":
                continue
            if len(line) < 72:
                warnings.append(f"Line {lineno}: too short ({len(line)} chars), skipping.")
                continue

            def _field(key: str) -> str:
                s, e = SSIM3_FIELDS[key]
                return line[s:e].strip()

            airline_raw      = _field("airline").strip()
            flight_num_raw   = _field("flight_number").strip()
            aircraft_raw     = _field("aircraft_type").strip()
            dep_station      = _field("dep_station").strip()
            arr_station      = _field("arr_station").strip()
            dep_time         = _field("dep_time_passenger").strip() or _field("dep_time_aircraft").strip()
            arr_time         = _field("arr_time_passenger").strip() or _field("arr_time_aircraft").strip()
            utc_var_dep      = _field("utc_variation_dep").strip()
            utc_var_arr      = _field("utc_variation_arr").strip()
            period_from      = _parse_ssim_date(_field("period_from"))
            period_to        = _parse_ssim_date(_field("period_to"))
            days_of_op       = _field("days_of_operation").strip()
            service_type     = _field("service_type").strip()
            terminal_dep     = _field("terminal_dep").strip() if len(line) > 53 else ""
            terminal_arr     = _field("terminal_arr").strip() if len(line) > 71 else ""

            if not dep_station or not arr_station:
                warnings.append(f"Line {lineno}: missing origin/destination, skipping.")
                continue

            records.append({
                "airline":           airline_raw,
                "flight_number":     (airline_raw + flight_num_raw).strip(),
                "origin":            dep_station,
                "destination":       arr_station,
                "departure_local":   dep_time[:4] if dep_time else "",
                "arrival_local":     arr_time[:4] if arr_time else "",
                "utc_offset_dep":    _parse_utc_offset(utc_var_dep),
                "utc_offset_arr":    _parse_utc_offset(utc_var_arr),
                "aircraft_type":     aircraft_raw,
                "frequency":         days_of_op.replace(" ", ""),
                "effective_from":    period_from or "",
                "effective_to":      period_to or "",
                "day_of_operation":  None,
                "service_type":      service_type,
                "terminal_dep":      terminal_dep,
                "terminal_arr":      terminal_arr,
                "source_file":       path.name,
            })

    df = pd.DataFrame(records) if records else pd.DataFrame()
    logger.info(f"SSIM {path.name}: {len(records)} leg records, {len(warnings)} warnings.")
    return df, warnings


def _parse_skd_utc_offset(s: str) -> int:
    """
    Parse Sabre SKD UTC offset string to signed minutes.
    Format: -HMM or +HMM  (e.g. -500 = -5:00 = -300 min, +530 = +5:30 = +330 min)
    """
    s = s.strip()
    if not s or s == "0":
        return 0
    sign = 1 if s[0] == "+" else -1
    num  = s.lstrip("+-")
    if not num:
        return 0
    if len(num) <= 2:
        return sign * int(num) * 60
    mins  = int(num[-2:])
    hours = int(num[:-2])
    return sign * (hours * 60 + mins)


def _skd_days_to_freq(days_str: str) -> str:
    """
    Convert SKD 7-char binary days string to IATA frequency string.
    Position 0 = Monday ... Position 6 = Sunday.
    e.g. '1010100' → '135'  (Mon/Wed/Fri)
         '0000001' → '7'    (Sunday only)
         '1111111' → '1234567'
    """
    return "".join(str(i + 1) for i, c in enumerate(days_str) if c == "1")


def load_skd_file(path: Path) -> Tuple[pd.DataFrame, List[str]]:
    """
    Parse a Sabre SKD schedule file (.out format) and return
    (DataFrame of raw fields, list of warning strings).

    Field layout (space-delimited, fields 0-indexed after split()):
      0  origin           IATA origin airport
      1  destination      IATA destination airport
      2  airline          IATA 2-letter airline code
      3  flight_number    flight number (up to 4 digits)
      4  effective_from   DDMMMYY
      5  effective_to     DDMMMYY
      6  days_of_op       7-char binary (pos 0=Mon … pos 6=Sun)
      7  dep_time_pax     HHMM
      8  dep_time_ac      HHMM
      9  utc_var_dep      ±HMM (hours + 2-digit minutes)
      10 flag1
      11 arr_time_pax     HHMM
      12 arr_time_ac      HHMM
      13 utc_var_arr      ±HMM
      14 flag2
      15 leg_sequence
      16 aircraft_type    IATA/ICAO code
      17 airline2         (repeated)
      18 stops
      19 itinerary_var
      20 codeshare_flags
      21 codeshare_airline
      22 codeshare_flight
      23 unknown
      24 service_type     J=Scheduled, G=NonRev, C=Charter, S/U/Q=Other
      ...rest unused
    """
    records: List[Dict[str, Any]] = []
    warnings: List[str] = []
    skipped = 0

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for lineno, raw in enumerate(fh, 1):
            parts = raw.split()
            if len(parts) < 17:
                skipped += 1
                continue

            origin      = parts[0].strip().upper()
            destination = parts[1].strip().upper()
            airline     = parts[2].strip().upper()
            flt_num     = parts[3].strip()
            eff_from    = _parse_ssim_date(parts[4])
            eff_to      = _parse_ssim_date(parts[5])
            days_raw    = parts[6].strip()
            dep_time    = parts[7].strip()    # prefer pax time
            arr_time    = parts[11].strip()   # prefer pax time
            utc_dep     = _parse_skd_utc_offset(parts[9])
            utc_arr     = _parse_skd_utc_offset(parts[13])
            aircraft    = parts[16].strip().upper()
            service_type = parts[24].strip() if len(parts) > 24 else "J"

            if not origin or not destination or len(origin) != 3 or len(destination) != 3:
                skipped += 1
                continue

            frequency = _skd_days_to_freq(days_raw)
            if not frequency:
                skipped += 1
                continue

            records.append({
                "airline":         airline,
                "flight_number":   airline + flt_num,
                "origin":          origin,
                "destination":     destination,
                "departure_local": dep_time[:4] if dep_time else "",
                "arrival_local":   arr_time[:4] if arr_time else "",
                "utc_offset_dep":  utc_dep,
                "utc_offset_arr":  utc_arr,
                "aircraft_type":   aircraft,
                "frequency":       frequency,
                "effective_from":  eff_from or "",
                "effective_to":    eff_to or "",
                "day_of_operation": None,
                "service_type":    service_type,
                "terminal_dep":    None,
                "terminal_arr":    None,
                "source_file":     path.name,
            })

    df = pd.DataFrame(records) if records else pd.DataFrame()
    logger.info(
        f"SKD  {path.name}: {len(records)} records parsed, {skipped} skipped."
    )
    return df, warnings
    """Load a CSV or TSV schedule file, trying common delimiters."""
    warnings: List[str] = []
    for sep in [",", ";", "\t", "|"]:
        try:
            df = pd.read_csv(path, sep=sep, dtype=str, encoding="utf-8", encoding_errors="replace")
            if df.shape[1] > 1:
                df["source_file"] = path.name
                logger.info(f"CSV  {path.name}: {len(df)} rows loaded (sep={repr(sep)}).")
                return df, warnings
        except Exception as exc:
            warnings.append(f"sep={repr(sep)} failed: {exc}")
    warnings.append(f"{path.name}: could not parse as delimited text.")
    return pd.DataFrame(), warnings


def load_schedule_folder(folder: str) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    Load all schedule files from *folder*.

    Returns:
        raw_df   – concatenated raw DataFrame (un-normalised)
        report   – dict with per-file stats and any warnings
    """
    folder_path = Path(folder)
    if not folder_path.is_dir():
        raise ValueError(f"Not a directory: {folder}")

    all_frames: List[pd.DataFrame] = []
    report: Dict[str, Any] = {"files": [], "total_rows": 0, "warnings": []}

    extensions = {".csv", ".txt", ".ssim", ".out"}
    files_found = sorted(
        p for p in folder_path.iterdir()
        if p.is_file() and p.suffix.lower() in extensions
    )

    if not files_found:
        logger.warning(f"No schedule files found in {folder}.")
        return pd.DataFrame(), report

    for fpath in files_found:
        file_info: Dict[str, Any] = {
            "name": fpath.name, "rows": 0, "warnings": []
        }
        try:
            if fpath.suffix.lower() == ".ssim":
                df, warns = load_ssim_file(fpath)
            elif fpath.suffix.lower() == ".out":
                df, warns = load_skd_file(fpath)
            else:
                df, warns = load_csv_file(fpath)

            file_info["warnings"].extend(warns)
            if not df.empty:
                all_frames.append(df)
                file_info["rows"] = len(df)
                report["total_rows"] += len(df)

        except Exception as exc:
            msg = f"{fpath.name}: unexpected error — {exc}"
            logger.error(msg)
            file_info["warnings"].append(msg)

        report["files"].append(file_info)

    combined = pd.concat(all_frames, ignore_index=True) if all_frames else pd.DataFrame()
    logger.info(
        f"Loaded {len(files_found)} files → {len(combined)} total rows."
    )
    return combined, report
