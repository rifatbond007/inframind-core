"""Domain exception hierarchy. Service loops catch these at the boundary, log, and continue."""

from __future__ import annotations


class InfraMindError(Exception):
    """Base class for every InfraMind domain error."""


class IngestionError(InfraMindError):
    """A collector failed to pull or map a backend payload."""


class DetectionError(InfraMindError):
    """A detector failed to evaluate a signal window."""


class RcaError(InfraMindError):
    """The RCA stage failed to build or rank candidates."""


class BusError(InfraMindError):
    """The event bus could not publish or read a message."""
