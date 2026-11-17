"""Pod-replacement detector: a service's workload was recreated (standard 05, D3, D29).

A killed or failed pod is replaced by a *new* pod within seconds. None of the other four
detectors sees that: CPU and memory alert upward only (a dead pod reads lower, not higher),
the restart counter belongs to the old pod and never moves on the new one, and a stateless
service often logs no error. On the live testbed a frontend pod-kill produced one missing CPU
sample, one latency bucket and one error line, and no incident.

What does change is the pod name. Every Prometheus signal carries the pod it was scraped from
(`attrs.labels.pod`), so per service this detector keeps the set of pods it has seen and fires
once when a new pod appears for a service it has watched for at least `warmup_s` (so the
first pods after a restart of the detection worker are learned, not reported) while another
pod of that service was seen within `pod_ttl_s`. Pods unseen for `pod_ttl_s` are forgotten,
which bounds the state; a service whose pods all expired (a gap in collection, as in the
recorded quiet-cluster fixture) is relearned rather than reported.

Scaling a Deployment up also adds a pod and fires; on the testbed every Deployment has one
replica, so a new pod name means a replacement. Time is the signal's `ts` (replay-safe), and
a redelivered signal of a known pod is a no-op. The detector has no baseline, so freezing a
service (D4) changes nothing about its scoring.
"""

from __future__ import annotations

from datetime import datetime

from inframind.common.anomaly import Anomaly
from inframind.common.signal import Severity, Signal, SignalType, Source

from .base import FrozenServices
from .config import PodReplacementParams


def pod_of(signal: Signal) -> str | None:
    """The pod a Prometheus metric signal was scraped from, or None for any other signal."""
    if signal.source is not Source.PROMETHEUS or signal.type is not SignalType.METRIC:
        return None
    labels = signal.attrs.get("labels")
    if not isinstance(labels, dict):
        return None
    pod = labels.get("pod")
    return pod if isinstance(pod, str) and pod else None


class PodReplacementDetector:
    """Fires once per new pod of an already-established service."""

    name = "pod_replaced"

    def __init__(self, params: PodReplacementParams | None = None) -> None:
        self._params = params or PodReplacementParams()
        self._first_seen: dict[str, float] = {}  # service -> ts it was first observed
        self._pods: dict[str, dict[str, float]] = {}  # service -> pod -> last seen ts
        self._frozen = FrozenServices()

    def update(self, signal: Signal) -> Anomaly | None:
        """Record the pod behind `signal`; return an anomaly when it is a new one."""
        pod = pod_of(signal)
        if pod is None:
            return None
        p = self._params
        t = signal.ts.timestamp()
        service = signal.service
        first = self._first_seen.setdefault(service, t)
        pods = self._pods.setdefault(service, {})
        for name, last in list(pods.items()):
            if t - last > p.pod_ttl_s:
                del pods[name]

        known = pod in pods
        pods[pod] = max(pods.get(pod, t), t)
        if known or t - first < p.warmup_s:
            return None

        previous = sorted(name for name in pods if name != pod)
        if not previous:
            # Nothing seen for this service within `pod_ttl_s`: the service (or the collector)
            # was away and came back. A replacement always has a recently seen predecessor.
            return None
        return Anomaly(
            ts=signal.ts,
            detector=self.name,
            service=service,
            signal_id=signal.id,
            score=p.score,
            severity=Severity.WARNING,
            attrs={
                "pod": pod,
                "previous_pods": previous,
                "metric": signal.attrs.get("metric"),
                "frozen": self._frozen.is_frozen(service),
            },
        )

    def freeze(self, service: str, ts: datetime) -> None:
        """Record that `service` is frozen; this detector has no baseline to stop learning."""
        self._frozen.freeze(service)

    def unfreeze(self, service: str) -> None:
        """Clear the frozen mark for `service`."""
        self._frozen.unfreeze(service)

    def is_frozen(self, service: str) -> bool:
        """Return whether `service` is marked frozen."""
        return self._frozen.is_frozen(service)
