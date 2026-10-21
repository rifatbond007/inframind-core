"""The RCA worker: `stream:graph-updates` + `stream:incidents` in, ranked candidates out.

This is the single RCA process (D21). It owns the live `ServiceGraph` — no other module holds a
copy — and it is the only consumer of both streams:

* **`stream:graph-updates`** — the OTel/Jaeger collector emits one `GraphUpdate` per observed
  cross-service call. The worker folds each into the in-memory graph (standard 13.4).
* **`stream:incidents`** — the correlation worker's `Incident` snapshots. For each one the
  worker walks toward callees (D2), bundles evidence (D15), scores candidates (standard 06.4),
  and emits the ranked list.

Both streams use `TypedStreamConsumer`'s at-least-once + dedup semantics, so a redelivered update is
a no-op on the graph and a redelivered incident re-ranks deterministically to the same answer
(D1, standard 06.7).

The graph is snapshotted to `RCA_GRAPH_PATH` on a timer and on SIGTERM (standard 13.6). A
corrupt snapshot never crashes the worker — it cold-starts (standard 13.6).

Run with: ``python -m inframind.rca.worker``
"""

from __future__ import annotations

import asyncio
import os
import signal as os_signal
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis

from inframind.common import stream_keys
from inframind.common.bus import TypedStreamConsumer
from inframind.common.exceptions import BusError
from inframind.common.graph_update import GraphUpdate
from inframind.common.incident import Incident
from inframind.common.logging import configure_logging, get_logger

from .change_correlation import ChangeEvent
from .config import RCAParams, load_rca_params
from .evidence import Candidate
from .graph import ServiceGraph
from .rank import rank_candidates

log = get_logger(__name__)


class IncidentAnalysis(BaseModel):
    """The RCA output for one incident: the ranked candidates plus the graph version used.

    This is the artefact P5b (`llm/`) consumes. It is *not* published on `stream:incidents` —
    the surface map keeps RCA output in-process until the storage/API stages land in P6.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_id: str
    graph_version: int
    window: tuple[datetime, datetime]
    candidates: list[Candidate] = Field(default_factory=list)

    @property
    def primary(self) -> Candidate | None:
        """The top-ranked candidate, or None when the walk found nothing."""
        return self.candidates[0] if self.candidates else None


class RCAWorker:
    """Consumes graph updates and incidents; owns the graph and produces ranked candidates."""

    def __init__(
        self,
        redis: Redis,
        graph: ServiceGraph,
        *,
        params: RCAParams | None = None,
        graph_group: str = "rca-graph",
        incident_group: str = "rca-incidents",
        consumer: str = "rca-1",
        block_ms: int = 1000,
        start_id: str = "0",
        change_events: Sequence[ChangeEvent] = (),
    ) -> None:
        self._redis = redis
        self._graph = graph
        self._params = params or RCAParams()
        self._graph_consumer = TypedStreamConsumer(
            redis,
            graph_group,
            consumer,
            GraphUpdate,
            stream=stream_keys.GRAPH_UPDATES,
            block_ms=block_ms,
            start_id=start_id,
        )
        self._incident_consumer = TypedStreamConsumer(
            redis,
            incident_group,
            consumer,
            Incident,
            stream=stream_keys.INCIDENTS,
            block_ms=block_ms,
            start_id=start_id,
        )
        # Change events are injected (the K8s watcher lands with P8 packaging); the ranker
        # reads them but the worker never fabricates one.
        self._change_events: list[ChangeEvent] = list(change_events)

    # --- graph stream ---

    async def ensure_groups(self) -> None:
        await self._graph_consumer.ensure_group()
        await self._incident_consumer.ensure_group()

    async def poll_graph_once(self) -> int:
        """Read and apply one batch of graph updates."""
        return await self._graph_consumer.poll_once(self.apply_graph_update)

    async def apply_graph_update(self, update: GraphUpdate) -> None:
        """Fold one graph update into the graph (standard 13.4)."""
        self._graph.apply_update(update)

    # --- incident stream ---

    async def poll_incidents_once(self) -> int:
        """Read and analyse one batch of incidents."""
        return await self._incident_consumer.poll_once(self.handle_incident)

    async def handle_incident(self, incident: Incident) -> None:
        """Analyse one incident snapshot and log the ranking."""
        analysis = self.analyse(incident)
        primary = analysis.primary
        log.info(
            "rca_incident_analysed",
            incident_id=analysis.incident_id,
            revision=incident.revision,
            state=incident.state,
            graph_version=analysis.graph_version,
            candidate_count=len(analysis.candidates),
            primary=primary.service if primary else None,
            primary_score=primary.score if primary else None,
        )

    def analyse(self, incident: Incident) -> IncidentAnalysis:
        """Pure RCA: walk, bundle evidence, score. No I/O, no clock (standard 06.7).

        `incident.closed_at` may be None while the incident is still open; the window then runs
        to `incident.updated_at`, so an open incident is still analysable. The ranked result is
        logged by `handle_incident`; this method returns the artefact for callers that need it
        without going through the bus (P5b, P6).
        """
        closed = incident.closed_at or incident.updated_at
        window = (incident.opened_at, closed)

        symptoms = _symptom_services(incident)
        candidates = rank_candidates(
            incident.id,
            symptoms,
            incident.anomalies,
            self._graph,
            self._change_events,
            window=window,
            params=self._params,
        )

        return IncidentAnalysis(
            incident_id=incident.id,
            graph_version=self._graph.version(),
            window=window,
            candidates=candidates,
        )

    # --- graph snapshot (standard 13.6) ---

    async def snapshot_loop(self, stop: asyncio.Event) -> None:
        """Persist the graph every ``snapshot_interval_s`` until `stop` is set."""
        interval = self._params.snapshot_interval_s
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                self._graph.persist()

    def persist_now(self) -> None:
        """Write the graph immediately (called on SIGTERM)."""
        self._graph.persist()

    # --- main loop ---

    async def run(self, stop: asyncio.Event) -> None:
        """Poll both streams and snapshot the graph until `stop` is set."""
        log.info(
            "rca_worker_started",
            graph_stream=stream_keys.GRAPH_UPDATES,
            incident_stream=stream_keys.INCIDENTS,
            graph_path=str(self._graph._seed_path) if self._graph._seed_path else None,
        )
        await self.ensure_groups()
        snapshot_task = asyncio.create_task(self.snapshot_loop(stop))
        try:
            while not stop.is_set():
                try:
                    await self.poll_graph_once()
                    await self.poll_incidents_once()
                except BusError as e:
                    log.warning("rca_worker_bus_error", error=str(e))
                    await asyncio.sleep(1.0)
        finally:
            snapshot_task.cancel()
            self.persist_now()


def _symptom_services(incident: Incident) -> list[str]:
    """The services to start the walk from.

    Prefer the services that actually carry anomalies; fall back to the incident's service list
    when an incident has no anomalies yet (an Alertmanager-only open, for example).
    """
    with_anomalies = sorted({a.service for a in incident.anomalies})
    return with_anomalies or sorted(incident.services)


async def amain() -> None:
    """Run until SIGINT/SIGTERM. Reads REDIS_URL, RCA_GRAPH_PATH, INFRAMIND_RCA_*."""
    configure_logging()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (os_signal.SIGINT, os_signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    params = load_rca_params()
    graph_path = Path(os.environ.get("RCA_GRAPH_PATH", "data/service-graph.json"))
    graph = ServiceGraph(seed_path=graph_path, params=params)
    redis = Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))

    worker = RCAWorker(redis, graph, params=params)
    try:
        await worker.run(stop)
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(amain())
