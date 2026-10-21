"""FastAPI app factory — `/healthz`, `/readyz`, `/metrics`, the Alertmanager webhook, and the incident routes.

The P3 surface (`/healthz`, `/webhooks/alertmanager`) is unchanged. P6 adds the readiness
probe, the Prometheus endpoint, and the incident routes; the Postgres pool and the
alerting router are optional keyword-only arguments so the existing webhook tests (which
run without storage) keep passing.
"""

from __future__ import annotations

import os

import asyncpg
from fastapi import FastAPI, Response, status
from redis.asyncio import Redis

from inframind.alerting import AlertRouter
from inframind.api.alertmanager import router as alertmanager_router
from inframind.api.incidents import router as incidents_router
from inframind.common.logging import configure_logging, get_logger

logger = get_logger(__name__)


def create_app(
    redis: Redis,
    *,
    webhook_secret: str,
    postgres_pool: asyncpg.Pool | None = None,
    alerting_router: AlertRouter | None = None,
) -> FastAPI:
    """Build the app.

    `webhook_secret` is the bearer token Alertmanager must present.
    `postgres_pool` and `alerting_router` are optional; without them the incident routes
    and `/readyz` report "storage not configured" rather than crashing at boot, which is
    what keeps the P3 test suite honest.
    """
    if not webhook_secret:
        raise ValueError("webhook secret must not be empty (INFRAMIND_ALERT_WEBHOOK_SECRET)")
    app = FastAPI(title="InfraMind", docs_url=None, redoc_url=None)
    app.state.redis = redis
    app.state.webhook_secret = webhook_secret
    app.state.postgres_pool = postgres_pool
    app.state.alerting_router = alerting_router
    app.include_router(alertmanager_router)
    app.include_router(incidents_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> Response:
        """200 only when Redis AND Postgres respond; 503 naming the failing dependency."""
        checks: dict[str, str] = {}
        try:
            await redis.ping()
            checks["redis"] = "ok"
        except Exception:
            checks["redis"] = "unavailable"

        pool = app.state.postgres_pool
        if pool is None:
            checks["postgres"] = "not_configured"
        else:
            try:
                async with pool.acquire() as conn:
                    await conn.fetchval("SELECT 1")
                checks["postgres"] = "ok"
            except Exception:
                checks["postgres"] = "unavailable"

        healthy = all(v == "ok" for v in checks.values())
        body = {"status": "ready" if healthy else "not_ready", "checks": checks}
        code = status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
        return Response(
            content=__import__("json").dumps(body),
            status_code=code,
            media_type="application/json",
        )

    @app.get("/metrics")
    async def metrics() -> Response:
        """Prometheus exposition for the InfraMind process itself.

        Kept dependency-free: the counters below are the process-level signals the
        testbed needs (uptime, request counter). A richer exposition (histograms,
        per-incident gauges) lands with the P8 deployment, which is where a real
        prometheus_client integration belongs.
        """
        import time

        payload = (
            "# HELP inframind_up Whether the InfraMind API process is serving.\n"
            "# TYPE inframind_up gauge\n"
            "inframind_up 1\n"
            "# HELP inframind_uptime_seconds Seconds since process start.\n"
            "# TYPE inframind_uptime_seconds gauge\n"
            f"inframind_uptime_seconds {time.monotonic():.3f}\n"
        )
        return Response(content=payload, media_type="text/plain; version=0.0.4")

    return app


def main() -> None:
    """Serve on INFRAMIND_API_HOST:INFRAMIND_API_PORT (default 0.0.0.0:8080)."""
    import asyncio

    import uvicorn

    configure_logging()

    async def _serve() -> None:
        pool = None
        from inframind.storage import build_params, create_pool, init_schema

        params, dsn, _scenario = build_params()
        try:
            pool = await create_pool(params, dsn)
            await init_schema(pool)
        except Exception as e:
            logger.warning("storage_init_failed", error=str(e))
            pool = None

        app = create_app(
            Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0")),
            webhook_secret=os.environ.get("INFRAMIND_ALERT_WEBHOOK_SECRET", ""),
            postgres_pool=pool,
            alerting_router=AlertRouter(),
        )
        config = uvicorn.Config(
            app,
            host=os.environ.get("INFRAMIND_API_HOST", "0.0.0.0"),  # - in-cluster service
            port=int(os.environ.get("INFRAMIND_API_PORT", "8080")),
        )
        await uvicorn.Server(config).serve()

    asyncio.run(_serve())


if __name__ == "__main__":
    main()
