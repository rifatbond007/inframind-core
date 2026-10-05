# 7. LLM explainer — prompts, validator, redaction, Ollama
> standards · Python conventions · InfraMind. Pairs with the `llm-evidence-explainer` skill in `.claude/skills/llm-evidence-explainer/SKILL.md`. This file is the rationale.

## 7.1 The discipline (D1)

The LLM **summarises evidence**. It does not pick the root cause. The graph stage ranks; the
LLM writes. Anyone who proposes to let the LLM pick is proposing a change that needs a new
D-number and supervisor sign-off.

## 7.2 What the LLM receives

A structured prompt:

- The **ranked candidates** (top-N) from `rca/rank.py` — service name, score, evidence id list.
- The **evidence bundle** for each candidate — log lines, metric snapshots, change events.
- The **incident metadata** — `incident_id`, `opened_at`, `closed_at`, the originating
  observability signals.

The prompt template is `src/inframind/llm/prompts/rca_summary.v1.j2`. Every template edit
bumps the version (`.v2.j2`, etc.) — the old template stays in the tree for the reproducibility
cache.

## 7.3 What the LLM produces

A JSON object:

```json
{
  "summary_text": "Plain-language description of the incident and the ranked candidates.",
  "claims": [
    {"text": "p95 latency rose from 40ms to 2.1s", "evidence_id": "ev_123"},
    {"text": "3 pod restarts at 14:02", "evidence_id": "ev_456"}
  ]
}
```

Every `claim.text` MUST cite an `evidence_id` that is in the bundle. A claim without a
citation is rejected.

## 7.4 The validator

After the LLM call:

1. **JSON-schema validate** the response against the `LLMSummary` model.
2. **For each `claim.evidence_id`**, check it exists in the bundle.
3. **If a claim cites a missing ID**, reject and retry once with a hint: *"your previous
   response cited evidence ID X that is not in the bundle"*.
4. **If retry also fails**, fall back to a deterministic template summary (a single
   sentence generated from the top candidate's evidence list).
5. **Log the failure** with `WARNING` level, including the LLM provider and the rejected
   response. The audit table stores the raw LLM output regardless.

The validator never softens a hard rule. A claim without a real evidence id is rejected.

## 7.5 Redaction (D9)

Before any text leaves the process, the redactor strips:

| Pattern | Example |
|---|---|
| JWT | `eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...` |
| Bearer tokens | `Bearer xxxxx` |
| Email addresses | `name@domain` |
| Password / api-key fields | `password=...`, `api_key=...` |
| Internal IP addresses | `10.x.x.x`, `192.168.x.x`, `172.16-31.x.x` (configurable) |
| Kubernetes secrets shape | `kind: Secret` blocks, anything under `data:` |
| Long random strings | anything > 40 chars matching `[A-Za-z0-9_-]{20,}` (heuristic) |

The redactor lives at `src/inframind/llm/redact.py`. It is pure — same input, same output,
no exceptions. A unit test asserts the full pattern set.

## 7.6 The local-LLM fallback (D9)

- `LLM_PROVIDER=OPENAI` (default) — calls the OpenAI API. Requires `OPENAI_API_KEY`.
- `LLM_PROVIDER=OLLAMA` — calls a local Ollama endpoint. No API key required.

Both adapters live in `src/inframind/llm/client.py`. The adapter contract is the same:
`summarise(prompt) -> str`. The validator runs after the adapter, never inside it.

Ollama is the fallback when:

- `OPENAI_API_KEY` is empty or unset.
- The OpenAI adapter raises `openai.AuthenticationError` or a network error.
- The operator has explicitly set `LLM_PROVIDER=OLLAMA`.

## 7.7 Reproducibility

- **`temperature=0`.** Always. A non-zero temperature breaks the reproducibility guarantee.
- **Cache key:** `(scenario_id, prompt_version, evidence_bundle_hash, model)`. A hit returns the
  cached summary without re-calling the LLM.
- **Same `(scenario_id, prompt_version)` returns the same summary**, regardless of when it is
  run. This is what makes the paper's "LLM summary" numbers reproducible.

## 7.8 Tests

```
tests/unit/llm/test_prompt.py
tests/unit/llm/test_validator.py
tests/unit/llm/test_redact.py
tests/integration/test_llm_openai.py
tests/integration/test_llm_ollama.py
```

- `test_prompt.py` — Jinja renders with a fixture bundle.
- `test_validator.py` — bad output is rejected; the retry hint is sent; the second failure
  triggers the template fallback.
- `test_redact.py` — every pattern in the table above is stripped.
- The OpenAI / Ollama integration tests skip when the env vars are absent.

## 7.9 Hard rules

- **D1: the LLM does not pick the root cause.** The prompt template forbids it. The validator
  enforces it.
- **D9: redact before send.** No raw log lines, env vars, request bodies, or K8s `Secret`
  payloads.
- **`temperature=0`.** Always.
- **Cache key is mandatory.** A summary that cannot be cached is broken.
- **Bump the prompt version on every template edit.** No silent edits to `v1.j2`.

## 7.10 Cross-reference

- **Skill:** `.claude/skills/llm-evidence-explainer/SKILL.md`.
- **Locked decisions:** D1 (LLM does not pick), D9 (redaction + Ollama), D15 (evidence
  bundled before ranking).
- **Upstream stage:** `06-rca-scoring.md` — the bundle the LLM receives.
- **Downstream stage:** `alerting/` consumes the summary as part of the alert payload.
