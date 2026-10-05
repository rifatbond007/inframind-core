"""RCA: deterministic root-cause ranking over the service dependency graph.

Planned (Step 6) — this is InfraMind's core contribution:

* Build a `DiGraph` with ``caller→callee`` edges (D2). Failures propagate
  ``callee→caller``, so we start at the symptom service and walk **toward its
  callees** to find the root.
* Score candidates with a transparent formula (e.g. weighted anomaly severity,
  earliest onset, downstream depth, change correlation).
* Alternative — Personalized PageRank weighted by anomaly score (MicroRCA-style).
* Return the top-K ranked candidates + the evidence bundle that justifies them.

The LLM never picks the root cause. It only explains it (D1).
"""
