---
name: add-collector
description: How to add a new signal collector (Prometheus, Loki, Jaeger, Alertmanager) to InfraMind ingestion.
---

# Add a collector (skill entrypoint)

**Canonical procedure:** [`docs/standards/04-add-collector.md`](../../../docs/standards/04-add-collector.md)

Before adding a collector:

1. Read [`docs/PROGRESS.md`](../../../docs/PROGRESS.md) — ingestion is Phase P3.
2. Follow the standards file step-by-step (`run()`, `_to_signal()`, `_publish()`, smoke check).
3. Log the session in `docs/PROGRESS.md` when done.

Owner: ingestion-engineer agent (`Moneem`).
