"""The `/change-events` route: read the K8s change log (D22, standard 14.5).

Read-only. The evaluation recorder uses it to copy what the change watcher observed during
a fault into the fixture, so replay ranks with the same `w4` input as the live process.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from inframind.api.deps import require_pool
from inframind.storage import StorageUnavailable, list_change_events

router = APIRouter(prefix="/change-events", tags=["change-events"])

PoolDep = Annotated[Any, Depends(require_pool)]


@router.get("")
async def list_route(
    start: Annotated[datetime, Query()],
    end: Annotated[datetime, Query()],
    service: Annotated[str | None, Query()] = None,
    pool: PoolDep = None,
) -> dict[str, Any]:
    """Change events with ``start <= occurred_at <= end`` (timezone-aware ISO 8601)."""
    if start.tzinfo is None or end.tzinfo is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="start and end must carry a timezone",
        )
    if end < start:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="end is before start"
        )
    try:
        events = await list_change_events(pool, start=start, end=end, service=service)
    except StorageUnavailable as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e)) from e
    return {"items": [e.model_dump(mode="json") for e in events]}


__all__ = ["router"]
