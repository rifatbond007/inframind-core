"""`Incident` wire model: lifecycle invariants and the JSON round trip."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from inframind.common.anomaly import Anomaly
from inframind.common.incident import Incident
from inframind.common.signal import Severity

NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)


def anomaly(service: str = "frontend") -> Anomaly:
    return Anomaly(
        ts=NOW,
        detector="zscore",
        service=service,
        signal_id="01SIG",
        score=4.0,
        severity=Severity.WARNING,
    )


def make(**over: Any) -> Incident:
    fields: dict[str, Any] = {
        "revision": 1,
        "state": "open",
        "opened_at": NOW,
        "updated_at": NOW,
        "closed_at": None,
        "title": "frontend: zscore anomaly",
        "services": ["frontend"],
        "signals": ["01SIG"],
        "anomalies": [anomaly(), anomaly("cartservice")],
        "severity": Severity.WARNING,
    }
    fields.update(over)
    return Incident(**fields)


def test_round_trips_through_json_with_anomalies() -> None:
    i = make()
    again = Incident.model_validate_json(i.model_dump_json())
    assert again == i
    assert [a.service for a in again.anomalies] == ["frontend", "cartservice"]


def test_resolved_requires_closed_at_and_open_forbids_it() -> None:
    closed = NOW + timedelta(minutes=5)
    assert make(state="resolved", closed_at=closed).closed_at == closed
    with pytest.raises(ValidationError):
        make(state="resolved", closed_at=None)
    with pytest.raises(ValidationError):
        make(state="open", closed_at=closed)


def test_expired_also_requires_closed_at() -> None:
    with pytest.raises(ValidationError):
        make(state="expired", closed_at=None)


@pytest.mark.parametrize(
    "bad",
    [
        {"services": []},
        {"revision": 0},
        {"state": "updating"},
        {"opened_at": datetime(2026, 10, 19, 12, 0, 0)},
        {"extra": 1},
    ],
)
def test_invalid_incidents_are_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        make(**bad)


def test_services_are_sorted_deduplicated_and_canonical() -> None:
    assert make(services=["Frontend", "cartservice", "frontend"]).services == [
        "cartservice",
        "frontend",
    ]


def test_id_is_a_ulid() -> None:
    assert len(make().id) == 26
