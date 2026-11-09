"""The hosted-API contract (D28): `LLM_*` env names, and no local model when the fallback is off.

The deployment contract (`.env.example`, Helm `secretRef`/`config`, docker compose) names the
settings `LLM_API_KEY`, `LLM_MODEL`, `LLM_BASE_URL` and `LLM_TIMEOUT_S`. With
`ollama_fallback=False` a missing key or a failed API call must degrade to the deterministic
template summary without ever constructing an Ollama call.
"""

from __future__ import annotations

import dataclasses

import pytest

from inframind.llm import explain
from inframind.llm.cache import SummaryCache
from inframind.llm.client import LLMAuthenticationError, LLMNetworkError
from inframind.llm.config import (
    LLMParams,
    llm_api_key,
    load_llm_params,
    should_fallback_to_ollama,
)
from inframind.llm.pipeline import _build_call

GROQ = "https://api.groq.com/openai/v1"


def test_contract_env_names_override_model_endpoint_and_timeout() -> None:
    params = load_llm_params(
        {
            "LLM_PROVIDER": "openai",
            "LLM_MODEL": "openai/gpt-oss-120b",
            "LLM_BASE_URL": GROQ,
            "LLM_TIMEOUT_S": "20",
            "INFRAMIND_LLM_OLLAMA_FALLBACK": "false",
        }
    )
    assert params.provider == "OPENAI"
    assert params.openai_model == "openai/gpt-oss-120b"
    assert params.openai_base_url == GROQ
    assert params.request_timeout_s == 20.0
    assert params.ollama_fallback is False


def test_contract_env_names_win_over_prefixed_ones() -> None:
    params = load_llm_params({"INFRAMIND_LLM_OPENAI_MODEL": "old", "LLM_MODEL": "new"})
    assert params.openai_model == "new"


def test_blank_contract_values_keep_the_defaults() -> None:
    params = load_llm_params({"LLM_MODEL": "  ", "LLM_BASE_URL": ""})
    assert params.openai_model == LLMParams().openai_model
    assert params.openai_base_url == LLMParams().openai_base_url


def test_a_non_numeric_timeout_is_rejected() -> None:
    with pytest.raises(ValueError, match="LLM_TIMEOUT_S"):
        load_llm_params({"LLM_TIMEOUT_S": "soon"})


def test_api_key_prefers_llm_api_key_and_accepts_openai_api_key() -> None:
    assert llm_api_key({"LLM_API_KEY": " gsk_a ", "OPENAI_API_KEY": "sk_b"}) == "gsk_a"
    assert llm_api_key({"OPENAI_API_KEY": "sk_b"}) == "sk_b"
    assert llm_api_key({}) == ""


def test_fallback_off_never_selects_ollama() -> None:
    params = LLMParams(ollama_fallback=False)
    assert should_fallback_to_ollama(params, env={}) is False
    # The D9 default is unchanged: no key and fallback on means Ollama.
    assert should_fallback_to_ollama(LLMParams(), env={}) is True


def _no_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("an Ollama client must not be built with the fallback off")

    monkeypatch.setattr("inframind.llm.pipeline.OllamaClient", boom)


def test_no_key_and_fallback_off_raises_network_error_without_any_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _no_ollama(monkeypatch)
    call = _build_call(LLMParams(ollama_fallback=False))
    with pytest.raises(LLMNetworkError, match="no LLM_API_KEY"):
        call("prompt")


def test_key_and_fallback_off_calls_only_the_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "gsk_test")
    _no_ollama(monkeypatch)
    seen: dict[str, str] = {}

    def fake_summarise(self: object, prompt: str) -> str:
        seen["key"] = self._api_key  # type: ignore[attr-defined]
        return "ok"

    monkeypatch.setattr("inframind.llm.pipeline.OpenAIClient.summarise", fake_summarise)
    assert _build_call(LLMParams(ollama_fallback=False))("prompt") == "ok"
    assert seen["key"] == "gsk_test"


def test_a_rejected_key_with_fallback_off_degrades_as_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_API_KEY", "gsk_bad")
    _no_ollama(monkeypatch)

    def rejected(self: object, prompt: str) -> str:
        raise LLMAuthenticationError("401")

    monkeypatch.setattr("inframind.llm.pipeline.OpenAIClient.summarise", rejected)
    with pytest.raises(LLMNetworkError, match="rejected the key"):
        _build_call(LLMParams(ollama_fallback=False))("prompt")


def test_explain_without_a_key_returns_the_template_summary(
    incident, ranked_candidate, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _no_ollama(monkeypatch)
    params = dataclasses.replace(LLMParams(), ollama_fallback=False)
    summary, provenance = explain(
        incident, [ranked_candidate], scenario_id="live", params=params, cache=SummaryCache()
    )
    assert provenance is not None
    assert provenance.status == "template_fallback"
    assert ranked_candidate.service in summary.summary_text
