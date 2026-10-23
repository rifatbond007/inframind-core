"""`GraphUpdate` validation and its publish path onto `stream:graph-updates`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from pydantic import ValidationError
from redis import exceptions as redis_exc

from inframind.common import stream_keys
from inframind.common.bus import publish_graph_update
from inframind.common.exceptions import BusError
from inframind.common.graph_update import GraphUpdate

NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)


def make(**over: Any) -> GraphUpdate:
    fields: dict[str, Any] = {
        "caller": "frontend",
        "callee": "productcatalogservice",
        "latency_ms": 12.5,
        "observed_at": NOW,
    }
    fields.update(over)
    return GraphUpdate(**fields)


def test_valid_update_round_trips_through_json() -> None:
    u = make()
    assert GraphUpdate.model_validate_json(u.model_dump_json()) == u


def test_service_names_are_canonicalised() -> None:
    u = make(caller="FrontEnd", callee="Product_Catalog.Service")
    assert (u.caller, u.callee) == ("frontend", "product-catalog-service")


def test_observed_at_is_converted_to_utc() -> None:
    plus6 = timezone(timedelta(hours=6))
    u = make(observed_at=datetime(2026, 10, 19, 18, 0, 0, tzinfo=plus6))
    assert u.observed_at == NOW
    assert u.observed_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "bad",
    [
        {"caller": "cart", "callee": "cart"},
        {"caller": "Cart", "callee": "cart"},
        {"caller": "unknown"},
        {"callee": ""},
        {"latency_ms": -1.0},
        {"latency_ms": float("nan")},
        {"latency_ms": float("inf")},
        {"observed_at": datetime(2026, 10, 19, 12, 0, 0)},
        {"observed_at": 1_792_000_000},
        {"extra": 1},
    ],
)
def test_invalid_updates_are_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        make(**bad)


def test_update_is_frozen() -> None:
    with pytest.raises(ValidationError):
        make().latency_ms = 1.0  # type: ignore[misc]


async def test_publish_graph_update_writes_to_the_pinned_stream() -> None:
    redis = FakeAsyncRedis()
    entry_id = await publish_graph_update(redis, make())
    assert entry_id is not None
    [(_, fields)] = await redis.xrange(stream_keys.GRAPH_UPDATES)
    assert GraphUpdate.model_validate_json(fields[b"data"]) == make()
    assert await redis.xlen(stream_keys.SIGNALS) == 0


async def test_publish_graph_update_failure_raises_bus_error() -> None:
    class Broken(FakeAsyncRedis):
        async def xadd(self, *args: Any, **kwargs: Any) -> Any:
            raise redis_exc.ConnectionError("down")

    with pytest.raises(BusError):
        await publish_graph_update(Broken(), make())
