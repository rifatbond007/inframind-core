"""Persisted incident records, ranked candidates, and evidence (D20).

All writes go through `write_incident` and `transition_incident` so a single transaction
covers the `incidents`, `candidates`, and `evidence` rows. A transaction that fails
halfway through leaves the database unchanged because asyncpg rolls back on any exception,
which is exactly the "refuse, not silently drop" behaviour D20 requires.

Reads paginate by an opaque cursor — the cursor is the incident id of the last row in
the previous page. The endpoint contract is `?cursor=<id>&limit=N`; `next_cursor` is
the id the client should pass to fetch the page after the one they just got, or `null`
when there is no more.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import asyncpg

from inframind.common.ids import new_ulid
from inframind.common.incident import Incident
from inframind.common.logging import get_logger
from inframind.rca.evidence import Candidate, Evidence

from .db import StorageUnavailable

logger = get_logger(__name__)

IncidentState = Literal["open", "acknowledged", "resolved", "expired"]
Severity = Literal["info", "warning", "critical"]


def _to_jsonb(value: object) -> str:
    """Serialise `value` for asyncpg JSONB; ``None`` is stored as SQL NULL."""
    if value is None:
        return ""
    return json.dumps(value, separators=(",", ":"), default=str)


@dataclass(frozen=True)
class IncidentListItem:
    """One row from `list_incidents` — denormalised for the API listing endpoint."""

    id: str
    state: str
    severity: str
    title: str
    fingerprint: str
    opened_at: object  # asyncpg returns datetime; the API serialises via Pydantic
    closed_at: object | None
    services: list[str]


@dataclass(frozen=True)
class IncidentDetail:
    """`get_incident` payload: the incident and its ranked candidates with evidence."""

    incident: Incident
    primary: Candidate | None
    candidates: list[Candidate]
    evidence_by_candidate: dict[str, list[Evidence]]


def _incident_record_args(
    incident: Incident, scenario_id: str | None
) -> tuple[
    str, str | None, int, str, str, str, str, object, object, object | None, str, str, str, str, str
]:
    """Project `Incident` into the positional tuple `write_incident` passes to asyncpg."""
    return (
        incident.id,
        scenario_id,
        incident.revision,
        incident.state,
        incident.severity,
        incident.fingerprint,
        incident.title,
        incident.opened_at,
        incident.updated_at,
        incident.closed_at,
        _to_jsonb(incident.services),
        _to_jsonb(incident.signals),
        _to_jsonb([a.model_dump(mode="json") for a in incident.anomalies]),
        _to_jsonb(None),  # summary_text is set later by the alerting pathway
        _to_jsonb([]),  # alerts list starts empty; the alerting layer appends
    )


async def write_incident(
    pool: asyncpg.Pool,
    incident: Incident,
    candidates: Sequence[Candidate],
    *,
    scenario_id: str | None = None,
) -> None:
    """Persist `incident` plus its ranked `candidates` and their evidence in one transaction.

    The transaction is implicit in asyncpg (a single `pool.acquire()` + one `execute()`
    per statement is not transactional; we use `pool.acquire()` + a manual
    `conn.transaction()`). Any error during the inserts rolls everything back — that is
    what makes this D20-compliant: either the incident is in Postgres or it isn't, never
    half.
    """
    try:
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                    INSERT INTO incidents (
                        id, scenario_id, revision, state, severity, fingerprint, title,
                        opened_at, updated_at, closed_at, services, signals, anomalies,
                        summary_text, alerts
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12::jsonb,
                              $13::jsonb, NULLIF($14, '')::text, $15::jsonb)
                    ON CONFLICT (id) DO UPDATE
                    SET revision = EXCLUDED.revision,
                        state = EXCLUDED.state,
                        severity = EXCLUDED.severity,
                        title = EXCLUDED.title,
                        opened_at = EXCLUDED.opened_at,
                        updated_at = EXCLUDED.updated_at,
                        closed_at = EXCLUDED.closed_at,
                        services = EXCLUDED.services,
                        signals = EXCLUDED.signals,
                        anomalies = EXCLUDED.anomalies
                    """,
                *_incident_record_args(incident, scenario_id),
            )
            # Re-write the children unconditionally: the ranker is deterministic
            # for one (graph, anomalies) input, so resync is idempotent.
            await conn.execute(
                "DELETE FROM candidates WHERE incident_id = $1",
                incident.id,
            )
            for rank, candidate in enumerate(candidates, start=1):
                await conn.execute(
                    """
                        INSERT INTO candidates (
                            id, incident_id, service, score, depth, change_correlation,
                            confidence, rank
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                        """,
                    candidate.id,
                    incident.id,
                    candidate.service,
                    candidate.score,
                    candidate.depth,
                    candidate.change_correlation,
                    candidate.confidence,
                    rank,
                )
                for evidence in candidate.evidence:
                    await conn.execute(
                        """
                            INSERT INTO evidence (
                                id, candidate_id, kind, summary, ref, weight
                            ) VALUES ($1, $2, $3, $4, $5, $6)
                            """,
                        evidence.id,
                        candidate.id,
                        evidence.kind,
                        evidence.summary,
                        evidence.ref,
                        evidence.weight,
                    )
    except (
        OSError,
        asyncpg.PostgresError,
        asyncpg.exceptions.IntegrityConstraintViolationError,
    ) as e:
        logger.warning(
            "storage_write_failed",
            incident_id=incident.id,
            error=str(e),
            error_type=type(e).__name__,
        )
        raise StorageUnavailable(f"write_incident for {incident.id!r} failed: {e}") from e


async def transition_incident(
    pool: asyncpg.Pool,
    incident_id: str,
    *,
    new_state: IncidentState,
    actor: str,
    audit_action: str,
) -> bool:
    """Move an incident to `new_state`, writing an audit row in the same transaction.

    Returns `True` when the row was updated, `False` when the id does not exist. State
    transitions that contradict the model (`open -> expired` from anywhere, ...) are
    rejected at the SQL level by `state` semantics if needed; today the API layer is the
    only caller and it already enforces valid transitions.

    `audit_action` is the verb recorded in `audit_log` (`ack`, `resolve`, `expire`); the
    `actor` is the D25 GitHub username that initiated the transition (D25: the API gets
    it from an `X-Actor` header and rejects display names).
    """
    try:
        async with pool.acquire() as conn, conn.transaction():
            updated = await conn.execute(
                "UPDATE incidents SET state = $1, updated_at = NOW() WHERE id = $2",
                new_state,
                incident_id,
            )
            if updated.endswith(" 0"):
                return False
            await conn.execute(
                """
                    INSERT INTO audit_log (id, ts, actor, action, target, before, after)
                    VALUES ($1, NOW(), $2, $3, $4, $5::jsonb, $6::jsonb)
                    """,
                new_ulid(),
                actor,
                audit_action,
                incident_id,
                _to_jsonb(None),
                _to_jsonb({"state": new_state}),
            )
        return True
    except (OSError, asyncpg.PostgresError) as e:
        logger.warning(
            "storage_transition_failed",
            incident_id=incident_id,
            new_state=new_state,
            actor=actor,
            error=str(e),
        )
        raise StorageUnavailable(f"transition for {incident_id!r} failed: {e}") from e


async def get_incident(pool: asyncpg.Pool, incident_id: str) -> IncidentDetail | None:
    """Return the full incident with its ranked candidates and evidence, or None."""
    try:
        async with pool.acquire() as conn:
            incident_row = await conn.fetchrow(
                """
                SELECT id, revision, state, severity, title, fingerprint,
                       opened_at, updated_at, closed_at, services, signals, anomalies
                FROM incidents WHERE id = $1
                """,
                incident_id,
            )
            if incident_row is None:
                return None
            candidate_rows = await conn.fetch(
                """
                SELECT id, service, score, depth, change_correlation, confidence
                FROM candidates WHERE incident_id = $1 ORDER BY rank
                """,
                incident_id,
            )
            evidence_rows = await conn.fetch(
                """
                SELECT e.id, e.candidate_id, e.kind, e.summary, e.ref, e.weight
                FROM evidence e
                JOIN candidates c ON c.id = e.candidate_id
                WHERE c.incident_id = $1
                """,
                incident_id,
            )
    except (OSError, asyncpg.PostgresError) as e:
        raise StorageUnavailable(f"get_incident for {incident_id!r} failed: {e}") from e

    evidence_by_candidate: dict[str, list[Evidence]] = {}
    for row in evidence_rows:
        item = Evidence(
            id=row["id"],
            kind=row["kind"],
            summary=row["summary"],
            ref=row["ref"],
            weight=row["weight"],
        )
        evidence_by_candidate.setdefault(row["candidate_id"], []).append(item)

    candidates: list[Candidate] = []
    for row in candidate_rows:
        candidates.append(
            Candidate(
                id=row["id"],
                service=row["service"],
                score=row["score"],
                depth=row["depth"],
                change_correlation=row["change_correlation"],
                confidence=row["confidence"],
                evidence=evidence_by_candidate.get(row["id"], []),
            )
        )

    incident = Incident(
        id=incident_row["id"],
        revision=incident_row["revision"],
        state=incident_row["state"],
        severity=incident_row["severity"],
        title=incident_row["title"],
        fingerprint=incident_row["fingerprint"],
        opened_at=incident_row["opened_at"],
        updated_at=incident_row["updated_at"],
        closed_at=incident_row["closed_at"],
        services=_as_str_list(incident_row["services"]),
        signals=_as_str_list(incident_row["signals"]),
        anomalies=[],
    )
    return IncidentDetail(
        incident=incident,
        primary=candidates[0] if candidates else None,
        candidates=candidates,
        evidence_by_candidate=evidence_by_candidate,
    )


def _as_str_list(value: object) -> list[str]:
    """Decode a JSONB array column into `list[str]`.

    `asyncpg` returns `jsonb` as a decoded Python object, but it can also surface the raw
    string depending on the codec configuration. Handle both.
    """
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return []
        return [str(item) for item in decoded] if isinstance(decoded, list) else []
    return []


async def list_incidents(
    pool: asyncpg.Pool,
    *,
    state: IncidentState | None = None,
    service: str | None = None,
    since: object | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[IncidentListItem], str | None]:
    """Paginated listing for `GET /incidents`.

    Filters compose with AND; `limit` is clamped to [1, 100]. `cursor` is the `id` of the
    last item in the previous page; results come strictly after that id in id ordering
    (incident ids are ULIDs, which sort by creation time, so this is also "after in time"
    modulo clock skew — acceptable for the API's purpose).
    """
    try:
        clamped = max(1, min(int(limit), 100))
        clauses: list[str] = []
        params: list[object] = []
        if state is not None:
            params.append(state)
            clauses.append(f"state = ${len(params)}")
        if service is not None:
            params.append(service)
            clauses.append(f"${len(params)} = ANY(SELECT jsonb_array_elements_text(services))")
        if since is not None:
            params.append(since)
            clauses.append(f"opened_at >= ${len(params)}")
        if cursor is not None:
            params.append(cursor)
            clauses.append(f"id > ${len(params)}")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(clamped + 1)  # ask for one extra to detect "more"
        sql = (
            f"SELECT id, state, severity, title, fingerprint, opened_at, closed_at, services "
            f"FROM incidents {where} ORDER BY id LIMIT ${len(params)}"
        )
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
    except (OSError, asyncpg.PostgresError) as e:
        raise StorageUnavailable(f"list_incidents failed: {e}") from e

    items = [
        IncidentListItem(
            id=row["id"],
            state=row["state"],
            severity=row["severity"],
            title=row["title"],
            fingerprint=row["fingerprint"],
            opened_at=row["opened_at"],
            closed_at=row["closed_at"],
            services=_as_str_list(row["services"]),
        )
        for row in rows[:clamped]
    ]
    next_cursor = rows[clamped - 1]["id"] if len(rows) > clamped else None
    return items, next_cursor


async def record_audit(
    pool: asyncpg.Pool,
    *,
    actor: str,
    action: str,
    target: str,
    before: object | None = None,
    after: object | None = None,
) -> str:
    """Append a row to `audit_log`. There is no `update_audit` or `delete_audit`.

    Returns the new audit row id so the caller can correlate downstream effects (the
    alerting layer stores it on the alert payload for forensics).
    """
    audit_id = new_ulid()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO audit_log (id, ts, actor, action, target, before, after)
                VALUES ($1, NOW(), $2, $3, $4, $5::jsonb, $6::jsonb)
                """,
                audit_id,
                actor,
                action,
                target,
                _to_jsonb(before),
                _to_jsonb(after),
            )
    except (OSError, asyncpg.PostgresError) as e:
        raise StorageUnavailable(f"record_audit for {target!r} failed: {e}") from e
    return audit_id


__all__ = [
    "IncidentDetail",
    "IncidentListItem",
    "get_incident",
    "list_incidents",
    "record_audit",
    "transition_incident",
    "write_incident",
]
