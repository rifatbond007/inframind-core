"""Window grouping: which open incident, if any, an anomaly joins (spec ruling 1)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from inframind.correlation.window import choose_incident

T0 = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Open:
    name: str
    services: frozenset[str]
    last_anomaly_ts: datetime


def s(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def incident(name: str, services: set[str], last: float) -> Open:
    return Open(name, frozenset(services), s(last))


def test_nothing_open_means_nothing_to_join() -> None:
    assert choose_incident([], "frontend", s(0), 300.0) is None


def test_same_service_wins_over_a_more_recent_other_service_incident() -> None:
    mine = incident("mine", {"frontend"}, last=0)
    recent = incident("recent", {"cartservice"}, last=100)
    assert choose_incident([recent, mine], "frontend", s(110), 300.0) is mine


def test_an_other_service_within_the_window_joins() -> None:
    open_ = incident("a", {"frontend"}, last=0)
    assert choose_incident([open_], "cartservice", s(120), 300.0) is open_


def test_beyond_the_window_there_is_nothing_to_join() -> None:
    open_ = incident("a", {"frontend"}, last=0)
    assert choose_incident([open_], "cartservice", s(301), 300.0) is None


def test_the_boundary_is_inclusive() -> None:
    open_ = incident("a", {"frontend"}, last=0)
    assert choose_incident([open_], "cartservice", s(300), 300.0) is open_
    assert choose_incident([open_], "cartservice", s(300.001), 300.0) is None


def test_the_same_service_rejoins_even_beyond_the_window() -> None:
    open_ = incident("a", {"frontend"}, last=0)
    assert choose_incident([open_], "frontend", s(5000), 300.0) is open_


def test_an_older_timestamp_within_the_window_still_joins() -> None:
    open_ = incident("a", {"frontend"}, last=100)
    assert choose_incident([open_], "cartservice", s(40), 300.0) is open_


def test_the_most_recently_active_candidate_wins_among_equals() -> None:
    older = incident("older", {"frontend"}, last=10)
    newer = incident("newer", {"frontend"}, last=50)
    assert choose_incident([older, newer], "frontend", s(60), 300.0) is newer
    a = incident("a", {"x"}, last=20)
    b = incident("b", {"y"}, last=80)
    assert choose_incident([a, b], "cartservice", s(90), 300.0) is b


def test_ties_break_by_position() -> None:
    first = incident("first", {"x"}, last=20)
    second = incident("second", {"y"}, last=20)
    assert choose_incident([first, second], "cartservice", s(30), 300.0) is first
