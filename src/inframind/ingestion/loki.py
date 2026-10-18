"""Loki collector: pulls warning/error log lines and publishes them as `Signal`s.

Only lines that classify as `warning` or `error` become signals; the volume of debug/info lines
(readiness probes alone are constant) would swamp the bus and the detector. A LogQL line filter
narrows the query server-side and `classify_line` makes the final call, so a `debug` JSON line
that merely mentions "error" is dropped. The raw line is never stored: only a truncated message,
the level, and a few structured fields, per the Signal contract.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from redis.asyncio import Redis

from inframind.common import stream_keys
from inframind.common.bus import publish
from inframind.common.collector_state import CollectorState, MemoryState, State
from inframind.common.exceptions import BusError
from inframind.common.logging import get_logger
from inframind.common.redact import redact
from inframind.common.signal import (
    Severity,
    Signal,
    SignalType,
    Source,
    normalise_service,
    service_from_pod,
)
from inframind.common.time import utcnow
from inframind.ingestion.runner import run_collector

log = get_logger(__name__)

DEFAULT_QUERY = (
    '{namespace="default", app!=""} '
    '|~ "(?i)(error|warn|fatal|panic|exception|critical|severe|traceback)"'
)
MAX_MESSAGE_CHARS = 500
_NS = 1_000_000_000

_LEVELS = {
    "warn": "warning",
    "warning": "warning",
    "error": "error",
    "err": "error",
    "fatal": "error",
    "panic": "error",
    "critical": "error",
    "crit": "error",
    "severe": "error",
}
_LEVEL_KEYS = ("severity", "level", "lvl", "log.level", "levelname")
_MESSAGE_KEYS = ("message", "msg")
_ERROR_KEYS = ("error", "err", "exception")
_TRACE_KEYS = ("trace_id", "traceId", "traceID", "trace.id")
_STATUS_KEYS = ("http.resp.status", "http_status", "status")
_GRPC_CODE = re.compile(r"\bcode = (\w+)")
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_TRACE_IN_TEXT = re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")
_TEXT_ERROR = re.compile(
    r"\b(?:ERROR|FATAL|PANIC|CRITICAL|SEVERE)\b|level=(?:error|fatal)|Traceback \(most recent"
    r"|\b[\w.]*Exception\b|\b[A-Z]\w*Error\b"
)
_TEXT_WARNING = re.compile(r"\b(?:WARN|WARNING)\b|level=warn")
_SEVERITY = {"warning": Severity.WARNING, "error": Severity.CRITICAL}


@dataclass(frozen=True)
class ParsedLine:
    """What `classify_line` could tell about one log line. `level` is None for non-problems."""

    level: str | None
    message: str = ""
    http_status: int | None = None
    trace_id: str | None = None
    grpc_code: str | None = None


def _first(fields: dict[str, Any], keys: tuple[str, ...]) -> Any:
    return next((fields[k] for k in keys if k in fields), None)


def _text_level(text: str) -> str | None:
    if _TEXT_ERROR.search(text):
        return "error"
    if _TEXT_WARNING.search(text):
        return "warning"
    return None


def classify_line(line: str) -> ParsedLine:
    """Classify one log line as `warning`, `error` or neither, extracting structured fields."""
    stripped = line.strip()
    if stripped.startswith("{"):
        try:
            fields = json.loads(stripped)
        except ValueError:
            fields = None
        if isinstance(fields, dict):
            return _classify_json(fields)
    trace = _TRACE_IN_TEXT.search(line)
    return ParsedLine(
        level=_text_level(line),
        message=redact(stripped)[:MAX_MESSAGE_CHARS],
        trace_id=trace.group(0) if trace else None,
        grpc_code=_grpc_code(line),
    )


def _grpc_code(text: str) -> str | None:
    match = _GRPC_CODE.search(text)
    return match.group(1) if match else None


def _classify_json(fields: dict[str, Any]) -> ParsedLine:
    message = str(_first(fields, _MESSAGE_KEYS) or "")
    detail = _first(fields, _ERROR_KEYS)
    if detail:  # the failing callee and gRPC code usually live here, not in `message`
        message = f"{message}: {detail}" if message else str(detail)
    message = redact(message)[:MAX_MESSAGE_CHARS]
    raw_level = _first(fields, _LEVEL_KEYS)
    # An explicit level always wins: a debug/info line mentioning "error" is not a problem.
    level = _LEVELS.get(str(raw_level).lower()) if raw_level is not None else _text_level(message)
    status = _first(fields, _STATUS_KEYS)
    trace = _first(fields, _TRACE_KEYS)
    return ParsedLine(
        level=level,
        message=message,
        http_status=status if isinstance(status, int) and not isinstance(status, bool) else None,
        trace_id=trace if isinstance(trace, str) and _TRACE_ID.match(trace) else None,
        grpc_code=_grpc_code(message),
    )


def _ns_to_datetime(ts_ns: int) -> datetime:
    return datetime.fromtimestamp(ts_ns // _NS, tz=UTC) + timedelta(
        microseconds=(ts_ns % _NS) // 1000
    )


def entry_to_signal(
    labels: dict[str, str], ts_ns: str, line: str, *, ingested_at: datetime
) -> Signal | None:
    """Map one Loki log entry to a `Signal`; None when it is not a warning/error or is unusable."""
    try:
        observed = _ns_to_datetime(int(ts_ns))
    except (TypeError, ValueError, OverflowError):
        return None
    parsed = classify_line(line)
    if parsed.level is None:
        return None
    pod = labels.get("pod", "")
    service = (
        normalise_service(labels["app"])
        if labels.get("app")
        else (service_from_pod(pod) if pod else normalise_service(""))
    )
    attrs: dict[str, Any] = {
        "level": parsed.level,
        "message": parsed.message,
        "pod": pod,
        "container": labels.get("container", ""),
        "namespace": labels.get("namespace", ""),
        "ingested_at": ingested_at.isoformat(),
    }
    if parsed.http_status is not None:
        attrs["http_status"] = parsed.http_status
    if parsed.grpc_code is not None:
        attrs["grpc_code"] = parsed.grpc_code
    return Signal(
        ts=observed,
        source=Source.LOKI,
        type=SignalType.LOG,
        service=service,
        severity=_SEVERITY[parsed.level],
        trace_id=parsed.trace_id,
        attrs=attrs,
    )


def _to_ns(moment: datetime) -> int:
    return int(moment.timestamp()) * _NS + moment.microsecond * 1000


class LokiCollector:
    """Polls Loki on an interval and publishes new warning/error lines to `stream:signals`.

    Loki ingests with a shipping delay, so each poll re-reads `overlap_s` behind the newest
    entry seen and a seen-set suppresses the repeats. A line that arrives later than the overlap
    is missed; that is the price of not storing server-side state.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        redis: Redis,
        *,
        query: str = DEFAULT_QUERY,
        interval_s: int = 30,
        initial_lookback_s: int = 60,
        overlap_s: int = 10,
        limit: int = 1000,
        stream: str = stream_keys.SIGNALS,
        state: State | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._client = client
        self._redis = redis
        self._query = query
        self._interval_s = interval_s
        self._initial_lookback_ns = initial_lookback_s * _NS
        self._overlap_ns = overlap_s * _NS
        self._limit = limit
        self._stream = stream
        self._clock = clock
        self._state: State = state or MemoryState()
        self._cursor_loaded = False
        self._cursor_ns: int | None = None

    async def poll_once(self) -> int:
        """Query Loki once. Returns the number of signals published."""
        if not self._cursor_loaded:
            try:
                self._cursor_ns = await self._state.load_cursor()
            except BusError as e:
                log.warning("loki_state_unavailable", error=str(e))
                return 0
            self._cursor_loaded = True
        now = self._clock()
        now_ns = _to_ns(now)
        start_ns = (
            now_ns - self._initial_lookback_ns
            if self._cursor_ns is None
            else self._cursor_ns - self._overlap_ns
        )
        try:
            streams = await self._fetch(start_ns, now_ns)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            log.warning("loki_query_failed", error=str(e))
            return 0
        published, newest_ns, complete = await self._publish_streams(streams, now)
        if complete and newest_ns is not None:
            self._cursor_ns = max(newest_ns, self._cursor_ns or 0)
            try:
                await self._state.save_cursor(self._cursor_ns)
            except BusError as e:
                log.warning("loki_cursor_save_failed", error=str(e))
        return published

    async def run(self, stop: asyncio.Event) -> None:
        """Poll until `stop` is set. Never raises on backend or bus failures."""
        log.info("collector_started", backend="loki", interval_s=self._interval_s)
        while not stop.is_set():
            await self.poll_once()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval_s)

    async def _fetch(self, start_ns: int, end_ns: int) -> list[dict[str, Any]]:
        params: dict[str, str | int] = {
            "query": self._query,
            "start": start_ns,
            "end": end_ns,
            "limit": self._limit,
            "direction": "forward",
        }
        resp = await self._client.get("/loki/api/v1/query_range", params=params)
        resp.raise_for_status()
        body = resp.json()
        if body["status"] != "success":
            raise ValueError(f"loki status={body['status']} error={body.get('error')}")
        return list(body["data"]["result"])

    async def _publish_streams(
        self, streams: list[dict[str, Any]], now: datetime
    ) -> tuple[int, int | None, bool]:
        """Publish new entries. Returns (published, newest ts seen, False if the bus failed)."""
        published = 0
        newest: int | None = None
        total = 0
        for stream in streams:
            labels: dict[str, str] = stream.get("stream", {})
            for value in stream.get("values", []):
                total += 1
                try:
                    ts_ns, line = int(value[0]), str(value[1])
                except (IndexError, TypeError, ValueError):
                    log.debug("loki_entry_malformed")
                    continue
                digest = hashlib.blake2b(line.encode(), digest_size=8).hexdigest()
                key = f"{labels.get('pod', '')}:{ts_ns}:{digest}"
                newest = ts_ns if newest is None else max(newest, ts_ns)
                try:
                    if await self._state.is_seen(key):
                        continue
                    signal = entry_to_signal(labels, str(ts_ns), line, ingested_at=now)
                    if (
                        signal is not None
                        and await publish(self._redis, signal, stream=self._stream) is not None
                    ):
                        published += 1
                    await self._state.mark_seen(key)
                except BusError as e:
                    log.warning("loki_publish_failed", error=str(e))
                    return published, newest, False
        if total >= self._limit:
            log.warning("loki_result_truncated", limit=self._limit)
        return published, newest, True


async def amain() -> None:
    """Run the collector until SIGINT/SIGTERM. Reads LOKI_URL, REDIS_URL, LOKI_INTERVAL_S."""
    await run_collector(
        lambda client, redis, interval_s: LokiCollector(
            client, redis, interval_s=interval_s, state=CollectorState(redis, "loki")
        ),
        url_env="LOKI_URL",
        default_url="http://localhost:30100",
        interval_env="LOKI_INTERVAL_S",
        timeout_s=15.0,
    )


if __name__ == "__main__":
    asyncio.run(amain())
