# `inframind.api`

The FastAPI surface for the InfraMind process — **the only module in the project that
imports FastAPI** (standard 01 §1.3, enforced by `tests/unit/test_module_dag.py`).

## Endpoints

| Route | Method | Behaviour |
|---|---|---|
| `/healthz` | GET | `{"status":"ok"}` — liveness, no dependencies. |
| `/readyz` | GET | **200 only when Redis ping AND Postgres `SELECT 1` both succeed**; otherwise 503 with a JSON body naming the failing dependency (D20 at the HTTP edge). |
| `/metrics` | GET | Prometheus exposition: `inframind_up`, `inframind_uptime_seconds`. A richer exposition (histograms, per-incident gauges) lands with the P8 deployment. |
| `/webhooks/alertmanager` | POST | Bearer-secret-authenticated; 1 MiB / 1000-alert limits; produces one `Signal` per firing alert. Unchanged from P3. |
| `/incidents` | GET | Paginated list. Query: `state`, `service`, `since` (ISO-8601), `limit` (max 100, default 50), `cursor` (incident id). Returns `{items, next_cursor}`. |
| `/incidents/{id}` | GET | The full incident with ranked candidates and evidence. |
| `/incidents/{id}/ack` | POST | Mark acknowledged. Requires `X-Actor: <github_username>` (D25). |
| `/incidents/{id}/resolve` | POST | Mark resolved. Same `X-Actor` contract. |
| `/incidents/{id}` | PATCH / DELETE | **405.** There is no generic mutation endpoint; state transitions are explicit and instrumented. |

## Boot order

`create_app(redis, *, webhook_secret, postgres_pool=None, alerting_router=None)` is the
factory the test suite uses. `postgres_pool=None` is the right default for the P3 webhook
tests that pre-date storage; the resulting app returns 503 from `/readyz` and every
incident route, but the webhook tests stay green. Production calls `_serve()` in
`app.main()` which opens the pool, runs `init_schema`, and constructs the router before
serving.

## Files

| File | What it holds |
|---|---|
| `app.py` | `create_app`, `/healthz`, `/readyz`, `/metrics`, and the `main()` entrypoint. |
| `deps.py` | `get_pool`, `require_pool`, `get_router`, `get_actor` — typed FastAPI dependencies. |
| `alertmanager.py` | The webhook surface and the pure `alerts_to_signals` mapper (P3, unchanged). |
| `incidents.py` | The `/incidents/*` routes. |

## The `X-Actor` header (D25)

`get_actor` rejects anything that doesn't look like a GitHub username: missing → 400,
display name (spaces) → 400, AI tool name (`copilot`, `claude`, `gpt`, `cursor`,
`codex`, `puku`) → 400, suffix `[bot]` → 400, longer than 39 chars → 400. The check is
deliberately narrow so a stray process can't smuggle a non-human author into the audit log.

## Tests

- `tests/unit/api/` — webhook payload mapping, the alertmanager POST, the `/readyz` matrix,
  and the `/incidents/*` routes.

## Owner

Rifat on `dev-rifat` (P6 ownership taken on this branch by user direction); Prome on paper.
