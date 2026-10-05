---
name: llm-evidence-explainer
description: How to use the LLM to explain RCA results. The LLM only summarises evidence — it never picks the root cause.
---

# LLM evidence explainer

The LLM's job is **summarise evidence**. **Never pick the root cause** (D1).

## What the LLM receives

A structured prompt containing:
- The ranked candidates (from `rca/rank.py`).
- The evidence bundle (from `rca/evidence.py`).
- The change history (from `rca/change_correlation.py`).

## What the LLM produces

A JSON object with:
- `summary_text` — human-readable summary.
- `claims[]` — each claim cites a real evidence ID from the bundle.

```json
{
  "summary_text": "...",
  "claims": [
    {"text": "p95 latency rose from 40ms to 2.1s", "evidence_id": "ev_123"},
    {"text": "3 pod restarts at 14:02", "evidence_id": "ev_456"}
  ]
}
```

## Validator

After the LLM call:
1. JSON-schema validate the response.
2. For each `claim.evidence_id`, check it exists in the bundle.
3. If a claim cites a missing ID, **reject** and retry once with a hint: "your previous response cited evidence ID X that is not in the bundle".
4. If retry also fails, fall back to a deterministic template summary.

## Redaction (D9)

Before any text hits the LLM:
- Strip JWTs (`eyJ...`).
- Strip bearer tokens (`Bearer xxxx`).
- Strip emails (`name@domain`).
- Strip obvious password / api-key fields.
- Strip internal IP addresses (configurable).

## Local LLM (Ollama) fallback (D9)

- `LLM_PROVIDER=ollama` runs against a local Ollama endpoint.
- No API key required.
- Same prompt, same validator.
- Used when `OPENAI_API_KEY` is empty or when the user explicitly chooses it.

## Reproducibility

- `temperature=0`.
- Cache keyed on `(scenario_id, prompt_version, evidence_bundle_hash, model)`.
- A given `(scenario, prompt_version)` always returns the same summary.

## How to implement

1. `src/inframind/llm/prompts/rca_summary.v1.j2` — versioned Jinja template.
2. `src/inframind/llm/client.py` — `openai` and `ollama` adapters.
3. `src/inframind/llm/validator.py` — JSON-schema + evidence-ID check.
4. `src/inframind/llm/cache.py` — keyed cache.
5. `src/inframind/llm/redact.py` — secret / PII redactor.

## Tests

- `tests/unit/llm/test_prompt.py` — Jinja renders with a fixture.
- `tests/unit/llm/test_validator.py` — bad output is rejected.
- `tests/unit/llm/test_redact.py` — secrets are stripped.
- `tests/integration/test_llm_openai.py` — live call against `OPENAI_API_KEY` (skipped if not set).
- `tests/integration/test_llm_ollama.py` — live call against local Ollama (skipped if not running).

## Commit

- `feat(llm): <change>`
- Update `docs/PROGRESS.md` with the new row.
- Bump the prompt version (`rca_summary.v2.j2`) for any template edit; do not silently edit v1.