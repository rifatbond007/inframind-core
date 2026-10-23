"""Storage against a real Postgres (`make up postgres`). Skipped when Postgres is unreachable.

This is the gate that proves the DDL in `storage/schema.py` actually executes and that the
INSERT/SELECT round-trip in `storage/incidents.py` works against a live server — the fake
pool in the unit tests proves the SQL *shape*, this proves the SQL *runs*.

Each test truncates the tables first so it does not depend on leftover state from a
previous run. Skipped unless Postgres is reachable at `POSTGRES_URL`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import asyncpg
import pytest

from inframind.common.incident import Incident
from inframind.common.signal import Severity
from inframind.rca.evidence import Candidate, Evidence
from inframind.storage import (
    StorageUnavailable,
    build_params,
    close_pool,
    create_pool,
    get_incident,
    init_schema,
    list_incidents,
    record_audit,
    transition_incident,
    write_incident,
)

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC)

TABLES = ("audit_log", "alerts", "evidence", "candidates", "incidents")


@pytest.fixture
async def pool() -> asyncpg.Pool:
    params, dsn, _ = build_params()
    try:
        p = await create_pool(params, dsn)
        await init_schema(p)
    except StorageUnavailable:
        pytest.skip("Postgres not reachable")
    # Clean slate for this test.
    async with p.acquire() as conn:
        for table in TABLES:
            await conn.execute(f"TRUNCATE {table} CASCADE")
    yield p
    await close_pool(p)


def _incident(id_: str = "inc_it_001", **over: object) -> Incident:
    base: dict[str, object] = dict(
        id=id_,
        revision=1,
        state="open",
        severity=Severity.WARNING,
        title="checkout latency spike",
        fingerprint="0123abcd",
        opened_at=T0,
        updated_at=T0,
        services=["frontend"],
        signals=["sig_1"],
        anomalies=[],
    )
    base.update(over)
    return Incident(**base)  # type: ignore[arg-type]


def _candidate(id_: str = "cand_1") -> Candidate:
    return Candidate(
        id=id_,
        service="paymentservice",
        score=0.91,
        depth=3,
        change_correlation=0.7,
        confidence=0.8,
        evidence=[
            Evidence(id=f"{id_}_ev", kind="metric", summary="p99 2.1s", ref="sig_1", weight=0.9)
        ],
    )


async def test_schema_executes(pool: asyncpg.Pool) -> None:
    """`init_schema` is idempotent and creates every table."""
    await init_schema(pool)  # second call must not fail
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = ANY($1::text[])",
            list(TABLES),
        )
    assert {r["table_name"] for r in rows} == set(TABLES)


async def test_write_then_get_round_trips_full_candidate(pool: asyncpg.Pool) -> None:
    """A write/read round-trip preserves the full P5 candidate shape and its evidence."""
    candidate = _candidate()
    await write_incident(pool, _incident(), [candidate], scenario_id="scn_it")

    detail = await get_incident(pool, "inc_it_001")
    assert detail is not None
    assert detail.incident.id == "inc_it_001"
    assert detail.incident.services == ["frontend"]
    assert detail.primary is not None
    assert detail.primary.service == "paymentservice"
    assert detail.primary.depth == 3
    assert detail.primary.change_correlation == 0.7
    assert detail.primary.confidence == 0.8
    assert [e.id for e in detail.primary.evidence] == [candidate.evidence[0].id]


async def test_write_is_idempotent_on_resend(pool: asyncpg.Pool) -> None:
    """Re-writing the same incident does not duplicate candidates."""
    incident = _incident()
    await write_incident(pool, incident, [_candidate()])
    await write_incident(pool, incident, [_candidate()])

    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM candidates WHERE incident_id = $1", "inc_it_001"
        )
    assert count == 1


async def test_write_with_no_candidates_persists_incident_only(pool: asyncpg.Pool) -> None:
    """An incident with no candidates is still recorded; the detail has a None primary."""
    await write_incident(pool, _incident(), [])
    detail = await get_incident(pool, "inc_it_001")
    assert detail is not None
    assert detail.primary is None
    assert detail.candidates == []


async def test_transition_persists_state_and_writes_audit(pool: asyncpg.Pool) -> None:
    """A transition updates the state and appends an audit row in the same transaction."""
    await write_incident(pool, _incident(), [_candidate()])
    ok = await transition_incident(
        pool, "inc_it_001", new_state="acknowledged", actor="rifatbond007", audit_action="ack"
    )
    assert ok is True

    detail = await get_incident(pool, "inc_it_001")
    assert detail is not None
    assert detail.incident.state == "acknowledged"

    async with pool.acquire() as conn:
        audit = await conn.fetchrow(
            "SELECT actor, action, target FROM audit_log WHERE target = $1", "inc_it_001"
        )
    assert audit is not None
    assert audit["actor"] == "rifatbond007"
    assert audit["action"] == "ack"


async def test_transition_missing_incident_returns_false(pool: asyncpg.Pool) -> None:
    ok = await transition_incident(
        pool, "inc_nope", new_state="resolved", actor="rifatbond007", audit_action="resolve"
    )
    assert ok is False


async def test_list_paginates_with_cursor(pool: asyncpg.Pool) -> None:
    """The list endpoint paginates and reports a cursor when more rows remain."""
    for i in range(3):
        await write_incident(pool, _incident(f"inc_page_{i}"), [])

    page1, cursor = await list_incidents(pool, limit=2)
    assert len(page1) == 2
    assert cursor is not None

    page2, cursor2 = await list_incidents(pool, limit=2, cursor=cursor)
    assert len(page2) == 1
    assert cursor2 is None
    assert {i.id for i in page1} | {i.id for i in page2} == {
        "inc_page_0",
        "inc_page_1",
        "inc_page_2",
    }


async def test_list_filters_by_state(pool: asyncpg.Pool) -> None:
    """State filtering composes with the rest of the query."""
    await write_incident(
        pool,
        _incident("inc_open"),
        [],
    )
    await write_incident(pool, _incident("inc_res", state="resolved", closed_at=T0), [])

    open_items, _ = await list_incidents(pool, state="open")
    assert [i.id for i in open_items] == ["inc_open"]


async def test_list_filters_by_service(pool: asyncpg.Pool) -> None:
    """Service filtering works over the JSONB `services` column."""
    await write_incident(pool, _incident("inc_front", services=["frontend"]), [])
    await write_incident(pool, _incident("inc_cart", services=["cartservice"]), [])

    items, _ = await list_incidents(pool, service="cartservice")
    assert [i.id for i in items] == ["inc_cart"]


async def test_record_audit_appends(pool: asyncpg.Pool) -> None:
    """`record_audit` inserts and returns a row id that can be correlated downstream."""
    audit_id = await record_audit(
        pool, actor="rifatbond007", action="rank", target="inc_x", after={"score": 0.91}
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id, actor, action FROM audit_log WHERE id = $1", audit_id)
    assert row is not None
    assert row["actor"] == "rifatbond007"


async def test_scenario_id_is_persisted_when_set(pool: asyncpg.Pool) -> None:
    """P7 scenario tagging round-trips through the `incidents.scenario_id` column."""
    incident = _incident()
    await write_incident(pool, incident, [], scenario_id="scn_p7_001")

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT scenario_id FROM incidents WHERE id = $1", incident.id)
    assert row is not None
    assert row["scenario_id"] == "scn_p7_001"


async def test_scenario_id_is_null_by_default(pool: asyncpg.Pool) -> None:
    """Outside P7 evaluation, every incident has scenario_id NULL."""
    await write_incident(pool, _incident(), [])
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT scenario_id FROM incidents WHERE id = $1", "inc_it_001")
    assert row is not None
    assert row["scenario_id"] is None


async def test_transition_to_resolved_persists_state(pool: asyncpg.Pool) -> None:
    """A second transition is allowed and updates the state row in place."""
    await write_incident(pool, _incident(), [_candidate()])
    await transition_incident(
        pool, "inc_it_001", new_state="acknowledged", actor="moneem", audit_action="ack"
    )
    ok = await transition_incident(
        pool, "inc_it_001", new_state="resolved", actor="moneem", audit_action="resolve"
    )
    assert ok is True

    detail = await get_incident(pool, "inc_it_001")
    assert detail is not None
    assert detail.incident.state == "resolved"

    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM audit_log WHERE target = $1", "inc_it_001"
        )
    assert count == 2  # both transitions wrote audit rows
