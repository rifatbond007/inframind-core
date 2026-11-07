"""Public surface of the LLM explainer (standard 07).

Importers should reach for `LLMParams`, `load_llm_params`, the `LLMSummary` model, and the
high-level `explain()` entry point. Provider adapters and the redaction layer are
implementation details; tests reach them directly, but no other module should.
"""

from __future__ import annotations

from .config import LLMParams, load_llm_params, should_fallback_to_ollama
from .pipeline import explain
from .types import LLMClaim, LLMSummary

__all__ = [
    "LLMClaim",
    "LLMParams",
    "LLMSummary",
    "explain",
    "load_llm_params",
    "should_fallback_to_ollama",
]
