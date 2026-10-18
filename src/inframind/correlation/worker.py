"""The "Detection + Correlation" worker: `stream:signals` in, `stream:incidents` out.

Each signal goes through the `DetectionEngine`; anomalies go to the `IncidentTracker`; the
tracker's snapshots are published to `stream:incidents`. Incident resolution is driven by the
newest signal timestamp seen (the watermark), so a replay behaves like a live run (D6), and by
wall-clock ticks when the stream goes quiet, but only while the watermark is recent, so
replaying old signals is never resolved prematurely.

Snapshots wait in an outbox until they are published. If publishing fails after the detectors
and tracker have already changed state, the handler raises so the consumer keeps the entry
pending; on redelivery the detectors ignore the duplicate signal, and the outbox is flushed
first, so the incident is neither lost nor duplicated.
"""

from __future__ import annotations

import asyncio
import os
import signal as os_signal
from collections.abc import Callable
from datetime import datetime

from redis.asyncio import Redis

from inframind.common import stream_keys
from inframind.common.bus import SignalConsumer, publish_incident
from inframind.common.exceptions import BusError
from inframind.common.incident import Incident
from inframind.common.logging import configure_logging, get_logger
from inframind.common.signal import Signal
from inframind.common.time import utcnow
from inframind.detection.config import cooldown_s
from inframind.detection.engine import DetectionEngine

from .tracker import IncidentTracker

log = get_logger(__name__)


class CorrelationWorker:
    """Consumes signals, detects anomalies, tracks incidents, publishes snapshots."""

    def __init__(
        self,
        redis: Redis,
        engine: DetectionEngine,
        tracker: IncidentTracker,
        *,
        group: str = "correlation",
        consumer: str = "worker-1",
        idle_tick_s: float = 5.0,
        live_window_s: float = 600.0,
        block_ms: int = 1000,
        start_id: str = "0",
        clock: Callable[[], datetime] = utcnow,
        in_stream: str = stream_keys.SIGNALS,
        out_stream: str = stream_keys.INCIDENTS,
    ) -> None:
        self._redis = redis
        self._engine = engine
        self._tracker = tracker
        self._idle_tick_s = idle_tick_s
        self._live_window_s = live_window_s
        self._clock = clock
        self._out_stream = out_stream
        self._consumer = SignalConsumer(
            redis, group, consumer, stream=in_stream, block_ms=block_ms, start_id=start_id
        )
        self._outbox: list[Incident] = []
        self._watermark: datetime | None = None

    async def ensure_group(self) -> None:
        """Create the consumer group on the signals stream if needed."""
        await self._consumer.ensure_group()

    async def poll_once(self) -> int:
        """Read and handle one batch of signals; return how many were handled."""
        return await self._consumer.poll_once(self.handle)

    async def handle(self, signal: Signal) -> None:
        """Detect on one signal, advance the tracker, and publish what is ready."""
        for anomaly in self._engine.process(signal):
            self._outbox.extend(self._tracker.on_anomaly(anomaly))
        if self._watermark is None or signal.ts > self._watermark:
            self._watermark = signal.ts
        self._outbox.extend(self._tracker.tick(self._watermark))
        await self._flush()

    async def tick_idle(self) -> None:
        """Resolve quiet incidents by the wall clock, only while the stream is live."""
        now = self._clock()
        if (
            self._watermark is not None
            and (now - self._watermark).total_seconds() <= self._live_window_s
        ):
            self._outbox.extend(self._tracker.tick(now))
        await self._flush()

    async def run(self, stop: asyncio.Event) -> None:
        """Process signals until `stop` is set; bus errors are logged and retried."""
        log.info("worker_started", in_stream=stream_keys.SIGNALS, out_stream=self._out_stream)
        await self.ensure_group()
        while not stop.is_set():
            try:
                await self.poll_once()
                await self.tick_idle()
            except BusError as e:
                log.warning("worker_bus_error", error=str(e))
                await asyncio.sleep(1.0)

    async def _flush(self) -> None:
        while self._outbox:
            await publish_incident(self._redis, self._outbox[0], stream=self._out_stream)
            log.info(
                "incident_published",
                incident_id=self._outbox[0].id,
                state=self._outbox[0].state,
                revision=self._outbox[0].revision,
                services=self._outbox[0].services,
            )
            self._outbox.pop(0)


async def amain() -> None:
    """Run until SIGINT/SIGTERM. Reads REDIS_URL, INFRAMIND_DETECTION_*, BASELINE_FREEZE_WINDOW."""
    configure_logging()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (os_signal.SIGINT, os_signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    redis = Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    engine = DetectionEngine.default()
    tracker = IncidentTracker(engine.coordinator, cooldown_s=cooldown_s())
    try:
        await CorrelationWorker(redis, engine, tracker).run(stop)
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(amain())
