"""
WorksetDataService — loads WORKSET12265 reference data files into DuckDB
and exposes comprehensive route/airport intelligence queries.

Files used:
  out/SPILLDATA.dat   — per-flight-per-day LF, spill, recapture, market-share
  out/BASEDATA.dat    — per-flight capacity, distance, block-time
  data/mktSize.dat    — O&D weekly demand index
  data/opp.dat        — airline market-share at each airport
  data/alliance.dat   — airline alliance memberships
"""

import os
import math
from pathlib import Path
from typing import Dict, Any, Optional
from loguru import logger

from app.database.db import get_connection

# ─────────────────────────────────────────────────────────────────────────────
# Static reference: airline full names
# ─────────────────────────────────────────────────────────────────────────────

AIRLINE_NAMES: Dict[str, str] = {
    "EK": "Emirates", "FZ": "flydubai", "AI": "Air India", "6E": "IndiGo",
    "SG": "SpiceJet", "QR": "Qatar Airways", "EY": "Etihad Airways",
    "BA": "British Airways", "LH": "Lufthansa", "AF": "Air France",
    "KL": "KLM", "UA": "United Airlines", "AA": "American Airlines",
    "DL": "Delta Air Lines", "TK": "Turkish Airlines", "SQ": "Singapore Airlines",
    "CX": "Cathay Pacific", "JL": "Japan Airlines", "NH": "ANA (All Nippon Airways)",
    "QF": "Qantas", "NZ": "Air New Zealand", "MH": "Malaysia Airlines",
    "TG": "Thai Airways", "GA": "Garuda Indonesia", "PR": "Philippine Airlines",
    "VN": "Vietnam Airlines", "CI": "China Airlines", "BR": "EVA Air",
    "MU": "China Eastern", "CZ": "China Southern", "CA": "Air China",
    "VS": "Virgin Atlantic", "IB": "Iberia", "TP": "TAP Air Portugal",
    "AZ": "ITA Airways", "LX": "Swiss International", "OS": "Austrian Airlines",
    "SN": "Brussels Airlines", "SK": "SAS Scandinavian", "AY": "Finnair",
    "WN": "Southwest Airlines", "B6": "JetBlue", "AS": "Alaska Airlines",
    "FR": "Ryanair", "U2": "easyJet", "W6": "Wizz Air", "VY": "Vueling",
    "PC": "Pegasus Airlines", "TU": "Tunisair", "MS": "EgyptAir",
    "RJ": "Royal Jordanian", "GF": "Gulf Air", "WY": "Oman Air",
    "KU": "Kuwait Airways", "SV": "Saudia", "ET": "Ethiopian Airlines",
    "KQ": "Kenya Airways", "SA": "South African Airways", "AC": "Air Canada",
    "AM": "Aeromexico", "LA": "LATAM Airlines", "G3": "GOL Linhas Aéreas",
    "9W": "Jet Airways", "IX": "Air India Express", "I5": "Air Asia India",
    "S5": "Shuttle America", "OZ": "Asiana Airlines", "KE": "Korean Air",
    "HX": "Hong Kong Airlines", "CZ": "China Southern", "JQ": "Jetstar",
    "TR": "Scoot", "AK": "AirAsia", "3K": "Jetstar Asia", "O3": "Air Arabia",
    "G9": "Air Arabia", "J9": "Jazeera Airways", "XY": "Flynas",
    "UL": "SriLankan Airlines", "BG": "Biman Bangladesh", "PK": "Pakistan International",
}

# ─────────────────────────────────────────────────────────────────────────────
# Static reference: airport city + UTC offset
# ─────────────────────────────────────────────────────────────────────────────

AIRPORT_INFO: Dict[str, Dict[str, str]] = {
    # Middle East / Gulf
    "DXB": {"city": "Dubai",         "country": "UAE",         "utc": "+04:00"},
    "DWC": {"city": "Dubai (Al Maktoum)", "country": "UAE",    "utc": "+04:00"},
    "AUH": {"city": "Abu Dhabi",     "country": "UAE",         "utc": "+04:00"},
    "DOH": {"city": "Doha",          "country": "Qatar",       "utc": "+03:00"},
    "BAH": {"city": "Bahrain",       "country": "Bahrain",     "utc": "+03:00"},
    "MCT": {"city": "Muscat",        "country": "Oman",        "utc": "+04:00"},
    "KWI": {"city": "Kuwait City",   "country": "Kuwait",      "utc": "+03:00"},
    "AMM": {"city": "Amman",         "country": "Jordan",      "utc": "+03:00"},
    "BEY": {"city": "Beirut",        "country": "Lebanon",     "utc": "+03:00"},
    "RUH": {"city": "Riyadh",        "country": "Saudi Arabia","utc": "+03:00"},
    "JED": {"city": "Jeddah",        "country": "Saudi Arabia","utc": "+03:00"},
    "TLV": {"city": "Tel Aviv",      "country": "Israel",      "utc": "+02:00"},
    "CAI": {"city": "Cairo",         "country": "Egypt",       "utc": "+02:00"},
    # India
    "BOM": {"city": "Mumbai",        "country": "India",       "utc": "+05:30"},
    "DEL": {"city": "Delhi",         "country": "India",       "utc": "+05:30"},
    "MAA": {"city": "Chennai",       "country": "India",       "utc": "+05:30"},
    "BLR": {"city": "Bangalore",     "country": "India",       "utc": "+05:30"},
    "CCU": {"city": "Kolkata",       "country": "India",       "utc": "+05:30"},
    "HYD": {"city": "Hyderabad",     "country": "India",       "utc": "+05:30"},
    "COK": {"city": "Kochi",         "country": "India",       "utc": "+05:30"},
    "AMD": {"city": "Ahmedabad",     "country": "India",       "utc": "+05:30"},
    "PNQ": {"city": "Pune",          "country": "India",       "utc": "+05:30"},
    "GOI": {"city": "Goa",           "country": "India",       "utc": "+05:30"},
    "TRV": {"city": "Thiruvananthapuram", "country": "India",  "utc": "+05:30"},
    "CCJ": {"city": "Kozhikode",     "country": "India",       "utc": "+05:30"},
    # Europe
    "LHR": {"city": "London",        "country": "UK",          "utc": "+00:00"},
    "LGW": {"city": "London Gatwick","country": "UK",          "utc": "+00:00"},
    "STN": {"city": "London Stansted","country": "UK",         "utc": "+00:00"},
    "MAN": {"city": "Manchester",    "country": "UK",          "utc": "+00:00"},
    "CDG": {"city": "Paris",         "country": "France",      "utc": "+01:00"},
    "ORY": {"city": "Paris Orly",    "country": "France",      "utc": "+01:00"},
    "AMS": {"city": "Amsterdam",     "country": "Netherlands", "utc": "+01:00"},
    "FRA": {"city": "Frankfurt",     "country": "Germany",     "utc": "+01:00"},
    "MUC": {"city": "Munich",        "country": "Germany",     "utc": "+01:00"},
    "ZRH": {"city": "Zurich",        "country": "Switzerland", "utc": "+01:00"},
    "VIE": {"city": "Vienna",        "country": "Austria",     "utc": "+01:00"},
    "MAD": {"city": "Madrid",        "country": "Spain",       "utc": "+01:00"},
    "BCN": {"city": "Barcelona",     "country": "Spain",       "utc": "+01:00"},
    "FCO": {"city": "Rome",          "country": "Italy",       "utc": "+01:00"},
    "MXP": {"city": "Milan",         "country": "Italy",       "utc": "+01:00"},
    "CPH": {"city": "Copenhagen",    "country": "Denmark",     "utc": "+01:00"},
    "ARN": {"city": "Stockholm",     "country": "Sweden",      "utc": "+01:00"},
    "OSL": {"city": "Oslo",          "country": "Norway",      "utc": "+01:00"},
    "HEL": {"city": "Helsinki",      "country": "Finland",     "utc": "+02:00"},
    "ATH": {"city": "Athens",        "country": "Greece",      "utc": "+02:00"},
    "IST": {"city": "Istanbul",      "country": "Turkey",      "utc": "+03:00"},
    "SAW": {"city": "Istanbul Sabiha","country": "Turkey",     "utc": "+03:00"},
    "DME": {"city": "Moscow",        "country": "Russia",      "utc": "+03:00"},
    "SVO": {"city": "Moscow",        "country": "Russia",      "utc": "+03:00"},
    "WAW": {"city": "Warsaw",        "country": "Poland",      "utc": "+01:00"},
    "PRG": {"city": "Prague",        "country": "Czech Rep.",  "utc": "+01:00"},
    "BUD": {"city": "Budapest",      "country": "Hungary",     "utc": "+01:00"},
    # North America
    "JFK": {"city": "New York JFK",  "country": "USA",         "utc": "-05:00"},
    "EWR": {"city": "Newark",        "country": "USA",         "utc": "-05:00"},
    "LGA": {"city": "New York LGA",  "country": "USA",         "utc": "-05:00"},
    "ORD": {"city": "Chicago",       "country": "USA",         "utc": "-06:00"},
    "LAX": {"city": "Los Angeles",   "country": "USA",         "utc": "-08:00"},
    "SFO": {"city": "San Francisco", "country": "USA",         "utc": "-08:00"},
    "ATL": {"city": "Atlanta",       "country": "USA",         "utc": "-05:00"},
    "MIA": {"city": "Miami",         "country": "USA",         "utc": "-05:00"},
    "DFW": {"city": "Dallas",        "country": "USA",         "utc": "-06:00"},
    "SEA": {"city": "Seattle",       "country": "USA",         "utc": "-08:00"},
    "BOS": {"city": "Boston",        "country": "USA",         "utc": "-05:00"},
    "IAD": {"city": "Washington DC", "country": "USA",         "utc": "-05:00"},
    "DEN": {"city": "Denver",        "country": "USA",         "utc": "-07:00"},
    "YYZ": {"city": "Toronto",       "country": "Canada",      "utc": "-05:00"},
    "YVR": {"city": "Vancouver",     "country": "Canada",      "utc": "-08:00"},
    "MEX": {"city": "Mexico City",   "country": "Mexico",      "utc": "-06:00"},
    # Asia Pacific
    "SIN": {"city": "Singapore",     "country": "Singapore",   "utc": "+08:00"},
    "KUL": {"city": "Kuala Lumpur",  "country": "Malaysia",    "utc": "+08:00"},
    "BKK": {"city": "Bangkok",       "country": "Thailand",    "utc": "+07:00"},
    "HKG": {"city": "Hong Kong",     "country": "China",       "utc": "+08:00"},
    "NRT": {"city": "Tokyo Narita",  "country": "Japan",       "utc": "+09:00"},
    "HND": {"city": "Tokyo Haneda",  "country": "Japan",       "utc": "+09:00"},
    "ICN": {"city": "Seoul",         "country": "South Korea", "utc": "+09:00"},
    "PVG": {"city": "Shanghai",      "country": "China",       "utc": "+08:00"},
    "PEK": {"city": "Beijing",       "country": "China",       "utc": "+08:00"},
    "CAN": {"city": "Guangzhou",     "country": "China",       "utc": "+08:00"},
    "SYD": {"city": "Sydney",        "country": "Australia",   "utc": "+11:00"},
    "MEL": {"city": "Melbourne",     "country": "Australia",   "utc": "+11:00"},
    "BNE": {"city": "Brisbane",      "country": "Australia",   "utc": "+10:00"},
    "PER": {"city": "Perth",         "country": "Australia",   "utc": "+08:00"},
    "AKL": {"city": "Auckland",      "country": "New Zealand", "utc": "+13:00"},
    "CGK": {"city": "Jakarta",       "country": "Indonesia",   "utc": "+07:00"},
    "MNL": {"city": "Manila",        "country": "Philippines", "utc": "+08:00"},
    # South Asia
    "KHI": {"city": "Karachi",       "country": "Pakistan",    "utc": "+05:00"},
    "LHE": {"city": "Lahore",        "country": "Pakistan",    "utc": "+05:00"},
    "ISB": {"city": "Islamabad",     "country": "Pakistan",    "utc": "+05:00"},
    "DAC": {"city": "Dhaka",         "country": "Bangladesh",  "utc": "+06:00"},
    "CMB": {"city": "Colombo",       "country": "Sri Lanka",   "utc": "+05:30"},
    "KTM": {"city": "Kathmandu",     "country": "Nepal",       "utc": "+05:45"},
    # Africa
    "JNB": {"city": "Johannesburg",  "country": "South Africa","utc": "+02:00"},
    "CPT": {"city": "Cape Town",     "country": "South Africa","utc": "+02:00"},
    "NBO": {"city": "Nairobi",       "country": "Kenya",       "utc": "+03:00"},
    "ADD": {"city": "Addis Ababa",   "country": "Ethiopia",    "utc": "+03:00"},
    "LOS": {"city": "Lagos",         "country": "Nigeria",     "utc": "+01:00"},
    "ACC": {"city": "Accra",         "country": "Ghana",       "utc": "+00:00"},
    "CMN": {"city": "Casablanca",    "country": "Morocco",     "utc": "+01:00"},
    # South America
    "GRU": {"city": "São Paulo",     "country": "Brazil",      "utc": "-03:00"},
    "EZE": {"city": "Buenos Aires",  "country": "Argentina",   "utc": "-03:00"},
    "SCL": {"city": "Santiago",      "country": "Chile",       "utc": "-03:00"},
    "LIM": {"city": "Lima",          "country": "Peru",        "utc": "-05:00"},
    "BOG": {"city": "Bogotá",        "country": "Colombia",    "utc": "-05:00"},
}

# ─────────────────────────────────────────────────────────────────────────────
# Carrier type classification
# ─────────────────────────────────────────────────────────────────────────────

CARRIER_TYPE: Dict[str, str] = {
    # Full-service network carriers
    "EK": "Full-service", "QR": "Full-service", "EY": "Full-service",
    "AI": "Full-service", "BA": "Full-service", "LH": "Full-service",
    "AF": "Full-service", "KL": "Full-service", "UA": "Full-service",
    "AA": "Full-service", "DL": "Full-service", "TK": "Full-service",
    "SQ": "Full-service", "CX": "Full-service", "JL": "Full-service",
    "NH": "Full-service", "QF": "Full-service", "MH": "Full-service",
    "CA": "Full-service", "MU": "Full-service", "CZ": "Full-service",
    "ET": "Full-service", "KQ": "Full-service", "SA": "Full-service",
    "MS": "Full-service", "RJ": "Full-service", "GF": "Full-service",
    "WY": "Full-service", "KU": "Full-service", "SV": "Full-service",
    "UL": "Full-service", "BG": "Full-service", "PK": "Full-service",
    # Low-cost / ultra-low-cost
    "FZ": "Low-cost", "6E": "Low-cost", "SG": "Low-cost", "FR": "Low-cost",
    "U2": "Low-cost", "W6": "Low-cost", "VY": "Low-cost", "B6": "Low-cost",
    "WN": "Low-cost", "AS": "Low-cost", "NK": "Low-cost", "F9": "Low-cost",
    "TR": "Low-cost", "AK": "Low-cost", "3K": "Low-cost", "JQ": "Low-cost",
    "IX": "Low-cost", "G9": "Low-cost", "XY": "Low-cost", "J9": "Low-cost",
    "PC": "Low-cost", "TU": "Low-cost",
    # Charter / leisure
    "TOM": "Charter", "TCX": "Charter", "BY": "Charter",
}


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_workset_dir() -> Path:
    data_folder = os.getenv("SCHEDAI_DATA_FOLDER", "")
    p = Path(data_folder)
    return p.parent if p.name == "out" else p


def _table_exists(conn, name: str) -> bool:
    r = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name=?", [name]
    ).fetchone()
    return (r[0] if r else 0) > 0


def _safe_float(v, default=0.0) -> float:
    try:
        x = float(v)
        return 0.0 if math.isnan(x) or math.isinf(x) else x
    except Exception:
        return default


def _safe_int(v, default=0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _csv_path(p: Path) -> str:
    """Return forward-slash path string safe for DuckDB SQL."""
    return str(p).replace("\\", "/")


# ─────────────────────────────────────────────────────────────────────────────
# Table loaders
# ─────────────────────────────────────────────────────────────────────────────

def _load_mkt_size(conn, path: Path):
    if not path.exists():
        logger.warning(f"mktSize.dat not found at {path}")
        return
    if _table_exists(conn, "workset_mkt"):
        return
    logger.info("Loading market-size data …")
    conn.execute(f"""
        CREATE TABLE workset_mkt AS
        SELECT
            column0 AS origin,
            column1 AS dest,
            TRY_CAST(column2 AS FLOAT) AS weekly_demand
        FROM read_csv('{_csv_path(path)}', header=false, delim=',',
                      null_padding=true, all_varchar=true)
        WHERE column0 IS NOT NULL AND LENGTH(TRIM(column0)) = 3
    """)
    n = conn.execute("SELECT COUNT(*) FROM workset_mkt").fetchone()[0]
    logger.info(f"  workset_mkt: {n:,} rows")


def _load_alliance(conn, path: Path):
    if not path.exists():
        logger.warning(f"alliance.dat not found at {path}")
        return
    if _table_exists(conn, "workset_alliance"):
        return
    logger.info("Loading alliance data …")
    conn.execute(f"""
        CREATE TABLE workset_alliance AS
        SELECT ALLNCENM AS alliance_name, ALNCD AS airline, ADJUSTPOO AS adjust_pool
        FROM read_csv('{_csv_path(path)}', header=true, delim=',', all_varchar=true)
        WHERE ALNCD IS NOT NULL AND LENGTH(TRIM(ALNCD)) >= 2
    """)
    n = conn.execute("SELECT COUNT(*) FROM workset_alliance").fetchone()[0]
    logger.info(f"  workset_alliance: {n:,} rows")


def _load_opp(conn, path: Path):
    if not path.exists():
        logger.warning(f"opp.dat not found at {path}")
        return
    if _table_exists(conn, "workset_opp"):
        return
    logger.info("Loading airport market-share (opp) data …")
    # opp.dat: space-separated, no header:  airport  airline  share_fraction
    conn.execute(f"""
        CREATE TABLE workset_opp AS
        SELECT
            column0 AS airport,
            column1 AS airline,
            TRY_CAST(column2 AS FLOAT) AS mkt_share
        FROM read_csv('{_csv_path(path)}', header=false, delim=' ',
                      null_padding=true, all_varchar=true)
        WHERE column0 IS NOT NULL AND LENGTH(TRIM(column0)) = 3
          AND column1 IS NOT NULL AND LENGTH(TRIM(column1)) >= 2
    """)
    n = conn.execute("SELECT COUNT(*) FROM workset_opp").fetchone()[0]
    logger.info(f"  workset_opp: {n:,} rows")


def _load_spill_data(conn, path: Path):
    if not path.exists():
        logger.warning(f"SPILLDATA.dat not found at {path}")
        return
    if _table_exists(conn, "workset_spill"):
        return
    logger.info("Loading SPILLDATA (this may take ~30 s) …")
    # 35+ comma-separated fields, no header — DuckDB zero-pads: column00..column34
    # 00=orig,01=dest,02=dep_time,03=day,04=cap_total,05=cap_biz,
    # 08=lf_pax,09=lf_rev, 12=spill_pax,13=spill_rev,
    # 14=recap_pax,15=recap_rev, 16=total_lf_pax,17=total_lf_rev,
    # 20=flag, 21=block_time,22=stops,23=mkt_share,24=airline,
    # 25=is_codeshare,26=flight_id
    conn.execute(f"""
        CREATE TABLE workset_spill AS
        SELECT
            column00                             AS origin,
            column01                             AS dest,
            column02                             AS dep_time,
            TRY_CAST(column03  AS INTEGER)       AS day_of_week,
            TRY_CAST(column04  AS INTEGER)       AS cap_total,
            TRY_CAST(column05  AS INTEGER)       AS cap_biz,
            TRY_CAST(column08  AS FLOAT)         AS lf_pax,
            TRY_CAST(column09  AS FLOAT)         AS lf_rev,
            TRY_CAST(column12  AS FLOAT)         AS spill_pax,
            TRY_CAST(column13  AS FLOAT)         AS spill_rev,
            TRY_CAST(column14  AS FLOAT)         AS recap_pax,
            TRY_CAST(column15  AS FLOAT)         AS recap_rev,
            TRY_CAST(column16  AS FLOAT)         AS total_lf_pax,
            TRY_CAST(column17  AS FLOAT)         AS total_lf_rev,
            column20                             AS status_flag,
            TRY_CAST(column21  AS INTEGER)       AS block_time,
            TRY_CAST(column22  AS INTEGER)       AS stops,
            TRY_CAST(column23  AS FLOAT)         AS mkt_share,
            column24                             AS airline,
            TRY_CAST(column25  AS INTEGER)       AS is_codeshare,
            TRY_CAST(column26  AS BIGINT)        AS flight_id
        FROM read_csv('{_csv_path(path)}',
                      header=false, delim=',', null_padding=true,
                      all_varchar=true, parallel=true)
        WHERE column00 IS NOT NULL
          AND LENGTH(TRIM(column00)) = 3
          AND LENGTH(TRIM(column01)) = 3
    """)
    n = conn.execute("SELECT COUNT(*) FROM workset_spill").fetchone()[0]
    logger.info(f"  workset_spill: {n:,} rows")


def _load_base_data(conn, path: Path):
    if not path.exists():
        logger.warning(f"BASEDATA.dat not found at {path}")
        return
    if _table_exists(conn, "workset_base"):
        return
    logger.info("Loading BASEDATA (this may take ~15 s) …")
    # 24 comma-separated fields, no header — DuckDB zero-pads: column00..column23
    # 00=rec_id,01=orig,02=dest,03=flt_num,04=dep,05=arr,06=block,07=dist,
    # 08=op_aln,09=ac_type,10=cap_total,11=booked,12=demand,13=yield,
    # 14=spill,15=day,16=stops,17=op_aln2,18=mkt_aln,19=flt_num2,
    # 20=mct_dep,21=mct_arr
    conn.execute(f"""
        CREATE TABLE workset_base AS
        SELECT
            TRY_CAST(column00 AS BIGINT)    AS record_id,
            column01                        AS origin,
            column02                        AS dest,
            column03                        AS flight_num,
            column04                        AS dep_time,
            column05                        AS arr_time,
            TRY_CAST(column06 AS INTEGER)   AS block_time,
            TRY_CAST(column07 AS INTEGER)   AS distance_mi,
            column08                        AS op_airline,
            column09                        AS aircraft_type,
            TRY_CAST(column10 AS INTEGER)   AS cap_total,
            TRY_CAST(column11 AS FLOAT)     AS booked_pax,
            TRY_CAST(column12 AS FLOAT)     AS demand_pax,
            TRY_CAST(column14 AS FLOAT)     AS spill_pax,
            TRY_CAST(column15 AS INTEGER)   AS day_of_week,
            TRY_CAST(column16 AS INTEGER)   AS stops,
            column18                        AS mkt_airline,
            TRY_CAST(column20 AS INTEGER)   AS mct_dep,
            TRY_CAST(column21 AS INTEGER)   AS mct_arr
        FROM read_csv('{_csv_path(path)}',
                      header=false, delim=',', null_padding=true,
                      all_varchar=true, parallel=true)
        WHERE column01 IS NOT NULL
          AND LENGTH(TRIM(column01)) = 3
          AND LENGTH(TRIM(column02)) = 3
    """)
    n = conn.execute("SELECT COUNT(*) FROM workset_base").fetchone()[0]
    logger.info(f"  workset_base: {n:,} rows")


# ─────────────────────────────────────────────────────────────────────────────
# Public initialiser
# ─────────────────────────────────────────────────────────────────────────────

_WORKSET_LOADED = False


def init_workset():
    """Load all workset reference data into DuckDB. Idempotent."""
    global _WORKSET_LOADED
    if _WORKSET_LOADED:
        return
    wd = _get_workset_dir()
    logger.info(f"Initialising workset data from {wd} …")
    conn = get_connection()
    try:
        _load_mkt_size(conn,   wd / "data" / "mktSize.dat")
        _load_alliance(conn,   wd / "data" / "alliance.dat")
        _load_opp(conn,        wd / "data" / "opp.dat")
        _load_base_data(conn,  wd / "out"  / "BASEDATA.dat")
        _load_spill_data(conn, wd / "out"  / "SPILLDATA.dat")
        _WORKSET_LOADED = True
        logger.info("Workset reference data ready.")
    except Exception as exc:
        logger.error(f"Workset init failed: {exc}")
        # Drop any partial tables so next call can retry cleanly
        for tbl in ("workset_mkt", "workset_alliance", "workset_opp", "workset_base", "workset_spill"):
            try:
                conn.execute(f"DROP TABLE IF EXISTS {tbl}")
            except Exception:
                pass


def is_loaded() -> bool:
    return _WORKSET_LOADED


# ─────────────────────────────────────────────────────────────────────────────
# Workset B — second scenario loader
# ─────────────────────────────────────────────────────────────────────────────

_WORKSET_B_PATH: Optional[str] = None


def get_workset_dirs() -> list:
    """
    Discover all valid workset directories sibling to the current workset.
    A valid workset dir must contain out/BASEDATA.dat.
    """
    wd = _get_workset_dir()
    parent = wd.parent
    found = []
    try:
        for d in sorted(parent.iterdir()):
            if d.is_dir() and (d / "out" / "BASEDATA.dat").exists():
                found.append({"name": d.name, "path": str(d), "current": d == wd})
    except Exception:
        pass
    # Also include the current workset even if somehow not found
    if not any(f["current"] for f in found):
        found.insert(0, {"name": wd.name, "path": str(wd), "current": True})
    return found


def load_workset_b(path: str) -> dict:
    """
    Load a second workset from *path* into DuckDB tables suffixed with _b.
    Returns a status dict.
    """
    global _WORKSET_B_PATH
    conn = get_connection()

    # Drop any existing _b tables
    for tbl in ("workset_base_b", "workset_spill_b"):
        try:
            conn.execute(f"DROP TABLE IF EXISTS {tbl}")
        except Exception:
            pass

    wd = Path(path)
    if not wd.exists():
        raise ValueError(f"Path not found: {path}")

    errors = []
    counts = {}

    # Load BASEDATA into workset_base_b
    base_path = wd / "out" / "BASEDATA.dat"
    if base_path.exists():
        try:
            conn.execute(f"""
                CREATE TABLE workset_base_b AS
                SELECT
                    TRIM(column01)                          AS origin,
                    TRIM(column02)                          AS dest,
                    column04                                AS dep_time,
                    column05                                AS arr_time,
                    TRY_CAST(column06 AS INTEGER)           AS block_time,
                    TRY_CAST(column07 AS INTEGER)           AS distance_mi,
                    column08                                AS op_airline,
                    column09                                AS aircraft_type,
                    TRY_CAST(column10 AS INTEGER)           AS cap_total,
                    TRY_CAST(column11 AS FLOAT)             AS booked_pax,
                    TRY_CAST(column12 AS FLOAT)             AS demand_pax,
                    TRY_CAST(column14 AS FLOAT)             AS spill_pax,
                    TRY_CAST(column15 AS INTEGER)           AS day_of_week,
                    TRY_CAST(column16 AS INTEGER)           AS stops,
                    column18                                AS mkt_airline,
                    TRY_CAST(column20 AS INTEGER)           AS mct_dep,
                    TRY_CAST(column21 AS INTEGER)           AS mct_arr
                FROM read_csv('{_csv_path(base_path)}',
                              header=false, delim=',', null_padding=true,
                              all_varchar=true, parallel=true)
                WHERE column01 IS NOT NULL
                  AND LENGTH(TRIM(column01)) = 3
                  AND LENGTH(TRIM(column02)) = 3
            """)
            counts["base"] = conn.execute("SELECT COUNT(*) FROM workset_base_b").fetchone()[0]
        except Exception as e:
            errors.append(f"BASEDATA: {e}")
    else:
        errors.append("BASEDATA.dat not found")

    # Load SPILLDATA into workset_spill_b (optional — large file)
    spill_path = wd / "out" / "SPILLDATA.dat"
    if spill_path.exists():
        try:
            conn.execute(f"""
                CREATE TABLE workset_spill_b AS
                SELECT
                    column00 AS origin, column01 AS dest,
                    column02 AS dep_time,
                    TRY_CAST(column04 AS INTEGER) AS cap_total,
                    TRY_CAST(column08 AS FLOAT)   AS lf_pax,
                    TRY_CAST(column12 AS FLOAT)   AS spill_pax,
                    TRY_CAST(column14 AS FLOAT)   AS recap_pax,
                    TRY_CAST(column16 AS FLOAT)   AS total_lf_pax,
                    TRY_CAST(column23 AS FLOAT)   AS mkt_share,
                    column24                      AS airline
                FROM read_csv('{_csv_path(spill_path)}',
                              header=false, delim=',', null_padding=true,
                              all_varchar=true, parallel=true)
                WHERE column00 IS NOT NULL
                  AND LENGTH(TRIM(column00)) = 3
                  AND LENGTH(TRIM(column01)) = 3
            """)
            counts["spill"] = conn.execute("SELECT COUNT(*) FROM workset_spill_b").fetchone()[0]
        except Exception as e:
            errors.append(f"SPILLDATA: {e}")

    _WORKSET_B_PATH = path
    return {"loaded": not errors, "path": path, "name": wd.name, "counts": counts, "errors": errors}


def compare_worksets(origin: str = "", dest: str = "", airline: str = "", top_n: int = 50) -> dict:
    """
    FULL OUTER JOIN workset_base vs workset_base_b, returning per-route deltas.
    Returns summary metrics + top_n route rows sorted by |demand_delta|.
    """
    conn = get_connection()
    if not _table_exists(conn, "workset_base_b"):
        raise ValueError("Workset B not loaded. Call load_workset_b first.")

    where_a = where_b = "1=1"
    filters = []
    if origin:
        filters.append(f"origin = '{origin.upper()}'")
    if dest:
        filters.append(f"dest = '{dest.upper()}'")
    if airline:
        filters.append(f"op_airline = '{airline.upper()}'")
    if filters:
        clause = " AND ".join(filters)
        where_a = where_b = clause

    sql = f"""
    WITH a AS (
        SELECT origin, dest,
               COUNT(*)                  AS flights_a,
               SUM(cap_total)            AS seats_a,
               SUM(demand_pax)           AS demand_a,
               SUM(spill_pax)            AS spill_a,
               AVG(CASE WHEN cap_total > 0 THEN booked_pax / cap_total END) AS lf_a
        FROM workset_base
        WHERE {where_a}
        GROUP BY origin, dest
    ),
    b AS (
        SELECT origin, dest,
               COUNT(*)                  AS flights_b,
               SUM(cap_total)            AS seats_b,
               SUM(demand_pax)           AS demand_b,
               SUM(spill_pax)            AS spill_b,
               AVG(CASE WHEN cap_total > 0 THEN booked_pax / cap_total END) AS lf_b
        FROM workset_base_b
        WHERE {where_b}
        GROUP BY origin, dest
    )
    SELECT
        COALESCE(a.origin, b.origin)  AS origin,
        COALESCE(a.dest,   b.dest)    AS dest,
        COALESCE(a.flights_a, 0)      AS flights_a,
        COALESCE(b.flights_b, 0)      AS flights_b,
        COALESCE(a.seats_a,   0)      AS seats_a,
        COALESCE(b.seats_b,   0)      AS seats_b,
        ROUND(COALESCE(a.demand_a, 0), 0)  AS demand_a,
        ROUND(COALESCE(b.demand_b, 0), 0)  AS demand_b,
        ROUND(COALESCE(b.demand_b, 0) - COALESCE(a.demand_a, 0), 0) AS demand_delta,
        ROUND(COALESCE(a.spill_a, 0), 0)   AS spill_a,
        ROUND(COALESCE(b.spill_b, 0), 0)   AS spill_b,
        ROUND(COALESCE(b.spill_b, 0) - COALESCE(a.spill_a, 0), 0)  AS spill_delta,
        ROUND(COALESCE(a.lf_a, 0) * 100, 1) AS lf_a_pct,
        ROUND(COALESCE(b.lf_b, 0) * 100, 1) AS lf_b_pct,
        CASE
            WHEN a.origin IS NULL THEN 'new'
            WHEN b.origin IS NULL THEN 'dropped'
            WHEN COALESCE(b.spill_b,0) < COALESCE(a.spill_a,0) * 0.9 THEN 'improved'
            WHEN COALESCE(b.spill_b,0) > COALESCE(a.spill_a,0) * 1.1 THEN 'deteriorated'
            ELSE 'stable'
        END AS status
    FROM a FULL OUTER JOIN b ON a.origin = b.origin AND a.dest = b.dest
    ORDER BY ABS(demand_delta) DESC NULLS LAST
    LIMIT {top_n}
    """
    rows = conn.execute(sql).fetchdf()

    # Summary metrics
    total_demand_delta = int(rows["demand_delta"].sum())
    total_spill_delta  = int(rows["spill_delta"].sum())
    new_routes     = int((rows["status"] == "new").sum())
    dropped_routes = int((rows["status"] == "dropped").sum())
    improved       = int((rows["status"] == "improved").sum())
    deteriorated   = int((rows["status"] == "deteriorated").sum())

    route_rows = rows.to_dict(orient="records")
    # Convert numpy types to python native for JSON serialisation
    for r in route_rows:
        for k, v in r.items():
            if hasattr(v, "item"):
                r[k] = v.item()

    wd_a = _get_workset_dir()
    return {
        "workset_a": wd_a.name,
        "workset_b": Path(_WORKSET_B_PATH).name if _WORKSET_B_PATH else "?",
        "summary": {
            "total_demand_delta": total_demand_delta,
            "total_spill_delta":  total_spill_delta,
            "new_routes":         new_routes,
            "dropped_routes":     dropped_routes,
            "improved_routes":    improved,
            "deteriorated_routes":deteriorated,
            "routes_compared":    len(rows),
        },
        "routes": route_rows,
    }

# ─────────────────────────────────────────────────────────────────────────────
# Query helpers
# ─────────────────────────────────────────────────────────────────────────────

def _utc_diff_str(utc_a: str, utc_b: str) -> str:
    """Return human-readable time difference between two UTC offset strings."""
    def _mins(s: str) -> int:
        s = s.strip()
        sign = -1 if s.startswith("-") else 1
        s = s.lstrip("+-")
        parts = s.split(":")
        return sign * (int(parts[0]) * 60 + int(parts[1]))
    try:
        diff = _mins(utc_b) - _mins(utc_a)
        sign = "+" if diff >= 0 else "-"
        diff = abs(diff)
        h, m = divmod(diff, 60)
        if m:
            return f"{sign}{h}h {m}min"
        return f"{sign}{h}h"
    except Exception:
        return "unknown"


def _demand_rating(val: float) -> str:
    if val is None:
        return "Unknown"
    if val >= 5000:
        return "Very High"
    if val >= 1000:
        return "High"
    if val >= 200:
        return "Medium"
    return "Low"


def _time_bucket(hhmm: str) -> str:
    try:
        h = int(str(hhmm).zfill(4)[:2])
        if h < 6:
            return "red_eye (00-06)"
        if h < 12:
            return "morning (06-12)"
        if h < 18:
            return "afternoon (12-18)"
        return "evening (18-24)"
    except Exception:
        return "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# Core intelligence query
# ─────────────────────────────────────────────────────────────────────────────

def get_route_intelligence(origin: str, dest: str) -> Dict[str, Any]:
    """
    Return a comprehensive intelligence dossier for an O&D pair combining:
    - SPILLDATA: airline market shares, demand pressure, spill/recapture
    - BASEDATA: distance, block time
    - mktSize: market demand index
    - alliance: carrier alliance memberships
    - opp: airport dominance
    - Static: timezone, city names, carrier types
    """
    if not _WORKSET_LOADED:
        return {"error": "Workset data not yet loaded. Please retry in a few seconds."}

    conn = get_connection()

    # ── 1. Timezone & city info ───────────────────────────────────────────────
    orig_info = AIRPORT_INFO.get(origin, {})
    dest_info = AIRPORT_INFO.get(dest, {})
    tz_orig   = orig_info.get("utc", "unknown")
    tz_dest   = dest_info.get("utc", "unknown")
    tz_diff   = _utc_diff_str(tz_orig, tz_dest) if tz_orig != "unknown" else "unknown"

    # ── 2. Route distance & block time (BASEDATA) ────────────────────────────
    route_meta = conn.execute("""
        SELECT
            CAST(AVG(distance_mi) AS INTEGER) AS dist,
            CAST(AVG(block_time)  AS INTEGER) AS bt,
            MIN(dep_time) AS earliest_dep,
            MAX(dep_time) AS latest_dep
        FROM workset_base
        WHERE origin=? AND dest=?
    """, [origin, dest]).fetchone()

    dist_mi   = _safe_int(route_meta[0]) if route_meta else 0
    block_min = _safe_int(route_meta[1]) if route_meta else 0
    earliest  = route_meta[2] if route_meta else None
    latest    = route_meta[3] if route_meta else None

    h, m = divmod(block_min, 60) if block_min else (0, 0)
    flight_time_str = f"~{h}h {m}min" if h else (f"~{m}min" if m else "unknown")

    # ── 3. Market demand (mktSize) ───────────────────────────────────────────
    mkt_row = conn.execute(
        "SELECT weekly_demand FROM workset_mkt WHERE origin=? AND dest=?",
        [origin, dest]
    ).fetchone()
    weekly_demand = _safe_float(mkt_row[0]) if mkt_row else None

    # ── 4. Per-airline performance from SPILLDATA ────────────────────────────
    aln_rows = conn.execute("""
        SELECT
            airline,
            COUNT(*) AS flight_days,
            CAST(AVG(cap_total) AS INTEGER) AS avg_cap,
            SUM(cap_total)                  AS total_weekly_seats,
            ROUND(SUM(mkt_share) * 100, 2)  AS mkt_share_pct,
            ROUND(SUM(spill_pax), 2)        AS total_spill,
            ROUND(SUM(recap_pax), 2)        AS total_recap,
            ROUND(AVG(lf_pax), 3)           AS avg_lf_pax,
            ROUND(AVG(total_lf_pax), 3)     AS avg_total_lf,
            COUNT(DISTINCT dep_time)        AS unique_dep_times,
            STRING_AGG(DISTINCT dep_time, '|' ORDER BY dep_time) AS dep_times_raw
        FROM workset_spill
        WHERE origin=? AND dest=? AND (is_codeshare IS NULL OR is_codeshare=0)
        GROUP BY airline
        ORDER BY mkt_share_pct DESC
    """, [origin, dest]).fetchall()

    # ── 5. Alliance info for these airlines ──────────────────────────────────
    airline_codes = [r[0] for r in aln_rows] if aln_rows else []
    alliances: Dict[str, str] = {}
    if airline_codes:
        placeholders = ",".join(["?"] * len(airline_codes))
        al_rows = conn.execute(
            f"SELECT airline, alliance_name FROM workset_alliance WHERE airline IN ({placeholders})",
            airline_codes,
        ).fetchall()
        for aln, al_name in al_rows:
            if aln not in alliances:
                alliances[aln] = al_name

    # ── 6. Aircraft types per airline (from flights table) ───────────────────
    ac_by_airline: Dict[str, list] = {}
    if airline_codes:
        placeholders = ",".join(["?"] * len(airline_codes))
        ac_rows = conn.execute(
            f"""SELECT airline, STRING_AGG(DISTINCT aircraft_type, ', ' ORDER BY aircraft_type)
                FROM flights WHERE origin=? AND destination=? AND airline IN ({placeholders})
                GROUP BY airline""",
            [origin, dest] + airline_codes,
        ).fetchall()
        for aln, acs in ac_rows:
            ac_by_airline[aln] = [a.strip() for a in (acs or "").split(",") if a.strip()]

    # Departure time strings per airline from flights table
    dep_by_airline: Dict[str, list] = {}
    if airline_codes:
        placeholders = ",".join(["?"] * len(airline_codes))
        dep_rows = conn.execute(
            f"""SELECT airline,
                    STRING_AGG(DISTINCT strftime(departure_local, '%H:%M'), '|' ORDER BY strftime(departure_local, '%H:%M'))
                FROM flights
                WHERE origin=? AND destination=? AND airline IN ({placeholders})
                GROUP BY airline""",
            [origin, dest] + airline_codes,
        ).fetchall()
        for aln, deps in dep_rows:
            dep_by_airline[aln] = sorted(set((deps or "").split("|"))) if deps else []

    # ── 7. Departure time distribution (time-of-day buckets) ────────────────
    bucket_rows = conn.execute("""
        SELECT dep_time, COUNT(DISTINCT airline) AS airlines_count
        FROM workset_spill
        WHERE origin=? AND dest=? AND (is_codeshare IS NULL OR is_codeshare=0)
        GROUP BY dep_time ORDER BY dep_time
    """, [origin, dest]).fetchall()

    buckets: Dict[str, int] = {
        "red_eye (00-06)": 0, "morning (06-12)": 0,
        "afternoon (12-18)": 0, "evening (18-24)": 0,
    }
    for dep_time, _ in bucket_rows:
        b = _time_bucket(dep_time)
        if b in buckets:
            buckets[b] += 1

    # ── 8. Airport dominance from workset_opp ────────────────────────────────
    def _top_airlines_at(airport: str, limit=5):
        rows = conn.execute("""
            SELECT airline, ROUND(mkt_share * 100, 1) AS share_pct
            FROM workset_opp WHERE airport=?
            ORDER BY mkt_share DESC LIMIT ?
        """, [airport, limit]).fetchall()
        return [{"airline": r[0], "share_pct": _safe_float(r[1])} for r in rows]

    origin_top = _top_airlines_at(origin)
    dest_top   = _top_airlines_at(dest)

    # ── 9. Spill summary ─────────────────────────────────────────────────────
    spill_row = conn.execute("""
        SELECT
            ROUND(SUM(spill_pax), 2) AS total_spill,
            COUNT(CASE WHEN spill_pax > 0.05 THEN 1 END) AS flights_with_spill,
            ROUND(MAX(spill_pax), 2) AS max_spill,
            ROUND(SUM(recap_pax), 2) AS total_recap
        FROM workset_spill
        WHERE origin=? AND dest=? AND (is_codeshare IS NULL OR is_codeshare=0)
    """, [origin, dest]).fetchone()

    total_spill  = _safe_float(spill_row[0]) if spill_row else 0.0
    spill_count  = _safe_int(spill_row[1])   if spill_row else 0
    max_spill    = _safe_float(spill_row[2]) if spill_row else 0.0
    total_recap  = _safe_float(spill_row[3]) if spill_row else 0.0
    demand_pressure = (
        "High"   if total_spill > 20 else
        "Moderate" if total_spill > 5 else
        "Low"
    )

    # ── 10. Connecting routes (feed markets via origin / onward from dest) ──────
    try:
        # Carriers that operate both a feed leg INTO origin AND the subject route
        # e.g. for DXB-BOM: airlines that fly X→DXB and also DXB→BOM
        conn_rows = conn.execute("""
            SELECT
                f1.origin          AS feed_origin,
                f1.airline         AS airline,
                COUNT(DISTINCT f1.flight_number) AS weekly_freq
            FROM flights f1
            WHERE f1.destination = ?
              AND f1.origin != ?
              AND f1.airline IN (
                  SELECT DISTINCT airline FROM flights WHERE origin=? AND destination=?
              )
            GROUP BY f1.origin, f1.airline
            ORDER BY weekly_freq DESC
            LIMIT 20
        """, [origin, origin, origin, dest]).fetchall()

        # Onward routes FROM destination operated by same carriers
        onward_rows = conn.execute("""
            SELECT
                f2.destination     AS onward_dest,
                f2.airline         AS airline,
                COUNT(DISTINCT f2.flight_number) AS weekly_freq
            FROM flights f2
            WHERE f2.origin = ?
              AND f2.destination != ?
              AND f2.airline IN (
                  SELECT DISTINCT airline FROM flights WHERE origin=? AND destination=?
              )
            GROUP BY f2.destination, f2.airline
            ORDER BY weekly_freq DESC
            LIMIT 20
        """, [dest, dest, origin, dest]).fetchall()

        connecting_routes_out = []
        for via, al, freq in conn_rows[:15]:
            connecting_routes_out.append({
                "via": via,
                "direction": "feed_into_origin",
                "airline": al,
                "weekly_freq": _safe_int(freq),
            })
        for via, al, freq in onward_rows[:10]:
            connecting_routes_out.append({
                "via": via,
                "direction": "onward_from_dest",
                "airline": al,
                "weekly_freq": _safe_int(freq),
            })
    except Exception:
        connecting_routes_out = []

    # ── 10b. Local vs Flow pax estimation ───────────────────────────────────
    # Estimate based on carrier type: hub FSC carriers carry significant flow pax;
    # LCC and regional carriers are predominantly local O&D.
    FLOW_RATIOS = {
        "Full-service": 0.55,   # typical hub carrier: ~55% flow
        "Low-cost":     0.10,   # LCC: mostly local
        "Regional":     0.15,
        "Charter":      0.05,
        "Ultra low-cost": 0.08,
    }
    total_wk_seats_for_est = sum(_safe_int(r[3]) for r in aln_rows) or 1
    est_local_pax = 0.0
    est_flow_pax  = 0.0
    for row_a in aln_rows:
        aln_code_a = row_a[0]
        wk_seats_a = _safe_int(row_a[3])
        avg_lf_a   = _safe_float(row_a[7]) or 0.75  # default 75% LF if missing
        pax_a = wk_seats_a * avg_lf_a
        flow_ratio = FLOW_RATIOS.get(CARRIER_TYPE.get(aln_code_a, "Full-service"), 0.4)
        est_flow_pax  += pax_a * flow_ratio
        est_local_pax += pax_a * (1 - flow_ratio)
    est_total_pax = est_local_pax + est_flow_pax or 1

    # ── 11. Assemble airline list ─────────────────────────────────────────────
    total_weekly_seats = sum(_safe_int(r[3]) for r in aln_rows)
    airlines_out = []
    for row in aln_rows:
        aln_code, flight_days, avg_cap, wk_seats, mkt_pct, spill, recap, avg_lfp, avg_tfl, n_deps, _ = row
        deps = dep_by_airline.get(aln_code, [])
        airlines_out.append({
            "code":               aln_code,
            "name":               AIRLINE_NAMES.get(aln_code, aln_code),
            "carrier_type":       CARRIER_TYPE.get(aln_code, "Full-service"),
            "alliance":           alliances.get(aln_code, "None / Independent"),
            "weekly_flight_days": _safe_int(flight_days),
            "unique_dep_times":   _safe_int(n_deps),
            "avg_seats_per_flight": _safe_int(avg_cap),
            "weekly_seat_capacity": _safe_int(wk_seats),
            "market_share_pct":   _safe_float(mkt_pct),
            "weekly_spill":       _safe_float(spill),
            "weekly_recap":       _safe_float(recap),
            "demand_pressure":    "High" if _safe_float(spill) > 2 else "Low",
            "aircraft_types":     ac_by_airline.get(aln_code, []),
            "departure_times":    deps[:12],  # cap for readability
        })

    # ── 11. Traveler recommendations ─────────────────────────────────────────
    lcc = [a["name"] for a in airlines_out if a["carrier_type"] == "Low-cost"]
    fsc = [a["name"] for a in airlines_out if a["carrier_type"] == "Full-service"]
    top_airline = airlines_out[0] if airlines_out else None

    # First / last departure from all dep times across airlines
    all_deps = sorted(set(
        d for a in airlines_out for d in a["departure_times"] if d and len(d) == 5
    ))
    first_dep = all_deps[0]  if all_deps else "N/A"
    last_dep  = all_deps[-1] if all_deps else "N/A"

    recommendations = {
        "earliest_departure": first_dep,
        "latest_departure":   last_dep,
        "total_departure_slots": len(all_deps),
        "best_value": (
            f"{', '.join(lcc)} — lower-cost carrier{'s' if len(lcc)>1 else ''} on this route"
            if lcc else "No low-cost carrier currently operates this route"
        ),
        "best_comfort": (
            f"{fsc[0]} — full-service with premium cabin options" if fsc
            else "No full-service carrier operates this route"
        ),
        "most_frequent": (
            f"{top_airline['name']} ({top_airline['code']}) "
            f"— {top_airline['market_share_pct']}% market share"
            if top_airline else "N/A"
        ),
        "booking_note": (
            f"Some flights show demand pressure (spill={max_spill:.1f}). "
            "Book early for best availability, especially on EK."
            if total_spill > 1 else
            "Availability generally good. No significant demand spill detected."
        ),
    }

    # ── 12. Final assembly ────────────────────────────────────────────────────
    return {
        "route":      f"{origin}-{dest}",
        "origin": {
            "code":        origin,
            "city":        orig_info.get("city", origin),
            "country":     orig_info.get("country", ""),
            "utc_offset":  tz_orig,
        },
        "destination": {
            "code":        dest,
            "city":        dest_info.get("city", dest),
            "country":     dest_info.get("country", ""),
            "utc_offset":  tz_dest,
        },
        "timezone": {
            "time_difference":  tz_diff,
            "note": (
                f"{dest_info.get('city', dest)} is "
                f"{tz_diff.lstrip('+')} ahead of {orig_info.get('city', origin)}"
                if tz_diff.startswith("+") and tz_diff != "+0h"
                else (
                    f"{dest_info.get('city', dest)} is "
                    f"{tz_diff.lstrip('-')} behind {orig_info.get('city', origin)}"
                    if tz_diff.startswith("-") else
                    "Same timezone"
                )
            ),
        },
        "route_metrics": {
            "distance_miles":    dist_mi,
            "avg_block_time_min": block_min,
            "flight_time":       flight_time_str,
        },
        "market_demand": {
            "demand_index":    weekly_demand,
            "demand_rating":   _demand_rating(weekly_demand),
            "note": "SABRE demand model index — higher = stronger O&D market",
        },
        "schedule_summary": {
            "airlines_count":          len(airlines_out),
            "total_weekly_flight_days": sum(a["weekly_flight_days"] for a in airlines_out),
            "total_weekly_seat_capacity": total_weekly_seats,
            "departure_time_buckets":  buckets,
        },
        "airlines":     airlines_out,
        "spill_analysis": {
            "total_weekly_spill":    total_spill,
            "flights_with_spill":    spill_count,
            "max_spill_per_flight":  max_spill,
            "total_weekly_recapture": total_recap,
            "demand_pressure":       demand_pressure,
            "local_pax":             round(est_local_pax, 0),
            "flow_pax":              round(est_flow_pax, 0),
            "local_pct":             round(est_local_pax / est_total_pax * 100, 1),
            "flow_pct":              round(est_flow_pax  / est_total_pax * 100, 1),
            "connecting_routes":     connecting_routes_out[:20],
            "interpretation": (
                "Demand exceeds capacity on some flights — market is supply-constrained."
                if total_spill > 5 else
                "Demand generally within capacity — good availability expected."
            ),
        },
        "airport_dominance": {
            "at_origin": {
                "airport": origin,
                "top_airlines": origin_top,
            },
            "at_destination": {
                "airport": dest,
                "top_airlines": dest_top,
            },
        },
        "traveler_recommendations": recommendations,
        "connecting_routes": connecting_routes_out[:20],
        "data_sources": ["SPILLDATA.dat", "BASEDATA.dat", "mktSize.dat", "opp.dat", "alliance.dat"],
    }


def get_airport_overview(airport: str) -> Dict[str, Any]:
    """Return airline market-share breakdown and operational profile for an airport."""
    if not _WORKSET_LOADED:
        return {"error": "Workset data not yet loaded."}

    conn = get_connection()

    # Top airlines at the airport
    top_rows = conn.execute("""
        SELECT airline, ROUND(mkt_share * 100, 2) AS share_pct
        FROM workset_opp WHERE airport=?
        ORDER BY mkt_share DESC LIMIT 15
    """, [airport]).fetchall()

    top_airlines = [
        {
            "code": r[0],
            "name": AIRLINE_NAMES.get(r[0], r[0]),
            "market_share_pct": _safe_float(r[1]),
        }
        for r in top_rows
    ]

    # Routes served from this airport (from flights table)
    route_rows = conn.execute("""
        SELECT destination,
               COUNT(DISTINCT airline) AS airlines,
               COUNT(DISTINCT flight_number) AS unique_flights
        FROM flights WHERE origin=?
        GROUP BY destination
        ORDER BY airlines DESC, unique_flights DESC
        LIMIT 20
    """, [airport]).fetchall()

    routes = [
        {"destination": r[0], "competing_airlines": r[1], "unique_flights": r[2]}
        for r in route_rows
    ]

    info = AIRPORT_INFO.get(airport, {})

    return {
        "airport":       airport,
        "city":          info.get("city", airport),
        "country":       info.get("country", ""),
        "utc_offset":    info.get("utc", "unknown"),
        "top_airlines":  top_airlines,
        "top_routes":    routes,
        "note": "Market-share from SABRE WORKSET simulation model.",
    }
