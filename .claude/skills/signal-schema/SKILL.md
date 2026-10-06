---
name: signal-schema
description: The unified Signal Pydantic model. Source of truth for ingestion and downstream stages.
---

# Signal schema (skill entrypoint)

**Canonical rules:** [`docs/standards/03-signal-schema.md`](../../../docs/standards/03-signal-schema.md)

Before implementing or changing collectors, detectors, or bus payloads:

1. Read the standards file and [`docs/surface-map.md`](../../../docs/surface-map.md) for wire shapes.
2. Add or update unit tests when the schema changes.

Implementation lives in `src/inframind/common/` (Phase P2).
