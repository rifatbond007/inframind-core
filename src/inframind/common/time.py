"""Time helpers. Every collector and detector uses `utcnow()`, never `datetime.utcnow()`."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)
