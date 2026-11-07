"""`/incidents` route tests — list, detail, ack, and resolve (surface-map).

The storage layer is patched so the routes can be tested without Postgres. What is under
test here is the HTTP contract: status codes, response shapes, the required `X-Actor`
header on transitions (D25), the 503 on storage failure (D20), and 404 on a missing
incident.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fakeredis import FakeAsyncRedis

from inframind.api.app import create_app
from inframind.common.incident import Incident
from inframind.common.signal import Severity
from inframind.rca.evidence import Candidate, Evidence
from inframind.storage.db import StorageUnavailable

SECRET = "s3cret"
T0 = datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC)


def _incident() -> Incident:
    return Incident(
        id="inc_001",
        revision=1,
        state="open",
        severity=Severity.WARNING,
        title="checkout latency spike",
        fingerprint="0123abcd",
        opened_at=T0,
        updated_at=T0,
        services=["frontend"],
        signals=["sig_1"],
        anomalies=[],
    )


def _candidate(service: str = "paymentservice") -> Candidate:
    return Candidate(
        id="cand_1",
        service=service,
        score=0.91,
        depth=3,
        change_correlation=0.7,
        confidence=0.8,
        evidence=[Evidence(id="ev_1", kind="metric", summary="p99 2.1s", ref="sig_1", weight=0.9)],
    )


class FakeDetail:
    """A stand-in for `IncidentDetail` with just enough surface for the route."""

    def __init__(self, incident: Incident, candidates: list[Candidate] | None = None) -> None:
        self.incident = incident
        self.candidates = candidates or []
        self.primary = self.candidates[0] if self.candidates else None
        self.evidence_by_candidate = {}


class FakeListItem:
    def __init__(self, incident: Incident) -> None:
        self.id = incident.id
        self.state = incident.state
        self.severity = incident.severity
        self.title = incident.title
        self.fingerprint = incident.fingerprint
        self.opened_at = incident.opened_at
        self.closed_at = incident.closed_at
        self.services = incident.services


@pytest.fixture
def redis() -> FakeAsyncRedis:
    return FakeAsyncRedis()


@pytest.fixture
async def client(redis: FakeAsyncRedis) -> httpx.AsyncClient:
    """An ASGI client with a non-None pool so `require_pool` passes."""
    app = create_app(redis, webhook_secret=SECRET, postgres_pool=object())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as c:
        yield c


@pytest.fixture
async def client_no_storage(redis: FakeAsyncRedis) -> httpx.AsyncClient:
    """An ASGI client with no pool — `require_pool` will 503 every incident route."""
    app = create_app(redis, webhook_secret=SECRET, postgres_pool=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as c:
        yield c


ACTOR = {"X-Actor": "rifatbond007"}


# --- list ---


async def test_list_returns_items_and_next_cursor(client: httpx.AsyncClient) -> None:
    incident = _incident()
    with patch(
        "inframind.api.incidents.list_incidents",
        AsyncMock(return_value=([FakeListItem(incident)], None)),
    ):
        resp = await client.get("/incidents")

    assert resp.status_code == 200
    body = resp.json()
    assert body["next_cursor"] is None
    assert [item["id"] for item in body["items"]] == ["inc_001"]
    assert body["items"][0]["severity"] == "warning"


async def test_list_forwards_query_params_to_storage(client: httpx.AsyncClient) -> None:
    """`state`, `service`, `since`, `limit`, and `cursor` reach the storage call."""
    mock = AsyncMock(return_value=([], None))
    with patch("inframind.api.incidents.list_incidents", mock):
        await client.get("/incidents?state=open&service=frontend&limit=10&cursor=inc_000")

    kwargs = mock.call_args.kwargs
    assert kwargs["state"] == "open"
    assert kwargs["service"] == "frontend"
    assert kwargs["limit"] == 10
    assert kwargs["cursor"] == "inc_000"


async def test_list_503_when_storage_unavailable(client: httpx.AsyncClient) -> None:
    """D20 at the HTTP edge: a storage failure is 503, never a fake empty 200."""
    with patch(
        "inframind.api.incidents.list_incidents",
        AsyncMock(side_effect=StorageUnavailable("postgres is down")),
    ):
        resp = await client.get("/incidents")

    assert resp.status_code == 503
    assert "postgres is down" in resp.json()["detail"]


async def test_list_503_when_pool_not_configured(client_no_storage: httpx.AsyncClient) -> None:
    """No pool wired → `require_pool` returns 503 before storage is even called."""
    resp = await client_no_storage.get("/incidents")
    assert resp.status_code == 503


# --- detail ---


async def test_detail_returns_incident_with_candidates(client: httpx.AsyncClient) -> None:
    incident = _incident()
    detail = FakeDetail(incident, [_candidate("paymentservice")])
    with patch("inframind.api.incidents.get_incident", AsyncMock(return_value=detail)):
        resp = await client.get("/incidents/inc_001")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "inc_001"
    assert body["primary"] == "paymentservice"
    assert body["candidates"][0]["service"] == "paymentservice"


async def test_detail_primary_is_none_when_no_candidates(client: httpx.AsyncClient) -> None:
    """An incident with no ranked candidates serialises `primary` as `null`."""
    detail = FakeDetail(_incident(), [])
    with patch("inframind.api.incidents.get_incident", AsyncMock(return_value=detail)):
        resp = await client.get("/incidents/inc_001")

    assert resp.status_code == 200
    assert resp.json()["primary"] is None


async def test_detail_404_when_missing(client: httpx.AsyncClient) -> None:
    with patch("inframind.api.incidents.get_incident", AsyncMock(return_value=None)):
        resp = await client.get("/incidents/nope")

    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


async def test_detail_503_when_storage_unavailable(client: httpx.AsyncClient) -> None:
    with patch(
        "inframind.api.incidents.get_incident",
        AsyncMock(side_effect=StorageUnavailable("postgres is down")),
    ):
        resp = await client.get("/incidents/inc_001")

    assert resp.status_code == 503


# --- ack ---


async def test_ack_transitions_and_returns_new_state(client: httpx.AsyncClient) -> None:
    mock = AsyncMock(return_value=True)
    with patch("inframind.api.incidents.transition_incident", mock):
        resp = await client.post("/incidents/inc_001/ack", headers=ACTOR)

    assert resp.status_code == 200
    assert resp.json() == {"id": "inc_001", "state": "acknowledged"}
    mock.assert_awaited_once()
    args, kwargs = mock.call_args
    assert args[0] is not None  # the injected pool from the fixture
    assert args[1] == "inc_001"
    assert kwargs["new_state"] == "acknowledged"
    assert kwargs["actor"] == "rifatbond007"
    assert kwargs["audit_action"] == "ack"


async def test_ack_requires_actor_header(client: httpx.AsyncClient) -> None:
    """D25: a transition without `X-Actor` is rejected before it touches storage."""
    with patch("inframind.api.incidents.transition_incident", AsyncMock(return_value=True)):
        resp = await client.post("/incidents/inc_001/ack")

    assert resp.status_code == 400
    assert "X-Actor" in resp.json()["detail"]


async def test_ack_rejects_display_name_actor(client: httpx.AsyncClient) -> None:
    """D25: a display name (with a space) is rejected."""
    with patch("inframind.api.incidents.transition_incident", AsyncMock(return_value=True)):
        resp = await client.post("/incidents/inc_001/ack", headers={"X-Actor": "Rifat Bond"})

    assert resp.status_code == 400


async def test_ack_rejects_ai_tool_actor(client: httpx.AsyncClient) -> None:
    """D25: an AI tool name is rejected as an actor."""
    with patch("inframind.api.incidents.transition_incident", AsyncMock(return_value=True)):
        resp = await client.post("/incidents/inc_001/ack", headers={"X-Actor": "copilot"})

    assert resp.status_code == 400


async def test_ack_404_when_incident_missing(client: httpx.AsyncClient) -> None:
    with patch("inframind.api.incidents.transition_incident", AsyncMock(return_value=False)):
        resp = await client.post("/incidents/nope/ack", headers=ACTOR)

    assert resp.status_code == 404


async def test_ack_503_when_storage_unavailable(client: httpx.AsyncClient) -> None:
    with patch(
        "inframind.api.incidents.transition_incident",
        AsyncMock(side_effect=StorageUnavailable("postgres is down")),
    ):
        resp = await client.post("/incidents/inc_001/ack", headers=ACTOR)

    assert resp.status_code == 503


# --- resolve ---


async def test_resolve_transitions_and_returns_new_state(client: httpx.AsyncClient) -> None:
    mock = AsyncMock(return_value=True)
    with patch("inframind.api.incidents.transition_incident", mock):
        resp = await client.post("/incidents/inc_001/resolve", headers=ACTOR)

    assert resp.status_code == 200
    assert resp.json() == {"id": "inc_001", "state": "resolved"}
    kwargs = mock.call_args.kwargs
    assert kwargs["new_state"] == "resolved"
    assert kwargs["audit_action"] == "resolve"


async def test_resolve_404_when_incident_missing(client: httpx.AsyncClient) -> None:
    with patch("inframind.api.incidents.transition_incident", AsyncMock(return_value=False)):
        resp = await client.post("/incidents/nope/resolve", headers=ACTOR)

    assert resp.status_code == 404


async def test_resolve_requires_actor_header(client: httpx.AsyncClient) -> None:
    with patch("inframind.api.incidents.transition_incident", AsyncMock(return_value=True)):
        resp = await client.post("/incidents/inc_001/resolve")

    assert resp.status_code == 400


# --- there is no PATCH or DELETE ---


async def test_no_patch_or_delete_route_exists(client: httpx.AsyncClient) -> None:
    """State transitions are explicit; there is no generic mutation endpoint."""
    assert (await client.patch("/incidents/inc_001", json={"state": "resolved"})).status_code == 405
    assert (await client.delete("/incidents/inc_001")).status_code == 405
