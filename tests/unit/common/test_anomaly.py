"""`Anomaly` wire model: validation and the JSON round trip."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from inframind.common.anomaly import Anomaly
from inframind.common.signal import Severity

NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)


def make(**over: Any) -> Anomaly:
    fields: dict[str, Any] = {
        "ts": NOW,
        "detector": "zscore",
        "service": "frontend",
        "signal_id": "01ABCDEFGHJKMNPQRSTVWXYZ01",
        "score": 4.2,
        "severity": Severity.CRITICAL,
        "attrs": {"value": 1.0},
    }
    fields.update(over)
    return Anomaly(**fields)


def test_round_trips_through_json() -> None:
    a = make()
    assert Anomaly.model_validate_json(a.model_dump_json()) == a


def test_id_is_a_ulid_and_unique() -> None:
    assert len(make().id) == 26
    assert make().id != make().id


def test_service_is_canonicalised() -> None:
    assert make(service="FrontEnd").service == "frontend"


@pytest.mark.parametrize(
    "bad",
    [
        {"service": "unknown"},
        {"service": ""},
        {"ts": datetime(2026, 10, 19, 12, 0, 0)},
        {"ts": 1_792_000_000},
        {"score": float("nan")},
        {"score": float("inf")},
        {"extra": 1},
    ],
)
def test_invalid_anomalies_are_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        make(**bad)


def test_non_json_attrs_are_dropped_not_fatal() -> None:
    a = make(attrs={"ok": 1, "bad": object(), "nan": float("nan")})
    assert a.attrs == {"ok": 1}


def test_anomaly_is_frozen() -> None:
    with pytest.raises(ValidationError):
        make().score = 1.0  # type: ignore[misc]
