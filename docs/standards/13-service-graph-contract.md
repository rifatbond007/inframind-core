# 13. Service graph contract — the canonical primitive for RCA and correlation
> standards · Architecture · InfraMind. Pairs with the `rca-scoring` skill in `.claude/skills/rca-scoring/SKILL.md` (uses the graph) and the `add-collector` skill (builds the graph from OTel spans). This file is the rationale and the wire shape. D21.

## 13.1 What the service graph is

A `ServiceGraph` is a directed graph of the microservices in the system-under-test. It is the single source of truth for "which service calls which service" and is consumed by the RCA walker, the PageRank baseline, the correlation severity classifier, and the LLM evidence bundler. The same graph is used at training time (dev split) and at report time (test split), so the per-scenario reproducibility contract (D6) holds.

Without a pinned primitive, every consumer invents its own — and a deviation is a silent scoring bug. This standard fixes the shape, the storage, and the update protocol so no consumer has to re-decide it.

## 13.2 Shape

```python
# Interface only — implementation lives in src/inframind/rca/graph.py (P5).

class ServiceGraph:
    def __init__(self, seed_path: Path | None) -> None: ...
    def apply_update(self, update: GraphUpdate) -> None: ...
    def successors(self, service: str) -> list[str]: ...
    def predecessors(self, service: str) -> list[str]: ...
    def edge_weight(self, caller: str, callee: str) -> float: ...
    def snapshot(self) -> dict: ...
    def persist(self) -> None: ...

@dataclass
class GraphUpdate:
    caller: str           # canonical service name (lower-kebab)
    callee: str           # canonical service name (lower-kebab)
    latency_ms: float     # observed span duration
    observed_at: datetime
```

- **Nodes:** canonical service names, normalised via `re.sub(r"[^a-z0-9-]", "-", name.lower())` — the same rule the `Signal.service` field uses (section 3.4). Two collectors observing the same microservice MUST agree on the spelling.
- **Edges:** `caller -> callee`. The direction matches D2. An edge `(a, b)` means "service `a` called service `b` in the observation window."
- **Edge weight:** `p95_latency_ms * log(call_count)` over the last 100 samples, exponentially weighted moving average with α = 0.1. Computed on update, not on read.
- **Per-node metadata:** `confidence` in `[0, 1]`. An edge observed fewer than `GRAPH_MIN_OBSERVATIONS` (default 3) times contributes zero confidence; the ranker downweights low-confidence edges by a configurable multiplier (default 0.1).
- **Per-graph metadata:** `updated_at` (UTC), `version` (monotonic int, bumped on every apply).

## 13.3 Storage — pinned

- **Authoritative live copy:** in-memory in the **RCA worker** (`src/inframind/rca/graph.py`, P5). Single writer, single reader. The RCA stage is the only consumer on the request path.
- **Cold-start seed:** `data/service-graph.json` on disk. The path is pinned by `RCA_GRAPH_PATH` (already named in `docs/surface-map.md` environment keys). Default `data/service-graph.json`. The file is gitignored when generated, committed when used as a fixture.
- **Live updates:** the **OTel / Jaeger collector** (`src/inframind/ingestion/otel.py` and `jaeger.py`) emits a `GraphUpdate` event on the pinned Redis stream `stream:graph-updates`. The RCA worker consumes the stream, applies updates, and acks. **No synchronous write back to disk** on every update — disk snapshot is on a separate timer (section 13.6).
- **Replication:** none. There is exactly one RCA worker. If the project ever scales to multiple RCA replicas, the contract changes — that is a D-change.

### Why the RCA worker owns the live copy

1. **Hot-path latency.** The RCA walk + PageRank variant run on every incident. A graph lookup must be O(1) node/edge access. Round-tripping to Redis on the hot path adds 1–5 ms per request; failure to find a node in Redis blocks the ranker.
2. **Reproducibility (D6).** A frozen `(graph, anomaly_set)` produces a frozen ranking. The graph in the RCA worker's memory is the one used in the evaluation. Given the same `scenario_id + seed`, the OTel collector emits the same `GraphUpdate` sequence, the RCA worker rebuilds the same graph, and the ranker produces the same output.
3. **Write contention.** Ingestion is the only writer (via `stream:graph-updates`); RCA is the only reader. If both shared a Redis hash, every update would acquire a lock. The single-writer / single-reader in-memory copy sidesteps this.

### Why a separate stream key

The existing `stream:signals` carries `Signal` objects. A `GraphUpdate` is structurally different (no `attrs`, no `severity`, no `trace_id`) and has a different consumer lifecycle (one consumer — the RCA worker — vs. many for signals). Reusing `stream:signals` would couple the OTel collector's emission rate to the detection cadence. `stream:graph-updates` is a new pinned key — D21.

## 13.4 Update protocol

```
[OTel collector]                          [graph stream]                   [RCA worker]
   |                                            |                                |
   | observes span (parent=X, child=Y)          |                                |
   |-------------------------------------------->| GraphUpdate {x->y, weight}    |
   |                                            |------------------------------->|
   |                                            |                                | EWMA-decay old edge,
   |                                            |                                | apply new sample,
   |                                            |                                | bump version.
```

**Rules:**

- The OTel collector emits one `GraphUpdate` per observed parent-child span pair, batched at flush time. The flush interval is the same as the detection cycle (default 30 s, per the ingestion engineer's defaults).
- The RCA worker consumes the stream, updates its in-memory graph, and acks. No synchronous disk write on every update.
- **Edge weight is an exponentially weighted moving average** with α = 0.1 (configurable via `GRAPH_EWMA_ALPHA`). The moving average is over the last 100 samples.
- **Edges age out:** every 5 minutes (configurable via `GRAPH_DECAY_HALF_LIFE_S`), every edge loses 0.5% weight. An edge that has not been observed for `2 * GRAPH_DECAY_HALF_LIFE_S` is removed. A removed edge is re-added only after `GRAPH_MIN_OBSERVATIONS` fresh samples land.
- **No edge is added on a single observation.** An edge must be observed at least `GRAPH_MIN_OBSERVATIONS` (default 3) times before it is added to the graph. Before that, the samples are counted toward the threshold but the ranker sees no edge.

## 13.5 Cold start

When the RCA worker boots with no `data/service-graph.json`:

1. Log `WARN graph_cold_start_no_seed` with `path=...`.
2. Build a **stub graph** from the `Signal.service` values observed on `stream:signals` over the last 60 s. Each `(a, b)` co-occurrence in the same 5-minute window becomes a zero-confidence edge candidate.
3. Mark every edge as `confidence = 0`. The ranker downweights low-confidence edges by the multiplier named in section 13.2. Cold-start rankings are valid but not authoritative.
4. As real `GraphUpdate` events arrive, the graph becomes authoritative within roughly `5 * 60 s = 5 minutes` of warm traffic.

**Implication:** the first fault after a fresh `make up` produces a low-confidence ranking. That is acceptable for the capstone; the paper notes it as a "warm-up caveat" in section 5 (Experimental Setup). The caveat belongs in `evaluation/results/<warmup_meta>.csv`.

## 13.6 Disk snapshot

- **Trigger:** every 60 s (configurable via `GRAPH_SNAPSHOT_INTERVAL_S`) OR on `SIGTERM` (graceful shutdown — Helm sends SIGTERM on `helm uninstall` and on pod-termination grace).
- **Format:** `networkx.readwrite.json_graph.adjacency_data`. JSON-serializable; diff-friendly if the team ever wants to commit a known fixture.
- **Path:** `RCA_GRAPH_PATH`. Default `data/service-graph.json`. Parent directory is created if absent.
- **What is NOT snapshotted:** `updated_at` and `version` (recomputed on load).
- **Restore on boot:** the file is loaded if it exists. If the file is missing or unparseable, the worker logs `ERROR graph_snapshot_unreadable` and proceeds with the cold-start path. **The worker never crashes on a bad snapshot** — that would amplify an InfraMind-internal fault into a pod-restart loop.

## 13.7 Failure modes

| Failure | Behaviour |
|---|---|
| OTel collector crashes | No new `GraphUpdate` events. Existing edges age out per the decay rule (section 13.4). Rankings continue on stale data. |
| RCA worker crashes mid-update | The last on-disk snapshot is the truth. On restart, the worker loads from disk. In-flight updates not yet snapshotted are lost (acceptable per section 13.5 — cold start rebuilds them). |
| `data/service-graph.json` is corrupt | Log `ERROR graph_snapshot_unreadable`. Cold start from `stream:signals`. |
| `stream:graph-updates` is full | Redis `MAXLEN approx 100_000` (same as `stream:signals`). Old events are aged out; old edges are aged out anyway. |
| Two RCA workers accidentally run (replica count misconfig) | Both load the same on-disk snapshot and apply updates independently. Last writer wins on the next snapshot. This is a configuration error, not a correctness issue — Helm `replicas: 1` for the RCA `Deployment`. |

## 13.8 Reproducibility contract (D6)

For the same `scenario_id + seed`:

- The OTel collector's `GraphUpdate` emission is deterministic from the seed.
- The RCA worker rebuilds the same in-memory graph from those updates.
- The ranker produces the same output.

The on-disk snapshot is a runtime artefact, not part of the reproducibility contract. The paper's `evaluation/results/main.csv` records the graph version at rank time, not the snapshot path.

## 13.9 Tests

```
tests/unit/rca/test_graph_apply.py
    # apply_update correctly decays / adds edges
    # an edge below GRAPH_MIN_OBSERVATIONS is not added
    # weight is an EWMA over the last 100 samples

tests/unit/rca/test_graph_coldstart.py
    # no seed file -> stub from stream:signals
    # stub edges have confidence = 0
    # ranker downweights confidence-0 edges

tests/unit/rca/test_graph_snapshot.py
    # snapshot -> reload round-trip preserves edges and weights
    # corrupt snapshot -> ERROR log + cold start, no crash

tests/integration/test_graph_live.py
    # OTel collector -> stream:graph-updates -> RCA worker, end-to-end
    # on the testbed: apply a known trace, assert the graph updates land
```

## 13.10 Hard rules

- **D2 — edges are `caller -> callee`.** Anyone writing "upstream" without defining which direction is wrong.
- **D21 — one RCA worker.** Helm `replicas: 1`. Replicas > 1 is a D-change.
- **D21 — `stream:graph-updates` is pinned.** Renaming the stream key is a D-change and breaks every running deployment.
- **D21 — live graph lives in the RCA worker.** No other module holds a graph copy. Detection reads `Signal`; correlation reads `Anomaly`; only RCA reads the graph.
- **D6 — reproducibility.** Same `scenario_id + seed` produces the same graph. The graph version is recorded in the result CSV, not the snapshot path.
- **No DL in the graph builder.** Edge weights are computed from latency and call count, not learned. D3.
- **No Secret values reach the graph.** The OTel collector does not emit Secret contents in `GraphUpdate`. D9.

## 13.11 Cross-reference

- **Locked decision:** `docs/decision-tree.md` — D2 (graph orientation), D6 (reproducibility), D21 (this standard).
- **Consumers:** `06-rca-scoring.md` (the ranker and the PageRank variant); `05-add-detector.md` (severity classifier reads `predecessors`); `07-llm-explainer.md` (evidence bundler reads `successors` for trace paths).
- **Producers:** `04-add-collector.md` — specifically the OTel and Jaeger collectors.
- **Persistence:** `RCA_GRAPH_PATH` — listed in `docs/surface-map.md` environment keys.
- **Skill:** `.claude/skills/rca-scoring/SKILL.md` (procedural entrypoint).
