# `inframind.alerting`

Delivers deduplicated, actionable incident notifications.

This stage turns ranked RCA output into alerts a human can act on. It consumes the ranked
candidate list **in-process** from the RCA worker — there is no new Redis stream key for
the RCA→alerting hand-off (D20 + surface-map `Stages`). That keeps the bus keys pinned and
makes the seam a Python call.

## What it does

- **Dedupes**: one alert per open incident, re-arming on severity escalation. The state
  is process-local; the durable mirror is the `alerts` table written by `inframind.storage`.
- **Renders the payload**: top candidate with evidence ids, blast radius (services
  downstream of the primary), the LLM summary re-redacted before it goes out (D9).
- **Fans out** to the configured sinks: `console` (always in dev) and `webhook` (when
  `INFRAMIND_ALERTING_WEBHOOK_URL` is set). Slack, Telegram, and email sinks are deferred.

## Files

| File | What it holds |
|---|---|
| `config.py` | `AlertingParams` — dedup window, console on/off, webhook URL, webhook timeout. |
| `dedup.py` | `DedupTracker` — keyed by incident id, severity-escalation re-arms. Clock injectable for tests. |
| `payload.py` | `build_payload(incident, candidates, summary_text, audit_id)` — pure function. |
| `router.py` | `AlertRouter.dispatch(...)` — the one object the RCA worker holds. |
| `sinks/console.py` | Synchronous print to the JSON logger. |
| `sinks/webhook.py` | `httpx.AsyncClient` POST, with `delivered` / `queued` / `failed` outcomes. |
| `sinks/__init__.py` | Eagerly imports both submodules so `sinks.console` and `sinks.webhook` resolve. |

## Sink contract

Every sink exposes `send(payload) -> (alert_id, status)`:

- **`delivered`** — the sink reached the human (console, or webhook 2xx).
- **`queued`** — the webhook responded non-2xx; we tried, retry on the bus.
- **`failed`** — a transport error; the next attempt will re-encode and try again.

The router collects the outcomes and the worker writes one row per `alert_id` into the
`alerts` table, so every fire is durably recorded regardless of sink status.

## Dedup contract

- **One alert per open incident** within `dedup_window_s` (default 5 minutes).
- **Severity escalation re-arms**: a `warning` already-alerted incident still fires on
  `critical`. A demotion does not.
- The tracker is **process-local**. A process restart can re-fire an alert but can never
  *miss* one — the `alerts` table is the durable mirror, written by the caller.

## The LLM summary contract

- The summary is **presentation only** (D1). The ranker's order passes through unchanged.
- `summary_text` is **re-redacted** before it goes out. The model's output is not trusted
  verbatim; the same redactor that ran before sending to the provider runs again here.

## Tests

- `tests/unit/alerting/` — pure functions and the dispatcher with patched sinks.
- No integration test for alerting: sinks are either local (console) or HTTP (webhook),
  and the synthetic testbed has no upstream webhook to receive from.

## Owner

Rifat on `dev-rifat` (P6 ownership taken on this branch by user direction); Prome on paper.
