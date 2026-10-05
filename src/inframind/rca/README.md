# `inframind.rca`

Graph-guided, deterministic root cause analysis. **Core contribution.**

## Will be added in Step 6

- `graph.py` — NetworkX `DiGraph` builder (edges = `caller→callee`).
- `traversal.py` — walk from symptom toward callees.
- `rank.py` — weighted score formula and PageRank variant.
- `evidence.py` — evidence bundler (proof-of-cause per candidate).
- `change_correlation.py` — correlate with K8s rollouts / config changes.

## Hard rules

- Edges go `caller → callee`. Failures propagate `callee → caller`. We walk from symptom **toward callees**. Never say "upstream" without defining it (D2).
- RCA is deterministic. LLM only summarizes (D1).

## Owner

Prome — RCA, LLM, alerting, storage.
