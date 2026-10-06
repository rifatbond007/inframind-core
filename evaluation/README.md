# Evaluation harness (Phase P7)

Fault scenarios, runner, baselines, and scoring. See `docs/standards/10-evaluation.md` and the `run-evaluation` / `fault-scenario` skills.

Layout:

- `scenarios/` — one directory per scenario id (`scenario.yaml` + seed)
- `runner/` — applies Chaos Mesh CRs, records InfraMind output
- `baselines/` — D7 comparators
- `scoring/` — MTTD, precision/recall, RCA accuracy, soak FPR
- `notebooks/` — exploratory analysis (keep small)
- `results/` — generated CSVs (gitignored except `.gitkeep`)
