"""LLM explanation: turn evidence into a human-readable summary.

Planned (Step 7):

* Versioned prompts in ``prompts/`` directory.
* Structured JSON output — every claim references an evidence ID.
* Validator that checks each cited evidence ID actually exists in the bundle.
* Async execution, response cache.
* Secret/PII redaction before sending (D9).
* Local LLM fallback via Ollama (D9).
"""
