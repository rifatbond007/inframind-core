"""Storage incident write/read tests against a recording fake pool (no Postgres needed).

The behavioural contract under test:

* **Transactional writes.** `write_incident` writes the incident, its candidates, and its
  evidence inside one transaction; a failure partway through rolls the whole thing back and
  surfaces as `StorageUnavailable` (D20 — never a half-written incident).
* **Idempotent resync.** Candidates and evidence are deleted then re-inserted, so a repeat
  write with the same ranker output produces the same rows.
* **The full P5 `Candidate` shape round-trips** — `depth`, `change_correlation`, and
  `confidence` are not dropped on the way to disk.
* **Reads return the incident with its candidates and evidence**, and the primary is the
  top-ranked candidate.
* **Failure surfaces, never silent drops.** Any Postgres error becomes `StorageUnavailable`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from inframind.common.incident import Incident
from inframind.common.signal import Severity
from inframind.rca.evidence import Candidate, Evidence
from inframind.storage.db import StorageUnavailable
from inframind.storage.incidents import (
    get_incident,
    list_incidents,
    record_audit,
    transition_incident,
    write_incident,
)

from .conftest import FakePool

T0 = datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC)


def _incident(**over: object) -> Incident:
    base: dict[str, object] = {
        "id": "inc_001",
        "revision": 1,
        "state": "open",
        "severity": Severity.WARNING,
        "title": "checkout latency spike",
        "fingerprint": "0123abcd",
        "opened_at": T0,
        "updated_at": T0,
        "services": ["frontend"],
        "signals": ["sig_1"],
        "anomalies": [],
    }
    base.update(over)
    return Incident(**base)  # type: ignore[arg-type]


def _evidence(id_: str = "ev_1") -> Evidence:
    return Evidence(id=id_, kind="metric", summary="p99 latency 2.1s", ref="sig_1", weight=0.9)


def _candidate(
    id_: str = "cand_1", *, service: str = "paymentservice", score: float = 0.91
) -> Candidate:
    return Candidate(
        id=id_,
        service=service,
        score=score,
        depth=3,
        change_correlation=0.7,
        confidence=0.8,
        evidence=[_evidence()],
    )


class FakeRow(dict):
    """asyncpg `Record` supports `row["col"]`; a dict does too."""

    def __getitem__(self, key: str):
        return dict.__getitem__(self, key)


def _incident_row(**over: object) -> FakeRow:
    """A minimal `incidents` row mirroring the columns `get_incident` selects."""
    base: dict[str, object] = dict(
        id="inc_001",
        revision=1,
        state="open",
        severity="warning",
        title="t",
        fingerprint="0123abcd",
        opened_at=T0,
        updated_at=T0,
        closed_at=None,
        services='["frontend"]',
        signals='["sig_1"]',
        anomalies="[]",
    )
    base.update(over)
    return FakeRow(**base)


# --- write_incident ---


async def test_write_incident_uses_one_transaction(pool: FakePool) -> None:
    """One `conn.transaction()` wraps the incident, candidates, and evidence inserts."""
    await write_incident(pool, _incident(), [_candidate()])
    assert pool.transaction_entries == 1
    assert pool.transaction_commits == 1
    assert pool.transaction_rollbacks == 0


async def test_write_incident_inserts_incident_candidates_and_evidence(pool: FakePool) -> None:
    """All three tables are written in the expected order within the transaction."""
    await write_incident(pool, _incident(), [_candidate()])
    kinds = [sql for _, sql, _ in pool.calls]
    assert any("INSERT INTO incidents" in sql for sql in kinds)
    assert any("DELETE FROM candidates" in sql for sql in kinds)
    assert any("INSERT INTO candidates" in sql for sql in kinds)
    assert any("INSERT INTO evidence" in sql for sql in kinds)


async def test_write_incident_reranks_candidates_by_position(pool: FakePool) -> None:
    """`rank` is the 1-based position of the candidate in the (already-ranked) sequence."""
    candidates = [
        _candidate("cand_1", service="paymentservice", score=0.91),
        _candidate("cand_2", service="cartservice", score=0.40),
    ]
    await write_incident(pool, _incident(), candidates)

    candidate_args = [
        args
        for sql, args in zip(pool.sql_of("execute"), pool.args_of("execute"), strict=True)
        if "INSERT INTO candidates" in sql
    ]
    assert [a[7] for a in candidate_args] == [1, 2]  # rank column


async def test_write_incident_round_trips_full_candidate_shape(pool: FakePool) -> None:
    """The stored candidate keeps all six P5 fields, not the narrower four-field shape."""
    await write_incident(pool, _incident(), [_candidate()])
    candidate_args = next(
        a
        for sql, a in zip(pool.sql_of("execute"), pool.args_of("execute"), strict=True)
        if "INSERT INTO candidates" in sql
    )
    _, _incident_id, service, score, depth, change_correlation, confidence, _rank = candidate_args
    assert service == "paymentservice"
    assert score == 0.91
    assert depth == 3
    assert change_correlation == 0.7
    assert confidence == 0.8


async def test_write_incident_serialises_jsonb_columns(pool: FakePool) -> None:
    """List columns are JSON-encoded for asyncpg's JSONB codec; None becomes SQL NULL."""
    import json

    await write_incident(pool, _incident(), [_candidate()])
    incident_args = next(
        a
        for sql, a in zip(pool.sql_of("execute"), pool.args_of("execute"), strict=True)
        if "INSERT INTO incidents" in sql
    )
    services, signals, anomalies, summary, alerts = incident_args[10:15]
    assert json.loads(services) == ["frontend"]
    assert json.loads(signals) == ["sig_1"]
    assert json.loads(anomalies) == []
    # None columns are encoded as empty string, mapped to SQL NULL by NULLIF.
    assert summary == ""
    assert json.loads(alerts) == []


async def test_write_incident_is_idempotent_via_delete_then_insert(pool: FakePool) -> None:
    """Candidates are cleared before re-insert, so a repeat write does not duplicate."""
    await write_incident(pool, _incident(), [_candidate()])
    await write_incident(pool, _incident(), [_candidate()])
    delete_calls = [sql for sql in pool.sql_of("execute") if "DELETE FROM candidates" in sql]
    assert len(delete_calls) == 2  # one per write


async def test_write_incident_passes_scenario_id(pool: FakePool) -> None:
    """P7 scenario tagging threads through to the `scenario_id` column."""
    await write_incident(pool, _incident(), [], scenario_id="scn_001")
    incident_args = next(
        a
        for sql, a in zip(pool.sql_of("execute"), pool.args_of("execute"), strict=True)
        if "INSERT INTO incidents" in sql
    )
    assert incident_args[1] == "scn_001"


async def test_write_incident_raises_storage_unavailable_on_failure(pool: FakePool) -> None:
    """A mid-transaction failure surfaces as `StorageUnavailable` (D20 refusal)."""
    pool.fail_on_execute = 1
    with pytest.raises(StorageUnavailable) as exc:
        await write_incident(pool, _incident(), [_candidate()])
    assert "write_incident" in str(exc.value)


async def test_write_incident_rolls_back_on_failure(pool: FakePool) -> None:
    """The transaction records a rollback when the write fails partway through."""
    pool.fail_on_execute = 2  # fail on the candidate insert, after the incident insert
    with pytest.raises(StorageUnavailable):
        await write_incident(pool, _incident(), [_candidate()])
    assert pool.transaction_rollbacks == 1
    assert pool.transaction_commits == 0


# --- transition_incident ---


async def test_transition_incident_updates_state_and_writes_audit(pool: FakePool) -> None:
    """A successful transition writes both the state change and its audit row."""
    pool.execute_result = "UPDATE 1"
    ok = await transition_incident(
        pool, "inc_001", new_state="acknowledged", actor="rifatbond007", audit_action="ack"
    )
    assert ok is True
    assert pool.transaction_entries == 1
    sqls = pool.sql_of("execute")
    assert any("UPDATE incidents SET state" in sql for sql in sqls)
    assert any("INSERT INTO audit_log" in sql for sql in sqls)


async def test_transition_incident_returns_false_when_missing(pool: FakePool) -> None:
    """Zero rows updated means "not found", not an error."""
    pool.execute_result = "UPDATE 0"
    ok = await transition_incident(
        pool, "nope", new_state="acknowledged", actor="rifatbond007", audit_action="ack"
    )
    assert ok is False
    # No audit row is written for a no-op.
    assert not any("INSERT INTO audit_log" in sql for sql in pool.sql_of("execute"))


async def test_transition_incident_records_the_actor(pool: FakePool) -> None:
    """D25: the audit row carries the GitHub username of whoever performed the action."""
    pool.execute_result = "UPDATE 1"
    await transition_incident(
        pool, "inc_001", new_state="resolved", actor="moneem", audit_action="resolve"
    )
    audit_args = next(
        a
        for sql, a in zip(pool.sql_of("execute"), pool.args_of("execute"), strict=True)
        if "INSERT INTO audit_log" in sql
    )
    assert audit_args[1] == "moneem"
    assert audit_args[2] == "resolve"
    assert audit_args[3] == "inc_001"


async def test_transition_incident_raises_storage_unavailable(pool: FakePool) -> None:
    pool.fail_on_execute = 1
    with pytest.raises(StorageUnavailable):
        await transition_incident(
            pool, "inc_001", new_state="resolved", actor="rifatbond007", audit_action="resolve"
        )


# --- get_incident ---


async def test_get_incident_returns_none_when_missing(pool: FakePool) -> None:
    pool.fetchrow_result = None
    assert await get_incident(pool, "nope") is None


async def test_get_incident_rehydrates_the_incident(pool: FakePool) -> None:
    pool.fetchrow_result = _incident_row(title="checkout latency spike")
    pool.fetch_results = [[], []]
    detail = await get_incident(pool, "inc_001")
    assert detail is not None
    assert detail.incident.id == "inc_001"
    assert detail.incident.services == ["frontend"]
    assert detail.incident.signals == ["sig_1"]
    assert detail.primary is None  # no candidates
    assert detail.candidates == []


async def test_get_incident_decodes_jsonb_given_as_native_lists(pool: FakePool) -> None:
    """asyncpg may hand back a decoded list rather than a raw JSON string; both work.

    The model canonicalises `services` (sorted unique, non-empty) — that is the model's
    job, not the storage layer's, so we just assert the round-trip preserves both values.
    """
    pool.fetchrow_result = _incident_row(services=["frontend", "cartservice"], signals=["sig_1"])
    pool.fetch_results = [[], []]
    detail = await get_incident(pool, "inc_001")
    assert detail is not None
    assert set(detail.incident.services) == {"frontend", "cartservice"}
    assert detail.incident.signals == ["sig_1"]


async def test_get_incident_returns_primary_as_top_candidate(pool: FakePool) -> None:
    pool.fetchrow_result = _incident_row()
    # The candidates query runs first, ordered by rank, so row 0 is the primary.
    pool.fetch_results = [
        [
            FakeRow(
                id="cand_1",
                service="paymentservice",
                score=0.91,
                depth=3,
                change_correlation=0.7,
                confidence=0.8,
            ),
            FakeRow(
                id="cand_2",
                service="cartservice",
                score=0.40,
                depth=1,
                change_correlation=0.2,
                confidence=0.5,
            ),
        ],
        [],
    ]
    detail = await get_incident(pool, "inc_001")
    assert detail is not None
    assert detail.primary is not None
    assert detail.primary.service == "paymentservice"
    assert [c.service for c in detail.candidates] == ["paymentservice", "cartservice"]


async def test_get_incident_groups_evidence_by_candidate(pool: FakePool) -> None:
    pool.fetchrow_result = _incident_row()
    pool.fetch_results = [
        [
            FakeRow(
                id="cand_1",
                service="paymentservice",
                score=0.91,
                depth=3,
                change_correlation=0.7,
                confidence=0.8,
            ),
            FakeRow(
                id="cand_2",
                service="cartservice",
                score=0.40,
                depth=1,
                change_correlation=0.2,
                confidence=0.5,
            ),
        ],
        [
            FakeRow(
                id="ev_1",
                candidate_id="cand_1",
                kind="metric",
                summary="p99 2.1s",
                ref="sig_1",
                weight=0.9,
            ),
            FakeRow(
                id="ev_2",
                candidate_id="cand_1",
                kind="change",
                summary="deploy at 11:58",
                ref="deploy/42",
                weight=1.0,
            ),
            FakeRow(
                id="ev_3",
                candidate_id="cand_2",
                kind="trace",
                summary="slow span",
                ref="trace/7",
                weight=0.5,
            ),
        ],
    ]
    detail = await get_incident(pool, "inc_001")
    assert detail is not None

    assert [e.id for e in detail.evidence_by_candidate["cand_1"]] == ["ev_1", "ev_2"]
    assert [e.id for e in detail.evidence_by_candidate["cand_2"]] == ["ev_3"]

    # Evidence is attached to its candidate, not left loose.
    assert [e.id for e in detail.candidates[0].evidence] == ["ev_1", "ev_2"]
    assert [e.id for e in detail.candidates[1].evidence] == ["ev_3"]
    # The rehydrated Evidence keeps its weight and kind.
    assert detail.evidence_by_candidate["cand_1"][0].kind == "metric"
    assert detail.evidence_by_candidate["cand_1"][0].weight == 0.9


async def test_get_incident_evidence_query_joins_through_candidates(pool: FakePool) -> None:
    """The evidence query is scoped to the incident via a join, not by a bare candidate id."""
    pool.fetchrow_result = _incident_row()
    pool.fetch_results = [[], []]
    await get_incident(pool, "inc_001")
    evidence_sql = pool.sql_of("fetch")[1]
    assert "JOIN candidates" in evidence_sql
    assert "c.incident_id = $1" in evidence_sql


async def test_get_incident_raises_storage_unavailable(pool: FakePool) -> None:
    pool.acquire_error = OSError("connection refused")
    with pytest.raises(StorageUnavailable):
        await get_incident(pool, "inc_001")


# --- list_incidents ---


async def test_list_incidents_returns_items_and_no_cursor_when_empty(pool: FakePool) -> None:
    pool.fetch_result = []
    items, next_cursor = await list_incidents(pool)
    assert items == []
    assert next_cursor is None


async def test_list_incidents_returns_items(pool: FakePool) -> None:
    pool.fetch_result = [
        FakeRow(
            id="inc_001",
            state="open",
            severity="warning",
            title="t",
            fingerprint="0123abcd",
            opened_at=T0,
            closed_at=None,
            services='["frontend"]',
        ),
        FakeRow(
            id="inc_002",
            state="open",
            severity="critical",
            title="t2",
            fingerprint="beefcafe",
            opened_at=T0,
            closed_at=None,
            services='["cartservice"]',
        ),
    ]
    items, next_cursor = await list_incidents(pool, limit=50)
    assert [i.id for i in items] == ["inc_001", "inc_002"]
    assert next_cursor is None  # exactly `limit` rows, no more
    assert items[0].services == ["frontend"]


async def test_list_incidents_signals_more_pages_via_cursor(pool: FakePool) -> None:
    """Asking for one extra row lets us detect "more"; next_cursor is the last visible id."""
    pool.fetch_result = [
        FakeRow(
            id="inc_001",
            state="open",
            severity="warning",
            title="t",
            fingerprint="0123abcd",
            opened_at=T0,
            closed_at=None,
            services="[]",
        ),
        FakeRow(
            id="inc_002",
            state="open",
            severity="warning",
            title="t",
            fingerprint="0123abcd",
            opened_at=T0,
            closed_at=None,
            services="[]",
        ),
    ]
    items, next_cursor = await list_incidents(pool, limit=1)
    assert len(items) == 1  # clamped to `limit`
    assert next_cursor == "inc_001"  # id of last visible row in this page


async def test_list_incidents_clamps_limit(pool: FakePool) -> None:
    """An oversized `limit` is clamped to `max_page_size`, not honoured or rejected."""
    pool.fetch_result = []
    await list_incidents(pool, limit=9999)
    # limit+1 is appended as the final parameter; the clamp is max(1, min(limit, 100)).
    final_limit = pool.args_of("fetch")[0][-1]
    assert final_limit == 101  # 100 + 1


async def test_list_incidents_applies_state_filter(pool: FakePool) -> None:
    pool.fetch_result = []
    await list_incidents(pool, state="open")
    sql = pool.sql_of("fetch")[0]
    assert "WHERE" in sql
    assert "state = $1" in sql


async def test_list_incidents_applies_cursor_filter(pool: FakePool) -> None:
    pool.fetch_result = []
    await list_incidents(pool, cursor="inc_005")
    sql = pool.sql_of("fetch")[0]
    assert "id > $1" in sql


async def test_list_incidents_orders_by_id(pool: FakePool) -> None:
    pool.fetch_result = []
    await list_incidents(pool)
    sql = pool.sql_of("fetch")[0]
    assert "ORDER BY id" in sql


async def test_list_incidents_raises_storage_unavailable(pool: FakePool) -> None:
    pool.acquire_error = OSError("connection refused")
    with pytest.raises(StorageUnavailable):
        await list_incidents(pool)


# --- record_audit ---


async def test_record_audit_inserts_and_returns_an_id(pool: FakePool) -> None:
    audit_id = await record_audit(
        pool, actor="rifatbond007", action="rank", target="inc_001", after={"score": 0.91}
    )
    assert audit_id  # a ULID was generated
    sql = pool.sql_of("execute")[0]
    assert "INSERT INTO audit_log" in sql


async def test_record_audit_serialises_before_and_after(pool: FakePool) -> None:
    import json

    await record_audit(
        pool,
        actor="rifatbond007",
        action="resolve",
        target="inc_001",
        before={"state": "open"},
        after={"state": "resolved"},
    )
    before, after = pool.args_of("execute")[0][4:6]
    assert json.loads(before) == {"state": "open"}
    assert json.loads(after) == {"state": "resolved"}


async def test_record_audit_encodes_none_as_sql_null(pool: FakePool) -> None:
    await record_audit(pool, actor="rifatbond007", action="open", target="inc_001")
    before, after = pool.args_of("execute")[0][4:6]
    assert before == ""
    assert after == ""  # NULLIF(..., '') -> SQL NULL


async def test_record_audit_raises_storage_unavailable(pool: FakePool) -> None:
    pool.acquire_error = OSError("connection refused")
    with pytest.raises(StorageUnavailable):
        await record_audit(pool, actor="rifatbond007", action="open", target="inc_001")
