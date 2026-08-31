"""Application logging via Loguru: daily rotation, gzip compression, correlation ids.

Secrets are never logged. A per-request/operation correlation id is threaded
through the async pipeline via a ContextVar and rendered into file records.
"""
from __future__ import annotations

import sys
from contextvars import ContextVar
from pathlib import Path

from loguru import logger

from .config import get_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"

# Correlation id propagated through the async pipeline.
correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")


def setup_logging() -> "logger":
    """Configure Loguru sinks once. Safe to call multiple times."""
    settings = get_settings()
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger.remove()

    console_level = "DEBUG" if settings.app.environment != "production" else "INFO"
    logger.add(
        sys.stderr,
        level=console_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>"
        ),
    )

    logger.add(
        str(LOG_DIR / "app_{time:YYYY-MM-DD}.log"),
        level="DEBUG",
        rotation="00:00",          # daily rotation
        retention="30 days",
        compression="gz",
        enqueue=True,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
            "{extra[correlation]} | {name}:{function} | {message}"
        ),
    )

    logger.configure(extra={"correlation": "-"})
    logger.info("Logging initialized (dir={})", str(LOG_DIR))
    return logger


def bind_correlation(cid: str) -> "logger":
    """Set the correlation id for the current async context and bind it."""
    correlation_id.set(cid)
    return logger.bind(correlation=cid)


def get_operation_logger(operation: str) -> "logger":
    """Logger bound with the current correlation id."""
    return logger.bind(correlation=correlation_id.get(), operation=operation)
