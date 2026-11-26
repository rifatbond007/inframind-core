"""The K8s change watcher: the data source of the RCA ``w4`` term (D22, standard 14.2-14.4).

One in-process watcher in the RCA process opens a list-then-watch stream per watched
namespace and resource kind, and turns each relevant mutation into one `ChangeEvent`:

| Kind | Path | A change is | ``revision`` |
|---|---|---|---|
| ``deployment_rollout`` | ``apps/v1 deployments`` | a new ``.spec.template`` | ``metadata.generation`` |
| ``statefulset_rollout`` | ``apps/v1 statefulsets`` | a new ``.spec.template`` | ``metadata.generation`` |
| ``daemonset_rollout`` | ``apps/v1 daemonsets`` | a new ``.spec.template`` | ``metadata.generation`` |
| ``configmap_change`` | ``v1 configmaps`` | new ``.data`` / ``.binaryData`` | ``resourceVersion`` |
| ``secret_change`` | ``v1 secrets`` (metadata only) | see below | ``resourceVersion`` |
| ``hpa_change`` | ``autoscaling/v2 horizontalpodautoscalers`` | a new desired replica count | the new count |

Status-only updates never count: a workload or ConfigMap is compared by a digest of the
filtered field only. A rolling update that replaces five pods is one event (one new
template), and every event is deduplicated by ``(resource, revision)`` in `ChangeLog`.

**Secrets are watched as `PartialObjectMetadata`** (D9): the API server never sends the
watcher a Secret's data, so no value can be read, logged or stored. A Secret counts as
changed when its ``resourceVersion`` moves while its labels and annotations do not, which
is a data (or type) change. A single update that changes labels and data together is missed;
that trade is deliberate, the alternative is reading ``.data``.

HPA scaling: an ``autoscaling/v2`` HPA has no ``.spec.replicas``, so "scaling" is a change of
``.status.desiredReplicas``, the replica count the HPA asks of its target.

The initial LIST seeds the comparison state silently: the watcher records what changes
while it watches and never reconstructs history (standard 14.9). A ``410 Gone`` watch
relists the same way. Services: a workload by its name, an HPA by its scale target, a
ConfigMap or Secret by its ``app`` / ``app.kubernetes.io/name`` label or first owner
reference; one with none of those is not attributable and is skipped.

Access: `load_kube_access` uses ``$KUBECONFIG`` when set (the compose testbed mounts a
read-only ServiceAccount kubeconfig there), else the in-cluster ServiceAccount (Helm).
Neither present means no watcher; the RCA process still runs, with ``w4`` = 0.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import tempfile
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import yaml

from inframind.common.logging import get_logger
from inframind.common.signal import UNKNOWN_SERVICE, normalise_service
from inframind.common.time import utcnow

from .change_correlation import ChangeEvent, ChangeKind

log = get_logger(__name__)

WATCH_NAMESPACES_ENV = "INFRAMIND_WATCH_NAMESPACES"
WATCH_TIMEOUT_ENV = "K8S_WATCH_TIMEOUT_S"
DEFAULT_WATCH_TIMEOUT_S = 300.0

_IN_CLUSTER_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_PARTIAL_LIST = "application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1"
_PARTIAL_ONE = "application/json;as=PartialObjectMetadata;g=meta.k8s.io;v=v1"
_MAX_BACKOFF_S = 60.0
_MIN_WATCH_S = 1.0


# --- what is watched --------------------------------------------------------------------


@dataclass(frozen=True)
class WatchedKind:
    """One watched resource kind: where it lives and how a change is recognised."""

    kind: ChangeKind
    api_prefix: str  # "/api/v1" or "/apis/<group>/<version>"
    plural: str
    short: str  # the `resource` prefix, e.g. "deploy" -> "deploy/frontend"
    metadata_only: bool = False


WATCHED_KINDS: tuple[WatchedKind, ...] = (
    WatchedKind("deployment_rollout", "/apis/apps/v1", "deployments", "deploy"),
    WatchedKind("statefulset_rollout", "/apis/apps/v1", "statefulsets", "sts"),
    WatchedKind("daemonset_rollout", "/apis/apps/v1", "daemonsets", "ds"),
    WatchedKind("configmap_change", "/api/v1", "configmaps", "cm"),
    WatchedKind("secret_change", "/api/v1", "secrets", "secret", metadata_only=True),
    WatchedKind("hpa_change", "/apis/autoscaling/v2", "horizontalpodautoscalers", "hpa"),
)

_WORKLOADS = frozenset({"deployment_rollout", "statefulset_rollout", "daemonset_rollout"})


@dataclass(frozen=True)
class ObjectState:
    """What a change is compared against. ``fingerprint`` is the filtered field's digest."""

    fingerprint: str
    revision: str | None
    resource_version: str | None = None


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _meta(obj: Mapping[str, Any]) -> Mapping[str, Any]:
    meta = obj.get("metadata")
    return meta if isinstance(meta, Mapping) else {}


def object_state(spec: WatchedKind, obj: Mapping[str, Any]) -> ObjectState:
    """The comparison state of `obj` for its kind (module docstring table)."""
    meta = _meta(obj)
    rv = meta.get("resourceVersion")
    rv = str(rv) if rv is not None else None
    if spec.kind in _WORKLOADS:
        template = (obj.get("spec") or {}).get("template")
        generation = meta.get("generation")
        return ObjectState(_digest(template), str(generation) if generation else None, rv)
    if spec.kind == "configmap_change":
        payload = {"data": obj.get("data"), "binaryData": obj.get("binaryData")}
        return ObjectState(_digest(payload), rv, rv)
    if spec.kind == "secret_change":
        # Metadata only: labels + annotations, never data (D9).
        labels_annotations = {
            "labels": meta.get("labels"),
            "annotations": meta.get("annotations"),
        }
        return ObjectState(_digest(labels_annotations), rv, rv)
    desired = (obj.get("status") or {}).get("desiredReplicas")
    return ObjectState(_digest(desired), str(desired) if desired is not None else None, rv)


def is_change(spec: WatchedKind, old: ObjectState, new: ObjectState) -> bool:
    """True when `old -> new` is a change event for `spec` (module docstring)."""
    if spec.kind == "secret_change":
        # Same labels/annotations but a new resourceVersion: the data (or type) changed.
        return old.fingerprint == new.fingerprint and old.resource_version != new.resource_version
    return old.fingerprint != new.fingerprint


def service_of(spec: WatchedKind, obj: Mapping[str, Any]) -> str | None:
    """The service `obj` belongs to, or None when it cannot be attributed."""
    meta = _meta(obj)
    if spec.kind in _WORKLOADS:
        candidate = meta.get("name")
    elif spec.kind == "hpa_change":
        candidate = ((obj.get("spec") or {}).get("scaleTargetRef") or {}).get("name")
    else:
        labels = meta.get("labels") or {}
        owners = meta.get("ownerReferences") or []
        candidate = (
            labels.get("app")
            or labels.get("app.kubernetes.io/name")
            or (owners[0].get("name") if owners and isinstance(owners[0], Mapping) else None)
        )
    if not candidate:
        return None
    service = normalise_service(str(candidate))
    return None if service == UNKNOWN_SERVICE else service


def change_event_for(
    spec: WatchedKind,
    obj: Mapping[str, Any],
    old: ObjectState | None,
    new: ObjectState,
    *,
    now: datetime,
) -> ChangeEvent | None:
    """The `ChangeEvent` for a watch notification, or None when it is not a change.

    `old` is None for an object the watcher had not seen (created while watching): that is
    a change for every kind but a Secret, whose creation carries no comparable state.
    """
    if old is None:
        if spec.kind == "secret_change":
            return None
    elif not is_change(spec, old, new):
        return None
    service = service_of(spec, obj)
    if service is None:
        return None
    name = _meta(obj).get("name", "")
    return ChangeEvent(
        service=service,
        kind=spec.kind,
        resource=f"{spec.short}/{name}",
        occurred_at=now,
        revision=new.revision,
        prev_revision=old.revision if old else None,
    )


# --- the in-memory buffer ---------------------------------------------------------------


@dataclass
class ChangeLog:
    """The hot-path ring buffer the ranker reads (standard 14.8), deduplicated.

    Events older than `retention_s` are dropped on every add; the ranker only looks 30 min
    back (`CHANGE_CORRELATION_PRE_WINDOW_S`), so the default keeps twice that.
    """

    retention_s: float = 3600.0
    max_events: int = 10_000
    _events: deque[ChangeEvent] = field(default_factory=deque)
    _keys: set[tuple[str, str | None]] = field(default_factory=set)

    def add(self, event: ChangeEvent) -> bool:
        """Append `event` unless its ``(resource, revision)`` is already held. True if added."""
        key = (event.resource, event.revision)
        if key in self._keys:
            return False
        self._events.append(event)
        self._keys.add(key)
        self._prune(event.occurred_at)
        return True

    def extend(self, events: Iterable[ChangeEvent]) -> None:
        """Add each event (cold-start seeding from Postgres)."""
        for event in sorted(events, key=lambda e: e.occurred_at):
            self.add(event)

    def events(self) -> list[ChangeEvent]:
        """A snapshot of the buffered events, oldest first."""
        return list(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.retention_s)
        while self._events and (
            self._events[0].occurred_at < cutoff or len(self._events) > self.max_events
        ):
            old = self._events.popleft()
            self._keys.discard((old.resource, old.revision))


# --- API access ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KubeAccess:
    """How to reach the API server: base URL, auth header, TLS settings."""

    server: str
    headers: Mapping[str, str]
    verify: str | bool
    cert: tuple[str, str] | None = None

    def client(self, timeout_s: float) -> httpx.AsyncClient:
        """An `httpx.AsyncClient` for this cluster; the read timeout outlasts a watch."""
        return httpx.AsyncClient(
            base_url=self.server,
            headers=dict(self.headers),
            verify=self.verify,
            cert=self.cert,
            timeout=httpx.Timeout(10.0, read=timeout_s + 30.0),
        )


def _materialise(data_b64: str, directory: Path, name: str) -> str:
    path = directory / name
    path.write_bytes(base64.b64decode(data_b64))
    path.chmod(0o600)
    return str(path)


def kube_access_from_kubeconfig(path: Path) -> KubeAccess:
    """Parse the current context of a kubeconfig (token or client-certificate auth).

    Inline ``*-data`` fields are written to a private temp directory, because TLS libraries
    read certificates from files.

    Raises:
        ValueError: the kubeconfig has no usable current context, cluster or user.
    """
    config = yaml.safe_load(path.read_text()) or {}
    by_name = {
        section: {e["name"]: e.get(section[:-1], {}) for e in config.get(section) or []}
        for section in ("contexts", "clusters", "users")
    }
    context = by_name["contexts"].get(config.get("current-context", ""))
    if not context:
        raise ValueError(f"{path}: no current context")
    cluster = by_name["clusters"].get(context.get("cluster", ""))
    user = by_name["users"].get(context.get("user", ""), {})
    if not cluster or not cluster.get("server"):
        raise ValueError(f"{path}: the current context has no cluster server")

    scratch = Path(tempfile.mkdtemp(prefix="inframind-kube-"))
    verify: str | bool = True
    if cluster.get("insecure-skip-tls-verify"):
        verify = False
    elif cluster.get("certificate-authority-data"):
        verify = _materialise(cluster["certificate-authority-data"], scratch, "ca.crt")
    elif cluster.get("certificate-authority"):
        verify = str(cluster["certificate-authority"])

    headers: dict[str, str] = {}
    cert: tuple[str, str] | None = None
    token = user.get("token")
    if not token and user.get("tokenFile"):
        token = Path(user["tokenFile"]).read_text().strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif user.get("client-certificate-data") and user.get("client-key-data"):
        cert = (
            _materialise(user["client-certificate-data"], scratch, "client.crt"),
            _materialise(user["client-key-data"], scratch, "client.key"),
        )
    elif user.get("client-certificate") and user.get("client-key"):
        cert = (str(user["client-certificate"]), str(user["client-key"]))
    return KubeAccess(str(cluster["server"]).rstrip("/"), headers, verify, cert)


def kube_access_in_cluster(directory: Path = _IN_CLUSTER_DIR) -> KubeAccess | None:
    """The pod's ServiceAccount, or None outside a cluster."""
    host = os.environ.get("KUBERNETES_SERVICE_HOST")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    token_file = directory / "token"
    if not host or not token_file.is_file():
        return None
    if ":" in host:  # IPv6
        host = f"[{host}]"
    return KubeAccess(
        f"https://{host}:{port}",
        {"Authorization": f"Bearer {token_file.read_text().strip()}"},
        str(directory / "ca.crt"),
    )


def load_kube_access(env: Mapping[str, str] | None = None) -> KubeAccess | None:
    """``$KUBECONFIG`` if it names a file, else the in-cluster ServiceAccount, else None."""
    env = os.environ if env is None else env
    kubeconfig = env.get("KUBECONFIG", "").strip()
    if kubeconfig:
        path = Path(kubeconfig)
        if path.is_file():
            return kube_access_from_kubeconfig(path)
        log.warning("change_watcher_kubeconfig_missing", path=kubeconfig)
    return kube_access_in_cluster()


@dataclass(frozen=True)
class WatchParams:
    """``INFRAMIND_WATCH_NAMESPACES`` (mandatory, never "all") and the watch timeout."""

    namespaces: tuple[str, ...]
    watch_timeout_s: float = DEFAULT_WATCH_TIMEOUT_S


def load_watch_params(env: Mapping[str, str] | None = None) -> WatchParams:
    """Read the watcher settings. An empty namespace list means: do not watch."""
    env = os.environ if env is None else env
    namespaces = tuple(
        ns.strip() for ns in env.get(WATCH_NAMESPACES_ENV, "").split(",") if ns.strip()
    )
    timeout = float(env.get(WATCH_TIMEOUT_ENV, "") or DEFAULT_WATCH_TIMEOUT_S)
    return WatchParams(namespaces=namespaces, watch_timeout_s=timeout)


# --- the watcher --------------------------------------------------------------------------

OnChange = Callable[[ChangeEvent], Awaitable[None]]


class _Gone(Exception):
    """The watch's resourceVersion expired (HTTP 410); relist."""


class ChangeWatcher:
    """List-then-watch every (namespace, kind) pair and feed `ChangeLog` (module docstring).

    `on_change` (the composition root persists to Postgres there) runs for every new event;
    its failure is logged and never stops the watch, the event stays in the buffer.
    """

    def __init__(
        self,
        access: KubeAccess,
        params: WatchParams,
        change_log: ChangeLog,
        *,
        on_change: OnChange | None = None,
        kinds: Sequence[WatchedKind] = WATCHED_KINDS,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        if not params.namespaces:
            raise ValueError(f"{WATCH_NAMESPACES_ENV} is empty; refusing to watch all namespaces")
        self._access = access
        self._params = params
        self._log = change_log
        self._on_change = on_change
        self._kinds = tuple(kinds)
        self._now = now
        self._state: dict[tuple[str, str, str], ObjectState] = {}

    async def run(self, stop: asyncio.Event) -> None:
        """Watch until `stop` is set."""
        log.info(
            "change_watcher_started",
            server=self._access.server,
            namespaces=list(self._params.namespaces),
            kinds=[k.kind for k in self._kinds],
        )
        async with self._access.client(self._params.watch_timeout_s) as client:
            tasks = [
                asyncio.create_task(self._watch_forever(client, ns, spec, stop))
                for ns in self._params.namespaces
                for spec in self._kinds
            ]
            try:
                await stop.wait()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _watch_forever(
        self, client: httpx.AsyncClient, namespace: str, spec: WatchedKind, stop: asyncio.Event
    ) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                rv = await self.list_once(client, namespace, spec)
                backoff = 1.0
                while not stop.is_set():
                    started = time.monotonic()
                    rv = await self.watch_once(client, namespace, spec, rv)
                    if time.monotonic() - started < _MIN_WATCH_S:
                        # A watch that ends at once (a proxy closing it, a misbehaving
                        # server) must not become a hot loop that starves the event loop.
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(stop.wait(), timeout=_MIN_WATCH_S)
            except _Gone:
                log.info("change_watcher_relist", namespace=namespace, kind=spec.kind)
            except (httpx.HTTPError, ValueError, KeyError) as e:
                log.warning(
                    "change_watcher_error",
                    namespace=namespace,
                    kind=spec.kind,
                    error=str(e),
                    retry_in_s=backoff,
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                backoff = min(_MAX_BACKOFF_S, backoff * 2)

    def _path(self, namespace: str, spec: WatchedKind) -> str:
        return f"{spec.api_prefix}/namespaces/{namespace}/{spec.plural}"

    async def list_once(self, client: httpx.AsyncClient, namespace: str, spec: WatchedKind) -> str:
        """LIST and seed the comparison state silently. Returns the list's resourceVersion."""
        headers = {"Accept": _PARTIAL_LIST} if spec.metadata_only else {}
        response = await client.get(self._path(namespace, spec), headers=headers)
        response.raise_for_status()
        body = response.json()
        for obj in body.get("items") or []:
            name = _meta(obj).get("name")
            if name:
                self._state[(namespace, spec.kind, name)] = object_state(spec, obj)
        return str(_meta(body).get("resourceVersion", ""))

    async def watch_once(
        self, client: httpx.AsyncClient, namespace: str, spec: WatchedKind, rv: str
    ) -> str:
        """One watch request (up to the timeout). Returns the resourceVersion to resume from.

        Raises:
            _Gone: the API answered 410, the caller relists.
        """
        headers = {"Accept": _PARTIAL_ONE} if spec.metadata_only else {}
        query = {
            "watch": "1",
            "resourceVersion": rv,
            "allowWatchBookmarks": "true",
            "timeoutSeconds": str(int(self._params.watch_timeout_s)),
        }
        async with client.stream(
            "GET", self._path(namespace, spec), params=query, headers=headers
        ) as response:
            if response.status_code == 410:
                raise _Gone
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                rv = await self.handle(namespace, spec, json.loads(line), rv)
        return rv

    async def handle(
        self, namespace: str, spec: WatchedKind, notification: Mapping[str, Any], rv: str
    ) -> str:
        """Apply one watch notification. Returns the resourceVersion to resume from."""
        kind = notification.get("type")
        obj = notification.get("object") or {}
        if kind == "ERROR":
            if obj.get("code") == 410:
                raise _Gone
            raise ValueError(f"watch error: {obj.get('message', obj)}")
        new_rv = str(_meta(obj).get("resourceVersion") or rv)
        name = _meta(obj).get("name")
        if kind == "BOOKMARK" or not name:
            return new_rv
        key = (namespace, spec.kind, name)
        if kind == "DELETED":
            self._state.pop(key, None)
            return new_rv
        new = object_state(spec, obj)
        old = self._state.get(key)
        self._state[key] = new
        event = change_event_for(spec, obj, old, new, now=self._now())
        if event is not None and self._log.add(event):
            log.info(
                "change_event",
                service=event.service,
                kind=event.kind,
                resource=event.resource,
                revision=event.revision,
            )
            if self._on_change is not None:
                try:
                    await self._on_change(event)
                except Exception as e:  # persistence must never stop the watch
                    log.warning("change_event_persist_failed", error=str(e))
        return new_rv


def build_change_watcher(
    change_log: ChangeLog,
    *,
    on_change: OnChange | None = None,
    env: Mapping[str, str] | None = None,
) -> ChangeWatcher | None:
    """The watcher from env, or None (logged) when there is no cluster or no namespace."""
    params = load_watch_params(env)
    if not params.namespaces:
        log.warning("change_watcher_disabled", reason=f"{WATCH_NAMESPACES_ENV} is not set")
        return None
    try:
        access = load_kube_access(env)
    except (OSError, ValueError, yaml.YAMLError) as e:
        log.warning("change_watcher_disabled", reason=f"bad kubeconfig: {e}")
        return None
    if access is None:
        log.warning("change_watcher_disabled", reason="no kubeconfig and not in a cluster")
        return None
    return ChangeWatcher(access, params, change_log, on_change=on_change)


__all__ = [
    "WATCHED_KINDS",
    "WATCH_NAMESPACES_ENV",
    "ChangeLog",
    "ChangeWatcher",
    "KubeAccess",
    "ObjectState",
    "WatchParams",
    "WatchedKind",
    "build_change_watcher",
    "change_event_for",
    "is_change",
    "kube_access_from_kubeconfig",
    "kube_access_in_cluster",
    "load_kube_access",
    "load_watch_params",
    "object_state",
    "service_of",
]
