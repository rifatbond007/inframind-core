"""Storage config + connection-lifecycle tests (no Postgres required).

The async tests use `storage.db.create_pool` against an unreachable host so we can
prove the D20 "refuse, not silently drop" contract without spinning up a real cluster.
"""

from __future__ import annotations

import pytest

from inframind.storage.config import (
    DEFAULT_POSTGRES_URL,
    StorageParams,
    build_params,
    eval_scenario_id,
    load_storage_params,
    postgres_url,
)
from inframind.storage.db import StorageUnavailable, create_pool


def test_default_postgres_url_is_documented() -> None:
    assert DEFAULT_POSTGRES_URL.startswith("postgresql://inframind:")
    assert "@localhost:5432/inframind" in DEFAULT_POSTGRES_URL


def test_load_storage_params_returns_defaults_when_env_empty() -> None:
    params = load_storage_params(env={})
    assert params == StorageParams()


def test_load_storage_params_honours_overrides() -> None:
    env = {
        "INFRAMIND_STORAGE_POOL_MIN_SIZE": "4",
        "INFRAMIND_STORAGE_POOL_MAX_SIZE": "20",
        "INFRAMIND_STORAGE_COMMAND_TIMEOUT_S": "3.5",
        "INFRAMIND_STORAGE_MAX_PAGE_SIZE": "200",
        "INFRAMIND_STORAGE_DEFAULT_PAGE_SIZE": "25",
    }
    params = load_storage_params(env=env)
    assert params.pool_min_size == 4
    assert params.pool_max_size == 20
    assert params.command_timeout_s == 3.5
    assert params.max_page_size == 200
    assert params.default_page_size == 25


def test_postgres_url_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    assert postgres_url() == DEFAULT_POSTGRES_URL


def test_eval_scenario_id_returns_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INFRAMIND_EVAL_SCENARIO_ID", raising=False)
    assert eval_scenario_id() is None


def test_eval_scenario_id_returns_value_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INFRAMIND_EVAL_SCENARIO_ID", "scn_frontend_cpu")
    assert eval_scenario_id() == "scn_frontend_cpu"


def test_build_params_returns_three_aligned_values() -> None:
    env = {
        "INFRAMIND_STORAGE_POOL_MIN_SIZE": "5",
        "POSTGRES_URL": "postgresql://x:y@db:5432/z",
        "INFRAMIND_EVAL_SCENARIO_ID": "scn_a",
    }
    params, dsn, scenario = build_params(env=env)
    assert params.pool_min_size == 5
    assert dsn == "postgresql://x:y@db:5432/z"
    assert scenario == "scn_a"


async def test_create_pool_raises_storage_unavailable_on_unreachable_host() -> None:
    """An unreachable host must surface as `StorageUnavailable`, not an opaque asyncpg error."""
    params = StorageParams(pool_min_size=1, pool_max_size=1, command_timeout_s=0.5)
    dsn = "postgresql://nope:nope@127.0.0.1:1/nope"  # port 1 is reserved
    with pytest.raises(StorageUnavailable) as exc:
        await create_pool(params, dsn)
    assert "could not connect" in str(exc.value).lower()
