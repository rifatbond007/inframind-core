"""Ollama adapter live test (standard 07.8).

Skipped unless ``OLLAMA_BASE_URL`` is set (or Ollama is reachable on its default port).
No API key needed (D9). Exercises the ``/api/chat`` wire path and the ``temperature=0``
option that keeps summaries reproducible.
"""

from __future__ import annotations

import os

import httpx
import pytest

from inframind.llm.client import OllamaClient
from inframind.llm.config import LLMParams


def _ollama_reachable(base_url: str) -> bool:
    try:
        response = httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=1.0)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


_DEFAULT = "http://localhost:11434"
_BASE = os.environ.get("OLLAMA_BASE_URL", _DEFAULT)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _ollama_reachable(_BASE),
        reason=f"no Ollama reachable at {_BASE}; live Ollama integration is skipped",
    ),
]


@pytest.fixture
def live_llm_params() -> LLMParams:
    """LLMParams pinned to the local model, with a slightly longer timeout."""
    return LLMParams(
        provider="OLLAMA",
        ollama_model=os.environ.get("OLLAMA_MODEL", "llama3.1:8b-instruct-q5_K_M"),
        ollama_base_url=_BASE,
        request_timeout_s=120.0,
    )


def test_ollama_summarise_round_trip(live_llm_params: LLMParams) -> None:
    client = OllamaClient(live_llm_params)
    raw = client.summarise("Say the word banana and nothing else.")
    assert "banana" in raw.lower()
