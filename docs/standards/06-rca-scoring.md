# 6. RCA scoring — graph, traversal, ranker, evidence
> standards · Python conventions · InfraMind. Pairs with the `rca-scoring` skill in `.claude/skills/rca-scoring/SKILL.md`. This file is the rationale.

## 6.1 What the RCA stage does

The RCA stage is the **core contribution**. It takes a set of `Anomaly` objects and a
service-call graph, and emits a **ranked list of candidate root-cause services**, each with
an **evidence bundle**. The ranking is deterministic. The LLM only summarises the evidence;
it never picks the root cause. D1.

## 6.2 The graph (D2)

- **Library:** `NetworkX DiGraph`.
- **Edges:** `caller -> callee`. Edge weight = call count or p95 latency, computed from
  traces. The graph is built from OTel span parent-child relationships and updated as new
  traces arrive.
- **Failure propagation:** `callee -> caller`. A failing `redis` (callee) impacts `frontend`
  (caller).
- **RCA walk direction:** start at the **symptom** service (the service whose `Anomaly` we
  are explaining) and walk **toward its callees** — that is, into the graph in the direction
  the failure propagated. D2.

The orientation rule is the single most-violated convention in the project. When you write
"upstream", define which way you mean. The standard is "toward the callee".

## 6.3 The walk

```python
import networkx as nx
from inframind.rca.graph import DiGraph

def walk_toward_callees(graph: DiGraph, symptom: str, max_depth: int = 4) -> list[str]:
    """Return the set of services reachable from `symptom` along `caller -> callee` edges,
    bounded by `max_depth`."""
    visited: set[str] = {symptom}
    frontier: list[tuple[str, int]] = [(symptom, 0)]
    out: list[str] = []
    while frontier:
        node, depth = frontier.pop()
        if depth >= max_depth:
            continue
        for callee in graph.successors(node):  # toward callees
            if callee not in visited:
                visited.add(callee)
                out.append(callee)
                frontier.append((callee, depth + 1))
    return out
```

The walk is bounded. A `max_depth=4` default is a starting point; tune on the dev split.

## 6.4 The default scoring formula

```
score(s) = w1 * anomaly_severity
        + w2 * earliest_onset
        + w3 * downstream_depth
        + w4 * change_correlation
```

| Term | What it measures | Range |
|---|---|---|
| `anomaly_severity` | Sum of z-scores (or detector equivalent) for `s` in the incident window. | [0, +inf) |
| `earliest_onset` | 1 / (time since the first `Anomaly` on `s`). Earlier onset = higher score. | (0, 1] |
| `downstream_depth` | The shortest distance from `s` to the symptom service in the graph. Closer = higher score. | [0, `max_depth`] |
| `change_correlation` | 1.0 if a rollout / ConfigMap / Secret change to `s` happened in the window, else 0. | {0, 1} |

The four `w` weights sum to 1; tune on the dev split.

## 6.5 The PageRank variant

The MicroRCA-style alternative is personalised PageRank seeded by anomaly scores:

```python
import networkx as nx

def rank_pagerank(graph: DiGraph, anomaly_scores: dict[str, float], alpha: float = 0.85) -> dict[str, float]:
    """Personalised PageRank seeded by per-service anomaly score. Returns score per service."""
    pr = nx.pagerank(graph, alpha=alpha, personalization=anomaly_scores, weight="weight")
    return pr
```

The default ranker (`rank.py`) and the variant (`rank_pagerank.py`) are reported side by side in
the paper. InfraMind's headline number is the **default formula**; the PageRank variant is a
sensitivity analysis.

## 6.6 The evidence bundle

For each candidate, attach:

| Field | Source |
|---|---|
| `linked_anomaly_ids` | The `Anomaly.id` list the ranker used. |
| `log_lines` | Up to 10 recent log lines from `s` matching the incident window. |
| `metric_snapshots` | The metric at the fault's start time and at the rank time. |
| `spans` | Trace spans showing the failure path through `s`. |
| `change_events` | K8s rollouts, ConfigMap changes, Secret changes touching `s` in the window. |
| `baseline` | The pre-fault baseline for the metrics in the window. |

The bundle is what the LLM summarises. Every claim the LLM makes must cite an evidence id
from the bundle (D1 + the validator in section 7).

## 6.7 Reproducibility

The same `(graph, anomaly_set)` produces the same score, regardless of the LLM version:

- **No `random.random()`** in the ranker. Any tie-breaking uses the deterministic
  `RCA_TIEBREAK_SEED` from `inframind.common.config`.
- **No current-time leakage.** The ranker reads `incident.opened_at` and
  `incident.closed_at`. It does not call `utcnow()`.
- **A `seed` field on the scenario YAML** is required (D6). The runner pins it; the ranker
  reads it for any tie-break; the paper's CSVs are reproducible from `scenario_id + seed`.

## 6.8 Tests

```
tests/unit/rca/test_graph.py
tests/unit/rca/test_walk.py
tests/unit/rca/test_rank.py
tests/integration/test_rca_e2e.py
```

- `test_graph.py` — known topology, known symptom; walk returns the expected set.
- `test_walk.py` — `max_depth` truncation works; cycles don't loop.
- `test_rank.py` — a frozen `Incident` produces a frozen ranking. Bumping the ranker
  deliberately breaks the test, which is the point — reviewers see the diff in the PR.
- `test_rca_e2e.py` — synthetic fault on the live testbed; ranking is correct.

## 6.9 Hard rules

- **D1: ranking is deterministic.** The LLM does not pick. A reviewer who spots an
  LLM-influenced score rejects the PR.
- **D2: edges are `caller -> callee`; the walk is toward callees.** Anyone writing
  "upstream" without defining it is wrong.
- **No `utcnow()` in the ranker.** Time comes from the incident.
- **Tune on dev, report on test.** D6.
- **No DL in the ranker.** D3.

## 6.10 Cross-reference

- **Skill:** `.claude/skills/rca-scoring/SKILL.md`.
- **Locked decisions:** D1 (deterministic ranking), D2 (graph orientation), D3 (no DL),
  D6 (reproducibility from scenario id + seed), D15 (evidence bundled before ranking).
- **The next stage:** `llm/` summarises the bundle. The validator in section 7 enforces that
  every claim cites a real evidence id.
- **Evaluation:** `10-evaluation.md` — how the ranker is scored against the 6 baselines (D7).
