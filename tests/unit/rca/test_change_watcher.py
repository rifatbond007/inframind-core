"""K8s change watcher tests (D22, standard 14.10): filter, buffer, access, list-then-watch.

No cluster: the watch runs against an `httpx.MockTransport` that plays an API server.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from inframind.rca.change_correlation import ChangeEvent, change_correlation
from inframind.rca.change_watcher import (
    WATCHED_KINDS,
    ChangeLog,
    ChangeWatcher,
    KubeAccess,
    WatchParams,
    build_change_watcher,
    change_event_for,
    kube_access_from_kubeconfig,
    load_watch_params,
    object_state,
    service_of,
)

T0 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
KIND = {k.kind: k for k in WATCHED_KINDS}


def _deployment(name: str = "frontend", *, env: str = "a", generation: int = 1, ready: int = 1):
    return {
        "metadata": {"name": name, "generation": generation, "resourceVersion": str(generation)},
        "spec": {"template": {"spec": {"containers": [{"name": "server", "env": env}]}}},
        "status": {"readyReplicas": ready},
    }


def _configmap(data: dict[str, str], *, rv: str, annotations: dict[str, str] | None = None):
    return {
        "metadata": {
            "name": "frontend-config",
            "resourceVersion": rv,
            "labels": {"app": "frontend"},
            "annotations": annotations or {},
        },
        "data": data,
    }


def _secret_meta(*, rv: str, labels: dict[str, str] | None = None):
    """A Secret as the API sends it under `as=PartialObjectMetadata`: no data at all."""
    return {
        "metadata": {
            "name": "db-credentials",
            "resourceVersion": rv,
            "labels": labels if labels is not None else {"app": "cartservice"},
        }
    }


def _event(kind: str, old_obj: Any, new_obj: Any) -> ChangeEvent | None:
    spec = KIND[kind]
    old = object_state(spec, old_obj) if old_obj is not None else None
    return change_event_for(spec, new_obj, old, object_state(spec, new_obj), now=T0)


# --- the filter (standard 14.10) ----------------------------------------------------------


def test_deployment_status_update_is_not_an_event() -> None:
    assert _event("deployment_rollout", _deployment(ready=1), _deployment(ready=0)) is None


def test_deployment_template_change_is_one_event() -> None:
    event = _event("deployment_rollout", _deployment(env="a"), _deployment(env="b", generation=2))
    assert event is not None
    assert (event.service, event.kind, event.resource) == (
        "frontend",
        "deployment_rollout",
        "deploy/frontend",
    )
    assert (event.revision, event.prev_revision, event.occurred_at) == ("2", "1", T0)


def test_configmap_annotation_change_is_not_an_event() -> None:
    old = _configmap({"k": "v"}, rv="1")
    new = _configmap({"k": "v"}, rv="2", annotations={"note": "x"})
    assert _event("configmap_change", old, new) is None


def test_configmap_data_change_is_one_event() -> None:
    event = _event(
        "configmap_change", _configmap({"k": "v"}, rv="1"), _configmap({"k": "w"}, rv="2")
    )
    assert event is not None
    assert (event.service, event.resource, event.revision) == (
        "frontend",
        "cm/frontend-config",
        "2",
    )


def test_secret_data_change_is_one_event_and_carries_no_value() -> None:
    """A new resourceVersion with unchanged labels/annotations is a data change (D9)."""
    event = _event("secret_change", _secret_meta(rv="1"), _secret_meta(rv="2"))
    assert event is not None
    assert (event.service, event.resource) == ("cartservice", "secret/db-credentials")
    assert set(event.model_dump()) == {
        "service",
        "kind",
        "resource",
        "occurred_at",
        "revision",
        "prev_revision",
    }


def test_secret_label_change_is_not_an_event() -> None:
    old = _secret_meta(rv="1")
    new = _secret_meta(rv="2", labels={"app": "cartservice", "tier": "db"})
    assert _event("secret_change", old, new) is None


def test_secret_watch_requests_metadata_only() -> None:
    """The API never sends the watcher a Secret's data (D9)."""
    assert KIND["secret_change"].metadata_only
    assert not any(k.metadata_only for k in WATCHED_KINDS if k.kind != "secret_change")


def test_hpa_scaling_is_an_event_on_its_target() -> None:
    def hpa(desired: int) -> dict[str, Any]:
        return {
            "metadata": {"name": "frontend-hpa", "resourceVersion": str(desired)},
            "spec": {"scaleTargetRef": {"name": "frontend"}},
            "status": {"desiredReplicas": desired},
        }

    event = _event("hpa_change", hpa(1), hpa(3))
    assert event is not None
    assert (event.service, event.revision, event.prev_revision) == ("frontend", "3", "1")
    assert _event("hpa_change", hpa(3), hpa(3)) is None


def test_new_workload_is_an_event_but_a_new_secret_is_not() -> None:
    assert _event("deployment_rollout", None, _deployment()) is not None
    assert _event("secret_change", None, _secret_meta(rv="1")) is None


def test_unattributable_configmap_is_skipped() -> None:
    orphan = {"metadata": {"name": "kube-root-ca.crt", "resourceVersion": "1"}, "data": {}}
    assert service_of(KIND["configmap_change"], orphan) is None
    assert _event("configmap_change", None, orphan) is None


def test_configmap_service_falls_back_to_owner_reference() -> None:
    owned = {"metadata": {"name": "x", "ownerReferences": [{"name": "checkoutservice"}]}}
    assert service_of(KIND["configmap_change"], owned) == "checkoutservice"


# --- the buffer -----------------------------------------------------------------------------


def _change(resource: str = "deploy/frontend", revision: str = "2", *, at: datetime = T0):
    return ChangeEvent(
        service=resource.split("/")[1],
        kind="deployment_rollout",
        resource=resource,
        occurred_at=at,
        revision=revision,
    )


def test_change_log_deduplicates_by_resource_and_revision() -> None:
    log = ChangeLog()
    assert log.add(_change(revision="2"))
    assert not log.add(_change(revision="2"))
    assert log.add(_change(revision="3"))
    assert [e.revision for e in log.events()] == ["2", "3"]


def test_change_log_drops_events_past_retention() -> None:
    log = ChangeLog(retention_s=600)
    log.add(_change(revision="1", at=T0))
    log.add(_change(revision="2", at=T0 + timedelta(seconds=900)))
    assert [e.revision for e in log.events()] == ["2"]
    assert log.add(_change(revision="1", at=T0 + timedelta(seconds=901)))  # key was released


# --- access and params ----------------------------------------------------------------------


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def test_kubeconfig_with_token_and_inline_ca(tmp_path: Path) -> None:
    path = tmp_path / "config"
    path.write_text(
        json.dumps(
            {
                "current-context": "watcher",
                "contexts": [{"name": "watcher", "context": {"cluster": "c", "user": "u"}}],
                "clusters": [
                    {
                        "name": "c",
                        "cluster": {
                            "server": "https://inframind-control-plane:6443/",
                            "certificate-authority-data": _b64("CA"),
                        },
                    }
                ],
                "users": [{"name": "u", "user": {"token": "t0ken"}}],
            }
        )
    )
    access = kube_access_from_kubeconfig(path)
    assert access.server == "https://inframind-control-plane:6443"
    assert access.headers == {"Authorization": "Bearer t0ken"}
    assert isinstance(access.verify, str)
    assert Path(access.verify).read_text() == "CA"
    assert access.cert is None


def test_kubeconfig_with_client_certificates(tmp_path: Path) -> None:
    path = tmp_path / "config"
    path.write_text(
        json.dumps(
            {
                "current-context": "kind-inframind",
                "contexts": [{"name": "kind-inframind", "context": {"cluster": "c", "user": "u"}}],
                "clusters": [{"name": "c", "cluster": {"server": "https://127.0.0.1:1234"}}],
                "users": [
                    {
                        "name": "u",
                        "user": {
                            "client-certificate-data": _b64("CERT"),
                            "client-key-data": _b64("KEY"),
                        },
                    }
                ],
            }
        )
    )
    access = kube_access_from_kubeconfig(path)
    assert access.cert is not None
    assert [Path(p).read_text() for p in access.cert] == ["CERT", "KEY"]
    assert oct(Path(access.cert[1]).stat().st_mode & 0o777) == "0o600"


def test_watch_namespaces_are_mandatory() -> None:
    """Unset means do not watch, never "all namespaces" (standard 14.9)."""
    assert load_watch_params({}).namespaces == ()
    assert load_watch_params({"INFRAMIND_WATCH_NAMESPACES": " default, shop "}).namespaces == (
        "default",
        "shop",
    )
    assert build_change_watcher(ChangeLog(), env={}) is None
    access = KubeAccess("https://k8s", {}, True)
    with pytest.raises(ValueError, match="refusing to watch all namespaces"):
        ChangeWatcher(access, WatchParams(namespaces=()), ChangeLog())


def test_watcher_disabled_without_cluster_access(tmp_path: Path) -> None:
    env = {"INFRAMIND_WATCH_NAMESPACES": "default", "KUBECONFIG": str(tmp_path / "missing")}
    assert build_change_watcher(ChangeLog(), env=env) is None


# --- list-then-watch against a fake API server ---------------------------------------------


class FakeApiServer:
    """LIST returns `items`; the first WATCH streams `notifications`; later ones hang."""

    def __init__(self, items: list[dict[str, Any]], notifications: list[dict[str, Any]]) -> None:
        self.items = items
        self.notifications = notifications
        self.requests: list[httpx.Request] = []
        self.watched = asyncio.Event()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.params.get("watch") != "1":
            return httpx.Response(
                200, json={"metadata": {"resourceVersion": "10"}, "items": self.items}
            )
        if self.watched.is_set():
            return httpx.Response(200, content=b"")  # an idle watch that times out
        self.watched.set()
        lines = "".join(json.dumps(n) + "\n" for n in self.notifications)
        return httpx.Response(200, content=lines.encode())


async def test_list_then_watch_emits_only_real_changes() -> None:
    """LIST seeds silently; a status update is ignored; a template change is one event."""
    server = FakeApiServer(
        items=[_deployment(env="a")],
        notifications=[
            {"type": "MODIFIED", "object": _deployment(env="a", ready=0)},
            {"type": "MODIFIED", "object": _deployment(env="b", generation=2)},
            {"type": "BOOKMARK", "object": {"metadata": {"resourceVersion": "12"}}},
        ],
    )
    persisted: list[ChangeEvent] = []

    async def persist(event: ChangeEvent) -> None:
        persisted.append(event)

    log = ChangeLog()
    watcher = ChangeWatcher(
        KubeAccess("https://k8s", {}, True),
        WatchParams(namespaces=("default",)),
        log,
        on_change=persist,
        kinds=[KIND["deployment_rollout"]],
        now=lambda: T0,
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(server.handler), base_url="https://k8s"
    )
    spec = KIND["deployment_rollout"]
    rv = await watcher.list_once(client, "default", spec)
    assert rv == "10"
    assert log.events() == []
    rv = await watcher.watch_once(client, "default", spec, rv)
    await client.aclose()

    assert rv == "12"
    assert [(e.resource, e.revision) for e in log.events()] == [("deploy/frontend", "2")]
    assert persisted == log.events()
    watch = server.requests[1]
    assert watch.url.path == "/apis/apps/v1/namespaces/default/deployments"
    assert watch.url.params["resourceVersion"] == "10"


async def test_secret_requests_ask_for_partial_metadata() -> None:
    server = FakeApiServer(items=[_secret_meta(rv="1")], notifications=[])
    watcher = ChangeWatcher(
        KubeAccess("https://k8s", {}, True), WatchParams(namespaces=("default",)), ChangeLog()
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(server.handler), base_url="https://k8s"
    )
    rv = await watcher.list_once(client, "default", KIND["secret_change"])
    await watcher.watch_once(client, "default", KIND["secret_change"], rv)
    await client.aclose()
    assert all("PartialObjectMetadata" in r.headers["accept"] for r in server.requests)


async def test_a_persistence_failure_keeps_the_event_and_the_watch() -> None:
    async def broken(_: ChangeEvent) -> None:
        raise RuntimeError("postgres down")

    log = ChangeLog()
    watcher = ChangeWatcher(
        KubeAccess("https://k8s", {}, True),
        WatchParams(namespaces=("default",)),
        log,
        on_change=broken,
        now=lambda: T0,
    )
    spec = KIND["deployment_rollout"]
    await watcher.handle("default", spec, {"type": "ADDED", "object": _deployment()}, "1")
    assert len(log) == 1


async def test_run_stops_cleanly() -> None:
    server = FakeApiServer(items=[], notifications=[])
    watcher = ChangeWatcher(
        KubeAccess("https://k8s", {}, True),
        WatchParams(namespaces=("default",)),
        ChangeLog(),
        kinds=[KIND["deployment_rollout"]],
    )
    stop = asyncio.Event()
    transport = httpx.MockTransport(server.handler)
    original = KubeAccess.client

    def mocked(self: KubeAccess, timeout_s: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, base_url=self.server)

    KubeAccess.client = mocked  # type: ignore[method-assign]
    try:
        task = asyncio.create_task(watcher.run(stop))
        await asyncio.wait_for(server.watched.wait(), timeout=2)
        stop.set()
        await asyncio.wait_for(task, timeout=2)
    finally:
        KubeAccess.client = original  # type: ignore[method-assign]


# --- end to end with the w4 term -----------------------------------------------------------


def test_a_watched_rollout_moves_change_correlation() -> None:
    """The dev-11 shape: a frontend rollout at the incident's open scores w4 = 1.0."""
    event = _event("deployment_rollout", _deployment(env="a"), _deployment(env="b", generation=2))
    assert event is not None
    window = (T0, T0 + timedelta(minutes=5))
    assert change_correlation("frontend", window, [event]) == pytest.approx(1.0)
    assert change_correlation("productcatalogservice", window, [event]) == 0.0
