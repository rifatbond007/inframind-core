---
name: rca-scoring
description: How to implement and tune the root-cause ranking in InfraMind. Deterministic, graph-guided, evidence-bundled.
---

# RCA scoring

The RCA stage is the **core contribution**. It is deterministic. The LLM never picks the root cause (D1).

## Graph

- `NetworkX DiGraph` with edges `caller → callee`. Edge weight = call count or latency.
- The graph is built from traces (OTel span parent-child) and from manual annotation.
- Failures propagate `callee → caller`. Therefore RCA starts at the **symptom** service and walks **toward its callees** (D2).
- Bound traversal depth (default 4 hops).

## Scoring formula (default starting point)

```
score(s) = w1·anomaly_severity
        + w2·earliest_onset
        + w3·downstream_depth
        + w4·change_correlation
```

Weights tune on the **dev** split; report on the **test** split.

## PageRank variant

```
personalized_pagerank(graph, anomaly_scores, alpha=0.85)
```

Compare the weighted-formula vs. PageRank on the dev split. Report both.

## Evidence bundle

For each candidate, attach:
- Linked anomaly IDs.
- Log lines with error codes.
- Metric snapshots at the fault's start time.
- Spans showing the failure path.
- Linked change events (rollouts, ConfigMap changes).

## Reproducibility: same `IncidentRecord` → same ranking, regardless of LLM version.

- Same `(graph, anomaly_set)` produces the same score.
- A final `seed` parameter is included for any tie-breaking.

## How to implement

1. `src/inframind/rca/graph.py` — `build_graph(traces)`, `update_graph(graph, new_traces)`.
2. `src/inframind/rca/traversal.py` — `walk_toward_callees(graph, symptom, max_depth)`.
3. `src/inframind/rca/rank.py` — weighted-formula ranker.
4. `src/inframind/rca/rank_pagerank.py` — PageRank variant.
5. `src/inframind/rca/evidence.py` — bundle per candidate.
6. `src/inframind/rca/change_correlation.py` — K8s rollouts, ConfigMap changes.

## Tests

- `tests/unit/rca/test_graph.py` — known topology, known symptom.
- `tests/unit/rca/test_rank.py` — known incident, frozen expected ranking.
- `tests/integration/test_rca_e2e.py` — synthetic fault on the live testbed, assert ranking.

## Commit

- `feat(rca): <change>`
- Update `docs/PROGRESS.md` with the new row.
- Update `docs/ARCHITECTURE.md` RCA section.