"""`/change-events` route tests: the read surface of the K8s change log (D22).

Storage is patched; what is under test is the HTTP contract the evaluation recorder relies
on: the response shape, timezone-aware bounds, and 503 when storage is unavailable (D20).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fakeredis import FakeAsyncRedis

from inframind.api.app import create_app
from inframind.rca.change_correlation import ChangeEvent
from inframind.storage.db import StorageUnavailable

T0 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
WINDOW = {"start": "2026-11-20T11:00:00+00:00", "end": "2026-11-20T13:00:00+00:00"}


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(FakeAsyncRedis(), webhook_secret="s3cret", postgres_pool=object())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as c:
        yield c


async def test_lists_change_events_in_the_window(client: httpx.AsyncClient) -> None:
    event = ChangeEvent(
        service="frontend",
        kind="deployment_rollout",
        resource="deploy/frontend",
        occurred_at=T0,
        revision="3",
        prev_revision="2",
    )
    mock = AsyncMock(return_value=[event])
    with patch("inframind.api.change_events.list_change_events", mock):
        response = await client.get("/change-events", params={**WINDOW, "service": "frontend"})
    assert response.status_code == 200
    assert response.json()["items"] == [event.model_dump(mode="json")]
    kwargs = mock.await_args.kwargs
    assert kwargs["service"] == "frontend"
    assert kwargs["start"] == datetime(2026, 11, 20, 11, 0, tzinfo=UTC)


async def test_naive_or_reversed_bounds_are_rejected(client: httpx.AsyncClient) -> None:
    naive = {"start": "2026-11-20T11:00:00", "end": "2026-11-20T13:00:00"}
    assert (await client.get("/change-events", params=naive)).status_code == 422
    reversed_window = {"start": WINDOW["end"], "end": WINDOW["start"]}
    assert (await client.get("/change-events", params=reversed_window)).status_code == 422
    assert (await client.get("/change-events")).status_code == 422


async def test_503_when_storage_is_unavailable(client: httpx.AsyncClient) -> None:
    mock = AsyncMock(side_effect=StorageUnavailable("down"))
    with patch("inframind.api.change_events.list_change_events", mock):
        response = await client.get("/change-events", params=WINDOW)
    assert response.status_code == 503
