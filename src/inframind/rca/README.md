# `inframind.rca`

Graph-guided, deterministic root cause analysis. **Core contribution.**

## Components (P5 — implemented)

- `config.py` — `RCAParams` + `load_rca_params()` (env keys `INFRAMIND_RCA_*`).
- `graph.py` — `ServiceGraph` per D21 / standard 13. EWMA α=`GRAPH_EWMA_ALPHA`,
  min-observations gate, decay, snapshot-on-disk (no `networkx` dep — implemented natively).
- `traversal.py` — bounded BFS walk from the symptom **toward its callees** (D2).
- `evidence.py` — bundles the proof-of-cause per candidate before ranking (D15).
- `rank.py` — weighted score formula `w1*severity + w2*earliest + w3*depth + w4*change` (standard 06.4).
- `rank_pagerank.py` — MicroRCA-style personalised PageRank sensitivity analysis (standard 06.5).
- `change_correlation.py` — K8s change correlation as the `w4` term (D22 / standard 14).
- `worker.py` — `RCAWorker` consuming `stream:graph-updates` + `stream:incidents` via
  `TypedStreamConsumer`. Runs the live `analyse()` pipeline; emits ranked candidates.

Run with ``python -m inframind.rca.worker``.

## Deferred

- The K8s change watcher (`change_watcher.py`) lands with P8 packaging — the worker
  currently accepts `change_events` as an injected `Sequence[ChangeEvent]`. See
  `rca/worker.py:107` and standard 14 for the contract.

## Hard rules

- Edges go `caller → callee`. Failures propagate `callee → caller`. We walk from symptom **toward callees**. Never say "upstream" without defining it (D2).
- RCA is deterministic. LLM only summarizes (D1).
- No `networkx` dependency — the graph + PageRank are implemented natively. Adding a new runtime dep needs a PR justification (CONTRIBUTING).
- PageRank is a sensitivity analysis, **not** the headline number. The weighted ranker identifies the root cause; PageRank ranks mid-graph hubs and will diverge from it (test_worker's divergence test pins this).

## Owner

Prome — RCA, LLM, alerting, storage.
