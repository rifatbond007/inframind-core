"""Shared cross-cutting code (imports nothing from other InfraMind packages).

* ``signal`` - the ``Signal`` Pydantic model every collector publishes.
* ``bus`` - Redis Streams publish and the idempotent consumer-group reader.
* ``stream_keys``, ``ids``, ``time``, ``logging``, ``exceptions`` - pinned keys and helpers.

``config`` (pydantic-settings) lands with the first service that needs it.
"""
