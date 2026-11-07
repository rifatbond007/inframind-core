"""The Postgres schema, as one forward-only DDL statement (D20, P6).

The whole schema lives here rather than in a `migrations/` directory for this iteration.
`migrations/` is the destination once the first *change* to a shipped table arrives — a
schema edit to a deployed table is a D-change and needs supervisor sign-off, which is
exactly the moment a migration tool starts earning its dependency. Until then
`CREATE TABLE IF NOT EXISTS` is honest about being idempotent bootstrap, not migration.

Hard rules encoded in the DDL itself:

* **D20 — storage is the source of truth.** Every table here is authoritative. Redis
  Streams is the conduit that carries incidents to this layer, never a store.
* **D22 — `change_events` is append-only.** The RCA stage writes those through a
  separate table pinned in `docs/standards/14-change-correlation.md`; it is created by
  the RCA worker, not here, because storage may not import `rca`'s watcher (DAG).
* **The audit log has no UPDATE or DELETE path.** `storage/audit.py` exposes only
  `record_audit`. The absence of an update method *is* the append-only guarantee at the
  application layer; a production Postgres role would additionally be granted
  INSERT/SELECT only (documented in `06b-storage-and-alerting.md`, not enforced here
  because the testbed runs as the table owner).

The `Candidate` shape stored in `candidates` is the **full** P5 shape from
`rca/evidence.py` — `depth`, `change_correlation`, and `confidence` included. The
narrower four-field shape in `docs/surface-map.md` predates P5 and would have dropped
the three terms the ranker actually reports; the surface map is reconciled separately.
"""

from __future__ import annotations

#: Every statement needed to bring an empty database to the P6 shape. Idempotent.
INIT_SQL = """
-- incidents: one row per incident, the authoritative lifecycle record (D20).
CREATE TABLE IF NOT EXISTS incidents (
    id            TEXT PRIMARY KEY,
    scenario_id   TEXT NULL,
    revision      INTEGER NOT NULL DEFAULT 1,
    state         TEXT NOT NULL
                  CHECK (state IN ('open', 'acknowledged', 'resolved', 'expired')),
    severity      TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    fingerprint   TEXT NOT NULL,
    title         TEXT NOT NULL,
    opened_at     TIMESTAMPTZ NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL,
    closed_at     TIMESTAMPTZ NULL,
    services      JSONB NOT NULL DEFAULT '[]'::jsonb,
    signals       JSONB NOT NULL DEFAULT '[]'::jsonb,
    anomalies     JSONB NOT NULL DEFAULT '[]'::jsonb,
    summary_text  TEXT NULL,
    alerts        JSONB NOT NULL DEFAULT '[]'::jsonb
);

-- The P7 evaluation runner filters by scenario; the partial index keeps that cheap
-- while leaving the common (scenario_id IS NULL) case unindexed.
CREATE INDEX IF NOT EXISTS incidents_state_opened_at
    ON incidents (state, opened_at DESC);
CREATE INDEX IF NOT EXISTS incidents_scenario_opened_at
    ON incidents (scenario_id, opened_at DESC) WHERE scenario_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS incidents_fingerprint ON incidents (fingerprint);

-- candidates: the ranked root-cause candidates, full P5 shape.
CREATE TABLE IF NOT EXISTS candidates (
    id                  TEXT PRIMARY KEY,
    incident_id         TEXT NOT NULL REFERENCES incidents (id) ON DELETE CASCADE,
    service             TEXT NOT NULL,
    score               DOUBLE PRECISION NOT NULL,
    depth               INTEGER NOT NULL,
    change_correlation  DOUBLE PRECISION NOT NULL,
    confidence          DOUBLE PRECISION NOT NULL,
    rank                INTEGER NOT NULL,
    UNIQUE (incident_id, rank)
);

CREATE INDEX IF NOT EXISTS candidates_incident_rank
    ON candidates (incident_id, rank);
CREATE INDEX IF NOT EXISTS candidates_service_score
    ON candidates (service, score DESC);

-- evidence: one row per evidence item, child of a candidate (D15).
CREATE TABLE IF NOT EXISTS evidence (
    id            TEXT PRIMARY KEY,
    candidate_id  TEXT NOT NULL REFERENCES candidates (id) ON DELETE CASCADE,
    kind          TEXT NOT NULL
                  CHECK (kind IN ('metric', 'log_ref', 'trace', 'change', 'baseline')),
    summary       TEXT NOT NULL,
    ref           TEXT NOT NULL,
    weight        DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS evidence_candidate ON evidence (candidate_id);

-- alerts: one row per alert sent, with the sink outcome (at-least-once delivery).
CREATE TABLE IF NOT EXISTS alerts (
    id           TEXT PRIMARY KEY,
    incident_id  TEXT NOT NULL REFERENCES incidents (id) ON DELETE CASCADE,
    severity     TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    channel      TEXT NOT NULL
                 CHECK (channel IN ('console', 'webhook', 'slack', 'email')),
    payload      JSONB NOT NULL,
    sent_at      TIMESTAMPTZ NOT NULL,
    sink_status  TEXT NOT NULL
                 CHECK (sink_status IN ('delivered', 'queued', 'failed'))
);

CREATE INDEX IF NOT EXISTS alerts_incident_sent_at
    ON alerts (incident_id, sent_at DESC);

-- audit_log: append-only. Every state change, ranking decision, and LLM call lands
-- here. There is deliberately no UPDATE or DELETE path in storage/audit.py.
CREATE TABLE IF NOT EXISTS audit_log (
    id       TEXT PRIMARY KEY,
    ts       TIMESTAMPTZ NOT NULL,
    actor    TEXT NOT NULL,
    action   TEXT NOT NULL,
    target   TEXT NOT NULL,
    before   JSONB NULL,
    after    JSONB NULL
);

CREATE INDEX IF NOT EXISTS audit_log_target_ts ON audit_log (target, ts DESC);
CREATE INDEX IF NOT EXISTS audit_log_actor_ts ON audit_log (actor, ts DESC);
"""


__all__ = ["INIT_SQL"]
