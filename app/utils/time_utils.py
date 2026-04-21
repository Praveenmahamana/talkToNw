"""Time utility functions for airline schedule processing."""

from datetime import datetime, timedelta, time, date
from typing import Optional, Tuple, List
import pytz


# ──────────────────────────────────────────────
# Parsing helpers
# ──────────────────────────────────────────────

def parse_time(time_str: str) -> Optional[time]:
    """Parse a time string in HH:MM, HHMM, or HH:MM:SS format."""
    if not time_str:
        return None
    s = str(time_str).strip()
    for fmt in ["%H:%M", "%H%M", "%H:%M:%S"]:
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    return None


def parse_date(date_str: str) -> Optional[date]:
    """Parse a date string in common formats."""
    if not date_str:
        return None
    s = str(date_str).strip()
    for fmt in ["%Y-%m-%d", "%d%b%Y", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"]:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def datetime_from_parts(date_str: str, time_str: str) -> Optional[datetime]:
    """Combine date and time strings into a datetime."""
    d = parse_date(date_str)
    t = parse_time(time_str)
    if d and t:
        return datetime.combine(d, t)
    return None


# ──────────────────────────────────────────────
# Timezone conversion
# ──────────────────────────────────────────────

def local_to_utc(dt: datetime, timezone_str: str) -> Optional[datetime]:
    """Convert a naive local datetime to UTC (naive)."""
    try:
        tz = pytz.timezone(timezone_str)
        if dt.tzinfo is None:
            dt = tz.localize(dt, is_dst=None)
        return dt.astimezone(pytz.utc).replace(tzinfo=None)
    except Exception:
        return None


def utc_to_local(dt: datetime, timezone_str: str) -> Optional[datetime]:
    """Convert a naive UTC datetime to local time (naive)."""
    try:
        tz = pytz.timezone(timezone_str)
        if dt.tzinfo is None:
            dt = pytz.utc.localize(dt)
        return dt.astimezone(tz).replace(tzinfo=None)
    except Exception:
        return None


def get_airport_timezone(iata_code: str) -> Optional[str]:
    """Return the IANA timezone for a known airport IATA code."""
    _TZ_MAP = {
        "LHR": "Europe/London", "LGW": "Europe/London", "STN": "Europe/London",
        "CDG": "Europe/Paris",  "ORY": "Europe/Paris",
        "FRA": "Europe/Berlin", "MUC": "Europe/Berlin", "BER": "Europe/Berlin",
        "AMS": "Europe/Amsterdam",
        "MAD": "Europe/Madrid",  "BCN": "Europe/Madrid",
        "FCO": "Europe/Rome",    "MXP": "Europe/Rome",
        "ZRH": "Europe/Zurich",  "GVA": "Europe/Zurich",
        "BRU": "Europe/Brussels",
        "VIE": "Europe/Vienna",
        "ARN": "Europe/Stockholm",
        "CPH": "Europe/Copenhagen",
        "OSL": "Europe/Oslo",
        "HEL": "Europe/Helsinki",
        "DXB": "Asia/Dubai",     "AUH": "Asia/Dubai",     "SHJ": "Asia/Dubai",
        "DOH": "Asia/Qatar",
        "KWI": "Asia/Kuwait",
        "BAH": "Asia/Bahrain",
        "RUH": "Asia/Riyadh",    "JED": "Asia/Riyadh",
        "BOM": "Asia/Kolkata",   "DEL": "Asia/Kolkata",   "BLR": "Asia/Kolkata",
        "MAA": "Asia/Kolkata",   "HYD": "Asia/Kolkata",   "CCU": "Asia/Kolkata",
        "SIN": "Asia/Singapore",
        "KUL": "Asia/Kuala_Lumpur",
        "BKK": "Asia/Bangkok",
        "HKG": "Asia/Hong_Kong",
        "PVG": "Asia/Shanghai",  "PEK": "Asia/Shanghai",
        "ICN": "Asia/Seoul",
        "NRT": "Asia/Tokyo",     "HND": "Asia/Tokyo",
        "SYD": "Australia/Sydney",
        "MEL": "Australia/Melbourne",
        "JFK": "America/New_York", "EWR": "America/New_York", "LGA": "America/New_York",
        "LAX": "America/Los_Angeles",
        "ORD": "America/Chicago", "MDW": "America/Chicago",
        "DFW": "America/Chicago",
        "SFO": "America/Los_Angeles",
        "MIA": "America/New_York",
        "DEN": "America/Denver",
        "YYZ": "America/Toronto",
        "YVR": "America/Vancouver",
        "GRU": "America/Sao_Paulo",
        "EZE": "America/Argentina/Buenos_Aires",
        "JNB": "Africa/Johannesburg",
        "CAI": "Africa/Cairo",
        "NBO": "Africa/Nairobi",
        "CMN": "Africa/Casablanca",
        "CPT": "Africa/Johannesburg",
    }
    return _TZ_MAP.get(iata_code.upper())


# ──────────────────────────────────────────────
# Block time & overlap
# ──────────────────────────────────────────────

def calculate_block_time_minutes(departure: datetime, arrival: datetime) -> int:
    """Calculate block time in minutes; handles overnight flights."""
    if arrival < departure:
        arrival += timedelta(days=1)
    return int((arrival - departure).total_seconds() / 60)


def is_overnight_flight(dep_time: time, arr_time: time) -> bool:
    """Return True if a flight's arrival time is before departure (crosses midnight)."""
    return arr_time < dep_time


def time_overlap(
    start1: datetime, end1: datetime,
    start2: datetime, end2: datetime,
    buffer_minutes: int = 0,
) -> bool:
    """Check if two datetime intervals overlap (with optional buffer on interval 1)."""
    s1 = start1 - timedelta(minutes=buffer_minutes)
    e1 = end1 + timedelta(minutes=buffer_minutes)
    return s1 < end2 and e1 > start2


def minutes_between_times(t1: time, t2: time) -> int:
    """Compute t2 − t1 in minutes (positive, wraps through midnight)."""
    base = date(2000, 1, 1)
    dt1 = datetime.combine(base, t1)
    dt2 = datetime.combine(base, t2)
    if dt2 < dt1:
        dt2 += timedelta(days=1)
    return int((dt2 - dt1).total_seconds() / 60)


# ──────────────────────────────────────────────
# Curfew check
# ──────────────────────────────────────────────

def is_within_curfew(t: time, curfew_start: time, curfew_end: time) -> bool:
    """
    Return True if time *t* falls inside the curfew window.
    Handles overnight curfews (e.g. 23:00 – 06:00).
    """
    if curfew_start <= curfew_end:
        return curfew_start <= t <= curfew_end
    # Overnight: start > end  (e.g. 23:00 -> 06:00)
    return t >= curfew_start or t <= curfew_end


# ──────────────────────────────────────────────
# Formatting
# ──────────────────────────────────────────────

def format_duration(minutes: int) -> str:
    """Format minutes as HH:MM string."""
    h = abs(minutes) // 60
    m = abs(minutes) % 60
    sign = "-" if minutes < 0 else ""
    return f"{sign}{h:02d}:{m:02d}"


def day_of_week_label(dow: int) -> str:
    """Convert 1-based day-of-week (1=Mon) to label."""
    labels = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}
    return labels.get(dow, "?")


def frequency_to_days(frequency: str) -> List[int]:
    """
    Convert frequency string (e.g. '1234567' or '135') to list of day numbers (1=Mon).
    """
    return [int(c) for c in str(frequency) if c.isdigit() and 1 <= int(c) <= 7]
