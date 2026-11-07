"""Storage tunables and their `INFRAMIND_STORAGE_<PARAM>` overrides.

Defaults come from `docs/standards/06b-storage-and-alerting.md` (P6) and the D20 contract
in `docs/decision-tree.md`. Two things are deliberately **not** configurable:

* **Table names.** The schema is versioned in `schema.py` and changing a table name is a
  D-change, not an env var.
* **The DSN itself.** That arrives via `POSTGRES_URL`, which `docker-compose.yml` and
  `.env.example` already own. This module reads it but does not reshape it.

`INFRAMIND_EVAL_SCENARIO_ID` is the one bare (unprefixed) variable: the P7 evaluation
runner sets it per run so incidents can be correlated back to a scenario. It is NULL in
normal operation, and the `incidents.scenario_id` column is nullable for exactly that
reason.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from inframind.common.config import load_prefixed

#: Default DSN, matching `docker-compose.yml` and `.env.example`.
DEFAULT_POSTGRES_URL = "postgresql://inframind:inframind@localhost:5432/inframind"


@dataclass(frozen=True)
class StorageParams:
    """Postgres connection and pool tunables (P6)."""

    # Pool sizing. The testbed runs one API pod and one RCA worker; 2-10 is ample. A
    # larger pool costs Postgres connections, which are a scarce resource on the slim
    # kind profile (D5).
    pool_min_size: int = 2
    pool_max_size: int = 10

    # Every statement is bounded so a pathological query cannot pin a pool slot forever.
    command_timeout_s: float = 10.0

    # Pagination ceiling for `GET /incidents`. A client asking for more gets clamped, not
    # an error — the endpoint contract is a list, not a database dump.
    max_page_size: int = 100
    default_page_size: int = 50


def load_storage_params(env: Mapping[str, str] | None = None) -> StorageParams:
    """Build `StorageParams` from env (`os.environ` when `env` is None)."""
    return load_prefixed(StorageParams, "INFRAMIND_STORAGE", env)


def postgres_url(env: Mapping[str, str] | None = None) -> str:
    """Return the Postgres DSN.

    Reads `POSTGRES_URL` (the name `.env.example` and `docker-compose.yml` already use).
    Falls back to the documented default so a unit test does not need a real DSN.
    """
    source = os.environ if env is None else env
    return source.get("POSTGRES_URL", "").strip() or DEFAULT_POSTGRES_URL


def eval_scenario_id(env: Mapping[str, str] | None = None) -> str | None:
    """Return the P7 scenario id to stamp on new incidents, or None outside evaluation.

    Set by the evaluation runner per run (`INFRAMIND_EVAL_SCENARIO_ID=scn_frontend_cpu`).
    Normal operation leaves it unset, and every incident row gets `scenario_id = NULL`.
    """
    source = os.environ if env is None else env
    value = source.get("INFRAMIND_EVAL_SCENARIO_ID", "").strip()
    return value or None


def build_params(env: Mapping[str, str] | None = None) -> tuple[StorageParams, str, str | None]:
    """Convenience: return `(params, dsn, scenario_id)` resolved from one env mapping."""
    return load_storage_params(env), postgres_url(env), eval_scenario_id(env)


__all__ = [
    "DEFAULT_POSTGRES_URL",
    "StorageParams",
    "build_params",
    "eval_scenario_id",
    "load_storage_params",
    "postgres_url",
]
