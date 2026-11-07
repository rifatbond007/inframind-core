"""The baseline contract (standard 10.6).

A baseline sees what InfraMind sees — the same signals, the same graph snapshot, the same
incident open time — and returns a ranked list of services. It does **not** get the RCA
ranker's internals: it gets a `BaselineInput` and returns a ranking. That symmetry is the
point; a baseline that peeked at the answer would not be a comparison.

D7 locks six baselines. `random_service` is the chance floor; `raw_alertmanager` is the
"what do we already have" floor; `pagerank` re-exports the P5 implementation rather than
reimplementing it; `llm_only` is D1's control and *should* score near chance — a high score
is a finding to report, not a bug to fix.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from inframind.common.exceptions import InfraMindError
from inframind.rca.graph import ServiceGraph


@dataclass(frozen=True, slots=True)
class BaselineInput:
    """Everything a baseline is allowed to see.

    Deliberately narrow: per-service error rates, per-service anomaly severities, the
    trace graph, the anomaly list, and the set of services in play. A baseline that needs
    something else must justify it — the contract is here to keep the comparison honest.
    """

    services: tuple[str, ...]
    signals_by_service: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    anomalies_by_service: dict[str, list[tuple[str, float]]] = field(default_factory=dict)
    trace_depth_by_service: dict[str, int] = field(default_factory=dict)
    graph: ServiceGraph | None = None
    alerts_by_service: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    summary_text: str = ""
    ranked_hints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.services:
            raise ValueError("BaselineInput.services must be non-empty")

    @property
    def service_set(self) -> frozenset[str]:
        """The set of services in play."""
        return frozenset(self.services)


class EmptyServiceSet(InfraMindError):
    """A baseline was asked to rank an empty service set (should never happen)."""


class Baseline(Protocol):
    """Anything with `name` and `rank(input) -> list[str]`."""

    name: str

    def rank(self, input: BaselineInput) -> list[str]:
        """Return a best-first ranking of services from `input`."""
        ...


def _validate_nonempty(services: Sequence[str]) -> None:
    if not services:
        raise EmptyServiceSet("cannot rank an empty service set")


__all__ = ["Baseline", "BaselineInput", "EmptyServiceSet"]
