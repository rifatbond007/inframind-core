"""FastAPI dependencies: the Postgres pool, the alerting router, and the actor identity.

The pool and router live on `app.state` (set by `create_app`), so tests can inject a fake
pool without booting Postgres. The actor comes from an `X-Actor` header per D25: the
value is the GitHub username of the human who initiated the action — never a display
name and never an AI tool name. The API is the boundary where that is validated.
"""

from __future__ import annotations

from typing import Annotated

import asyncpg
from fastapi import Header, HTTPException, Request, status

from inframind.alerting import AlertRouter
from inframind.storage import StorageUnavailable

# D25: the commit author format. A value that does not look like a GitHub username is
# rejected rather than silently recorded.
_ACTOR_MAX_LEN = 39  # GitHub's username length ceiling


def get_pool(request: Request) -> asyncpg.Pool | None:
    """Return the Postgres pool from `app.state`, or None when storage is not wired."""
    return getattr(request.app.state, "postgres_pool", None)


def require_pool(request: Request) -> asyncpg.Pool:
    """Return the pool or raise 503 — the D20 "refuse, don't silently drop" contract."""
    pool = get_pool(request)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="storage is not configured",
        )
    return pool


def get_router(request: Request) -> AlertRouter | None:
    """Return the alerting router from `app.state`, or None when not wired."""
    return getattr(request.app.state, "alerting_router", None)


def get_actor(x_actor: Annotated[str | None, Header()] = None) -> str:
    """Extract and validate the acting GitHub username from `X-Actor`.

    D25 is an audit-log rule, and the audit log records whoever performed the action.
    We accept the GitHub username form (alphanumeric plus hyphens, matching a real
    handle) and reject anything that looks like a display name (spaces) or an AI tool
    (contains "copilot", "claude", "gpt", "cursor", "codex", or ends in "[bot]").
    """
    if not x_actor:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Actor header is required (GitHub username per D25)",
        )
    actor = x_actor.strip()
    if not actor or len(actor) > _ACTOR_MAX_LEN:
        raise HTTPException(status_code=400, detail="X-Actor is empty or too long")
    if " " in actor:
        raise HTTPException(
            status_code=400, detail="X-Actor must be a username, not a display name"
        )
    lowered = actor.lower()
    for banned in ("copilot", "claude", "gpt", "cursor", "codex", "puku"):
        if banned in lowered:
            raise HTTPException(status_code=400, detail="X-Actor must be a human GitHub username")
    if lowered.endswith("[bot]"):
        raise HTTPException(status_code=400, detail="X-Actor must be a human, not a bot")
    return actor


__all__ = ["StorageUnavailable", "get_actor", "get_pool", "get_router", "require_pool"]
