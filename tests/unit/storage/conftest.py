"""Shared fakes for the storage unit tests.

`FakePool` mimics the slice of the asyncpg surface that `storage/incidents.py` uses:
`pool.acquire()` (an async context manager) yielding a connection with `execute`,
`fetchrow`, `fetch`, `fetchval`, and `transaction()` (an async context manager). Every
call is recorded so tests can assert the SQL shape and the parameter tuples.

`fail_on` makes the pool raise a chosen exception on the Nth `execute`, which is how the
transactionality tests prove that a mid-write failure surfaces as `StorageUnavailable`.
"""

from __future__ import annotations

from typing import Any

import asyncpg
import pytest


class FakeConnection:
    """Records every statement it is asked to run."""

    def __init__(self, pool: FakePool) -> None:
        self._pool = pool

    async def execute(self, sql: str, *args: Any) -> str:
        self._pool.calls.append(("execute", sql, args))
        self._pool.execute_count += 1
        if self._pool.fail_on_execute == self._pool.execute_count:
            raise self._pool.failure
        return self._pool.execute_result

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self._pool.calls.append(("fetchrow", sql, args))
        return self._pool.fetchrow_result

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self._pool.calls.append(("fetch", sql, args))
        # Successive `fetch` calls drain a queue so a two-query read (candidates then
        # evidence) can return different row shapes.
        if self._pool.fetch_results:
            return self._pool.fetch_results.pop(0)
        return []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self._pool.calls.append(("fetchval", sql, args))
        return self._pool.fetchval_result

    def transaction(self) -> FakeTransaction:
        self._pool.transactions += 1
        return FakeTransaction(self._pool)


class FakeTransaction:
    def __init__(self, pool: FakePool) -> None:
        self._pool = pool

    async def __aenter__(self) -> FakeTransaction:
        self._pool.transaction_entries += 1
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is None:
            self._pool.transaction_commits += 1
        else:
            self._pool.transaction_rollbacks += 1
        return False  # never swallow


class FakeAcquire:
    def __init__(self, pool: FakePool) -> None:
        self._pool = pool

    async def __aenter__(self) -> FakeConnection:
        if self._pool.acquire_error is not None:
            raise self._pool.acquire_error
        return FakeConnection(self._pool)

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False


class FakePool:
    """A recording stand-in for `asyncpg.Pool`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []
        self.transactions = 0
        self.transaction_entries = 0
        self.transaction_commits = 0
        self.transaction_rollbacks = 0
        self.execute_count = 0
        self.execute_result = "UPDATE 1"
        self.fetchrow_result: Any = None
        self.fetch_results: list[list[Any]] = []
        self.fetchval_result: Any = None
        self.fail_on_execute: int | None = None
        self.failure: Exception = OSError("connection reset")
        self.acquire_error: Exception | None = None

    @property
    def fetch_result(self) -> list[Any]:
        """Backwards-compat: returning the first queued result makes single-fetch tests easy."""
        return self.fetch_results[0] if self.fetch_results else []

    @fetch_result.setter
    def fetch_result(self, value: list[Any]) -> None:
        # Replace the entire queue so the legacy `pool.fetch_result = [...]` style keeps working.
        self.fetch_results = [value]

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self)

    async def close(self) -> None:
        return None

    # --- assertion helpers ---

    def sql_of(self, kind: str) -> list[str]:
        return [sql for k, sql, _ in self.calls if k == kind]

    def args_of(self, kind: str) -> list[tuple[Any, ...]]:
        return [args for k, _, args in self.calls if k == kind]


@pytest.fixture
def pool() -> FakePool:
    return FakePool()


@pytest.fixture
def postgres_error() -> asyncpg.PostgresError:
    """A real asyncpg error class so the `except` clauses in storage are exercised."""
    return asyncpg.PostgresError("connection refused")
