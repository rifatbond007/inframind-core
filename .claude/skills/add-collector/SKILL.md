---
name: add-collector
description: How to add a new signal collector (Prometheus, Loki, Jaeger/OTel, Alertmanager) to InfraMind's ingestion layer.
---

# Add a collector

Use this skill when adding a new backend collector under `src/inframind/ingestion/`.

## Pre-flight

1. Read `docs/PROGRESS.md` (Phase 3).
2. Read `src/inframind/common/README.md` — the `Signal` contract.
3. Read `src/inframind/ingestion/README.md`.
4. Read an existing collector (e.g. `prometheus.py`) as a template.

## Steps

1. Create `src/inframind/ingestion/<backend>.py`.
2. Implement a class with:
   - `async def run(self) -> None` — main loop.
   - `def _to_signal(self, raw: <backend_type>) -> Signal` — mapping logic.
   - `def _publish(self, signal: Signal) -> None` — Redis Streams publish (idempotent).
3. Use the project's logger (`inframind.common.logging.get_logger(__name__)`).
4. Honour `INFRAMIND_*` env vars for endpoint, interval, and retry.
6. **Map every backend payload to `Signal` before crossing a module boundary.** No raw backend objects downstream.

## Tests

- `tests/unit/ingestion/test_<backend>_mapping.py` — fixture payload → `Signal`.
- `tests/integration/test_<backend>_e2e.py` — run against the live testbed.

## Smoke check

- Start the collector against the testbed.
- Confirm `Signal` objects land on the Redis stream (`redis-cli XLEN inframind.signals`).
- Feed a downstream consumer and confirm it receives signals.

## Commit

- `feat(ingestion): add <backend> collector`
- Update `docs/PROGRESS.md` with the new row.
- Update `docs/ARCHITECTURE.md` ingestion diagram if the diagram needs a new box.