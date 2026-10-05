# `inframind.common`

Shared cross-cutting code used by every other module.

## Will be added in Step 2 (schema + event bus) and onward

- `Signal` — unified Pydantic model: `{id, ts, source, type, service, severity, attrs, trace_id}`.
- `Config` — env-driven settings loader (uses `.env` from repo root).
- Logging — structured JSON output.
- Clock / time utilities (frozen-time helpers for tests).
