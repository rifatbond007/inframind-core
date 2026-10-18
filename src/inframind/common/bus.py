"""Redis Streams event bus: typed `Signal` publish and an idempotent consumer-group reader.

The bus is a conduit, not the source of truth (D20): streams are length-capped and may trim.

Delivery is at-least-once. The consumer skips a signal whose id it already handled (a per-group
dedup key written *after* the handler succeeds, *before* the ACK), so redelivery after a crash
between ACK-less handler success and the ACK does not run the handler twice. A crash between the
handler returning and the dedup write can still run the handler twice, so handlers must tolerate
that rare repeat.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError
from redis import exceptions as redis_exc
from redis.asyncio import Redis

from . import stream_keys
from .exceptions import BusError
from .graph_update import GraphUpdate
from .incident import Incident
from .logging import get_logger
from .signal import Signal

log = get_logger(__name__)

_FIELD = "data"
DEFAULT_MAXLEN = 100_000
DEFAULT_DEDUP_TTL_S = 24 * 3600

Handler = Callable[[Signal], Awaitable[None]]


async def publish(
    client: Redis,
    signal: Signal,
    *,
    stream: str = stream_keys.SIGNALS,
    maxlen: int = DEFAULT_MAXLEN,
) -> str | None:
    """XADD a signal to `stream`.

    Returns:
        The Redis stream entry id, or None when the signal was dropped (unresolved service).

    Raises:
        BusError: Redis was unreachable or rejected the write.
    """
    if not signal.is_routable:
        log.warning("signal_dropped_unknown_service", signal_id=signal.id, source=signal.source)
        return None
    try:
        entry_id = await client.xadd(
            stream, {_FIELD: signal.model_dump_json()}, maxlen=maxlen, approximate=True
        )
    except redis_exc.RedisError as e:
        raise BusError(f"publish to {stream} failed: {e}") from e
    return str(entry_id.decode() if isinstance(entry_id, bytes) else entry_id)


async def publish_graph_update(
    client: Redis,
    update: GraphUpdate,
    *,
    stream: str = stream_keys.GRAPH_UPDATES,
    maxlen: int = DEFAULT_MAXLEN,
) -> str:
    """XADD a `GraphUpdate` to `stream:graph-updates` and return the stream entry id.

    Raises:
        BusError: Redis was unreachable or rejected the write.
    """
    try:
        entry_id = await client.xadd(
            stream, {_FIELD: update.model_dump_json()}, maxlen=maxlen, approximate=True
        )
    except redis_exc.RedisError as e:
        raise BusError(f"publish to {stream} failed: {e}") from e
    return str(entry_id.decode() if isinstance(entry_id, bytes) else entry_id)


async def publish_incident(
    client: Redis,
    incident: Incident,
    *,
    stream: str = stream_keys.INCIDENTS,
    maxlen: int = DEFAULT_MAXLEN,
) -> str:
    """XADD an `Incident` snapshot to `stream:incidents` and return the stream entry id.

    Raises:
        BusError: Redis was unreachable or rejected the write.
    """
    try:
        entry_id = await client.xadd(
            stream, {_FIELD: incident.model_dump_json()}, maxlen=maxlen, approximate=True
        )
    except redis_exc.RedisError as e:
        raise BusError(f"publish to {stream} failed: {e}") from e
    return str(entry_id.decode() if isinstance(entry_id, bytes) else entry_id)


class SignalConsumer:
    """Reads `Signal`s from a stream through a consumer group, exactly-once-per-id per group."""

    def __init__(
        self,
        client: Redis,
        group: str,
        consumer: str,
        *,
        stream: str = stream_keys.SIGNALS,
        batch: int = 100,
        block_ms: int = 1000,
        reclaim_idle_ms: int = 30_000,
        dedup_ttl_s: int = DEFAULT_DEDUP_TTL_S,
        start_id: str = "0",
    ) -> None:
        self._client = client
        self._group = group
        self._consumer = consumer
        self._stream = stream
        self._batch = batch
        self._block_ms = block_ms
        self._reclaim_idle_ms = reclaim_idle_ms
        self._dedup_ttl_s = dedup_ttl_s
        self._start_id = start_id

    def _dedup_key(self, signal_id: str) -> str:
        return f"dedup:{self._stream}:{self._group}:{signal_id}"

    async def ensure_group(self) -> None:
        """Create the group (and the stream) if absent, at `start_id` (`"$"` = new entries only)."""
        try:
            await self._client.xgroup_create(
                self._stream, self._group, id=self._start_id, mkstream=True
            )
        except redis_exc.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise BusError(f"cannot create group {self._group}: {e}") from e

    async def poll_once(self, handler: Handler) -> int:
        """Reclaim stale pending entries, then read one batch. Returns entries handled.

        A handler exception leaves the entry pending for redelivery; it is logged, not raised.
        """
        try:
            entries = await self._reclaim()
            entries += await self._read_new(self._batch - len(entries))
        except redis_exc.RedisError as e:
            raise BusError(f"read from {self._stream} failed: {e}") from e
        handled = 0
        for entry_id, fields in entries:
            if await self._process(entry_id, fields, handler):
                handled += 1
        return handled

    async def run(self, handler: Handler, stop: asyncio.Event) -> None:
        """Poll until `stop` is set. Bus errors are logged and retried after a short pause."""
        await self.ensure_group()
        while not stop.is_set():
            try:
                await self.poll_once(handler)
            except BusError as e:
                log.warning("bus_poll_failed", stream=self._stream, error=str(e))
                await asyncio.sleep(1.0)

    async def _reclaim(self) -> list[tuple[Any, dict[Any, Any]]]:
        _, claimed, _ = await self._client.xautoclaim(
            self._stream,
            self._group,
            self._consumer,
            min_idle_time=self._reclaim_idle_ms,
            start_id="0-0",
            count=self._batch,
        )
        return list(claimed)

    async def _read_new(self, count: int) -> list[tuple[Any, dict[Any, Any]]]:
        if count <= 0:
            return []
        resp: Any = await self._client.xreadgroup(
            self._group, self._consumer, {self._stream: ">"}, count=count, block=self._block_ms
        )
        return [entry for _, entries in resp for entry in entries]

    async def _process(self, entry_id: Any, fields: dict[Any, Any], handler: Handler) -> bool:
        raw: Any = fields.get(_FIELD) if _FIELD in fields else fields.get(_FIELD.encode())
        try:
            signal = Signal.model_validate_json(raw)
        except (ValidationError, TypeError, ValueError) as e:
            log.warning(
                "signal_poison_acked", stream=self._stream, entry=str(entry_id), error=str(e)
            )
            await self._client.xack(self._stream, self._group, entry_id)
            return False
        if await self._client.exists(self._dedup_key(signal.id)):
            log.debug("signal_duplicate_skipped", signal_id=signal.id)
            await self._client.xack(self._stream, self._group, entry_id)
            return False
        try:
            await handler(signal)
        except Exception as e:  # handler boundary: log, leave pending, keep the loop alive
            log.error("signal_handler_failed", signal_id=signal.id, error=str(e))
            return False
        await self._client.set(self._dedup_key(signal.id), 1, ex=self._dedup_ttl_s)
        await self._client.xack(self._stream, self._group, entry_id)
        return True
