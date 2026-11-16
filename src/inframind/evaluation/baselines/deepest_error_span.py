"""`deepest_error_span` baseline (D7).

Deepest erroring span in the trace graph — RCA's closest competitor among the lightweight
heuristics. "Deepest" means furthest from the leaf (the symptom) on a `trace_depth` axis
that the trace exporter populates when a span errors.

We rank by trace depth desc, with ties broken alphabetically. Services with no trace
record sit at depth 0 and end up last.
"""

from __future__ import annotations

from .base import BaselineInput, _validate_nonempty


class DeepestErrorSpanBaseline:
    """Take the service with the deepest erroring trace span as the root."""

    name = "deepest_error_span"

    def rank(self, input: BaselineInput) -> list[str]:
        """Return services sorted by `trace_depth` desc, then alphabetically."""
        _validate_nonempty(input.services)
        if input.error_depth_by_service is not None:
            # Deepest erroring span first; ties go to the service with more error events.
            depth = input.error_depth_by_service
            errors = input.error_events_by_service or {}
            return sorted(
                (s for s in input.services if s in depth),
                key=lambda s: (-depth[s], -errors.get(s, 0), s),
            )
        depths = {
            service: int(input.trace_depth_by_service.get(service, 0)) for service in input.services
        }
        return sorted(input.services, key=lambda s: (-depths[s], s))


__all__ = ["DeepestErrorSpanBaseline"]
