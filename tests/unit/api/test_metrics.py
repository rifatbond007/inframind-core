"""`/metrics` on the api process — the hand-rolled head plus the six pipeline metrics.

The endpoint is two concatenated blocks (standard 11 section 11.12):

1. The dependency-free head the P6 testbed always scraped — `inframind_up` and
   `inframind_uptime_seconds`. This block must stay byte-identical; it is a P6 contract.
2. `inframind.metrics.render_extra()` — the six metrics from `inframind.metrics`.

`/metrics` touches neither Redis nor Postgres, so a `FakeAsyncRedis` and no pool are enough.
"""

from __future__ import annotations

import httpx
import pytest
from fakeredis import FakeAsyncRedis

from inframind.api.app import create_app


@pytest.fixture
def transport() -> httpx.ASGITransport:
    app = create_app(redis=FakeAsyncRedis(), webhook_secret="s3cret")
    return httpx.ASGITransport(app=app)


async def test_metrics_returns_prometheus_text(transport: httpx.ASGITransport) -> None:
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
        r = await client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]


async def test_metrics_keeps_the_p6_head_block(transport: httpx.ASGITransport) -> None:
    """The two hand-rolled lines the testbed has always scraped must not change."""
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
        body = (await client.get("/metrics")).text
    assert "# HELP inframind_up " in body
    assert "inframind_up 1" in body
    assert "# TYPE inframind_uptime_seconds gauge" in body


async def test_metrics_appends_the_six_pipeline_families(transport: httpx.ASGITransport) -> None:
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
        body = (await client.get("/metrics")).text
    for name in (
        "inframind_signals_received",
        "inframind_anomalies_emitted",
        "inframind_incidents_opened",
        "inframind_rca_ranking_duration_seconds",
        "inframind_llm_call_duration_seconds",
        "inframind_alerts_sent",
    ):
        assert name in body, f"/metrics is missing {name}"


async def test_metrics_head_comes_before_the_pipeline_block(transport: httpx.ASGITransport) -> None:
    """The concatenation order is the contract: P6 consumers slice on the head."""
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
        body = (await client.get("/metrics")).text
    assert body.index("inframind_up ") < body.index("inframind_signals_received")
