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
        # Schema upgrade: if fare columns are missing, drop and re-load
        spill_cols = [c[1] for c in conn.execute("PRAGMA table_info(workset_spill)").fetchall()]
        if "fare_HO" not in spill_cols:
            conn.execute("DROP TABLE workset_spill")
            logger.info("workset_spill missing fare columns — dropping for schema upgrade reload")
        else:
            n = conn.execute("SELECT COUNT(*) FROM workset_spill").fetchone()[0]
            if n > 0:
                logger.info(f"  workset_spill: already loaded ({n:,} rows) — skipping reload")
                return
            # Table exists but empty — drop and reload
            conn.execute("DROP TABLE IF EXISTS workset_spill")
            logger.warning("workset_spill was empty — dropping and reloading from SPILLDATA.dat")
    logger.info("Loading SPILLDATA (this may take ~30 s) …")
    # 35 comma-separated fields, no header — market/itinerary level
    # Each row = ONE ITINERARY option for a market (true O&D pair)
    # 4-segment demand model: HO=High-yield Outbound, LO=Low-yield Outbound,
    #                          HR=High-yield Return,   LR=Low-yield Return
    # col[0]  = market_origin  (true passenger origin)
    # col[1]  = market_dest    (true passenger destination)
    # col[2]  = dep_time (first leg departure HHMM)
    # col[3]  = day_of_week (0=Sun, 1=Mon, 2=Tue, 3=Wed, 4=Thu, 5=Fri, 6=Sat — 0-based SUNDAY-FIRST)
    # col[4-7]= fare indices HO/LO/HR/LR (relative indices, NOT revenue)
    # col[8-11] = demand by yield segment: dmd_HO, dmd_LO, dmd_HR, dmd_LR
    # col[12-15]= spill by yield segment:  spill_HO, spill_LO, spill_HR, spill_LR
    # col[16-19]= traffic/pax by segment:  traffic_HO, traffic_LO, traffic_HR, traffic_LR
    # col[20] = jet_type (N=narrow, W=wide, R=regional)
    # col[21] = block_time (total itinerary elapsed time, min)
    # col[22] = stops (0=nonstop, 1=one-stop/two-legs, 2=two-stop/three-legs)
    # col[23] = mkt_share (market share fraction, PM logit model output)
    # col[24] = airline (dominant/marketing carrier)
    # col[25] = is_codeshare (interline/codeshare flag: 0=operating, 1=codeshare)
    # col[26] = baseIndex_l1  (BASEDATA record_id of leg 1)
    # col[27] = baseIndex_l2  (BASEDATA record_id of leg 2, empty if nonstop)
    # col[28] = baseIndex_l3  (BASEDATA record_id of leg 3, empty if ≤1-stop)
    # COMPUTED: total_demand = sum of 4 demand segments (dmd_HO+dmd_LO+dmd_HR+dmd_LR)
    #           total_pax    = sum of 4 traffic segments (traffic_HO+..+traffic_LR)
    #           total_spill  = sum of 4 spill segments   (spill_HO+..+spill_LR)
    # NOTE: Revenue is NOT available in SPILLDATA — fare columns are relative indices only.
    conn.execute(f"""
        CREATE TABLE workset_spill AS
        SELECT
            column00                                           AS market_origin,
            column01                                           AS market_dest,
            column02                                           AS dep_time,
            TRY_CAST(column03  AS INTEGER)                     AS day_of_week,
            -- Fares by yield segment (used to compute revenue)
            TRY_CAST(column04  AS FLOAT)                       AS fare_HO,
            TRY_CAST(column05  AS FLOAT)                       AS fare_LO,
            TRY_CAST(column06  AS FLOAT)                       AS fare_HR,
            TRY_CAST(column07  AS FLOAT)                       AS fare_LR,
            -- Demand by yield segment
            TRY_CAST(column08  AS FLOAT)                       AS dmd_HO,
            TRY_CAST(column09  AS FLOAT)                       AS dmd_LO,
            TRY_CAST(column10  AS FLOAT)                       AS dmd_HR,
            TRY_CAST(column11  AS FLOAT)                       AS dmd_LR,
            -- Spill by yield segment
            TRY_CAST(column12  AS FLOAT)                       AS spill_HO,
            TRY_CAST(column13  AS FLOAT)                       AS spill_LO,
            TRY_CAST(column14  AS FLOAT)                       AS spill_HR,
            TRY_CAST(column15  AS FLOAT)                       AS spill_LR,
            -- Traffic (booked pax) by yield segment
            TRY_CAST(column16  AS FLOAT)                       AS traffic_HO,
            TRY_CAST(column17  AS FLOAT)                       AS traffic_LO,
            TRY_CAST(column18  AS FLOAT)                       AS traffic_HR,
            TRY_CAST(column19  AS FLOAT)                       AS traffic_LR,
            -- Computed totals across all yield segments
            COALESCE(TRY_CAST(column08 AS FLOAT),0) + COALESCE(TRY_CAST(column09 AS FLOAT),0) +
            COALESCE(TRY_CAST(column10 AS FLOAT),0) + COALESCE(TRY_CAST(column11 AS FLOAT),0)
                                                               AS total_demand,
            COALESCE(TRY_CAST(column16 AS FLOAT),0) + COALESCE(TRY_CAST(column17 AS FLOAT),0) +
            COALESCE(TRY_CAST(column18 AS FLOAT),0) + COALESCE(TRY_CAST(column19 AS FLOAT),0)
                                                               AS total_pax,
            COALESCE(TRY_CAST(column12 AS FLOAT),0) + COALESCE(TRY_CAST(column13 AS FLOAT),0) +
            COALESCE(TRY_CAST(column14 AS FLOAT),0) + COALESCE(TRY_CAST(column15 AS FLOAT),0)
                                                               AS total_spill,
            -- Revenue = traffic × fare per yield segment (prorated allocation for multi-leg itineraries)
            COALESCE(TRY_CAST(column16 AS FLOAT),0) * COALESCE(TRY_CAST(column04 AS FLOAT),0) +
            COALESCE(TRY_CAST(column17 AS FLOAT),0) * COALESCE(TRY_CAST(column05 AS FLOAT),0) +
            COALESCE(TRY_CAST(column18 AS FLOAT),0) * COALESCE(TRY_CAST(column06 AS FLOAT),0) +
            COALESCE(TRY_CAST(column19 AS FLOAT),0) * COALESCE(TRY_CAST(column07 AS FLOAT),0)
                                                               AS total_revenue,
            column20                                           AS jet_type,
            TRY_CAST(column21  AS INTEGER)                     AS block_time,
            TRY_CAST(column22  AS INTEGER)                     AS stops,
            TRY_CAST(column23  AS FLOAT)                       AS mkt_share,
            column24                                           AS airline,
            TRY_CAST(column25  AS INTEGER)                     AS is_codeshare,
            TRY_CAST(column26  AS BIGINT)                      AS baseIndex_l1,
            TRY_CAST(column27  AS BIGINT)                      AS baseIndex_l2,
            TRY_CAST(column28  AS BIGINT)                      AS baseIndex_l3
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
        n = conn.execute("SELECT COUNT(*) FROM workset_base").fetchone()[0]
        if n > 0:
            logger.info(f"  workset_base: already loaded ({n:,} rows) — skipping reload")
            return
        # Table exists but empty (crashed mid-load) — drop and reload
        conn.execute("DROP TABLE IF EXISTS workset_base")
        logger.warning("workset_base was empty — dropping and reloading from BASEDATA.dat")
    logger.info("Loading BASEDATA (this may take ~15 s) …")
    # 24 comma-separated fields, no header — verified against WORKSET204 and filesColumnsSegmsPM.yaml
    # col[0]  = record_id   (baseIndex — unique leg identifier)
    # col[1]  = origin      (leg departure airport)
    # col[2]  = dest        (leg arrival airport)
    # col[3]  = flt_num
    # col[4]  = dep_time, col[5]=arr_time, col[6]=block_time_min, col[7]=distance_mi
    # col[8]  = mkt_airline (aln — MARKETING/ticket-issuing carrier)
    # col[9]  = aircraft_type (subfleet)
    # col[10] = apm_cap     (seat capacity)
    # col[11] = apm_dmd     (DEMAND — model-predicted demand per departure)  ← col[12] in old wrong code
    # col[12] = apm_pax     (TRAFFIC — predicted pax on board per departure) ← col[11] in old wrong code
    # col[13] = apm_lpax    (local pax — journey = this single leg)
    # col[14] = apm_spill   (spilled pax)
    # col[15] = day_of_week (0=Sun, 1=Mon, 2=Tue, 3=Wed, 4=Thu, 5=Fri, 6=Sat — 0-based SUNDAY-FIRST)
    # col[16] = mkt_ind     (dedup flag: 0-1=primary operating row, 2+=codeshare/thru duplicate)
    # col[17] = acf_own     (aircraft owner — not loaded)
    # col[18] = op_airline  (op_aln — OPERATING carrier, physically flies the aircraft)
    # col[19] = op_flt_num  (not loaded)
    # col[20] = dept_offset (UTC timezone offset at departure airport, minutes)
    # col[21] = arrv_offset (UTC timezone offset at arrival airport, minutes)
    # col[22] = restr_leg, col[23] = traf_rest_str
    # ⚠ DEDUP RULE: mkt_ind <= 1 = primary row (include); mkt_ind > 1 = codeshare/thru duplicate (exclude)
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
            column08                        AS mkt_airline,
            column09                        AS aircraft_type,
            TRY_CAST(column10 AS INTEGER)   AS apm_cap,
            TRY_CAST(column11 AS FLOAT)     AS apm_dmd,
            TRY_CAST(column12 AS FLOAT)     AS apm_pax,
            TRY_CAST(column13 AS FLOAT)     AS apm_lpax,
            TRY_CAST(column14 AS FLOAT)     AS apm_spill,
            TRY_CAST(column15 AS INTEGER)   AS day_of_week,
            TRY_CAST(column16 AS INTEGER)   AS mkt_ind,
            column18                        AS op_airline,
            TRY_CAST(column20 AS INTEGER)   AS dept_offset,
            TRY_CAST(column21 AS INTEGER)   AS arrv_offset
        FROM read_csv('{_csv_path(path)}',
                      header=false, delim=',', null_padding=true,
                      all_varchar=true, parallel=true)
        WHERE column01 IS NOT NULL
          AND LENGTH(TRIM(column01)) = 3
          AND LENGTH(TRIM(column02)) = 3
          AND TRY_CAST(column16 AS INTEGER) <= 1
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
        _load_dashboard_outputs(conn, wd / "dashboard_output")
        _WORKSET_LOADED = True
        logger.info("Workset reference data ready.")

        # Build the workset knowledge graph (LEG/MARKET/ITINERARY nodes + flow edges).
        # Read the DataFrames HERE in the main thread (DuckDB is not thread-safe).
        # The background thread receives already-loaded DataFrames and never touches DuckDB.
        import threading
        from app.knowledge_graph.workset_graph_builder import (
            init_workset_graph,
            _load_basedata as _wkg_load_base,
            _load_spilldata as _wkg_load_spill,
        )
        basedata_df  = _wkg_load_base()
        spilldata_df = _wkg_load_spill()
        logger.info(
            f"Workset KG DataFrames pre-loaded: "
            f"{len(basedata_df):,} base legs, {len(spilldata_df):,} spill rows"
        )
        t = threading.Thread(
            target=init_workset_graph,
            kwargs={"basedata": basedata_df, "spilldata": spilldata_df},
            name="workset-kg-build",
            daemon=True,
        )
        t.start()
        logger.info("Workset KG build started in background thread.")

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
# Dashboard output CSV loader
# ─────────────────────────────────────────────────────────────────────────────

_DASHBOARD_LOADED = False

_DASHBOARD_TABLES = [
    ("dm_itin_report",    "itinerary_report_summary.csv"),
    ("dm_flight_report",  "flight_report_summary.csv"),
    ("dm_market_summary", "level2_od_airline_share_summary.csv"),
    ("dm_network_summary","level1_host_od_summary.csv"),
    ("dm_market_carrier", "market_carrier_summary.csv"),
]


def _parse_host_airline(workset_dir: Path, conn=None) -> str:
    """
    Detect the true host-airline IATA code from analysis.dat.

    Strategy:
      1. Extract the 2-letter prefix of the Analysis Name  (e.g. "FZW24_RS31" → "FZ").
         This is what the workset was *named* after and is the display-facing IATA code.
      2. Also read the HOSTALN= field (may be an internal surrogate, e.g. "S5" for FZ).
      3. If both candidates differ *and* a DB connection is available, compare their
         row counts in workset_base and prefer the one with more data — the real host's
         own itineraries dominate that table.
      4. Fall back gracefully to whichever candidate is non-empty.
    """
    try:
        analysis = workset_dir / "data" / "analysis.dat"
        if not analysis.exists():
            return ""
        hostaln = ""
        analysis_prefix = ""
        for line in analysis.read_text(errors="ignore").splitlines():
            s = line.strip()
            if s.startswith("Analysis Name:"):
                # "Analysis Name: FZW24_RS31 Schedule Name: FZW24_HOST_RS ..."
                parts = s.split()
                if len(parts) >= 3:
                    name = parts[2]           # e.g. "FZW24_RS31"
                    if len(name) >= 2 and name[:2].isalpha():
                        analysis_prefix = name[:2].upper()
            elif s.startswith("HOSTALN="):
                hostaln = s.split("=", 1)[1].strip()
            if analysis_prefix and hostaln:
                break                         # found both — no need to read further

        # If they agree, or only one is present, return immediately
        if not analysis_prefix:
            return hostaln
        if not hostaln or analysis_prefix == hostaln:
            return analysis_prefix

        # They differ (e.g. prefix="FZ", hostaln="S5") — use BASEDATA to decide
        if conn is not None:
            try:
                rows = conn.execute(
                    "SELECT UPPER(TRIM(mkt_airline)) AS aln, COUNT(*) AS c "
                    "FROM workset_base WHERE UPPER(TRIM(mkt_airline)) IN (?, ?) "
                    "GROUP BY 1",
                    [analysis_prefix, hostaln],
                ).fetchall()
                counts = {r[0]: r[1] for r in rows}
                prefix_cnt = counts.get(analysis_prefix, 0)
                hostaln_cnt = counts.get(hostaln, 0)
                logger.info(
                    f"Host detection: analysis_prefix={analysis_prefix}({prefix_cnt} rows) "
                    f"hostaln={hostaln}({hostaln_cnt} rows) → "
                    f"{'analysis_prefix' if prefix_cnt >= hostaln_cnt else 'hostaln'} wins"
                )
                return analysis_prefix if prefix_cnt >= hostaln_cnt else hostaln
            except Exception as exc:
                logger.debug(f"Host detection DB query failed: {exc}")

        # Default: trust the analysis name prefix (more stable display identifier)
        return analysis_prefix
    except Exception:
        return ""


def _generate_dashboard_from_raw(conn, workset_dir: Path) -> None:
    """Generate dm_flight_report and dm_network_summary from workset_base when dashboard CSVs are absent."""
    if not _table_exists(conn, "workset_base"):
        return

    host = _parse_host_airline(workset_dir, conn)

    # ── Flight report ──────────────────────────────────────────────────────────
    if not _table_exists(conn, "dm_flight_report"):
        try:
            has_spill = _table_exists(conn, "workset_spill")
            # Pre-aggregate revenue from workset_spill per record_id (only if spill has fare columns)
            if has_spill:
                spill_cols = [c[1] for c in conn.execute("PRAGMA table_info(workset_spill)").fetchall()]
                has_fare = "fare_HO" in spill_cols and "total_revenue" in spill_cols
            else:
                has_fare = False

            if has_fare:
                rev_cte = """
                    leg_rev AS (
                        SELECT baseIndex_l1 AS record_id, SUM(total_revenue) AS rev
                        FROM workset_spill
                        WHERE is_codeshare = 0
                        GROUP BY baseIndex_l1
                    )
                """
                rev_join = "LEFT JOIN leg_rev lr ON workset_base.record_id = lr.record_id"
                rev_col = "CAST(ROUND(SUM(COALESCE(lr.rev, 0))) AS VARCHAR)"
                yield_col = """CASE
                    WHEN SUM(apm_pax) > 0 AND MAX(distance_mi) > 0
                    THEN CAST(ROUND(SUM(COALESCE(lr.rev, 0)) / NULLIF(SUM(apm_pax) * MAX(distance_mi) * 1.60934, 0) * 100, 2) AS VARCHAR)
                    ELSE '' END"""
            else:
                rev_cte = ""
                rev_join = ""
                rev_col = "''"
                yield_col = "''"

            cte_prefix = f"WITH {rev_cte}" if rev_cte.strip() else ""

            conn.execute(f"""
                CREATE TABLE dm_flight_report AS
                {cte_prefix}
                SELECT
                    UPPER(TRIM(origin))       AS "Dept Sta",
                    UPPER(TRIM(dest))         AS "Arvl Sta",
                    UPPER(TRIM(mkt_airline)) || '  ' || TRIM(flight_num) AS "Flt Desg",
                    -- day_of_week: 0=Sun,1=Mon,2=Tue,3=Wed,4=Thu,5=Fri,6=Sat (SUNDAY-FIRST)
                    -- IATA freq: position 1=Mon..7=Sun → map dow 1→'1',2→'2',...,6→'6',0→'7'
                    CASE WHEN MAX(CASE WHEN day_of_week=1 THEN 1 ELSE 0 END)=1 THEN '1' ELSE '.' END ||
                    CASE WHEN MAX(CASE WHEN day_of_week=2 THEN 1 ELSE 0 END)=1 THEN '2' ELSE '.' END ||
                    CASE WHEN MAX(CASE WHEN day_of_week=3 THEN 1 ELSE 0 END)=1 THEN '3' ELSE '.' END ||
                    CASE WHEN MAX(CASE WHEN day_of_week=4 THEN 1 ELSE 0 END)=1 THEN '4' ELSE '.' END ||
                    CASE WHEN MAX(CASE WHEN day_of_week=5 THEN 1 ELSE 0 END)=1 THEN '5' ELSE '.' END ||
                    CASE WHEN MAX(CASE WHEN day_of_week=6 THEN 1 ELSE 0 END)=1 THEN '6' ELSE '.' END ||
                    CASE WHEN MAX(CASE WHEN day_of_week=0 THEN 1 ELSE 0 END)=1 THEN '7' ELSE '.' END
                                              AS "Freq",
                    MAX(dep_time)             AS "Dept Time",
                    MAX(arr_time)             AS "Arvl Time",
                    LPAD(CAST(MAX(TRY_CAST(block_time AS BIGINT)) // 60 AS VARCHAR),2,'0') || ':' ||
                    LPAD(CAST(MAX(TRY_CAST(block_time AS BIGINT)) % 60 AS VARCHAR),2,'0') AS "Elap Time",
                    -- Stops = 0 for all single-leg records in workset_base
                    0                                                   AS "Stops",
                    -- Subfleet = "AIRLINE TYPE" e.g. "FZ 7M8R"
                    UPPER(TRIM(mkt_airline)) || ' ' || TRIM(aircraft_type) AS "Subfleet",
                    -- Weekly totals (SUM over all operating days in the week)
                    CAST(SUM(apm_cap) AS VARCHAR)                      AS "Seats",
                    CAST(ROUND(MAX(distance_mi) * 1.60934) AS VARCHAR) AS "Distance(km)",
                    CAST(ROUND(SUM(apm_dmd), 1) AS VARCHAR) AS "Total Demand",
                    CAST(ROUND(SUM(apm_pax), 1) AS VARCHAR) AS "Total Traffic",
                    -- Lcl Demand ≈ lpax × (dmd/pax) — always ≥ Lcl Traffic (logical floor)
                    CAST(ROUND(SUM(
                        GREATEST(apm_lpax,
                            CASE WHEN apm_pax > 0 THEN apm_lpax * apm_dmd / apm_pax ELSE apm_lpax END)
                    ), 1) AS VARCHAR) AS "Lcl Demand (Mktd)",
                    -- Lcl Traffic = local pax who actually boarded (weekly total)
                    CAST(ROUND(SUM(apm_lpax), 1) AS VARCHAR) AS "Lcl Traffic",
                    -- LF = weekly pax / weekly seats × 100
                    CAST(ROUND(
                        LEAST(100.0,
                            SUM(apm_pax) / NULLIF(SUM(CAST(apm_cap AS FLOAT)), 0) * 100
                        )
                    , 1) AS VARCHAR)          AS "Load Factor (%)",
                    -- Pax Revenue = sum(traffic × fare) from workset_spill, aggregated per leg
                    {rev_col}                 AS "Pax Revenue($)",
                    {yield_col}               AS "Total Yield(Cents per RPk)",
                    'Y'                       AS "Op/Nonop Flight",
                    ''                        AS "Applied A/C Config"
                FROM workset_base
                {rev_join}
                WHERE mkt_airline IS NOT NULL AND LENGTH(TRIM(mkt_airline)) > 0
                GROUP BY origin, dest, mkt_airline, flight_num, aircraft_type
                ORDER BY origin, dest, mkt_airline, flight_num
            """)
            n = conn.execute("SELECT COUNT(*) FROM dm_flight_report").fetchone()[0]
            logger.info(f"  dm_flight_report (generated): {n:,} rows")
        except Exception as exc:
            logger.error(f"Failed to generate dm_flight_report: {exc}")

    # ── Network summary (host OD pairs) ───────────────────────────────────────
    if not _table_exists(conn, "dm_network_summary"):
        try:
            host_filter = f"AND UPPER(TRIM(mkt_airline)) = '{host}'" if host else ""
            conn.execute(f"""
                CREATE TABLE dm_network_summary AS
                SELECT
                    UPPER(TRIM(origin))  AS orig,
                    UPPER(TRIM(dest))    AS dest,
                    COUNT(DISTINCT flight_num || dep_time) AS weekly_departures,
                    -- Weekly totals: SUM across all day rows (each row = 1 departure)
                    CAST(ROUND(SUM(apm_pax),  0) AS VARCHAR) AS weekly_pax_est,
                    CAST(ROUND(SUM(apm_lpax), 0) AS VARCHAR) AS apm_weekly_pax_est,
                    CAST(ROUND(SUM(apm_dmd),  0) AS VARCHAR) AS market_weekly_demand,
                    -- Host share: host pax / total demand (as %)
                    CAST(ROUND(
                        LEAST(100.0, SUM(apm_pax) / NULLIF(SUM(apm_dmd), 0) * 100)
                    , 1) AS VARCHAR)           AS host_share_of_market_demand_pct_est,
                    -- Load factor: aggregate pax/cap (not row-by-row average)
                    CAST(ROUND(
                        LEAST(100.0, SUM(apm_pax) / NULLIF(SUM(CAST(apm_cap AS FLOAT)), 0) * 100)
                    , 1) AS VARCHAR)           AS load_factor_pct_est,
                    CAST(ROUND(
                        LEAST(100.0, SUM(apm_lpax) / NULLIF(SUM(CAST(apm_cap AS FLOAT)), 0) * 100)
                    , 1) AS VARCHAR)           AS apm_load_factor_pct_est,
                    -- Flow pax % of total pax (cap local at pax to avoid negative flow)
                    CAST(ROUND(
                        GREATEST(0.0, LEAST(100.0,
                            (SUM(apm_pax) - SUM(LEAST(apm_lpax, apm_pax))) / NULLIF(SUM(apm_pax), 0) * 100
                        ))
                    , 1) AS VARCHAR)           AS flow_pdd_pct_est,
                    ''                         AS abs_total_pax_diff_pct_est,
                    ''                         AS abs_plf_diff_pct_est
                FROM workset_base
                WHERE mkt_airline IS NOT NULL AND LENGTH(TRIM(mkt_airline)) > 0
                {host_filter}
                GROUP BY origin, dest
                ORDER BY SUM(apm_pax) DESC
            """)
            n = conn.execute("SELECT COUNT(*) FROM dm_network_summary").fetchone()[0]
            logger.info(f"  dm_network_summary (generated): {n:,} rows  host={host or 'all'}")
        except Exception as exc:
            logger.error(f"Failed to generate dm_network_summary: {exc}")

    # ── Market summary (O&D level, per airline) ───────────────────────────────
    if not _table_exists(conn, "dm_market_summary") and _table_exists(conn, "workset_spill"):
        try:
            host_case = f"CASE WHEN UPPER(TRIM(carrier)) = '{host}' THEN 'True' ELSE 'False' END" if host else "'False'"
            conn.execute(f"""
                CREATE TABLE dm_market_summary AS
                WITH airline_stats AS (
                    SELECT
                        UPPER(TRIM(market_origin)) AS orig,
                        UPPER(TRIM(market_dest))   AS dest,
                        UPPER(TRIM(airline))       AS carrier,
                        -- Count distinct nonstop vs connecting itinerary options
                        COUNT(DISTINCT CASE WHEN stops = 0 THEN baseIndex_l1 END)
                            AS nonstop_itinerary_count,
                        COUNT(DISTINCT CASE WHEN stops > 0 THEN baseIndex_l1 END)
                            AS single_connect_itinerary_count,
                        -- Per-departure averages across operating days
                        ROUND(SUM(total_demand + COALESCE(total_spill, 0))
                              / NULLIF(COUNT(DISTINCT day_of_week), 0), 1) AS total_demand_est,
                        ROUND(SUM(total_pax)
                              / NULLIF(COUNT(DISTINCT day_of_week), 0), 1) AS total_traffic_est,
                        ROUND(SUM(total_revenue)
                              / NULLIF(COUNT(DISTINCT day_of_week), 0), 1) AS total_revenue_est
                    FROM workset_spill
                    WHERE airline IS NOT NULL AND LENGTH(TRIM(airline)) >= 2
                      AND is_codeshare = 0
                    GROUP BY market_origin, market_dest, airline
                ),
                market_totals AS (
                    SELECT orig, dest,
                           SUM(total_demand_est)  AS mkt_dmd,
                           SUM(total_traffic_est) AS mkt_pax,
                           SUM(total_revenue_est) AS mkt_rev
                    FROM airline_stats
                    GROUP BY orig, dest
                )
                SELECT
                    a.orig, a.dest, a.carrier,
                    {host_case}                                                         AS is_host_airline,
                    CAST(a.nonstop_itinerary_count        AS VARCHAR)                  AS nonstop_itinerary_count,
                    CAST(a.single_connect_itinerary_count AS VARCHAR)                  AS single_connect_itinerary_count,
                    a.total_demand_est,
                    ROUND(a.total_demand_est  / NULLIF(m.mkt_dmd, 0) * 100, 1)        AS demand_share_pct_est,
                    a.total_traffic_est,
                    ROUND(a.total_traffic_est / NULLIF(m.mkt_pax, 0) * 100, 1)        AS traffic_share_pct_est,
                    a.total_revenue_est,
                    ROUND(a.total_revenue_est / NULLIF(m.mkt_rev, 0) * 100, 1)        AS revenue_share_pct_est
                FROM airline_stats a
                JOIN market_totals m ON a.orig = m.orig AND a.dest = m.dest
                ORDER BY a.orig, a.dest, a.total_traffic_est DESC
            """)
            n = conn.execute("SELECT COUNT(*) FROM dm_market_summary").fetchone()[0]
            logger.info(f"  dm_market_summary (generated): {n:,} rows")
        except Exception as exc:
            logger.error(f"Failed to generate dm_market_summary: {exc}")

    # ── dm_itin_report is generated on-demand per OD in get_itin_report() ─────
    # (Not pre-built at startup: too slow on large worksets with 2M+ spill rows)

    profile_path = workset_dir / "dashboard_output" / "workset_profile.json"
    if host:
        try:
            import json as _json
            profile_path.parent.mkdir(parents=True, exist_ok=True)
            profile_path.write_text(_json.dumps({
                "host_airline": host,
                "workset_id": workset_dir.name,
                "generated_from_raw": True
            }))
            logger.info(f"  workset_profile.json written  host={host}")
        except Exception as exc:
            logger.warning(f"Could not write workset_profile.json: {exc}")


def _load_dashboard_outputs(conn, dashboard_dir: Path) -> None:
    global _DASHBOARD_LOADED
    if _DASHBOARD_LOADED:
        return
    for tbl, fname in _DASHBOARD_TABLES:
        csv_p = dashboard_dir / fname
        if not csv_p.exists():
            logger.warning(f"Dashboard CSV missing: {csv_p}")
            continue
        if _table_exists(conn, tbl):
            continue
        try:
            conn.execute(f"""
                CREATE TABLE {tbl} AS
                SELECT * FROM read_csv('{_csv_path(csv_p)}',
                             header=true, delim=',',
                             null_padding=true, all_varchar=true)
            """)
            n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            logger.info(f"  {tbl}: {n:,} rows")
        except Exception as exc:
            logger.error(f"Failed to load {tbl}: {exc}")
    # Fall back to generating from raw workset data when CSVs are absent
    _generate_dashboard_from_raw(conn, dashboard_dir.parent)
    _DASHBOARD_LOADED = True


def init_dashboard():
    """Load dashboard CSV outputs into DuckDB. Idempotent."""
    wd = _get_workset_dir()
    conn = get_connection()
    _load_dashboard_outputs(conn, wd / "dashboard_output")


def rebuild_dm_tables() -> dict:
    """Drop and regenerate all dm_* tables from workset_base/workset_spill.
    Use after schema fixes to force fresh regeneration without full restart."""
    global _DASHBOARD_LOADED
    conn = get_connection()
    dropped = []
    for tbl in ("dm_flight_report", "dm_network_summary", "dm_market_summary"):
        if _table_exists(conn, tbl):
            try:
                conn.execute(f"DROP TABLE {tbl}")
                dropped.append(tbl)
                logger.info(f"Dropped {tbl} for rebuild")
            except Exception as exc:
                logger.warning(f"Could not drop {tbl}: {exc}")
    _DASHBOARD_LOADED = False
    wd = _get_workset_dir()
    _generate_dashboard_from_raw(conn, wd)
    _DASHBOARD_LOADED = True
    rebuilt = []
    errors = []
    for tbl in ("dm_flight_report", "dm_network_summary", "dm_market_summary"):
        if _table_exists(conn, tbl):
            n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            rebuilt.append({"table": tbl, "rows": n})
        else:
            try:
                conn.execute(f"SELECT 1 FROM {tbl} LIMIT 1")
            except Exception as exc:
                errors.append(f"{tbl}: {exc}")
            rebuilt.append({"table": tbl, "rows": None, "status": "not_created"})
    result: dict = {"dropped": dropped, "rebuilt": rebuilt}
    if errors:
        result["errors"] = errors
    return result


def dashboard_is_ready() -> bool:
    conn = get_connection()
    return _table_exists(conn, "dm_flight_report")


def get_dashboard_profile() -> dict:
    """Return workset profile JSON if available."""
    wd = _get_workset_dir()
    profile_path = wd / "dashboard_output" / "workset_profile.json"
    if not profile_path.exists():
        return {}
    import json
    try:
        return json.loads(profile_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_workset_hints() -> dict:
    """
    Query live workset tables to extract contextual hints:
    top airports, top routes, top O&D pairs, and top competing airlines.
    Used to make personas and suggestion chips workset-aware.
    """
    conn = get_connection()
    hints: dict = {
        "top_airports": [],
        "top_routes": [],
        "top_ods": [],
        "top_competitors": [],
    }

    # ── Top airports by flight count ─────────────────────────────────────────
    if _table_exists(conn, "dm_flight_report"):
        try:
            profile = get_dashboard_profile()
            host = (profile.get("host_airline") or "").upper()

            rows = conn.execute(
                'SELECT "Dept Sta", COUNT(*) AS cnt FROM dm_flight_report '
                'GROUP BY "Dept Sta" ORDER BY cnt DESC LIMIT 5'
            ).fetchall()
            hints["top_airports"] = [r[0] for r in rows if r[0]]

            # Top routes (dept→arvl) by flight count
            route_rows = conn.execute(
                'SELECT "Dept Sta", "Arvl Sta", COUNT(*) AS cnt FROM dm_flight_report '
                'GROUP BY "Dept Sta","Arvl Sta" ORDER BY cnt DESC LIMIT 6'
            ).fetchall()
            hints["top_routes"] = [f"{r[0]}-{r[1]}" for r in route_rows if r[0] and r[1]]

            # Top O&D pairs (alphabetically normalised to avoid duplicates)
            od_rows = conn.execute(
                'SELECT "Dept Sta", "Arvl Sta", COUNT(*) AS cnt FROM dm_flight_report '
                'GROUP BY "Dept Sta","Arvl Sta" ORDER BY cnt DESC LIMIT 3'
            ).fetchall()
            hints["top_ods"] = [f"{r[0]}-{r[1]}" for r in od_rows if r[0] and r[1]]

            # Top competing airlines (excluding host) by flight count
            comp_rows = conn.execute(
                'SELECT SUBSTR("Flt Desg", 1, 2) AS al, COUNT(*) AS cnt FROM dm_flight_report '
                'WHERE SUBSTR("Flt Desg", 1, 2) != ? '
                'GROUP BY al ORDER BY cnt DESC LIMIT 4',
                [host]
            ).fetchall()
            hints["top_competitors"] = [r[0] for r in comp_rows if r[0]]
        except Exception as exc:
            logger.debug(f"get_workset_hints flight_report query failed: {exc}")

    # ── Top O&D pairs by demand (from network summary) ───────────────────────
    if _table_exists(conn, "dm_network_summary") and not hints["top_ods"]:
        try:
            nd_rows = conn.execute(
                "SELECT orig, dest FROM dm_network_summary "
                "ORDER BY TRY_CAST(market_weekly_demand AS FLOAT) DESC NULLS LAST LIMIT 3"
            ).fetchall()
            hints["top_ods"] = [f"{r[0]}-{r[1]}" for r in nd_rows if r[0] and r[1]]
        except Exception as exc:
            logger.debug(f"get_workset_hints network_summary query failed: {exc}")

    return hints


def _rows_from_table(tbl: str, conditions: list, params: list) -> list:
    conn = get_connection()
    if not _table_exists(conn, tbl):
        return []
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    try:
        rows = conn.execute(f"SELECT * FROM {tbl} {where}", params).fetchall()
        cols = [d[0] for d in conn.description]
        return [dict(zip(cols, r)) for r in rows]
    except Exception as exc:
        logger.warning(f"Query {tbl} failed: {exc}")
        return []


def get_network_summary(top_n: int = 200) -> list:
    conn = get_connection()
    if not _table_exists(conn, "dm_network_summary"):
        return []
    try:
        rows = conn.execute(
            f"SELECT * FROM dm_network_summary ORDER BY TRY_CAST(market_weekly_demand AS FLOAT) DESC NULLS LAST LIMIT ?",
            [top_n]
        ).fetchall()
        cols = [d[0] for d in conn.description]
        return [dict(zip(cols, r)) for r in rows]
    except Exception as exc:
        logger.warning(f"get_network_summary failed: {exc}")
        return []


def get_flight_report(orig: str = "", dest: str = "", carrier: str = "", top_n: int = 500) -> list:
    conn = get_connection()
    if not _table_exists(conn, "dm_flight_report"):
        return []
    conds, params = [], []
    if orig:
        conds.append('"Dept Sta" = ?'); params.append(orig.upper())
    if dest:
        conds.append('"Arvl Sta" = ?'); params.append(dest.upper())
    if carrier:
        conds.append('"Flt Desg" LIKE ?'); params.append(f"{carrier.upper()}%")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    try:
        rows = conn.execute(
            f'SELECT * FROM dm_flight_report {where} ORDER BY "Dept Sta","Arvl Sta","Flt Desg" LIMIT ?',
            params + [top_n]
        ).fetchall()
        cols = [d[0] for d in conn.description]
        return [dict(zip(cols, r)) for r in rows]
    except Exception as exc:
        logger.warning(f"get_flight_report failed: {exc}")
        return []


def get_flight_count(orig: str = "", dest: str = "", carrier: str = "") -> int:
    """Return total number of rows in dm_flight_report matching the filters (no LIMIT)."""
    conn = get_connection()
    if not _table_exists(conn, "dm_flight_report"):
        return 0
    conds, params = [], []
    if orig:
        conds.append('"Dept Sta" = ?'); params.append(orig.upper())
    if dest:
        conds.append('"Arvl Sta" = ?'); params.append(dest.upper())
    if carrier:
        conds.append('"Flt Desg" LIKE ?'); params.append(f"{carrier.upper()}%")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    try:
        row = conn.execute(f'SELECT COUNT(*) FROM dm_flight_report {where}', params).fetchone()
        return int(row[0]) if row else 0
    except Exception as exc:
        logger.warning(f"get_flight_count failed: {exc}")
        return 0


def get_flight_kpis(orig: str = "", dest: str = "", carrier: str = "") -> dict:
    """
    Return pre-aggregated KPIs from ALL rows in dm_flight_report matching the filters.
    Uses SQL SUM/AVG/COUNT — never touches the top_n cap, so numbers are always correct.
    Also returns the top 6 routes by total demand.
    """
    conn = get_connection()
    if not _table_exists(conn, "dm_flight_report"):
        return {}
    conds, params = [], []
    if orig:
        conds.append('"Dept Sta" = ?'); params.append(orig.upper())
    if dest:
        conds.append('"Arvl Sta" = ?'); params.append(dest.upper())
    if carrier:
        conds.append('"Flt Desg" LIKE ?'); params.append(f"{carrier.upper()}%")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    try:
        agg = conn.execute(
            f'''SELECT
                  COUNT(*)                                          AS total_flights,
                  COALESCE(SUM(TRY_CAST("Seats" AS BIGINT)), 0)    AS total_seats,
                  COALESCE(SUM(TRY_CAST("Total Demand" AS DOUBLE)), 0) AS total_demand,
                  COALESCE(SUM(TRY_CAST("Pax Revenue($)" AS DOUBLE)), 0) AS total_revenue,
                  COALESCE(AVG(TRY_CAST("Load Factor (%)" AS DOUBLE)), 0) AS avg_lf,
                  COUNT(DISTINCT "Dept Sta" || \'-\' || "Arvl Sta") AS route_count
               FROM dm_flight_report {where}''',
            params
        ).fetchone()
        kpis = {
            "total_flights": int(agg[0]) if agg else 0,
            "total_seats":   int(agg[1]) if agg else 0,
            "total_demand":  float(agg[2]) if agg else 0.0,
            "total_revenue": float(agg[3]) if agg else 0.0,
            "avg_lf":        round(float(agg[4]), 2) if agg else 0.0,
            "route_count":   int(agg[5]) if agg else 0,
        }
        # Top 6 routes by demand
        top_rows = conn.execute(
            f'''SELECT "Dept Sta" || \' → \' || "Arvl Sta" AS route,
                       SUM(TRY_CAST("Total Demand" AS DOUBLE)) AS dem
               FROM dm_flight_report {where}
               GROUP BY "Dept Sta", "Arvl Sta"
               ORDER BY dem DESC NULLS LAST
               LIMIT 6''',
            params
        ).fetchall()
        kpis["top_routes"] = [r[0] for r in top_rows]
        return kpis
    except Exception as exc:
        logger.warning(f"get_flight_kpis failed: {exc}")
        return {}


def get_market_summary(orig: str = "", dest: str = "") -> list:
    conn = get_connection()
    if not _table_exists(conn, "dm_market_summary"):
        return []
    conds, params = [], []
    if orig:
        conds.append("orig = ?"); params.append(orig.upper())
    if dest:
        conds.append("dest = ?"); params.append(dest.upper())
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    try:
        rows = conn.execute(
            f"SELECT * FROM dm_market_summary {where} ORDER BY TRY_CAST(traffic_share_pct_est AS FLOAT) DESC NULLS LAST",
            params
        ).fetchall()
        cols = [d[0] for d in conn.description]
        return [dict(zip(cols, r)) for r in rows]
    except Exception as exc:
        logger.warning(f"get_market_summary failed: {exc}")
        return []


def get_itin_report(orig: str = "", dest: str = "", carrier: str = "", top_n: int = 500) -> list:
    conn = get_connection()

    # ── Fast path: pre-built table (legacy CSV worksets) ──────────────────────
    if _table_exists(conn, "dm_itin_report"):
        conds, params = [], []
        if orig:
            conds.append('"Dept Arp" = ?'); params.append(orig.upper())
        if dest:
            conds.append('"Arvl Arp" = ?'); params.append(dest.upper())
        if carrier:
            conds.append('"Flt Desg (Seg1)" LIKE ?'); params.append(f"{carrier.upper()}%")
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        try:
            rows = conn.execute(
                f'SELECT * FROM dm_itin_report {where} ORDER BY "Dept Arp","Arvl Arp","Flt Desg (Seg1)" LIMIT ?',
                params + [top_n]
            ).fetchall()
            cols = [d[0] for d in conn.description]
            return [dict(zip(cols, r)) for r in rows]
        except Exception as exc:
            logger.warning(f"get_itin_report (pre-built) failed: {exc}")

    # ── Live path: query workset_spill + workset_base on demand ───────────────
    if not (_table_exists(conn, "workset_spill") and _table_exists(conn, "workset_base")):
        return []

    try:
        od_conds, params = [], []
        if orig:
            od_conds.append("UPPER(TRIM(s.market_origin)) = ?"); params.append(orig.upper())
        if dest:
            od_conds.append("UPPER(TRIM(s.market_dest)) = ?");   params.append(dest.upper())
        od_where = ("AND " + " AND ".join(od_conds)) if od_conds else ""
        carrier_where = f"AND UPPER(TRIM(s.airline)) = '{carrier.upper()}'" if carrier else ""

        rows = conn.execute(f"""
            WITH raw AS (
                -- Join spill with base legs to get flight identifiers (not per-day IDs)
                SELECT
                    UPPER(TRIM(s.market_origin))  AS mkt_orig,
                    UPPER(TRIM(s.market_dest))    AS mkt_dest,
                    UPPER(TRIM(s.airline))        AS airline,
                    s.stops,
                    s.day_of_week,
                    s.total_demand,
                    s.total_pax,
                    s.block_time,
                    s.total_revenue,
                    -- Seg1 identity: airline+flight+dep_time (stable across days)
                    UPPER(TRIM(wb1.mkt_airline)) || ' ' || TRIM(wb1.flight_num)  AS seg1,
                    wb1.dep_time   AS dep1,
                    wb1.arr_time   AS arr1,
                    UPPER(TRIM(wb1.dest))          AS cp1,
                    -- Seg2
                    CASE WHEN COALESCE(s.baseIndex_l2,0) > 0
                         THEN UPPER(TRIM(wb2.mkt_airline)) || ' ' || TRIM(wb2.flight_num) ELSE '*' END AS seg2,
                    CASE WHEN COALESCE(s.baseIndex_l2,0) > 0 THEN wb2.dep_time ELSE NULL END  AS dep2,
                    CASE WHEN COALESCE(s.baseIndex_l2,0) > 0 THEN wb2.arr_time ELSE NULL END  AS arr2,
                    CASE WHEN COALESCE(s.baseIndex_l3,0) > 0 THEN UPPER(TRIM(wb2.dest)) ELSE '*' END  AS cp2,
                    -- Seg3
                    CASE WHEN COALESCE(s.baseIndex_l3,0) > 0
                         THEN UPPER(TRIM(wb3.mkt_airline)) || ' ' || TRIM(wb3.flight_num) ELSE '*' END AS seg3,
                    CASE WHEN COALESCE(s.baseIndex_l3,0) > 0 THEN wb3.arr_time ELSE NULL END  AS arr3
                FROM workset_spill s
                LEFT JOIN workset_base wb1 ON wb1.record_id = s.baseIndex_l1
                LEFT JOIN workset_base wb2 ON COALESCE(s.baseIndex_l2,0) > 0
                                           AND wb2.record_id = s.baseIndex_l2
                LEFT JOIN workset_base wb3 ON COALESCE(s.baseIndex_l3,0) > 0
                                           AND wb3.record_id = s.baseIndex_l3
                WHERE s.airline IS NOT NULL AND LENGTH(TRIM(s.airline)) >= 2
                  AND COALESCE(s.total_pax, 0) > 0
                  AND s.is_codeshare = 0
                  AND wb1.record_id IS NOT NULL
                  {od_where} {carrier_where}
            )
            SELECT
                mkt_orig    AS "Dept Arp",
                mkt_dest    AS "Arvl Arp",
                seg1        AS "Flt Desg (Seg1)",
                CASE WHEN stops >= 1 THEN cp1 ELSE '*' END                  AS "Connect Point 1",
                '*'                                                          AS "Minimum Connect Time 1",
                CASE WHEN stops >= 1 AND MAX(dep2) IS NOT NULL AND MAX(arr1) IS NOT NULL THEN
                    CAST(
                        (TRY_CAST(SUBSTR(MAX(dep2),1,2) AS INTEGER)*60 + TRY_CAST(SUBSTR(MAX(dep2),3,2) AS INTEGER)) -
                        (TRY_CAST(SUBSTR(MAX(arr1),1,2) AS INTEGER)*60 + TRY_CAST(SUBSTR(MAX(arr1),3,2) AS INTEGER))
                    AS VARCHAR)
                ELSE '*' END                                                 AS "Connect Time 1",
                CASE WHEN stops >= 1 THEN seg2 ELSE '*' END                 AS "Flt Desg (Seg2)",
                CASE WHEN stops >= 2 THEN cp2  ELSE '*' END                 AS "Connect Point 2",
                '*'                                                          AS "Minimum Connect Time 2",
                '*'                                                          AS "Connect Time 2",
                CASE WHEN stops >= 2 THEN seg3 ELSE '*' END                 AS "Flt Desg (Seg3)",
                stops                                                        AS "Stops",
                stops + 1                                                    AS "Segs",
                -- SSIM 7-char freq: 0=Sun,1=Mon,...,6=Sat → IATA 1=Mon..7=Sun
                CASE WHEN MAX(CASE WHEN day_of_week=1 THEN 1 ELSE 0 END)=1 THEN '1' ELSE '.' END ||
                CASE WHEN MAX(CASE WHEN day_of_week=2 THEN 1 ELSE 0 END)=1 THEN '2' ELSE '.' END ||
                CASE WHEN MAX(CASE WHEN day_of_week=3 THEN 1 ELSE 0 END)=1 THEN '3' ELSE '.' END ||
                CASE WHEN MAX(CASE WHEN day_of_week=4 THEN 1 ELSE 0 END)=1 THEN '4' ELSE '.' END ||
                CASE WHEN MAX(CASE WHEN day_of_week=5 THEN 1 ELSE 0 END)=1 THEN '5' ELSE '.' END ||
                CASE WHEN MAX(CASE WHEN day_of_week=6 THEN 1 ELSE 0 END)=1 THEN '6' ELSE '.' END ||
                CASE WHEN MAX(CASE WHEN day_of_week=0 THEN 1 ELSE 0 END)=1 THEN '7' ELSE '.' END
                                                                             AS "Freq",
                MAX(dep1)[1:2] || ':' || MAX(dep1)[3:4]                     AS "Dept Time",
                COALESCE(MAX(arr3), MAX(arr2), MAX(arr1))[1:2] || ':' ||
                COALESCE(MAX(arr3), MAX(arr2), MAX(arr1))[3:4]              AS "Arvl Time",
                LPAD(CAST(CAST(MAX(block_time) / 60 AS INTEGER) AS VARCHAR), 2, '0') || ':' ||
                LPAD(CAST(CAST(MAX(block_time) % 60 AS INTEGER) AS VARCHAR), 2, '0')        AS "Elap Time",
                -- Weekly totals: SUM over all operating days (not per-departure averages)
                ROUND(SUM(total_demand), 1) AS "Total Demand",
                ROUND(SUM(total_pax), 1)    AS "Total Traffic",
                CAST(ROUND(SUM(total_revenue)) AS VARCHAR) AS "Pax Revenue($)"
            FROM raw
            GROUP BY
                mkt_orig, mkt_dest, airline, stops,
                seg1, cp1, dep1, arr1,
                seg2, dep2, arr2, cp2,
                seg3, arr3
            ORDER BY mkt_orig, mkt_dest, stops, seg1
            LIMIT {top_n}
        """, params).fetchall()
        cols = [d[0] for d in conn.description]
        return [dict(zip(cols, r)) for r in rows]
    except Exception as exc:
        logger.warning(f"get_itin_report (live) failed: {exc}")
        return []


def get_route_market_report(orig: str, dest: str, top_n: int = 200) -> list:
    """Market Report for a route — all O&D markets whose pax flow through orig→dest.

    Matches the PMCal 'flow_traf-report-market' CSV columns:
      Market | Market Size | Total Demand | Total Traffic |
      Prorated Revenue (to leg) ($) | Prorated Revenue (beyond) ($) |
      Avg OD Fare ($) | Avg Prorated Revenue ($) | Spill
    """
    conn = get_connection()
    if not (_table_exists(conn, "workset_spill") and _table_exists(conn, "workset_base")):
        return []
    o, d = orig.upper().strip(), dest.upper().strip()
    try:
        # Fetch route record_ids as a Python list first (very fast, small result)
        id_rows = conn.execute(
            "SELECT record_id FROM workset_base WHERE UPPER(TRIM(origin)) = ? AND UPPER(TRIM(dest)) = ?",
            [o, d]
        ).fetchall()
        if not id_rows:
            return []
        ids_str = ",".join(str(r[0]) for r in id_rows)

        rows = conn.execute(f"""
            WITH route_spill AS (
                SELECT
                    UPPER(TRIM(s.market_origin)) AS mkt_orig,
                    UPPER(TRIM(s.market_dest))   AS mkt_dest,
                    s.total_demand, s.total_pax, s.total_spill, s.total_revenue
                FROM workset_spill s
                WHERE s.is_codeshare = 0
                  AND COALESCE(s.total_pax, 0) > 0
                  AND (s.baseIndex_l1 IN ({ids_str})
                    OR s.baseIndex_l2 IN ({ids_str})
                    OR s.baseIndex_l3 IN ({ids_str}))
            ),
            aggregated AS (
                SELECT
                    mkt_orig || mkt_dest         AS mkt,
                    mkt_orig, mkt_dest,
                    ROUND(SUM(total_demand), 1)  AS tot_demand,
                    ROUND(SUM(total_pax), 1)     AS tot_pax,
                    ROUND(SUM(total_spill), 2)   AS tot_spill,
                    ROUND(SUM(total_revenue), 0) AS tot_rev
                FROM route_spill
                GROUP BY mkt_orig, mkt_dest
            )
            SELECT
                a.mkt                            AS "Market",
                CAST(COALESCE(
                    (SELECT ROUND(SUM(total_demand + COALESCE(total_spill, 0)))
                     FROM workset_spill
                     WHERE is_codeshare = 0
                       AND UPPER(TRIM(market_origin)) = a.mkt_orig
                       AND UPPER(TRIM(market_dest))   = a.mkt_dest), 0) AS INTEGER)
                                                 AS "Market Size",
                a.tot_demand                     AS "Total Demand",
                a.tot_pax                        AS "Total Traffic",
                CAST(a.tot_rev AS VARCHAR)       AS "Prorated Revenue (to leg) ($)",
                ''                               AS "Prorated Revenue (beyond) ($)",
                CAST(ROUND(a.tot_rev / NULLIF(a.tot_pax, 0), 2) AS VARCHAR) AS "Avg OD Fare ($)",
                CAST(ROUND(a.tot_rev / NULLIF(a.tot_pax, 0), 2) AS VARCHAR) AS "Avg Prorated Revenue ($)",
                a.tot_spill                      AS "Spill"
            FROM aggregated a
            ORDER BY a.tot_pax DESC
            LIMIT {top_n}
        """).fetchall()
        cols = [x[0] for x in conn.description]
        return [dict(zip(cols, r)) for r in rows]
    except Exception as exc:
        logger.warning(f"get_route_market_report failed: {exc}")
        return []


def get_route_flow_itins(orig: str, dest: str, top_n: int = 500) -> list:
    """Flow Itinerary Report for a route — all itineraries whose pax flow through orig→dest.

    Matches the PMCal 'flow_traf-report-itin' CSV columns:
      Market | Leg1 | Leg2 | Leg3 | Leg4 | Leg5 |
      Departure Day(s) | Total Demand | Total Traffic |
      Prorated Revenue (to leg) ($) | Prorated Revenue (beyond) ($) | Spill
    """
    conn = get_connection()
    if not (_table_exists(conn, "workset_spill") and _table_exists(conn, "workset_base")):
        return []
    o, d = orig.upper().strip(), dest.upper().strip()
    try:
        # Fetch route record_ids as Python list (fast, small)
        id_rows = conn.execute(
            "SELECT record_id FROM workset_base WHERE UPPER(TRIM(origin)) = ? AND UPPER(TRIM(dest)) = ?",
            [o, d]
        ).fetchall()
        if not id_rows:
            return []
        ids_str = ",".join(str(r[0]) for r in id_rows)

        # Build a temp lookup for workset_base leg descriptions
        rows = conn.execute(f"""
            WITH raw AS (
                SELECT
                    UPPER(TRIM(s.market_origin)) || UPPER(TRIM(s.market_dest))  AS market,
                    UPPER(TRIM(wb1.mkt_airline)) || '  ' ||
                        LPAD(TRIM(wb1.flight_num), 4, ' ') || ' ' ||
                        UPPER(TRIM(wb1.origin)) || ' ' || UPPER(TRIM(wb1.dest)) AS leg1,
                    CASE WHEN COALESCE(s.baseIndex_l2, 0) > 0 AND wb2.mkt_airline IS NOT NULL
                         THEN UPPER(TRIM(wb2.mkt_airline)) || '  ' ||
                              LPAD(TRIM(wb2.flight_num), 4, ' ') || ' ' ||
                              UPPER(TRIM(wb2.origin)) || ' ' || UPPER(TRIM(wb2.dest))
                         ELSE '*' END                                            AS leg2,
                    CASE WHEN COALESCE(s.baseIndex_l3, 0) > 0 AND wb3.mkt_airline IS NOT NULL
                         THEN UPPER(TRIM(wb3.mkt_airline)) || '  ' ||
                              LPAD(TRIM(wb3.flight_num), 4, ' ') || ' ' ||
                              UPPER(TRIM(wb3.origin)) || ' ' || UPPER(TRIM(wb3.dest))
                         ELSE '*' END                                            AS leg3,
                    s.day_of_week,
                    s.total_demand, s.total_pax, s.total_spill, s.total_revenue
                FROM workset_spill s
                JOIN workset_base wb1 ON wb1.record_id = s.baseIndex_l1
                LEFT JOIN workset_base wb2 ON wb2.record_id = s.baseIndex_l2
                LEFT JOIN workset_base wb3 ON wb3.record_id = s.baseIndex_l3
                WHERE s.is_codeshare = 0
                  AND COALESCE(s.total_pax, 0) > 0
                  AND (s.baseIndex_l1 IN ({ids_str})
                    OR s.baseIndex_l2 IN ({ids_str})
                    OR s.baseIndex_l3 IN ({ids_str}))
            )
            SELECT
                market                                                          AS "Market",
                leg1                                                            AS "Leg1",
                leg2                                                            AS "Leg2",
                leg3                                                            AS "Leg3",
                '*'                                                             AS "Leg4",
                '*'                                                             AS "Leg5",
                CASE WHEN MAX(CASE WHEN day_of_week=1 THEN 1 ELSE 0 END)=1 THEN '1' ELSE '.' END ||
                CASE WHEN MAX(CASE WHEN day_of_week=2 THEN 1 ELSE 0 END)=1 THEN '2' ELSE '.' END ||
                CASE WHEN MAX(CASE WHEN day_of_week=3 THEN 1 ELSE 0 END)=1 THEN '3' ELSE '.' END ||
                CASE WHEN MAX(CASE WHEN day_of_week=4 THEN 1 ELSE 0 END)=1 THEN '4' ELSE '.' END ||
                CASE WHEN MAX(CASE WHEN day_of_week=5 THEN 1 ELSE 0 END)=1 THEN '5' ELSE '.' END ||
                CASE WHEN MAX(CASE WHEN day_of_week=6 THEN 1 ELSE 0 END)=1 THEN '6' ELSE '.' END ||
                CASE WHEN MAX(CASE WHEN day_of_week=0 THEN 1 ELSE 0 END)=1 THEN '7' ELSE '.' END
                                                                                AS "Departure Day(s)",
                ROUND(SUM(total_demand), 1)                                     AS "Total Demand",
                ROUND(SUM(total_pax), 1)                                        AS "Total Traffic",
                CAST(ROUND(SUM(total_revenue), 0) AS VARCHAR)                   AS "Prorated Revenue (to leg) ($)",
                ''                                                              AS "Prorated Revenue (beyond) ($)",
                ROUND(SUM(total_spill), 2)                                      AS "Spill"
            FROM raw
            GROUP BY market, leg1, leg2, leg3
            ORDER BY SUM(total_pax) DESC
            LIMIT {top_n}
        """).fetchall()
        cols = [x[0] for x in conn.description]
        return [dict(zip(cols, r)) for r in rows]
    except Exception as exc:
        logger.warning(f"get_route_flow_itins failed: {exc}")
        return []



def get_flight_flow_od(flt: str) -> list:
    """Return flow OD distribution for a specific flight designator.

    Uses SPILLDATA itinerary records linked via baseIndex_l1/l2/l3 to BASEDATA legs.
    For each itinerary touching this flight's legs, returns the true market OD
    (market_origin → market_dest) with predicted pax and itinerary type (local/flow).
    Falls back to dm_itin_report if workset_spill is unavailable.
    """
    conn = get_connection()
    flt_upper = flt.strip().upper()

    # ── Try SPILLDATA-based reconstruction first (WORKSET204 path) ────────────
    if _table_exists(conn, "workset_spill") and _table_exists(conn, "workset_base"):
        try:
            # Find all baseIndex record_ids for legs belonging to this flight
            aln = flt_upper[:2].strip()
            fnum = flt_upper[2:].strip()
            leg_ids = conn.execute(
                """
                SELECT DISTINCT record_id
                FROM workset_base
                WHERE UPPER(TRIM(mkt_airline)) = ?
                  AND TRIM(flight_num) = ?
                """,
                [aln, fnum]
            ).fetchall()
            if not leg_ids:
                return []
            ids = [r[0] for r in leg_ids]
            placeholders = ",".join("?" * len(ids))

            # Find all SPILLDATA itineraries that use any of these legs
            rows = conn.execute(
                f"""
                SELECT
                    UPPER(TRIM(s.market_origin))  AS orig,
                    UPPER(TRIM(s.market_dest))    AS dest,
                    s.stops,
                    SUM(s.total_pax)              AS total_traffic,
                    SUM(s.total_demand)           AS total_demand,
                    SUM(s.total_spill)            AS total_spill,
                    COUNT(*)                      AS itin_count
                FROM workset_spill s
                WHERE s.baseIndex_l1 IN ({placeholders})
                   OR s.baseIndex_l2 IN ({placeholders})
                   OR s.baseIndex_l3 IN ({placeholders})
                GROUP BY s.market_origin, s.market_dest, s.stops
                ORDER BY SUM(s.total_pax) DESC NULLS LAST
                """,
                ids * 3
            ).fetchall()
            cols = [d[0] for d in conn.description]
            return [dict(zip(cols, r)) for r in rows]
        except Exception as exc:
            logger.warning(f"get_flight_flow_od (SPILLDATA path) failed: {exc}")

    # ── Fallback: dm_itin_report (legacy CSV-based worksets) ─────────────────
    if not _table_exists(conn, "dm_itin_report"):
        return []
    try:
        rows = conn.execute(
            """
            SELECT
                "Dept Arp"                           AS orig,
                "Arvl Arp"                           AS dest,
                SUM(TRY_CAST("Total Demand"    AS FLOAT)) AS total_demand,
                SUM(TRY_CAST("Total Traffic"   AS FLOAT)) AS total_traffic,
                SUM(TRY_CAST("Pax Revenue($)"  AS FLOAT)) AS total_revenue,
                COUNT(*)                             AS itin_count
            FROM dm_itin_report
            WHERE UPPER(TRIM("Flt Desg (Seg1)")) = ?
               OR UPPER(TRIM("Flt Desg (Seg2)")) = ?
               OR UPPER(TRIM("Flt Desg (Seg3)")) = ?
            GROUP BY "Dept Arp", "Arvl Arp"
            ORDER BY SUM(TRY_CAST("Total Traffic" AS FLOAT)) DESC NULLS LAST
            """,
            [flt_upper, flt_upper, flt_upper],
        ).fetchall()
        cols = [d[0] for d in conn.description]
        return [dict(zip(cols, r)) for r in rows]
    except Exception as exc:
        logger.warning(f"get_flight_flow_od (itin_report path) failed: {exc}")
        return []


def get_market_carrier_detail(orig: str, dest: str) -> list:
    """Full Market Summary by Airline for a specific OD (insightsDB OD Detail Panel)."""
    conn = get_connection()
    if not _table_exists(conn, "dm_market_carrier"):
        return []
    od = f"{orig.upper()}{dest.upper()}"
    try:
        rows = conn.execute(
            'SELECT * FROM dm_market_carrier WHERE "Market" = ? ORDER BY TRY_CAST("Demand Share" AS FLOAT) DESC NULLS LAST',
            [od]
        ).fetchall()
        cols = [d[0] for d in conn.description]
        return [dict(zip(cols, r)) for r in rows]
    except Exception as exc:
        logger.warning(f"get_market_carrier_detail failed: {exc}")
        return []


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
                    column08                                AS mkt_airline,
                    column09                                AS aircraft_type,
                    TRY_CAST(column10 AS INTEGER)           AS apm_cap,
                    TRY_CAST(column11 AS FLOAT)             AS apm_dmd,
                    TRY_CAST(column12 AS FLOAT)             AS apm_pax,
                    TRY_CAST(column13 AS FLOAT)             AS apm_lpax,
                    TRY_CAST(column14 AS FLOAT)             AS apm_spill,
                    TRY_CAST(column15 AS INTEGER)           AS day_of_week,
                    TRY_CAST(column16 AS INTEGER)           AS mkt_ind,
                    column18                                AS op_airline
                FROM read_csv('{_csv_path(base_path)}',
                              header=false, delim=',', null_padding=true,
                              all_varchar=true, parallel=true)
                WHERE column01 IS NOT NULL
                  AND LENGTH(TRIM(column01)) = 3
                  AND LENGTH(TRIM(column02)) = 3
                  AND TRY_CAST(column16 AS INTEGER) <= 1
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
                    column00 AS market_origin, column01 AS market_dest,
                    column02 AS dep_time,
                    TRY_CAST(column03 AS INTEGER) AS day_of_week,
                    COALESCE(TRY_CAST(column08 AS FLOAT),0) + COALESCE(TRY_CAST(column09 AS FLOAT),0) +
                    COALESCE(TRY_CAST(column10 AS FLOAT),0) + COALESCE(TRY_CAST(column11 AS FLOAT),0)
                                                  AS total_demand,
                    COALESCE(TRY_CAST(column16 AS FLOAT),0) + COALESCE(TRY_CAST(column17 AS FLOAT),0) +
                    COALESCE(TRY_CAST(column18 AS FLOAT),0) + COALESCE(TRY_CAST(column19 AS FLOAT),0)
                                                  AS total_pax,
                    COALESCE(TRY_CAST(column12 AS FLOAT),0) + COALESCE(TRY_CAST(column13 AS FLOAT),0) +
                    COALESCE(TRY_CAST(column14 AS FLOAT),0) + COALESCE(TRY_CAST(column15 AS FLOAT),0)
                                                  AS total_spill,
                    TRY_CAST(column23 AS FLOAT)   AS mkt_share,
                    column24                      AS airline,
                    TRY_CAST(column22 AS INTEGER) AS stops
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
        filters.append(f"mkt_airline = '{airline.upper()}'")
    if filters:
        clause = " AND ".join(filters)
        where_a = where_b = clause

    sql = f"""
    WITH a AS (
        SELECT origin, dest,
               COUNT(*)                  AS flights_a,
               SUM(apm_cap)              AS seats_a,
               SUM(apm_dmd)              AS demand_a,
               SUM(apm_spill)            AS spill_a,
               SUM(apm_pax) / NULLIF(SUM(CAST(apm_cap AS FLOAT)), 0) AS lf_a
        FROM workset_base
        WHERE {where_a}
        GROUP BY origin, dest
    ),
    b AS (
        SELECT origin, dest,
               COUNT(*)                  AS flights_b,
               SUM(apm_cap)              AS seats_b,
               SUM(apm_dmd)              AS demand_b,
               SUM(apm_spill)            AS spill_b,
               SUM(apm_pax) / NULLIF(SUM(CAST(apm_cap AS FLOAT)), 0) AS lf_b
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
            UPPER(TRIM(airline))                              AS airline,
            COUNT(*)                                          AS itin_count,
            ROUND(SUM(mkt_share) * 100, 2)                   AS mkt_share_pct,
            ROUND(SUM(total_spill), 2)                        AS total_spill,
            ROUND(SUM(total_pax), 2)                          AS total_pax_all,
            COUNT(DISTINCT dep_time)                          AS unique_dep_times,
            STRING_AGG(DISTINCT dep_time, '|' ORDER BY dep_time) AS dep_times_raw
        FROM workset_spill
        WHERE market_origin=? AND market_dest=? AND (is_codeshare IS NULL OR is_codeshare=0)
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
            ROUND(SUM(total_spill), 2) AS total_spill,
            COUNT(CASE WHEN total_spill > 0.05 THEN 1 END) AS flights_with_spill,
            ROUND(MAX(total_spill), 2) AS max_spill,
            0.0 AS total_recap
        FROM workset_spill
        WHERE market_origin=? AND market_dest=? AND (is_codeshare IS NULL OR is_codeshare=0)
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
    total_wk_seats_for_est = 1  # capacity not available from SPILLDATA directly

    # ── 11. Assemble airline list ─────────────────────────────────────────────
    total_weekly_seats = 0
    airlines_out = []
    for row in aln_rows:
        aln_code, itin_count, mkt_pct, spill, total_pax, n_deps, _ = row
        deps = dep_by_airline.get(aln_code, [])
        airlines_out.append({
            "code":               aln_code,
            "name":               AIRLINE_NAMES.get(aln_code, aln_code),
            "carrier_type":       CARRIER_TYPE.get(aln_code, "Full-service"),
            "alliance":           alliances.get(aln_code, "None / Independent"),
            "weekly_flight_days": _safe_int(itin_count),
            "unique_dep_times":   _safe_int(n_deps),
            "avg_seats_per_flight": 0,
            "weekly_seat_capacity": 0,
            "market_share_pct":   _safe_float(mkt_pct),
            "weekly_spill":       _safe_float(spill),
            "weekly_recap":       0.0,
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
