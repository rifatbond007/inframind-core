"""Call-error attribution: failing client calls count against the service they called (D30).

A service that emits no traces of its own (on the testbed: cartservice, adservice,
shippingservice, emailservice) is still visible in its callers' traces: when frontend's
`hipstershop.CartService/GetCart` client span fails, the failure is *about* cartservice.
`error_spike` counts that span against frontend, the caller, so a cartservice kill showed up
as a frontend error flood and InfraMind ranked frontend, the symptom, first (dev-01: top-1
0/5 while the `deepest_error_span` baseline, which follows the call, scored 4/5).

This detector runs the same count-spike algorithm as `error_spike` (same parameters,
`INFRAMIND_DETECTION_CALL_ERROR_*`), keyed by the **callee** of each failing client span.
The callee is the gRPC service in the span's operation (`pkg.FooService/Method` ->
`fooservice`), accepted only when it is a service this detector has already seen in some
signal, so an unrelated operation name never invents a service. Each anomaly records the
`caller`, which the RCA ranker reads as an observed call edge (D30).
"""

from __future__ import annotations

import re
from datetime import datetime

from inframind.common.anomaly import Anomaly
from inframind.common.signal import Signal, Source, normalise_service

from .config import ErrorSpikeParams
from .error_spike import ErrorSpikeDetector, is_error_event

#: The gRPC service of a full method name: `hipstershop.CartService/GetCart` -> `CartService`.
_GRPC_METHOD = re.compile(r"(?:^|[./])(\w+)/\w+$")


def grpc_callee(operation: object) -> str | None:
    """The service name a gRPC client operation called, lower-cased, or None."""
    if not isinstance(operation, str):
        return None
    match = _GRPC_METHOD.search(operation)
    return normalise_service(match.group(1).lower()) if match else None


class CallErrorDetector:
    """Error-count spike per callee of failing client spans."""

    name = "call_error"

    def __init__(self, params: ErrorSpikeParams | None = None) -> None:
        self._spike = ErrorSpikeDetector(params)
        self._known: set[str] = set()

    def update(self, signal: Signal) -> Anomaly | None:
        """Count a failing client call against its callee; fire on a spike."""
        self._known.add(signal.service)
        if (
            signal.source is not Source.JAEGER
            or signal.attrs.get("kind") != "client"
            or not is_error_event(signal)
        ):
            return None
        callee = grpc_callee(signal.attrs.get("operation"))
        if callee is None or callee == signal.service or callee not in self._known:
            return None
        found = self._spike.update(signal.model_copy(update={"service": callee}))
        if found is None:
            return None
        attrs = {**found.attrs, "caller": signal.service, "operation": signal.attrs["operation"]}
        return found.model_copy(update={"detector": self.name, "attrs": attrs})

    def freeze(self, service: str, ts: datetime) -> None:
        """Stop counting new failures toward `service`'s baseline; scoring continues (D4)."""
        self._spike.freeze(service, ts)

    def unfreeze(self, service: str) -> None:
        """Resume counting failures toward `service`'s baseline."""
        self._spike.unfreeze(service)

    def is_frozen(self, service: str) -> bool:
        """Return whether `service`'s baseline is frozen."""
        return self._spike.is_frozen(service)
