"""`highest_error_service` baseline (D7).

Highest error rate from the metrics — the obvious heuristic an SRE writes first, and the
baseline that beats raw Alertmanager the most often. If InfraMind cannot beat this, the
graph term is not earning its keep.
"""

from __future__ import annotations

from .base import BaselineInput, _validate_nonempty


class HighestErrorServiceBaseline:
    """Rank by observed error rate, highest first; ties broken alphabetically."""

    name = "highest_error_service"

    def rank(self, input: BaselineInput) -> list[str]:
        """Return services sorted by `-error_rate`, then alphabetically."""
        _validate_nonempty(input.services)
        rates = {
            service: _error_rate(input.signals_by_service.get(service) or [])
            for service in input.services
        }
        return sorted(input.services, key=lambda s: (-rates[s], s))


def _error_rate(signals: list[dict[str, object]]) -> float:
    """Compute error rate from `error_count / total_count` across signal attrs."""
    total = 0.0
    errors = 0.0
    for signal in signals:
        attrs = signal.get("attrs")
        if not isinstance(attrs, dict):
            attrs = signal
        t = attrs.get("total_count", attrs.get("count", 0))
        e = attrs.get("error_count", attrs.get("errors", 0))
        try:
            total += float(t)  # type: ignore[arg-type]
            errors += float(e)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    return errors / total if total else 0.0


__all__ = ["HighestErrorServiceBaseline"]
