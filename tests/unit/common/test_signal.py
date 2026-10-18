"""Signal validation: the rules in docs/standards/03-signal-schema.md section 3.4."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from inframind.common.ids import ULID_LENGTH, new_ulid
from inframind.common.signal import (
    UNKNOWN_SERVICE,
    Severity,
    Signal,
    SignalType,
    Source,
    normalise_service,
)
from inframind.common.time import utcnow


def make(**over: Any) -> Signal:
    base: dict[str, Any] = {
        "ts": utcnow(),
        "source": Source.PROMETHEUS,
        "type": SignalType.METRIC,
        "service": "frontend",
    }
    return Signal(**{**base, **over})


def test_defaults_and_ulid_id() -> None:
    s = make()
    assert len(s.id) == ULID_LENGTH
    assert s.severity is Severity.INFO
    assert s.attrs == {}
    assert s.trace_id is None


def test_ulids_are_unique_and_time_sortable() -> None:
    first = new_ulid()
    ids = {new_ulid() for _ in range(1000)}
    assert len(ids) == 1000
    assert first[:10] <= max(ids)[:10]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("frontend", "frontend"),
        ("FrontEnd", "frontend"),
        ("Cart Service_v2", "cart-service-v2"),
        ("  --x--  ", "x"),
        ("", UNKNOWN_SERVICE),
        ("???", UNKNOWN_SERVICE),
    ],
)
def test_service_normalised(raw: str, expected: str) -> None:
    assert normalise_service(raw) == expected
    assert make(service=raw).service == expected


def test_empty_service_is_not_routable() -> None:
    assert not make(service="").is_routable
    assert make().is_routable


def test_naive_ts_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        make(ts=datetime(2026, 1, 1, 12, 0, 0))


@pytest.mark.parametrize("epoch", [1704067200, 1704067200.5])
def test_epoch_ts_rejected(epoch: float) -> None:
    with pytest.raises(ValidationError, match="epoch"):
        make(ts=epoch)


def test_ts_converted_to_utc() -> None:
    plus6 = timezone(timedelta(hours=6))
    s = make(ts=datetime(2026, 1, 1, 12, 0, tzinfo=plus6))
    assert s.ts == datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
    assert s.ts.utcoffset() == timedelta(0)


def test_iso_string_ts_accepted() -> None:
    assert make(ts="2026-01-01T00:00:00Z").ts == datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize("trace_id", ["abc", "G" * 32, "A" * 32, "0" * 31])
def test_bad_trace_id_rejected(trace_id: str) -> None:
    with pytest.raises(ValidationError):
        make(trace_id=trace_id)


def test_good_trace_id_accepted() -> None:
    assert make(trace_id="0af7651916cd43dd8448eb211c80319c").trace_id is not None


def test_non_json_attrs_dropped_per_key() -> None:
    s = make(
        attrs={
            "ok": {"a": [1, 2.5, "x", None]},
            "raw": b"bytes",
            "when": utcnow(),
            "nan": float("nan"),
            "obj": object(),
        }
    )
    assert s.attrs == {"ok": {"a": [1, 2.5, "x", None]}}


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError):
        make(timestamp=utcnow())


def test_signal_is_immutable() -> None:
    with pytest.raises(ValidationError):
        make().service = "other"  # type: ignore[misc]


def test_json_round_trip() -> None:
    s = make(severity=Severity.CRITICAL, attrs={"value": 1.5}, trace_id="a" * 32)
    assert Signal.model_validate_json(s.model_dump_json()) == s
