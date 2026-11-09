"""`record_summary`, `record_alerts` and the read-back of both through `get_incident` (D28).

These are the writes the composition root makes after RCA: the LLM summary with an audit
row of the call, and one `alerts` row per sink outcome with an audit row of the dispatch.
"""

from __future__ import annotations

import json

import pytest

from inframind.storage.db import StorageUnavailable
from inframind.storage.incidents import (
    PIPELINE_ACTOR,
    get_incident,
    record_alerts,
    record_summary,
    write_incident,
)

from .conftest import FakePool
from .test_incidents import T0, FakeRow, _incident, _incident_row

# --- record_summary ---


async def test_record_summary_updates_the_incident_and_audits_the_call(pool: FakePool) -> None:
    ok = await record_summary(
        pool,
        "inc_001",
        summary_text="paymentservice is the root cause",
        provenance={"provider": "OPENAI", "model": "m", "status": "accepted", "raw": "{}"},
    )
    assert ok is True
    assert pool.transaction_entries == 1
    (update_sql, audit_sql) = pool.sql_of("execute")
    assert "UPDATE incidents SET summary_text" in update_sql
    assert "'llm_summary'" in audit_sql
    update_args, audit_args = pool.args_of("execute")
    assert update_args == ("paymentservice is the root cause", "inc_001")
    assert audit_args[1] == PIPELINE_ACTOR
    assert json.loads(audit_args[3])["status"] == "accepted"


async def test_record_summary_truncates_a_huge_raw_reply(pool: FakePool) -> None:
    await record_summary(
        pool, "inc_001", summary_text="s", provenance={"status": "rejected", "raw": "x" * 20000}
    )
    after = json.loads(pool.args_of("execute")[1][3])
    assert len(after["raw"]) == 8000
    assert after["raw_truncated"] is True


async def test_record_summary_returns_false_for_an_unknown_incident(pool: FakePool) -> None:
    pool.execute_result = "UPDATE 0"
    assert await record_summary(pool, "nope", summary_text="s", provenance={}) is False
    assert len(pool.sql_of("execute")) == 1  # no audit row for a missing incident


async def test_record_summary_raises_storage_unavailable(pool: FakePool) -> None:
    pool.fail_on_execute = 1
    with pytest.raises(StorageUnavailable):
        await record_summary(pool, "inc_001", summary_text="s", provenance={})


# --- record_alerts ---


async def test_record_alerts_writes_one_row_per_sink_and_one_audit(pool: FakePool) -> None:
    pool.execute_result = "INSERT 0 1"
    inserted = await record_alerts(
        pool,
        "inc_001",
        severity="critical",
        payload={"incident_id": "inc_001", "summary_text": "s"},
        outcomes=[("al_1", "console", "delivered"), ("al_2", "webhook", "failed")],
    )
    assert inserted == 2
    sqls = [" ".join(s.split()) for s in pool.sql_of("execute")]
    assert sum("INSERT INTO alerts" in s for s in sqls) == 2
    assert any("ON CONFLICT (id) DO NOTHING" in s for s in sqls)
    assert any("UPDATE incidents SET alerts = alerts ||" in s for s in sqls)
    assert any("'alert'" in s for s in sqls)
    first = pool.args_of("execute")[0]
    assert first[:4] == ("al_1", "inc_001", "critical", "console")
    assert json.loads(first[4])["alert_id"] == "al_1"  # the stored payload names its alert
    assert first[5] == "delivered"


async def test_record_alerts_counts_a_redelivered_alert_as_not_inserted(pool: FakePool) -> None:
    pool.execute_result = "INSERT 0 0"
    inserted = await record_alerts(
        pool, "inc_001", severity="warning", payload={}, outcomes=[("al_1", "console", "delivered")]
    )
    assert inserted == 0


async def test_record_alerts_with_no_outcomes_touches_nothing(pool: FakePool) -> None:
    assert await record_alerts(pool, "inc_001", severity="warning", payload={}, outcomes=[]) == 0
    assert pool.calls == []


async def test_record_alerts_raises_storage_unavailable(pool: FakePool) -> None:
    pool.fail_on_execute = 1
    with pytest.raises(StorageUnavailable):
        await record_alerts(
            pool, "inc_001", severity="warning", payload={}, outcomes=[("a", "console", "failed")]
        )


# --- write_incident keeps what the pipeline and humans own ---


async def test_write_incident_never_overwrites_summary_or_alerts(pool: FakePool) -> None:
    await write_incident(pool, _incident(), [])
    upsert = " ".join(pool.sql_of("execute")[0].split())
    on_conflict = upsert.split("ON CONFLICT", 1)[1]
    assert "summary_text =" not in on_conflict
    assert "alerts =" not in on_conflict


async def test_write_incident_keeps_a_human_state_over_a_pipeline_open(pool: FakePool) -> None:
    await write_incident(pool, _incident(), [])
    upsert = " ".join(pool.sql_of("execute")[0].split())
    assert "WHEN EXCLUDED.state = 'open' AND incidents.state <> 'open' THEN incidents.state" in (
        upsert
    )
    assert "THEN incidents.closed_at ELSE EXCLUDED.closed_at" in upsert


# --- get_incident read-back ---


async def test_get_incident_returns_summary_and_alerts(pool: FakePool) -> None:
    pool.fetchrow_result = _incident_row(summary_text="paymentservice is the root cause")
    pool.fetch_results = [
        [],
        [],
        [
            FakeRow(
                id="al_1",
                channel="console",
                severity="warning",
                sink_status="delivered",
                sent_at=T0,
                payload='{"incident_id": "inc_001"}',
            )
        ],
    ]
    detail = await get_incident(pool, "inc_001")
    assert detail is not None
    assert detail.summary_text == "paymentservice is the root cause"
    assert [a.id for a in detail.alerts] == ["al_1"]
    assert detail.alerts[0].payload == {"incident_id": "inc_001"}


async def test_get_incident_without_a_summary_column_reads_none(pool: FakePool) -> None:
    pool.fetchrow_result = _incident_row()  # no summary_text key at all
    pool.fetch_results = [[], [], []]
    detail = await get_incident(pool, "inc_001")
    assert detail is not None
    assert detail.summary_text is None
    assert detail.alerts == []
