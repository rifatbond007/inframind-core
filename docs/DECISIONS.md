# Decision Log (ADR-lite)
Format: ID · date · decision · why · alternatives · status (proposed / approved by supervisor)

## Setup-phase decisions
- D-Setup · 2026-10-05 · Step 0 only creates skeletons and tooling; no functional code in this phase. K8s testbed lives in `make up` (Step 1 work). docker-compose covers only Redis and PostgreSQL. Functional code lands phase by phase per `PROGRESS.md`. · Why: keep the scaffold verifiable (lint+test runs on real imports) without violating D3 (no DL training) or D5 (Online Boutique, not Sock Shop) prematurely. · Alternatives considered: build the full stack in Step 0 (rejected — too much surface area to debug at once). · Status: approved by the team; supervisor to follow up in next sync.

## Proposed deviations from the July 2026 proposal (get supervisor approval)
- D2 · Edge direction caller->callee; RCA walks toward callees. (Proposal said "upstream" ambiguously.)
- D5 · Online Boutique / OTel Demo instead of Sock Shop.
- D6 · 5 runs per scenario (proposal: 3); fault-free soak run added; FPR measured on soak (false alarms/hour), not 1 - precision.
- D7 · Stronger RCA baselines + LLM-only baseline added.
- NFR · Throughput targets (10k logs/s, 5k spans/s) replaced by load-test-measured numbers on the actual machine.
- Timeline · Add Phase 9 (paper, 3-4 weeks). Re-baseline dates from the real start date.
