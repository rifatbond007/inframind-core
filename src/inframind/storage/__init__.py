"""Storage: the durable incident history and audit log (D20).

Postgres is the **source of truth**. The Redis bus is the conduit that delivers work to
this layer — it is never a store, and nothing here reads from it as if it were one.

Public surface:

* `StorageParams`, `load_storage_params`, `postgres_url`, `eval_scenario_id` — config.
* `create_pool`, `init_schema`, `close_pool`, `StorageUnavailable` — connection lifecycle.
* `write_incident`, `transition_incident`, `get_incident`, `list_incidents` — incidents.
* `record_audit` — append-only audit log (no update, no delete).

The `llm` module may not import this one (DAG, standards 01 §1.3); `alerting` may not
either. Only `api` reaches storage directly, and it does so through these functions.
"""

from __future__ import annotations

from .audit import record_audit
from .config import (
    DEFAULT_POSTGRES_URL,
    StorageParams,
    build_params,
    eval_scenario_id,
    load_storage_params,
    postgres_url,
)
from .db import StorageUnavailable, close_pool, create_pool, init_schema
from .incidents import (
    IncidentDetail,
    IncidentListItem,
    get_incident,
    list_incidents,
    transition_incident,
    write_incident,
)

__all__ = [
    "DEFAULT_POSTGRES_URL",
    "IncidentDetail",
    "IncidentListItem",
    "StorageParams",
    "StorageUnavailable",
    "build_params",
    "close_pool",
    "create_pool",
    "eval_scenario_id",
    "get_incident",
    "init_schema",
    "list_incidents",
    "load_storage_params",
    "postgres_url",
    "record_audit",
    "transition_incident",
    "write_incident",
]
