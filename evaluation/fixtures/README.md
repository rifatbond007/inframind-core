# Evaluation fixtures (live recordings only)

Every directory here is a **real recording** written by the live runner
(`make eval-live`, `src/inframind/evaluation/runner/live.py`) against the
`kind-inframind` testbed. Nothing in this tree is hand-written: the paper's
numbers are replayed from these files (D7-Replay), so a hand-made file here
would be an invented result.

Layout, one directory per repeat (D6: each repeat is its own recording):

```
<scenario_id>/r<repeat>/
  signals.ndjson   stream:signals from onset - baseline_s to the end, sorted by ts
  graph.json       ServiceGraph folded from stream:graph-updates up to the end
  meta.json        scenario_id, seed, repeat, onset_ts, window_s, recorded_at,
                   baseline_s, recorded_live: true
```

Synthetic fixtures for unit and integration tests live in `tests/fixtures/eval/`.
