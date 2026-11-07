"""The `/incidents` routes — the read and transition surface (surface-map).

State transitions are **explicit** and there is no PATCH or DELETE: `POST /ack` and
`POST /resolve` are the only ways an incident moves, each writing an audit row with the
acting GitHub username (D25). A transition against an unavailable database returns 503 —
never a fake 200 — which is the D20 "refuse, don't silently drop" contract at the HTTP
edge.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from inframind.api.deps import get_actor, require_pool
from inframind.storage import (
    StorageUnavailable,
    get_incident,
    list_incidents,
    transition_incident,
)

router = APIRouter(prefix="/incidents", tags=["incidents"])

PoolDep = Annotated[Any, Depends(require_pool)]


def _incident_response(detail: Any) -> dict[str, Any]:
    """Serialise an `IncidentDetail` for JSON. `primary` is the top candidate or None."""
    incident = detail.incident
    return {
        "id": incident.id,
        "revision": incident.revision,
        "state": incident.state,
        "severity": incident.severity,
        "title": incident.title,
        "fingerprint": incident.fingerprint,
        "opened_at": incident.opened_at,
        "updated_at": incident.updated_at,
        "closed_at": incident.closed_at,
        "services": incident.services,
        "signals": incident.signals,
        "candidates": [c.model_dump(mode="json") for c in detail.candidates],
        "primary": detail.primary.service if detail.primary else None,
    }


@router.get("")
async def list_route(
    request: Request,
    state_filter: Annotated[str | None, Query(alias="state")] = None,
    service: Annotated[str | None, Query()] = None,
    since: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query()] = None,
    pool: PoolDep = None,
) -> dict[str, Any]:
    """Paginated incident list. Returns `{items, next_cursor}`."""
    try:
        items, next_cursor = await list_incidents(
            pool,
            state=state_filter,  # type: ignore[arg-type]
            service=service,
            since=since,
            limit=limit,
            cursor=cursor,
        )
    except StorageUnavailable as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e),
        ) from e
    return {
        "items": [
            {
                "id": item.id,
                "state": item.state,
                "severity": item.severity,
                "title": item.title,
                "fingerprint": item.fingerprint,
                "opened_at": item.opened_at,
                "closed_at": item.closed_at,
                "services": item.services,
            }
            for item in items
        ],
        "next_cursor": next_cursor,
    }


@router.get("/{incident_id}")
async def detail_route(incident_id: str, pool: PoolDep = None) -> dict[str, Any]:
    """Full incident with candidates and evidence, or 404."""
    try:
        detail = await get_incident(pool, incident_id)
    except StorageUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    if detail is None:
        raise HTTPException(status_code=404, detail=f"incident {incident_id!r} not found")
    return _incident_response(detail)


@router.post("/{incident_id}/ack")
async def ack_route(
    incident_id: str,
    actor: Annotated[str, Depends(get_actor)],
    pool: PoolDep = None,
) -> dict[str, Any]:
    """Mark an incident acknowledged. `X-Actor` must be a GitHub username (D25)."""
    try:
        ok = await transition_incident(
            pool, incident_id, new_state="acknowledged", actor=actor, audit_action="ack"
        )
    except StorageUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    if not ok:
        raise HTTPException(status_code=404, detail=f"incident {incident_id!r} not found")
    return {"id": incident_id, "state": "acknowledged"}


@router.post("/{incident_id}/resolve")
async def resolve_route(
    incident_id: str,
    actor: Annotated[str, Depends(get_actor)],
    pool: PoolDep = None,
) -> dict[str, Any]:
    """Mark an incident resolved. `X-Actor` must be a GitHub username (D25)."""
    try:
        ok = await transition_incident(
            pool, incident_id, new_state="resolved", actor=actor, audit_action="resolve"
        )
    except StorageUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    if not ok:
        raise HTTPException(status_code=404, detail=f"incident {incident_id!r} not found")
    return {"id": incident_id, "state": "resolved"}


__all__ = ["router"]
