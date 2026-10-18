# `inframind.api`

FastAPI surface for the Alertmanager webhook, health, metrics, and incident introspection.

## Built (P3)

- `app.py` — `create_app(redis, webhook_secret=…)` and `python -m inframind.api.app`; serves
  `GET /healthz`.
- `alertmanager.py` — `POST /webhooks/alertmanager` (bearer secret, 1 MiB / 1000-alert limits)
  and the pure `alerts_to_signals` mapper.

## Will be added in Step 8

- `/readyz`, `/metrics`, `/incidents` routes and `deps.py` (config, db, bus).

## Owner

Prome — RCA, LLM, alerting, storage.
