"""Logging: readable on the console, structured on disk.

Every run appends JSON lines to ``Logs/run-YYYY-MM-DD.jsonl``. That file is the
thing you grep when a night's batch misbehaves — one object per event, with the
concept id attached, so `jq 'select(.concept=="...")'` reconstructs a job.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

LOGGER_NAME = "pawparty"

_LEVEL_COLORS = {
    "DEBUG": "\033[90m",
    "INFO": "\033[36m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[41m",
}
_RESET = "\033[0m"


class ConsoleFormatter(logging.Formatter):
    """Compact, optionally coloured console output."""

    def __init__(self, use_color: bool = True) -> None:
        super().__init__()
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        concept = getattr(record, "concept", "")
        prefix = f"[{concept}] " if concept else ""
        level = record.levelname
        if self.use_color and level in _LEVEL_COLORS:
            level = f"{_LEVEL_COLORS[level]}{level:<7}{_RESET}"
        else:
            level = f"{level:<7}"
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)
        return f"{level} {prefix}{message}"


class JsonlFormatter(logging.Formatter):
    """One JSON object per line, with any `extra=` fields carried through."""

    RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
        "message",
        "asctime",
        "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self.RESERVED and not key.startswith("_"):
                try:
                    json.dumps(value)
                    payload[key] = value
                except (TypeError, ValueError):
                    payload[key] = repr(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(
    log_file: Path | None = None,
    *,
    verbose: bool = False,
    quiet: bool = False,
) -> logging.Logger:
    """Configure and return the package logger. Safe to call more than once."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.WARNING if quiet else (logging.DEBUG if verbose else logging.INFO))
    console.setFormatter(ConsoleFormatter(use_color=sys.stderr.isatty()))
    logger.addHandler(console)

    if log_file is not None:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(JsonlFormatter())
        logger.addHandler(file_handler)

    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(LOGGER_NAME if not name else f"{LOGGER_NAME}.{name}")
