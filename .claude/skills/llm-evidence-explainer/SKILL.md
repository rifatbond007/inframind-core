---
name: llm-evidence-explainer
description: How to use the LLM to explain RCA results. The LLM summarizes evidence; it never picks the root cause.
---

# LLM evidence explainer (skill entrypoint)

**Canonical procedure:** [`docs/standards/07-llm-explainer.md`](../../../docs/standards/07-llm-explainer.md)

Before changing prompts, validators, or adapters:

1. Read [`docs/PROGRESS.md`](../../../docs/PROGRESS.md) — LLM explainer is Phase P5b.
2. Enforce D1 and D9 (redaction before any external LLM call; Ollama fallback).
3. Log the session in `docs/PROGRESS.md` when done.

Owner: llm-explainer agent (`Prome`).
