"""DuckDB table definitions and initialisation."""

from app.database.db import execute
from loguru import logger


DDL_FLIGHTS = """
CREATE TABLE IF NOT EXISTS flights (
    id                  VARCHAR PRIMARY KEY,
    airline             VARCHAR NOT NULL,
    flight_number       VARCHAR NOT NULL,
    origin              VARCHAR NOT NULL,
    destination         VARCHAR NOT NULL,
    departure_local     TIMESTAMP,
    arrival_local       TIMESTAMP,
    departure_utc       TIMESTAMP,
    arrival_utc         TIMESTAMP,
    day_of_operation    INTEGER,          -- 1=Mon … 7=Sun
    aircraft_type       VARCHAR,
    block_time          INTEGER,          -- minutes
    frequency           VARCHAR,          -- e.g. '1234567'
    effective_from      DATE,
    effective_to        DATE,
    service_type        VARCHAR,          -- J=Scheduled, G=Positioning/NonOps
    terminal_dep        VARCHAR,          -- SSIM departure terminal code
    terminal_arr        VARCHAR,          -- SSIM arrival terminal code
    source_file         VARCHAR,
    load_timestamp      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

DDL_INGESTION_LOG = """
CREATE TABLE IF NOT EXISTS ingestion_log (
    id              INTEGER,
    file_name       VARCHAR,
    rows_loaded     INTEGER,
    rows_skipped    INTEGER,
    errors          VARCHAR,
    loaded_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

DDL_QUERY_LOG = """
CREATE TABLE IF NOT EXISTS query_log (
    id              INTEGER,
    user_query      VARCHAR,
    intent          VARCHAR,
    tools_called    VARCHAR,
    response_time   DOUBLE,
    logged_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def init_db() -> None:
    """Create all tables if they do not exist, and migrate existing schemas."""
    execute(DDL_FLIGHTS)
    execute(DDL_INGESTION_LOG)
    execute(DDL_QUERY_LOG)
    # Migrate existing DB — add new columns if absent (DuckDB supports IF NOT EXISTS)
    for col, typedef in [
        ("service_type",  "VARCHAR"),
        ("terminal_dep",  "VARCHAR"),
        ("terminal_arr",  "VARCHAR"),
    ]:
        try:
            execute(f"ALTER TABLE flights ADD COLUMN IF NOT EXISTS {col} {typedef}")
        except Exception:
            pass  # Column already exists or unsupported — safe to ignore
    logger.info("Database schema initialised.")


def drop_all() -> None:
    """Drop all application tables (use with caution)."""
    for table in ("flights", "ingestion_log", "query_log"):
        execute(f"DROP TABLE IF EXISTS {table}")
    logger.warning("All tables dropped.")
