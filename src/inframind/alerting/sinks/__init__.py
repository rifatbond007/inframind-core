"""Alert delivery sinks.

Each sink exposes `send(payload) -> (alert_id, status)` where status is one of
`delivered`, `queued`, or `failed`. `console` is synchronous (the router wraps it in
`asyncio.to_thread`); `webhook` is async.

Submodules are imported eagerly here so `sinks.console` / `sinks.webhook` resolve at
runtime and so mypy can see the attributes.
"""

from __future__ import annotations

from . import console, webhook

__all__ = ["console", "webhook"]
