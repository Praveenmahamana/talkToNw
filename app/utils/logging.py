"""Centralized logging configuration using loguru."""

import sys
import io
from pathlib import Path
from typing import Optional
from loguru import logger


_configured = False


def setup_logging(log_level: str = "INFO", log_file: Optional[str] = None) -> None:
    """Configure application-wide logging. Safe to call multiple times."""
    global _configured
    if _configured:
        return
    _configured = True

    logger.remove()  # Remove default handler

    # Wrap stdout with UTF-8 so Unicode log messages don't crash on Windows cp1252
    _stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)

    # ── Console ──────────────────────────────────────────────────────────────
    logger.add(
        _stdout,
        level=log_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
        colorize=True,
        enqueue=True,
    )

    # ── File ─────────────────────────────────────────────────────────────────
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            log_file,
            level=log_level,
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{line} - {message}",
            rotation="50 MB",
            retention="30 days",
            compression="gz",
            enqueue=True,
            encoding="utf-8",
        )


def get_logger(name: str):
    """Return a loguru logger bound to the given module name."""
    return logger.bind(name=name)
