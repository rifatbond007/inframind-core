"""Structured JSON logging: `log = get_logger(__name__); log.info("event", key=value)`."""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

_RESERVED = {"event", "fields"}


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update(getattr(record, "fields", {}))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class StructLogger:
    """Thin wrapper that turns keyword arguments into JSON fields."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def _log(self, level: int, event: str, fields: dict[str, Any]) -> None:
        if self._logger.isEnabledFor(level):
            clash = _RESERVED & fields.keys()
            if clash:
                raise ValueError(f"reserved log field(s): {sorted(clash)}")
            self._logger.log(level, event, extra={"fields": fields})

    def debug(self, event: str, **fields: Any) -> None:
        """Log a per-item detail event (off by default)."""
        self._log(logging.DEBUG, event, fields)

    def info(self, event: str, **fields: Any) -> None:
        """Log a state change."""
        self._log(logging.INFO, event, fields)

    def warning(self, event: str, **fields: Any) -> None:
        """Log a retry or a dropped item."""
        self._log(logging.WARNING, event, fields)

    def error(self, event: str, **fields: Any) -> None:
        """Log a failure."""
        self._log(logging.ERROR, event, fields)


def configure_logging(level: str | None = None) -> None:
    """Install the JSON handler on the root logger. Idempotent."""
    root = logging.getLogger()
    if not any(isinstance(h.formatter, _JsonFormatter) for h in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_JsonFormatter())
        root.addHandler(handler)
    root.setLevel((level or os.environ.get("INFRAMIND_LOG_LEVEL", "INFO")).upper())


def get_logger(name: str) -> StructLogger:
    """Return a structured logger for `name` (use `__name__`)."""
    return StructLogger(logging.getLogger(name))
