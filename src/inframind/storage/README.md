# `inframind.storage`

Durable incident history and audit log. **Postgres is the source of truth** (D20); the
Redis bus is the conduit that delivers work here, never a store.

## What it does

- **Persists incidents** with their ranked root-cause candidates and evidence bundles, in
  one transaction per incident.
- **Serves them back over HTTP** via the `/incidents` routes in `inframind.api`.
- **Records every state transition** in an append-only audit log.

## The D20 contract

> If Postgres is unavailable, the system refuses to acknowledge an incident rather than
> silently dropping it.

Every public function raises `StorageUnavailable` on a connection failure or a
post-connection Postgres error. The API translates that into **503** — never a fake 200.
A caller that receives `StorageUnavailable` must treat it as "nothing happened, escalate",
not as a retry trigger and not as a signal to fall back to the bus.

## Files

| File | What it holds |
|---|---|
| `config.py` | `StorageParams` (pool sizing, statement timeout, page-size ceiling) and the `POSTGRES_URL` / `INFRAMIND_EVAL_SCENARIO_ID` resolvers. |
| `schema.py` | `INIT_SQL` — the canonical forward-only DDL. Every statement is `CREATE ... IF NOT EXISTS`, so `init_schema` is idempotent and safe on every process start. |
| `db.py` | The asyncpg pool factory, `StorageUnavailable`, and `init_schema`. |
| `incidents.py` | `write_incident`, `transition_incident`, `get_incident`, `list_incidents`, `record_audit`. |
| `audit.py` | Re-export of `record_audit` so the "audit module" is where a reader expects it. |

## Transactions

`write_incident` writes the incident, its candidates, and its evidence inside a single
`conn.transaction()`. asyncpg rolls back on any exception, so an incident is either fully
present or fully absent — never half-written. Candidates and evidence are deleted then
re-inserted rather than upserted: the ranker is deterministic for one `(graph, anomalies)`
input, so resync is idempotent and the code stays simple.

`transition_incident` writes the state change and its audit row in one transaction, so an
incident can never change state without leaving an audit trail.

## The append-only audit log

`storage/audit.py` exposes exactly one function: `record_audit`. There is intentionally no
`update_audit`, no `delete_audit`, no `truncate_audit`. **The absence of an update method
is the append-only guarantee at the application layer.**

The DDL grants no UPDATE or DELETE path either. A production deployment would additionally
revoke the corresponding SQL privileges on `audit_log` from the application role; that
policy is documented in `docs/standards/06b-storage-and-alerting.md`. It is not enforced
here because the testbed runs as the table owner.

## Schema

Five tables: `incidents`, `candidates`, `evidence`, `alerts`, `audit_log`. See `schema.py`
for the DDL verbatim, and `docs/standards/06b-storage-and-alerting.md` for the rationale
behind each column and index.

Two shape decisions worth calling out:

- **The full P5 `Candidate` is stored** — `depth`, `change_correlation`, and `confidence`
  included. The narrower four-field shape in `docs/surface-map.md` predates P5 and would
  have dropped the three terms the ranker actually reports.
- **`scenario_id` is nullable.** P7 sets `INFRAMIND_EVAL_SCENARIO_ID` per run so incidents
  can be correlated back to a scenario; normal operation leaves it NULL.

## Pagination

`list_incidents` paginates by an opaque cursor — the incident id of the last row in the
previous page. Incident ids are ULIDs, which sort by creation time, so `id > cursor` is
also "after in time" modulo clock skew. `limit` is clamped to `[1, max_page_size]`; an
oversized request is clamped, not rejected.

## Migrations

The whole schema lives in `schema.py` rather than a `migrations/` directory for this
iteration. `CREATE TABLE IF NOT EXISTS` is honest about being idempotent bootstrap, not
migration. Promote to Alembic when the first schema **change** to a deployed table arrives
— that is a D-change anyway, which is exactly when a migration tool starts earning its
dependency.

## Tests

- `tests/unit/storage/` — SQL shape and transactionality against a recording fake pool.
- `tests/integration/test_storage_postgres.py` — the same round-trip against a live
  Postgres. Skipped unless Postgres is reachable at `POSTGRES_URL`.

## Owner

Rifat on `dev-rifat` (P6 ownership taken on this branch by user direction); Prome on paper.
