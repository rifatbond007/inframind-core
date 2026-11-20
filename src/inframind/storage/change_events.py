"""The append-only `change_events` table (D22, standard 14.5).

The K8s change watcher's events, persisted so a restarted RCA process can reseed its
in-memory buffer (the standard 14.8 cold path) and so the evaluation recorder can read
what the watcher saw (through the API). Only `record_change_event` writes; there is no
update and no delete path. Rows carry metadata only, never a Secret value (D9).
"""

from __future__ import annotations

from datetime import datetime

import asyncpg

from inframind.rca.change_correlation import ChangeEvent

from .db import StorageUnavailable


async def record_change_event(pool: asyncpg.Pool, event: ChangeEvent) -> None:
    """Append one change event."""
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO change_events
                    (occurred_at, service, kind, resource, revision, prev_revision)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                event.occurred_at,
                event.service,
                event.kind,
                event.resource,
                event.revision,
                event.prev_revision,
            )
    except (OSError, asyncpg.PostgresError) as e:
        raise StorageUnavailable(f"record_change_event for {event.resource!r} failed: {e}") from e


async def list_change_events(
    pool: asyncpg.Pool,
    *,
    start: datetime,
    end: datetime,
    service: str | None = None,
) -> list[ChangeEvent]:
    """Change events with ``start <= occurred_at <= end``, oldest first."""
    query = """
        SELECT occurred_at, service, kind, resource, revision, prev_revision
        FROM change_events
        WHERE occurred_at >= $1 AND occurred_at <= $2
          AND ($3::text IS NULL OR service = $3)
        ORDER BY occurred_at, id
    """
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, start, end, service)
    except (OSError, asyncpg.PostgresError) as e:
        raise StorageUnavailable(f"list_change_events failed: {e}") from e
    return [ChangeEvent(**dict(row)) for row in rows]


__all__ = ["list_change_events", "record_change_event"]
