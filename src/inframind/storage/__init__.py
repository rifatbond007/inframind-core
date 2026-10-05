"""Storage: durable incident history and audit log.

Planned (Step 8):

* PostgreSQL schema for incidents, signals, evidence, RCA results, alerts.
* Async SQLAlchemy / asyncpg access.
* Audit log table — every state change is recorded.
* Time-series indexes for retrospective evaluation.
"""
