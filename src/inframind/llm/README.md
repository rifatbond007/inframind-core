# `inframind.llm`

LLM-based natural-language explanation of RCA evidence.

## Will be added in Step 7

- `prompts/` — versioned prompt templates.
- `client.py` — provider abstraction (`openai`, `ollama`).
- `validator.py` — checks every claim references real evidence IDs.
- `cache.py` — response cache.
- `redact.py` — secret/PII redaction pre-send (D9).

## Hard rules

- The LLM **never** picks the root cause (D1).
- Every claim in the output must cite a valid evidence ID.
- Local LLM (Ollama) fallback must work without API keys (D9).

## Owner

Prome — RCA, LLM, alerting, storage.
