"""Collector state that survives a restart: a poll cursor and a set of already-published keys.

Pull collectors re-read an overlap behind their cursor to absorb backend delays, so they need to
remember what they already published. `CollectorState` keeps that in Redis (a restarted process
resumes where the last one stopped); `MemoryState` is the process-local fallback used by tests
and by collectors built without Redis state. Redis is a conduit (D20): every key expires.
"""

from __future__ import annotations

from typing import Protocol

from redis import exceptions as redis_exc
from redis.asyncio import Redis

from .exceptions import BusError


class State(Protocol):
    """What a collector needs to remember between polls."""

    async def load_cursor(self) -> int | None:
        """Return the saved cursor, or None when there is none."""

    async def save_cursor(self, value: int) -> None:
        """Persist the cursor."""

    async def is_seen(self, key: str) -> bool:
        """Return whether `key` was already marked."""

    async def mark_seen(self, key: str) -> bool:
        """Mark `key`; return True only if it was not marked before."""


class CollectorState:
    """Redis-backed state, namespaced by collector `name`. Failures raise `BusError`."""

    def __init__(self, client: Redis, name: str, *, ttl_s: int = 3600) -> None:
        self._client = client
        self._name = name
        self._ttl_s = ttl_s

    def _seen_key(self, key: str) -> str:
        return f"seen:{self._name}:{key}"

    async def load_cursor(self) -> int | None:
        """Return the saved cursor, or None when there is none or it is unreadable."""
        try:
            raw = await self._client.get(f"cursor:{self._name}")
        except redis_exc.RedisError as e:
            raise BusError(f"cannot load cursor for {self._name}: {e}") from e
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    async def save_cursor(self, value: int) -> None:
        """Persist the cursor for `ttl_s` seconds."""
        try:
            await self._client.set(f"cursor:{self._name}", value, ex=self._ttl_s)
        except redis_exc.RedisError as e:
            raise BusError(f"cannot save cursor for {self._name}: {e}") from e

    async def is_seen(self, key: str) -> bool:
        """Return whether `key` was already marked."""
        try:
            return bool(await self._client.exists(self._seen_key(key)))
        except redis_exc.RedisError as e:
            raise BusError(f"cannot read seen key for {self._name}: {e}") from e

    async def mark_seen(self, key: str) -> bool:
        """Atomically mark `key` (SET NX EX); True only for the first caller."""
        try:
            return bool(await self._client.set(self._seen_key(key), 1, nx=True, ex=self._ttl_s))
        except redis_exc.RedisError as e:
            raise BusError(f"cannot mark seen key for {self._name}: {e}") from e


class MemoryState:
    """Process-local state with a bound on remembered keys (oldest are forgotten first)."""

    def __init__(self, *, max_entries: int = 50_000) -> None:
        self._max_entries = max_entries
        self._cursor: int | None = None
        self._seen: dict[str, None] = {}

    async def load_cursor(self) -> int | None:
        """Return the saved cursor, or None when there is none."""
        return self._cursor

    async def save_cursor(self, value: int) -> None:
        """Keep the cursor in memory."""
        self._cursor = value

    async def is_seen(self, key: str) -> bool:
        """Return whether `key` was already marked."""
        return key in self._seen

    async def mark_seen(self, key: str) -> bool:
        """Mark `key`; True only if it was not marked before."""
        if key in self._seen:
            return False
        self._seen[key] = None
        while len(self._seen) > self._max_entries:
            del self._seen[next(iter(self._seen))]
        return True
