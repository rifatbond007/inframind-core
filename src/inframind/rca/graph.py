"""`ServiceGraph`: the canonical service dependency graph (D2, D21, standard 13).

Edges are ``caller -> callee``. Failures propagate ``callee -> caller``, so the RCA walk starts
at the symptom service and moves **toward its callees** (D2).

The live graph lives in the RCA worker — a single writer (the OTel/Jaeger collector via
`stream:graph-updates`) and a single reader (the ranker). The ranker is the only consumer that
needs O(1) node/edge access, so the graph is in-process rather than a Redis round-trip
(standard 13.3).

Edge weight is ``p95_latency_ms * log(call_count)`` maintained as an EWMA with alpha=0.1 over
the last ``GRAPH_MAX_SAMPLES`` observations (standard 13.2). An edge is only promoted into the
walkable graph after ``GRAPH_MIN_OBSERVATIONS`` observations; before that it is counted but
invisible to the ranker. Edges decay with a half-life and are pruned when they fall silent
(standard 13.4).

This module is deliberately dependency-light: the graph contract is a small directed
multi-graph with per-edge EWMA state, so it is implemented natively rather than pulling in
NetworkX. `rank_pagerank.py` implements the MicroRCA-style personalised PageRank on the same
structure (standard 06.5).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from inframind.common.graph_update import GraphUpdate
from inframind.common.logging import get_logger
from inframind.common.signal import UNKNOWN_SERVICE, normalise_service
from inframind.common.time import utcnow

from .config import RCAParams

log = get_logger(__name__)


def _is_invalid_pair(caller: str, callee: str) -> bool:
    """True when an edge endpoint is unusable: unknown on either side, or a self-loop.

    `GraphUpdate` already rejects these on the wire, but a hand-edited or stale snapshot file
    may not, so every path that ingests a caller/callee pair funnels through this check.
    """
    if UNKNOWN_SERVICE in (caller, callee):
        return True
    return caller == callee


@dataclass
class _Edge:
    """Per-edge observation state.

    ``weight`` is the EWMA of ``latency_ms * log(call_count)``. ``observations`` counts the
    real ``GraphUpdate`` events seen for this edge. ``stub`` marks a cold-start edge seeded
    from signal co-occurrences (standard 13.5): it gives the graph shape but carries no
    observation evidence, so its confidence is 0 until real updates promote it.
    """

    weight: float = 0.0
    observations: int = 0
    last_observed_at: datetime | None = None
    stub: bool = False

    def confidence(self, min_observations: int) -> float:
        """Confidence in [0, 1]; 0 until ``min_observations`` real samples land."""
        return 1.0 if self.observations >= min_observations else 0.0


class ServiceGraph:
    """Directed service graph with ``caller -> callee`` edges (D2).

    The interface matches standard 13.2:

    * ``apply_update`` folds one observed call edge.
    * ``successors`` / ``predecessors`` expose the caller's callees / the callee's callers.
    * ``edge_weight`` returns the current weighted importance of an edge.
    * ``snapshot`` / ``persist`` write the graph to ``RCA_GRAPH_PATH``.

    A corrupt or missing seed file never raises: the graph cold-starts and logs
    ``ERROR graph_snapshot_unreadable`` (standard 13.6).
    """

    def __init__(
        self,
        seed_path: Path | str | None = None,
        *,
        params: RCAParams | None = None,
    ) -> None:
        self._params = params or RCAParams()
        self._seed_path = Path(seed_path) if seed_path is not None else None
        # (caller, callee) -> _Edge
        self._edges: dict[tuple[str, str], _Edge] = {}
        # service -> set of callers (reverse index for predecessors)
        self._callers: dict[str, set[str]] = {}
        self._version: int = 0
        self._updated_at: datetime = utcnow()
        self._loaded_from_disk: bool = False

        if self._seed_path is not None:
            self._load_snapshot()

    # --- public interface (standard 13.2) ---

    def apply_update(self, update: GraphUpdate) -> None:
        """Fold one observed call edge into the graph (standard 13.4).

        The edge is only promoted (visible to the walk) once it has been observed
        ``GRAPH_MIN_OBSERVATIONS`` times. Weights are an EWMA with alpha=0.1 over the last 100
        samples of ``p95_latency_ms * log(call_count)``.
        """
        caller = normalise_service(update.caller)
        callee = normalise_service(update.callee)
        if _is_invalid_pair(caller, callee):
            # Defensive: GraphUpdate already rejects these, but a seeded file might not.
            return

        edge = self._edges.get((caller, callee))
        if edge is None:
            edge = _Edge()
            self._edges[(caller, callee)] = edge
            self._callers.setdefault(callee, set()).add(caller)

        # A single latency sample approximates p95 for the edge; log(call_count) is folded in
        # via the observation count so a frequently-called edge dominates a rarely-called one.
        sample = update.latency_ms * math.log1p(edge.observations + 1)
        edge.observations += 1
        edge.last_observed_at = update.observed_at
        # A real GraphUpdate promotes a cold-start stub edge out of the stub state.
        edge.stub = False

        alpha = self._params.ewma_alpha
        edge.weight = alpha * sample + (1 - alpha) * edge.weight

        self._version += 1
        self._updated_at = update.observed_at

    def successors(self, service: str) -> list[str]:
        """Services called by `service` (its callees), in deterministic sorted order."""
        s = normalise_service(service)
        return sorted(callee for (caller, callee) in self._edges if caller == s)

    def predecessors(self, service: str) -> list[str]:
        """Services that call `service` (its callers), in deterministic sorted order."""
        s = normalise_service(service)
        return sorted(self._callers.get(s, set()))

    def edge_weight(self, caller: str, callee: str) -> float:
        """Current weighted importance of ``caller -> callee``; 0.0 when unseen."""
        edge = self._edges.get((normalise_service(caller), normalise_service(callee)))
        return edge.weight if edge is not None else 0.0

    def edge_confidence(self, caller: str, callee: str) -> float:
        """Confidence in [0, 1] for ``caller -> callee``; 0.0 when unseen or below threshold."""
        edge = self._edges.get((normalise_service(caller), normalise_service(callee)))
        if edge is None:
            return 0.0
        return edge.confidence(self._params.min_observations)

    def is_walkable(self, caller: str, callee: str) -> bool:
        """True when the edge is visible to the walk.

        A cold-start stub edge (standard 13.5) *is* walkable: it gives the graph shape so a
        cold-start ranking is possible at all. But it carries ``confidence = 0``, so the ranker
        downweights the structural ``downstream_depth`` term accordingly (standard 13.2). A real
        edge becomes fully confident once ``GRAPH_MIN_OBSERVATIONS`` updates land.
        """
        edge = self._edges.get((normalise_service(caller), normalise_service(callee)))
        if edge is None:
            return False
        if edge.stub:
            return True
        return edge.observations >= self._params.min_observations

    def services(self) -> list[str]:
        """Every service that appears as a caller or callee of any tracked edge, sorted."""
        nodes: set[str] = set()
        for caller, callee in self._edges:
            nodes.add(caller)
            nodes.add(callee)
        return sorted(nodes)

    def version(self) -> int:
        """Monotonic version, bumped on every applied update (standard 13.2)."""
        return self._version

    def updated_at(self) -> datetime:
        """UTC timestamp of the last applied update."""
        return self._updated_at

    def snapshot(self) -> dict[str, Any]:
        """JSON-serialisable snapshot of the graph (standard 13.6).

        ``updated_at`` and ``version`` are deliberately excluded — they are recomputed on load.
        """
        return {
            "edges": [
                {
                    "caller": caller,
                    "callee": callee,
                    "weight": edge.weight,
                    "observations": edge.observations,
                    "stub": edge.stub,
                    "last_observed_at": (
                        edge.last_observed_at.isoformat() if edge.last_observed_at else None
                    ),
                }
                for (caller, callee), edge in sorted(self._edges.items())
            ],
            "callers": {
                callee: sorted(callers) for callee, callers in sorted(self._callers.items())
            },
        }

    def persist(self) -> None:
        """Write the snapshot to ``RCA_GRAPH_PATH``; create parent dirs. Never raises on IO."""
        if self._seed_path is None:
            return
        try:
            self._seed_path.parent.mkdir(parents=True, exist_ok=True)
            self._seed_path.write_text(json.dumps(self.snapshot(), indent=2))
        except OSError as e:
            log.warning("graph_snapshot_write_failed", path=str(self._seed_path), error=str(e))

    # --- cold start (standard 13.5) ---

    def seed_from_signals(self, service_pairs: list[tuple[str, str]]) -> None:
        """Seed zero-confidence stub edges from signal co-occurrences (standard 13.5).

        Used on cold start when no snapshot exists. Stub edges are walkable (so the graph
        has structural shape) but carry ``confidence = 0`` and ``observations = 0``, so the
        ranker's downweight kicks in and a cold-graph ranking cannot beat a warm one.

        The walker sees these as zero-weight edges; the ranker downweights the depth term by
        ``low_confidence_multiplier`` (default 0.1; standard 13.2).
        """
        for raw_caller, raw_callee in service_pairs:
            caller = normalise_service(raw_caller)
            callee = normalise_service(raw_callee)
            if _is_invalid_pair(caller, callee):
                continue
            key = (caller, callee)
            if key in self._edges:
                continue
            self._edges[key] = _Edge(stub=True)
            self._callers.setdefault(callee, set()).add(caller)
        self._version += 1

    def _load_snapshot(self) -> None:
        """Load the seed file if present and parseable; otherwise cold-start (standard 13.6)."""
        if self._seed_path is None or not self._seed_path.exists():
            log.warning("graph_cold_start_no_seed", path=str(self._seed_path))
            return
        try:
            data = json.loads(self._seed_path.read_text())
            edges = data["edges"]
            for raw in edges:
                caller = normalise_service(raw["caller"])
                callee = normalise_service(raw["callee"])
                if _is_invalid_pair(caller, callee):
                    continue
                last_observed = raw.get("last_observed_at")
                self._edges[(caller, callee)] = _Edge(
                    weight=float(raw["weight"]),
                    observations=int(raw["observations"]),
                    last_observed_at=(
                        datetime.fromisoformat(last_observed) if last_observed else None
                    ),
                    stub=bool(raw.get("stub", False)),
                )
                self._callers.setdefault(callee, set()).add(caller)
            self._loaded_from_disk = True
            self._version += 1
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
            log.error("graph_snapshot_unreadable", path=str(self._seed_path), error=str(e))
            self._edges.clear()
            self._callers.clear()

    def decay(self, now: datetime | None = None) -> int:
        """Apply time-based decay and prune silent edges (standard 13.4).

        Every ``GRAPH_DECAY_HALF_LIFE_S`` half-life, each edge loses 0.5% weight. An edge not
        observed for ``2 * half_life`` is removed. Stub edges (cold-start, no observations)
        keep their weight but never decay: their only role is structural shape.
        """
        now = now or utcnow()
        half_life = self._params.decay_half_life_s
        if half_life <= 0:
            return 0
        prune_after_s = 2 * half_life
        pruned = 0
        to_remove: list[tuple[str, str]] = []
        for key, edge in self._edges.items():
            if edge.stub and edge.observations == 0:
                # Stub edges have no observation time; they are structural, not temporal.
                continue
            if edge.last_observed_at is None:
                to_remove.append(key)
                continue
            idle_s = (now - edge.last_observed_at).total_seconds()
            if idle_s > prune_after_s:
                to_remove.append(key)
                continue
            # 0.5% loss per elapsed half-life.
            decay_factor = 0.995 ** max(0.0, idle_s / half_life)
            edge.weight *= decay_factor
        for key in to_remove:
            caller, callee = key
            del self._edges[key]
            self._callers.get(callee, set()).discard(caller)
            if not self._callers.get(callee):
                del self._callers[callee]
            pruned += 1
        if pruned:
            self._version += 1
        return pruned
