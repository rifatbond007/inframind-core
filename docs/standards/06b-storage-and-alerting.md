# 06b. Storage and alerting — the durable record and the page
> standards · Infrastructure · InfraMind. Pairs with the `alerting-storage-engineer` skill in `.claude/skills/` (the implementation checklist) and the module READMEs in `src/inframind/storage/`, `src/inframind/alerting/`, and `src/inframind/api/`. D20, D22, D25.

## 06b.1 Why this standard exists

By P6 the pipeline can detect, correlate, rank, and explain an incident. What it could not do was **remember** it or **tell anyone**. The `alerts` and `audit_log` tables named in the surface map had no DDL, no writer, and no reader. The `/incidents` routes existed on paper only.

This standard pins:

- **The schema** — every table, column, constraint, and index, with the reason each exists.
- **The RCA → alerting hand-off** — in-process, with no new Redis stream key.
- **The dedup contract** — one alert per open incident, re-arming on escalation.
- **The actor header** — who is allowed to transition an incident (D25).
- **The audit-log invariant** — append-only, and what enforces it.
- **The D20 refusal behaviour** — what the API returns when storage is unavailable.

Without a pinned spec, "what does acknowledging an incident mean?" has as many answers as there are authors, and the paper's reproducibility claim (D6) cannot survive that.

## 06b.2 The D20 refusal contract

> If Postgres is unavailable, the system refuses to acknowledge an incident rather than silently dropping it.

This is a hard rule (D20). It has exactly three layers, and each must hold:

1. **Storage raises.** Every public function in `storage/incidents.py` wraps its connection
   work in `except (OSError, asyncpg.PostgresError, ...)` and re-raises `StorageUnavailable`.
2. **The API refuses.** `require_pool` raises 503 when no pool is wired; every handler
   catches `StorageUnavailable` and returns 503. A transition never returns 200 on a
   storage failure — a fake 200 is worse than a 500 because the caller believes the
   transition happened.
3. **The caller escalates.** A `StorageUnavailable` means "nothing happened, escalate" —
   not "retry immediately", and **not** "fall back to the bus". The bus is a conduit, not a
   store; writing an incident to Redis because Postgres is down would silently create a
   second, divergent history.

The readiness probe encodes the same contract for operators: `/readyz` returns 200 only
when Redis ping *and* Postgres `SELECT 1` both succeed, and its 503 body names the failing
dependency so a Kubernetes rollout does not proceed on a half-live pod.

## 06b.3 The schema

Canonical DDL lives in `src/inframind/storage/schema.py` as a single `INIT_SQL` string.

### `incidents`

One row per incident — the authoritative lifecycle record.

| Column | Type | Why |
|---|---|---|
| `id` | `TEXT PRIMARY KEY` | ULID; sorts by creation time, which is what makes cursor pagination time-ordered. |
| `scenario_id` | `TEXT NULL` | P7 evaluation tagging. NULL in normal operation; a partial index keeps the evaluation query cheap without indexing the common case. |
| `revision` | `INTEGER NOT NULL` | A republish of the same incident bumps the revision. "Updating" is not a state. |
| `state` | `TEXT NOT NULL CHECK IN ('open','acknowledged','resolved','expired')` | The state machine, enforced DB-side as a backstop to the API. |
| `severity` | `TEXT NOT NULL CHECK IN ('info','warning','critical')` | Matches `Severity` in `common/signal.py`. |
| `fingerprint` | `TEXT NOT NULL` | Groups alerts that describe the same underlying fault. Indexed. |
| `title`, `opened_at`, `updated_at`, `closed_at` | | The human-readable summary and the lifecycle timestamps. |
| `services`, `signals`, `anomalies` | `JSONB NOT NULL` | The incident's context. JSONB rather than normalised tables: these are read as a unit, never joined. |
| `summary_text` | `TEXT NULL` | The LLM summary (D1). Written by the alerting pathway, not by `write_incident`. |
| `alerts` | `JSONB NOT NULL DEFAULT '[]'` | The alert ids fired for this incident. |

Indexes: `(state, opened_at DESC)` for the default listing, `(scenario_id, opened_at DESC)`
partial for P7, `(fingerprint)` for grouping.

### `candidates`

The ranked root-cause candidates, **full P5 shape**: `service`, `score`, `depth`,
`change_correlation`, `confidence`, and `rank`. `UNIQUE (incident_id, rank)` is what makes
the ranking total and the delete-then-reinsert resync idempotent.

> The narrower four-field `Candidate` in `docs/surface-map.md` predates P5 and would drop
> the three terms the ranker actually reports. Storage stores the real shape;
> `surface-map.md` is reconciled separately.

### `evidence`

One row per evidence item, child of a candidate (D15). `kind` is CHECK-constrained to
exactly the `EvidenceKind` literals (`metric`, `log_ref`, `trace`, `change`, `baseline`) —
a test asserts the DDL and the Python `Literal` agree, so the two cannot drift.

`evidence.ref` points at the source row (a signal id, a trace span id, a change resource).
It is a **reference, never the payload** — raw log lines and request bodies never land in
storage (D9).

### `alerts`

One row per alert sent, with the sink outcome. `sink_status` is CHECK-constrained to
`delivered`, `queued`, `failed`. At-least-once delivery: a `queued` row means "we
attempted and the target did not accept it", which is materially different from
"delivered" and must be visible in the record.

### `audit_log`

Append-only. Columns: `id`, `ts`, `actor`, `action`, `target`, `before`, `after` (JSONB).

**The append-only invariant has two halves:**

- *Application layer* — `storage/audit.py` exposes exactly one function, `record_audit`.
  There is no `update_audit`, no `delete_audit`, no `truncate_audit`. **The absence of an
  update method is the guarantee.** A test asserts the module's public surface stays at
  exactly that one name.
- *Database layer* — the application role would be granted `INSERT` and `SELECT` only on
  `audit_log`, with `UPDATE` and `DELETE` revoked. The testbed runs as the table owner so
  this is not enforced in the test environment; the grant belongs to the P8 deployment.

Note the deliberate absence of an `updated_at` column: there is nothing to update, and a
timestamp column would imply otherwise.

## 06b.4 The RCA → alerting hand-off

**In-process. No new Redis stream key.**

The RCA worker and the alerting router run in the same process, so the hand-off is a
Python call:

```python
await router.dispatch(incident, candidates, summary_text=summary.summary_text)
```

This is the simplest correct reading of D20 ("storage is the source of truth, the bus is
the conduit") and it avoids a D-change to the pinned stream keys in
`common/stream_keys.py` — renaming or adding one is a locked decision requiring supervisor
sign-off.

The cost is real and worth stating: the hand-off is **not durable**. If the worker crashes
between ranking and dispatching, the ranked candidates are lost. That is acceptable here
because the worker's output is recomputable from the graph snapshot and the anomalies
(standard 06 is deterministic for one input), whereas adding a stream key would make a
non-durable hand-off durable but at the cost of a D-change. Revisit if the LLM summary
becomes expensive enough that recomputing it is not worth it.

The DAG constraint backs this up: `alerting` may import `common`, `rca`, and `llm` — never
`storage`. The router therefore cannot persist its own outcomes; it returns them to the
caller, which writes them to `alerts` in the same transaction as the incident.

## 06b.5 The dedup contract

**One alert per open incident** within `dedup_window_s` (default 300 s — an SRE's
first-look SLA).

- `should_alert(incident_id, severity)` returns `True` the first time, then `False` for
  the rest of the window.
- **Severity escalation re-arms**: `info < warning < critical`. A `warning` incident that
  already alerted fires again when it escalates to `critical`, because the page should get
  harder. A demotion does not re-arm.
- The tracker is **process-local**, with an injectable clock. The durable mirror is the
  `alerts` table. A restart can re-fire an alert; it can never miss one.
- `forget(incident_id)` drops state when the incident is resolved.

## 06b.6 The actor header (D25)

`POST /incidents/{id}/ack` and `/resolve` require `X-Actor: <github_username>`.

The API validates it is a GitHub username and rejects:

| Input | Result | Why |
|---|---|---|
| missing header | 400 | An anonymous transition has no accountable author. |
| contains a space | 400 | A display name ("Rifat Bond"), not a handle. |
| contains `copilot`/`claude`/`gpt`/`cursor`/`codex`/`puku` | 400 | An AI tool is not a person (D25). |
| ends in `[bot]` | 400 | A bot is not a human. |
| longer than 39 chars | 400 | GitHub's username ceiling. |

The check is deliberately narrow — it is an audit-log integrity control, not a general
identity system. The value is written verbatim into `audit_log.actor`.

## 06b.7 The alert payload

`build_payload` is a pure function. Three rules:

- **`top_candidate` carries evidence ids, never raw payloads.** A reader clicks through to
  the `evidence` rows in Postgres.
- **`blast_radius` is the services downstream of the primary**, best-first, excluding the
  primary itself (its depth is 0 — it is the symptom).
- **`summary_text` is re-redacted before it goes out.** The redactor already ran before
  the prompt left the process, but the model's *output* is new text and must not be
  trusted verbatim (D9).

`links` carries `{"dashboard": null, "runbook": null}` — stable placeholders until P8
populates them.

## 06b.8 HTTP surface

| Route | Auth | Failure modes |
|---|---|---|
| `GET /incidents` | none | 503 if storage is down |
| `GET /incidents/{id}` | none | 404 if absent, 503 if storage is down |
| `POST /incidents/{id}/ack` | `X-Actor` | 400 bad actor, 404 absent, 503 storage down |
| `POST /incidents/{id}/resolve` | `X-Actor` | 400 bad actor, 404 absent, 503 storage down |

There is deliberately **no PATCH and no DELETE** on an incident. State transitions are
explicit verbs, each instrumented with an audit row. A generic mutation endpoint would let
a caller set `state` without a verb, without an `audit_action`, and therefore without an
audit row that says *why*.

## 06b.9 Configuration

| Variable | Default | Meaning |
|---|---|---|
| `POSTGRES_URL` | `postgresql://inframind:inframind@localhost:5432/inframind` | The DSN. Owned by `docker-compose.yml`. |
| `INFRAMIND_STORAGE_POOL_MIN_SIZE` | 2 | asyncpg min connections. |
| `INFRAMIND_STORAGE_POOL_MAX_SIZE` | 10 | asyncpg max connections. |
| `INFRAMIND_STORAGE_COMMAND_TIMEOUT_S` | 10.0 | Per-statement timeout. |
| `INFRAMIND_STORAGE_MAX_PAGE_SIZE` | 100 | `GET /incidents` ceiling. |
| `INFRAMIND_STORAGE_DEFAULT_PAGE_SIZE` | 50 | `GET /incidents` default. |
| `INFRAMIND_EVAL_SCENARIO_ID` | unset | P7 scenario tag; NULL in normal operation. |
| `INFRAMIND_ALERTING_DEDUP_WINDOW_S` | 300.0 | Dedup window. |
| `INFRAMIND_ALERTING_CONSOLE_ENABLED` | true | Emit to the JSON logger. |
| `INFRAMIND_ALERTING_WEBHOOK_URL` | `""` | Empty means the webhook sink is skipped. |
| `INFRAMIND_ALERTING_WEBHOOK_TIMEOUT_S` | 5.0 | Webhook HTTP timeout. |

**Table names are not configurable.** The schema is versioned in `schema.py`; renaming a
table is a D-change, not an env var.

## 06b.10 What this standard does not decide

- **Migrations.** The schema is one forward-only `INIT_SQL`. Promote to Alembic when the
  first *change* to a deployed table arrives — that is a D-change anyway.
- **Slack / Telegram / email sinks.** Console and webhook only. The sink interface
  (`send(payload) -> (alert_id, status)`) is the extension point.
- **TLS / IAM for Postgres.** The testbed runs `postgres:16-alpine` on a local network
  with no TLS. P8 decides the production posture.
- **Multi-process Prometheus.** `/metrics` is single-worker exposition. A multi-worker
  deployment needs `INFRAMIND_API_PROMETHEUS_MULTIPROC_DIR` and `prometheus_client`'s
  multi-process collector.

---

## Related

- `docs/decision-tree.md` — D20 (storage is the source of truth), D22 (change events are
  append-only), D25 (actor identity).
- `docs/standards/06-rca-scoring.md` — the ranked candidates this stage persists.
- `docs/standards/07-llm-explainer.md` — the summary this stage renders (D1: presentation only).
- `docs/surface-map.md` — the endpoint contract. Its `Candidate` shape predates P5 and is
  reconciled separately.
