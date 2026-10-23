"""The asyncpg pool factory and the D20 "unavailable" contract.

`StorageUnavailable` is the typed surface of the D20 rule "If Postgres is unavailable,
the system refuses to acknowledge an incident rather than silently dropping it." Every
public write in `storage/incidents.py` raises `StorageUnavailable` on connection failure
or any post-connection Postgres error; the caller in `alerting/router.py` translates
that into "do nothing, log loudly" — and the API endpoint that processes a state
transition returns 503 so the upstream knows it was *not* silently dropped.

The pool is configured but not opened — that is the API factory's job, so a misconfigured
DSN does not crash module import. `init_schema(pool)` runs the canonical DDL once on the
first startup; subsequent startups are a no-op because every statement is `IF NOT EXISTS`.
"""

from __future__ import annotations

import asyncpg

from inframind.common.exceptions import InfraMindError

from .config import StorageParams
from .schema import INIT_SQL


class StorageUnavailable(InfraMindError):
    """D20 surface: storage is unreachable or rejected a write.

    The caller MUST treat this as "nothing happened, escalate" — not as a retry trigger,
    not as a fallback to the bus (the bus is not a store).
    """


async def create_pool(params: StorageParams, dsn: str) -> asyncpg.Pool:
    """Open the asyncpg pool. Caller passes the resolved DSN.

    `statement_cache_size=0` so PgBouncer-style middlemen do not poison the cache; the
    testbed has no pooler in front of Postgres, but the option is a safe default for any
    future deployment.
    """
    try:
        return await asyncpg.create_pool(
            dsn=dsn,
            min_size=params.pool_min_size,
            max_size=params.pool_max_size,
            command_timeout=params.command_timeout_s,
            statement_cache_size=0,
        )
    except (OSError, asyncpg.PostgresError) as e:
        raise StorageUnavailable(f"could not connect to Postgres: {e}") from e


async def init_schema(pool: asyncpg.Pool) -> None:
    """Bring an empty database to the P6 shape.

    Idempotent: every statement is `CREATE TABLE IF NOT EXISTS`. Safe to call on every
    process start. Returns when the schema is ready; subsequent calls are a few no-op
    statements.
    """
    try:
        async with pool.acquire() as conn:
            await conn.execute(INIT_SQL)
    except (OSError, asyncpg.PostgresError) as e:
        raise StorageUnavailable(f"could not initialise schema: {e}") from e


async def close_pool(pool: asyncpg.Pool) -> None:
    """Close the pool gracefully; ``None`` if the pool never opened is the caller's job."""
    try:
        await pool.close()
    except (OSError, asyncpg.PostgresError) as e:
        raise StorageUnavailable(f"could not close pool cleanly: {e}") from e


__all__ = ["StorageUnavailable", "close_pool", "create_pool", "init_schema"]
