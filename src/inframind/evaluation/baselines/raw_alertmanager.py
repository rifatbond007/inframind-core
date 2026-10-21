"""`raw_alertmanager` baseline (D7).

Fires on the alert's `instance` label — no correlation, no graph, no LLM. This is the
"what do we already have" floor: InfraMind is only useful if its top-1 beats this on at
least some scenarios.

The Alertmanager payload normally carries `instance` and `service` (or `job`). We treat
the first service observed in the alerts as the root cause — the same answer an SRE
would land on after reading the page.
"""

from __future__ import annotations

from collections import Counter

from .base import BaselineInput, _validate_nonempty


class RawAlertmanagerBaseline:
    """Take the most-frequent service in the alert payload as the root cause."""

    name = "raw_alertmanager"

    def rank(self, input: BaselineInput) -> list[str]:
        """Return the services sorted by alert frequency, then the rest alphabetically."""
        _validate_nonempty(input.services)
        counts: Counter[str] = Counter()
        for service in input.services:
            for alert in input.alerts_by_service.get(service, ()):
                candidate = str(alert.get("instance") or alert.get("service") or service)
                if candidate:
                    counts[candidate] += 1
        ordered = sorted(input.services, key=lambda s: (-counts.get(s, 0), s))
        return ordered


__all__ = ["RawAlertmanagerBaseline"]
