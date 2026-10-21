"""The pinned `change_correlation` algorithm (standard 14.10 `test_change_correlation`).

Covers the worked example from standard 14.7 exactly: a ConfigMap change at T-5s inside a
30-minute pre-window scores ~0.833; no events score 0.0; the sum saturates and caps at 1.0.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from inframind.rca.change_correlation import ChangeEvent, change_correlation

from .conftest import T0

OPENED = T0
CLOSED = T0 + timedelta(minutes=5)
WINDOW = (OPENED, CLOSED)
PRE_WINDOW_S = 1800.0


def _event(
    service: str,
    occurred_at,
    *,
    kind: str = "configmap_change",
    resource: str = "cm/test",
) -> ChangeEvent:
    return ChangeEvent(
        service=service,
        kind=kind,  # type: ignore[arg-type]
        resource=resource,
        occurred_at=occurred_at,
    )


def test_no_events_scores_zero() -> None:
    assert change_correlation("frontend", WINDOW, [], pre_window_s=PRE_WINDOW_S) == 0.0


def test_no_events_for_this_candidate_scores_zero() -> None:
    """Events for a *different* service do not contribute."""
    events = [_event("cartservice", OPENED - timedelta(seconds=5))]
    assert change_correlation("frontend", WINDOW, events, pre_window_s=PRE_WINDOW_S) == 0.0


def test_worked_example_from_standard_14_7() -> None:
    """One ConfigMap change 5s before open, in a 30-min pre-window -> ~0.833.

    Standard 14.7's example uses a 30s pre-window for legibility; the same arithmetic
    applies at the 1800s default: `recency = 1 - 5/1800 = 0.99722`.
    """
    events = [_event("frontend", OPENED - timedelta(seconds=5))]
    score = change_correlation("frontend", WINDOW, events, pre_window_s=PRE_WINDOW_S)

    assert score == pytest.approx(1.0 - (5.0 / PRE_WINDOW_S))


def test_worked_example_small_window_matches_standard() -> None:
    """Reproduce standard 14.7's illustrative numbers using a 30s pre-window."""
    events = [_event("frontend", OPENED - timedelta(seconds=5))]
    score = change_correlation("frontend", WINDOW, events, pre_window_s=30.0)

    assert score == pytest.approx(0.833, abs=1e-3)


def test_event_outside_the_pre_window_is_ignored() -> None:
    """An event older than `pre_window_s` before open contributes nothing."""
    events = [_event("frontend", OPENED - timedelta(seconds=PRE_WINDOW_S + 1))]
    assert change_correlation("frontend", WINDOW, events, pre_window_s=PRE_WINDOW_S) == 0.0


def test_event_after_open_but_before_close_is_counted() -> None:
    """The post-window extends to `closed_at` to catch late changes (e.g. HPA scale-up)."""
    events = [_event("frontend", OPENED + timedelta(seconds=60))]
    score = change_correlation("frontend", WINDOW, events, pre_window_s=PRE_WINDOW_S)
    assert score > 0.0


def test_multiple_events_soft_saturate() -> None:
    """Three events each weighted `r` sum to `3r`, then divide by `1 + 0.5*(3-1) = 2.0`."""
    events = [
        _event("frontend", OPENED - timedelta(seconds=0)),
        _event("frontend", OPENED - timedelta(seconds=0)),
        _event("frontend", OPENED - timedelta(seconds=0)),
    ]
    score = change_correlation("frontend", WINDOW, events, pre_window_s=PRE_WINDOW_S)

    # raw = 3 * 1.0; normalised = 3.0 / 2.0 = 1.5, capped to 1.0.
    assert score == pytest.approx(1.0)


def test_two_events_soft_saturate_to_one_point_five_x() -> None:
    """Two events at recency 1.0 -> raw 2.0, normalised 2.0/1.5 = 1.333, capped to 1.0."""
    events = [
        _event("frontend", OPENED),
        _event("frontend", OPENED),
    ]
    score = change_correlation("frontend", WINDOW, events, pre_window_s=PRE_WINDOW_S)
    assert score == pytest.approx(1.0)


def test_score_is_capped_at_one() -> None:
    """Even a storm of at-incident-open events never exceeds 1.0."""
    events = [_event("frontend", OPENED) for _ in range(20)]
    score = change_correlation("frontend", WINDOW, events, pre_window_s=PRE_WINDOW_S)
    assert score <= 1.0


def test_soft_saturation_math_for_three_half_weighted_events() -> None:
    """Three events each at recency 0.5: raw 1.5, /2.0 = 0.75."""
    events = [_event("frontend", OPENED - timedelta(seconds=PRE_WINDOW_S / 2)) for _ in range(3)]
    score = change_correlation("frontend", WINDOW, events, pre_window_s=PRE_WINDOW_S)
    assert score == pytest.approx(0.75)


def test_change_event_rejects_unknown_service() -> None:
    with pytest.raises(ValueError, match="unknown"):
        ChangeEvent(
            service="unknown",
            kind="configmap_change",
            resource="cm/x",
            occurred_at=T0,
        )


def test_change_event_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ChangeEvent(
            service="frontend",
            kind="configmap_change",
            resource="cm/x",
            occurred_at=T0.replace(tzinfo=None),
        )


def test_change_event_stores_no_secret_value() -> None:
    """D9: `ChangeEvent` has no field that could carry a Secret value."""
    fields = set(ChangeEvent.model_fields)
    assert "value" not in fields
    assert "data" not in fields
    assert "payload" not in fields
    assert fields == {"service", "kind", "resource", "occurred_at", "revision"}
