"""FastAPI app factory. Only the P3 surface exists so far: `/healthz` and the Alertmanager webhook.

`/readyz`, `/metrics` and the `/incidents` routes land with P6 (alerting + storage + API).
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from redis.asyncio import Redis

from inframind.api.alertmanager import router as alertmanager_router
from inframind.common.logging import configure_logging


def create_app(redis: Redis, *, webhook_secret: str) -> FastAPI:
    """Build the app. `webhook_secret` is the bearer token Alertmanager must present."""
    if not webhook_secret:
        raise ValueError("webhook secret must not be empty (INFRAMIND_ALERT_WEBHOOK_SECRET)")
    app = FastAPI(title="InfraMind", docs_url=None, redoc_url=None)
    app.state.redis = redis
    app.state.webhook_secret = webhook_secret
    app.include_router(alertmanager_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


def main() -> None:
    """Serve on INFRAMIND_API_HOST:INFRAMIND_API_PORT (default 0.0.0.0:8080)."""
    import uvicorn

    configure_logging()
    app = create_app(
        Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0")),
        webhook_secret=os.environ.get("INFRAMIND_ALERT_WEBHOOK_SECRET", ""),
    )
    uvicorn.run(
        app,
        host=os.environ.get("INFRAMIND_API_HOST", "0.0.0.0"),  # - in-cluster service
        port=int(os.environ.get("INFRAMIND_API_PORT", "8080")),
    )


if __name__ == "__main__":
    main()
