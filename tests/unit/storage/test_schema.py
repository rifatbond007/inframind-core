"""Schema tests for `storage.schema.INIT_SQL` (no Postgres required).

Two invariants matter here:

1. **Every table the code writes exists.** `write_incident` names columns positionally; if
   the DDL and the INSERT drift apart, asyncpg fails at runtime, not at import time. These
   tests cross-check the DDL against the parameter tuples so the drift is caught in CI.
2. **The append-only and lifecycle invariants are encoded in the DDL.** The audit log has
   no UPDATE/DELETE statement in this module, and the `state`/`severity` CHECK constraints
   are the DB-side backstop for the state machine.
"""

from __future__ import annotations

import re

from inframind.storage.incidents import _incident_record_args
from inframind.storage.schema import INIT_SQL

TABLES = ("incidents", "candidates", "evidence", "alerts", "audit_log")


def _columns(table: str) -> set[str]:
    """Extract column names from a `CREATE TABLE <table> (...)` block."""
    match = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", INIT_SQL, re.DOTALL)
    assert match is not None, f"no CREATE TABLE for {table}"
    body = match.group(1)
    names: set[str] = set()
    for raw in body.split(",\n"):
        line = raw.strip()
        if not line:
            continue
        # Skip constraint clauses (CHECK, UNIQUE, REFERENCES, PRIMARY KEY).
        first = line.split()[0]
        if first.upper() in {"CHECK", "UNIQUE", "FOREIGN", "PRIMARY"}:
            continue
        names.add(first)
    return names


def test_init_sql_declares_every_table() -> None:
    for table in TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in INIT_SQL, table


def test_init_sql_is_idempotent() -> None:
    """Every CREATE is `IF NOT EXISTS`, so `init_schema` is safe on every process start."""
    creates = re.findall(r"CREATE TABLE (?!.*IF NOT EXISTS)", INIT_SQL)
    assert creates == []
    # Indexes likewise.
    assert "CREATE INDEX IF NOT EXISTS" in INIT_SQL


def test_init_sql_has_no_update_or_delete_statement() -> None:
    """D20: the audit log is append-only. No mutation statements ship in the schema.

    `ON DELETE CASCADE` is a referential action on the child FKs, not a mutation surface
    on `audit_log`, so only top-level mutation *statements* are rejected. Comments are
    stripped first so the schema's explanatory prose cannot trip the check.
    """
    statements = "\n".join(
        line for line in INIT_SQL.splitlines() if not line.lstrip().startswith("--")
    )
    assert not re.search(r"^\s*UPDATE\s", statements, re.MULTILINE | re.IGNORECASE)
    assert not re.search(r"^\s*DELETE\s+FROM\s", statements, re.MULTILINE | re.IGNORECASE)
    assert not re.search(r"^\s*TRUNCATE\s", statements, re.MULTILINE | re.IGNORECASE)


def test_audit_log_table_has_no_update_or_delete_facility() -> None:
    """`audit_log` declares no surrogate key a mutator could address, only `id`. This is
    the application-layer half of append-only; the DB grants are documented in 06b."""
    columns = _columns("audit_log")
    assert columns == {"id", "ts", "actor", "action", "target", "before", "after"}
    # No timestamp trigger or `updated_at` column that a mutation could hide behind.
    assert "updated_at" not in columns


def test_incidents_columns_cover_the_write_tuple() -> None:
    """`write_incident` passes 15 positional args; the DDL must have a column for each."""
    from datetime import UTC, datetime

    from inframind.common.incident import Incident
    from inframind.common.signal import Severity

    incident = Incident(
        id="inc_1",
        revision=1,
        state="open",
        severity=Severity.WARNING,
        title="t",
        fingerprint="0123abcd",
        opened_at=datetime(2026, 10, 21, 12, 0, tzinfo=UTC),
        updated_at=datetime(2026, 10, 21, 12, 0, tzinfo=UTC),
        services=["frontend"],
        signals=[],
        anomalies=[],
    )
    args = _incident_record_args(incident, scenario_id="scn_x")
    assert len(args) == 15

    columns = _columns("incidents")
    expected = {
        "id",
        "scenario_id",
        "revision",
        "state",
        "severity",
        "fingerprint",
        "title",
        "opened_at",
        "updated_at",
        "closed_at",
        "services",
        "signals",
        "anomalies",
        "summary_text",
        "alerts",
    }
    assert expected <= columns


def test_candidates_columns_are_the_full_p5_shape() -> None:
    """The full P5 `Candidate` (6 fields) is stored, not the narrower surface-map shape."""
    columns = _columns("candidates")
    assert {"depth", "change_correlation", "confidence"} <= columns


def test_state_check_constraint_covers_the_four_states() -> None:
    for state in ("open", "acknowledged", "resolved", "expired"):
        assert f"'{state}'" in INIT_SQL


def test_scenario_id_column_is_nullable() -> None:
    """P7 scenario tagging is opt-in; the column is NULL in normal operation."""
    columns = _columns("incidents")
    assert "scenario_id" in columns
    assert re.search(r"scenario_id\s+TEXT NULL", INIT_SQL)


def test_alerts_sink_status_check_covers_all_three_outcomes() -> None:
    for status in ("delivered", "queued", "failed"):
        assert f"'{status}'" in INIT_SQL


def test_evidence_kind_check_matches_the_model() -> None:
    """The DDL CHECK must list exactly the `EvidenceKind` literals."""
    from inframind.rca.evidence import EvidenceKind

    literals = re.search(r"kind IN \(([^)]*)\)\)", INIT_SQL)
    assert literals is not None
    ddl_kinds = set(re.findall(r"'(\w+)'", literals.group(1)))
    # EvidenceKind is a Literal; introspect its args.
    import typing

    assert ddl_kinds == set(typing.get_args(EvidenceKind))
