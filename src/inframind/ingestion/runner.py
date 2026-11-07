"""Shared process wrapper for the pull collectors: env config, signals, resource cleanup."""

from __future__ import annotations

import asyncio
import logging
import os
import signal as os_signal
from collections.abc import Callable
from typing import Protocol

import httpx
from redis.asyncio import Redis

from inframind.common.logging import configure_logging
from inframind.metrics import start_http_server

DEFAULT_INTERVAL_S = 30

#: Port the worker's Prometheus endpoint binds (standard 11 §11.12). The chart's PodMonitor
#: scrapes this; the api process uses 8080 and is scraped by the ServiceMonitor instead.
METRICS_PORT = 9100


class Collector(Protocol):
    """Anything that polls until told to stop."""

    async def run(self, stop: asyncio.Event) -> None:
        """Poll until `stop` is set."""


async def run_collector(
    build: Callable[[httpx.AsyncClient, Redis, int], Collector],
    *,
    url_env: str,
    default_url: str,
    interval_env: str,
    timeout_s: float = 15.0,
    stop: asyncio.Event | None = None,
) -> None:
    """Build a collector from the environment and run it until SIGINT/SIGTERM (or `stop`).

    Reads `url_env` (backend base URL), `interval_env` (poll seconds) and `REDIS_URL`. The HTTP
    and Redis clients are closed on every exit path, including a crash of the collector.

    The Prometheus endpoint is started after logging is configured so a bind failure is
    recorded as a structured warning rather than a silent no-op.
    """
    configure_logging()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    start_http_server(int(os.environ.get("METRICS_PORT", str(METRICS_PORT))))
    if stop is None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (os_signal.SIGINT, os_signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
    redis = Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    interval_s = int(os.environ.get(interval_env, str(DEFAULT_INTERVAL_S)))
    base_url = os.environ.get(url_env, default_url)
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(timeout_s)) as client:
            await build(client, redis, interval_s).run(stop)
    finally:
        await redis.aclose()
