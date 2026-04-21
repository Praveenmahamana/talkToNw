"""DuckDB connection management — singleton-style pool."""

import threading
from pathlib import Path
from typing import Optional
import duckdb
from loguru import logger

_lock = threading.Lock()
_connection: Optional[duckdb.DuckDBPyConnection] = None
_DB_PATH: str = ":memory:"


def get_db_path() -> str:
    """Return the configured database path."""
    return _DB_PATH


def configure_db(path: str = ":memory:") -> None:
    """Set the database path before first use."""
    global _DB_PATH
    _DB_PATH = path


def get_connection() -> duckdb.DuckDBPyConnection:
    """
    Return the shared DuckDB connection.  Creates it on first call.
    Thread-safe via a module-level lock.
    """
    global _connection
    with _lock:
        if _connection is None:
            if _DB_PATH != ":memory:":
                Path(_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
            try:
                _connection = duckdb.connect(database=_DB_PATH, read_only=False)
            except Exception as exc:
                if "being used by another process" in str(exc) or "IO Error" in str(exc):
                    raise RuntimeError(
                        f"\n\n  DuckDB file is locked by another process:\n"
                        f"    {_DB_PATH}\n\n"
                        f"  Another server instance is already running.\n"
                        f"  Find and stop it first:\n"
                        f"    Get-Process python | Where-Object {{$_.MainWindowTitle -eq ''}} | Stop-Process -Force\n"
                        f"  Or just use a different port — the lock belongs to the old process.\n"
                    ) from exc
                raise
            logger.info(f"DuckDB connected -> {_DB_PATH}")
        return _connection


def close_connection() -> None:
    """Close the shared connection (e.g., at application shutdown)."""
    global _connection
    with _lock:
        if _connection is not None:
            _connection.close()
            _connection = None
            logger.info("DuckDB connection closed.")


def execute(sql: str, params=None):
    """Convenience wrapper — execute SQL on the shared connection."""
    conn = get_connection()
    if params:
        return conn.execute(sql, params)
    return conn.execute(sql)


def fetchall(sql: str, params=None):
    """Execute and fetch all rows as list of tuples."""
    return execute(sql, params).fetchall()


def fetchdf(sql: str, params=None):
    """Execute and return a pandas DataFrame."""
    return execute(sql, params).df()
