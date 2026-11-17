# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Live incidents are stored in Postgres, explained by a hosted LLM (Groq via an
  OpenAI-compatible API) and alerted, with every LLM call and alert in the audit log (D28).
- `GET /incidents/{id}` returns `summary_text` and `alerts`.
- Grafana dashboard "InfraMind / Signals" (`testbed/obs/dashboards/`), loaded by `make obs`.
- Live evaluation recording: `make eval-live [EVAL_SET=…] [SCENARIO=<id>]` injects each
  scenario's fault on the kind testbed, confirms it took effect, reverts it, and writes one
  compressed fixture per repeat (`evaluation/fixtures/<scenario>/r<k>/`).
- Detector `pod_replaced` (D29): a pod-kill is now detected (a new pod for an established
  service). Detector `call_error` (D30, proposed): failing client calls count against the
  service they called.
- Scenarios dev-11 (frontend bad-config rollout, the caller-bug check) and test-09
  (pod-failure on productcatalogservice); first real recordings: dev-01, dev-02.

### Fixed
- The LLM key named `LLM_API_KEY` in `.env.example` and Helm was never read.
- The Docker image shipped no LLM prompt template.
- Acknowledging/resolving an incident failed (`''::jsonb`), and resolved incidents could
  not be read back (`closed_at` unset).
- `make test-all` truncated the live incidents database.
- Permanent false control-plane alerts on kind; ignored Chaos Mesh values; Loki gateway
  rollout deadlock; browser console errors in the testbed UIs.
- Scenario files used Sock Shop names and Chaos Mesh actions that do not exist; they now
  match Online Boutique and are validated on load.
- The RCA process only ever handled revision 1 of each incident (bus dedup keyed on the
  incident id), so stored incidents never updated or resolved.
- Stored LLM summaries could name a different root cause than the stored ranking; the
  summary is refreshed when the top candidate changes and on resolve (D28 amendment).
- Evaluation: every repeat replayed one fixture; the D7 baselines fell back to alphabetical
  order and PageRank copied InfraMind; replay ranked over the ground-truth fault window;
  earlier faults leaked into recording baselines; a refused fault was recorded as a fault.
- Chaos Mesh ran with the docker runtime on a containerd node, so container-kill, stress
  and network faults never injected (D2-Chaos-runtime applied; container-kill verified).
- `make observe-fault` failed with the compose stack up (port 8080).

## [0.0.0] — 2026-10-05 — initial scaffold

### Added
- Repository scaffolding: tooling, CI, dev stack, module skeletons, docs.
- See `docs/PROGRESS.md` for the per-phase roadmap.
