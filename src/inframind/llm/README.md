# `inframind.llm`

LLM-based natural-language explanation of RCA evidence. The LLM **summarises** the
ranked candidates the RCA stage produces; it never picks the root cause (D1).

## Contents

- `config.py` — `LLMParams` and `load_llm_params`. `temperature` is pinned to 0 by
  `client.TEMPERATURE`; the dataclass has no field for it on purpose so a runtime override
  is impossible.
- `redact.py` — D9 redaction. Layers on top of `common/redact` with K8s `kind: Secret`
  blocks, `data:` payload stripping, hashed pod/node names, and a long-token fallback.
- `prompts/rca_summary.v1.j2` — pinned template. Every edit bumps to `v2`, etc.
- `prompt.py` — builder. Renders incident metadata plus the ranked candidate list plus
  the top candidate's evidence bundle; returns a fully redacted prompt.
- `client.py` — provider adapters. `OpenAIClient` (OpenAI) and `OllamaClient` (local).
  Both call with `temperature=0`. Exposes a `with_fallback` helper for the D9 fallback.
- `validator.py` — JSON-schema validate + citation rule + retry-once-with-hint +
  deterministic template fallback.
- `cache.py` — reproducibility cache keyed by
  `(scenario_id, prompt_version, evidence_bundle_hash, model)` (standard 07.7).
- `pipeline.py` — `explain()` ties it all together. The single entry point for the RCA
  worker and the evaluation harness.

## Hard rules

- The LLM **never** picks the root cause (D1). The ranking from `rca/rank.py` is passed
  through unchanged.
- Every claim in the LLM output must cite a real evidence id from the bundle (D1). The
  validator rejects anything else, retries once with a hint, then falls back to a
  deterministic template summary.
- Local LLM (Ollama) fallback must work without API keys (D9).
- `temperature=0`. Pinned in `client.TEMPERATURE`; the dataclass has no field for it.
- The cache key MUST include `prompt_version` (07.7). A bump to `v2.j2` invalidates the
  cache automatically because the key is now `(..., "v2", ...)`.

## Hard limits

- `llm` may only import `common` and `rca` (standards 01 §1.3). The DAG test
  `tests/unit/test_module_dag.py` enforces this.

## Owner

Rifat (RCA -> LLM handoff; Prome owns RCA ranking itself).

## Public API

```python
from inframind.llm import explain, LLMSummary, LLMClaim, LLMParams, load_llm_params

summary, provenance = explain(incident, candidates, scenario_id="scn_001")
```
