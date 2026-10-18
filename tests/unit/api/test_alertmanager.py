"""Alertmanager webhook: pure payload mapping and the authenticated POST route."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fakeredis import FakeAsyncRedis

from inframind.api.alertmanager import alerts_to_signals
from inframind.api.app import create_app
from inframind.common import stream_keys
from inframind.common.signal import Severity, Signal, SignalType, Source

NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)
SECRET = "s3cret"
URL = "/webhooks/alertmanager"


def alert(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "status": "firing",
        "labels": {
            "alertname": "KubePodCrashLooping",
            "namespace": "default",
            "pod": "frontend-5c744dd6bb-jfpxt",
            "severity": "warning",
        },
        "annotations": {"summary": "Pod is crash looping."},
        "startsAt": "2026-10-19T11:58:00.123456789Z",
        "endsAt": "0001-01-01T00:00:00Z",
        "fingerprint": "78da0ef7fc3bc57d",
    }
    base.update(over)
    return base


def payload(*alerts: dict[str, Any]) -> dict[str, Any]:
    return {"version": "4", "groupKey": '{}:{namespace="default"}', "alerts": list(alerts)}


def test_firing_alert_maps_to_signal() -> None:
    [sig] = alerts_to_signals(payload(alert()), request_id="r1", ingested_at=NOW)
    assert sig.source is Source.ALERTMANAGER
    assert sig.type is SignalType.ALERT
    assert sig.service == "frontend"
    assert sig.severity is Severity.WARNING
    assert sig.ts == datetime(2026, 10, 19, 11, 58, 0, 123456, tzinfo=UTC)
    assert sig.attrs["alertname"] == "KubePodCrashLooping"
    assert sig.attrs["status"] == "firing"
    assert sig.attrs["fingerprint"] == "78da0ef7fc3bc57d"
    assert sig.attrs["annotations"] == {"summary": "Pod is crash looping."}
    assert sig.attrs["request_id"] == "r1"
    assert sig.attrs["group_key"] == '{}:{namespace="default"}'


def test_every_alert_in_the_group_becomes_a_signal() -> None:
    other = alert(labels={"alertname": "X", "service": "cartservice", "severity": "critical"})
    sigs = alerts_to_signals(payload(alert(), other), request_id="r", ingested_at=NOW)
    assert [(s.service, s.severity) for s in sigs] == [
        ("frontend", Severity.WARNING),
        ("cartservice", Severity.CRITICAL),
    ]


@pytest.mark.parametrize(
    ("labels", "service"),
    [
        ({"deployment": "checkoutservice", "pod": "x-1234567890-abcde"}, "checkoutservice"),
        ({"pod": "adservice-5bc8b4b54b-cq6sn", "job": "kube-state-metrics"}, "adservice"),
        # Real kube-state-metrics labels: the operator-stamped `service` names the exporter.
        (
            {
                "pod": "frontend-5c744dd6bb-bmdg5",
                "job": "kube-state-metrics",
                "service": "kube-prometheus-stack-kube-state-metrics",
            },
            "frontend",
        ),
        ({"service": "Cart", "job": "j"}, "cart"),
        ({"job": "kube-etcd"}, "kube-etcd"),
    ],
)
def test_service_resolution_order(labels: dict[str, str], service: str) -> None:
    [sig] = alerts_to_signals(
        payload(alert(labels={"alertname": "A", **labels})), request_id="r", ingested_at=NOW
    )
    assert sig.service == service


def test_alert_with_no_service_labels_maps_to_unknown() -> None:
    [sig] = alerts_to_signals(
        payload(alert(labels={"alertname": "Watchdog", "severity": "none"})),
        request_id="r",
        ingested_at=NOW,
    )
    assert not sig.is_routable


def test_resolved_alert_is_info_and_observed_at_ends_at() -> None:
    resolved = alert(status="resolved", endsAt="2026-10-19T11:59:30Z")
    [sig] = alerts_to_signals(payload(resolved), request_id="r", ingested_at=NOW)
    assert sig.severity is Severity.INFO
    assert sig.ts == datetime(2026, 10, 19, 11, 59, 30, tzinfo=UTC)
    assert sig.attrs["status"] == "resolved"


@pytest.mark.parametrize("severity", ["none", "info", "", "page"])
def test_unrecognised_severity_is_info(severity: str) -> None:
    labels = {"alertname": "A", "pod": "p-1234567890-abcde", "severity": severity}
    [sig] = alerts_to_signals(payload(alert(labels=labels)), request_id="r", ingested_at=NOW)
    assert sig.severity is Severity.INFO


@pytest.mark.parametrize("bad", [{}, {"alerts": "nope"}, {"alerts": [1]}, {"alerts": [{}]}])
def test_malformed_payload_raises_value_error(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="alert"):
        alerts_to_signals(bad, request_id="r", ingested_at=NOW)


def test_bad_timestamp_raises_value_error() -> None:
    with pytest.raises(ValueError, match="startsAt"):
        alerts_to_signals(payload(alert(startsAt="yesterday")), request_id="r", ingested_at=NOW)


@pytest.fixture
def redis() -> FakeAsyncRedis:
    return FakeAsyncRedis()


@pytest.fixture
async def client(redis: FakeAsyncRedis) -> httpx.AsyncClient:
    app = create_app(redis, webhook_secret=SECRET)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as c:
        yield c


AUTH = {"Authorization": f"Bearer {SECRET}"}


async def stream(redis: FakeAsyncRedis) -> list[Signal]:
    entries = await redis.xrange(stream_keys.SIGNALS)
    return [Signal.model_validate_json(f[b"data"]) for _, f in entries]


def test_create_app_refuses_an_empty_secret(redis: FakeAsyncRedis) -> None:
    with pytest.raises(ValueError, match="secret"):
        create_app(redis, webhook_secret="")


async def test_healthz(client: httpx.AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert (resp.status_code, resp.json()) == (200, {"status": "ok"})


async def test_post_publishes_signals(client: httpx.AsyncClient, redis: FakeAsyncRedis) -> None:
    resp = await client.post(URL, json=payload(alert()), headers={**AUTH, "X-Request-ID": "req-7"})
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 1, "dropped": 0}
    [sig] = await stream(redis)
    assert sig.service == "frontend"
    assert sig.attrs["request_id"] == "req-7"


async def test_unroutable_alerts_are_counted_as_dropped(
    client: httpx.AsyncClient, redis: FakeAsyncRedis
) -> None:
    watchdog = alert(labels={"alertname": "Watchdog"})
    resp = await client.post(URL, json=payload(alert(), watchdog), headers=AUTH)
    assert resp.json() == {"accepted": 1, "dropped": 1}
    assert len(await stream(redis)) == 1


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": SECRET},
        {"Authorization": "Basic x"},
    ],
)
async def test_missing_or_wrong_secret_is_401_and_publishes_nothing(
    client: httpx.AsyncClient, redis: FakeAsyncRedis, headers: dict[str, str]
) -> None:
    resp = await client.post(URL, json=payload(alert()), headers=headers)
    assert resp.status_code == 401
    assert await stream(redis) == []


async def test_malformed_body_is_422(client: httpx.AsyncClient) -> None:
    assert (await client.post(URL, json={"alerts": 3}, headers=AUTH)).status_code == 422
    assert (await client.post(URL, content=b"not json", headers=AUTH)).status_code == 422


async def test_bus_failure_is_503_so_alertmanager_retries(client: httpx.AsyncClient) -> None:
    class Broken(FakeAsyncRedis):
        async def xadd(self, *args: Any, **kwargs: Any) -> Any:
            from redis import exceptions

            raise exceptions.ConnectionError("down")

    app = create_app(Broken(), webhook_secret=SECRET)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as c:
        assert (await c.post(URL, json=payload(alert()), headers=AUTH)).status_code == 503


async def test_service_from_pod_is_shared_with_the_prometheus_collector() -> None:
    from inframind.common.signal import service_from_pod
    from inframind.ingestion import prometheus

    assert prometheus.service_from_pod is service_from_pod


def test_secrets_and_pii_in_annotations_are_redacted_but_labels_are_kept() -> None:
    noisy = alert(
        annotations={"description": "login for a@b.com from 10.1.2.3 password=hunter2"},
    )
    [sig] = alerts_to_signals(payload(noisy), request_id="r", ingested_at=NOW)
    dumped = sig.attrs["annotations"]["description"]
    assert "a@b.com" not in dumped and "10.1.2.3" not in dumped and "hunter2" not in dumped
    assert sig.attrs["labels"]["pod"] == "frontend-5c744dd6bb-jfpxt"
