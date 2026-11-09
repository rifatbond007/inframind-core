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

### Fixed
- The LLM key named `LLM_API_KEY` in `.env.example` and Helm was never read.
- The Docker image shipped no LLM prompt template.
- Acknowledging/resolving an incident failed (`''::jsonb`), and resolved incidents could
  not be read back (`closed_at` unset).
- `make test-all` truncated the live incidents database.
- Permanent false control-plane alerts on kind; ignored Chaos Mesh values; Loki gateway
  rollout deadlock; browser console errors in the testbed UIs.

## [0.0.0] — 2026-10-05 — initial scaffold

### Added
- Repository scaffolding: tooling, CI, dev stack, module skeletons, docs.
- See `docs/PROGRESS.md` for the per-phase roadmap.
