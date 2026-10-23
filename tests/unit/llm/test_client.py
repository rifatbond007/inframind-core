"""Provider adapter behaviour without touching the network (standard 07.6, 07.7).

The OpenAI SDK is not required to be installed for these tests — the adapter imports it
lazily, so a missing dependency surfaces as `LLMNetworkError` at call time, not as an
import failure at collection time.
"""

from __future__ import annotations

import httpx
import pytest

from inframind.llm.client import (
    TEMPERATURE,
    LLMAuthenticationError,
    LLMError,
    LLMNetworkError,
    OllamaClient,
    OpenAIClient,
    build_client,
    with_fallback,
)
from inframind.llm.config import LLMParams, load_llm_params, should_fallback_to_ollama


def test_temperature_is_pinned_to_zero() -> None:
    assert TEMPERATURE == 0.0


def test_params_default_to_openai() -> None:
    assert LLMParams().provider == "OPENAI"


def test_load_llm_params_reads_inframind_prefix() -> None:
    params = load_llm_params({"INFRAMIND_LLM_TOP_K": "3"})
    assert params.top_k == 3


def test_load_llm_params_reads_bare_llm_provider() -> None:
    params = load_llm_params({"LLM_PROVIDER": "ollama"})
    assert params.provider == "OLLAMA"


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(ValueError, match="not supported"):
        load_llm_params({"LLM_PROVIDER": "gemini"})


def test_non_positive_top_k_is_rejected() -> None:
    with pytest.raises(ValueError, match="top_k"):
        load_llm_params({"INFRAMIND_LLM_TOP_K": "0"})


def test_fallback_triggers_without_api_key() -> None:
    params = load_llm_params({"LLM_PROVIDER": "OPENAI"})
    assert should_fallback_to_ollama(params, env={}) is True
    assert should_fallback_to_ollama(params, env={"OPENAI_API_KEY": "sk-present"}) is False


def test_build_client_selects_provider() -> None:
    env = {"OPENAI_API_KEY": "sk-x"}
    assert isinstance(build_client(LLMParams(provider="OPENAI"), env=env), OpenAIClient)
    assert isinstance(build_client(LLMParams(provider="OLLAMA"), env=env), OllamaClient)


def test_openai_refuses_empty_key_without_calling_the_sdk() -> None:
    client = OpenAIClient(LLMParams(), "")
    with pytest.raises(LLMAuthenticationError, match="empty"):
        client.summarise("prompt")


def test_ollama_posts_temperature_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _Response:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {"message": {"content": "hello"}}

    def fake_post(url: str, *, json: dict, timeout: float) -> _Response:  # type: ignore[no-untyped-def]
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(httpx, "post", fake_post)
    client = OllamaClient(LLMParams(provider="OLLAMA", request_timeout_s=3.0))
    assert client.summarise("p") == "hello"
    assert captured["json"]["options"]["temperature"] == 0.0
    assert captured["timeout"] == 3.0
    assert "api/chat" in str(captured["url"])


def test_ollama_network_error_is_mapped(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", boom)
    with pytest.raises(LLMNetworkError, match="refused"):
        OllamaClient(LLMParams(provider="OLLAMA")).summarise("p")


def test_ollama_server_error_is_mapped(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Response:
        status_code = 503
        text = "overloaded"

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Response())
    with pytest.raises(LLMNetworkError, match="503"):
        OllamaClient(LLMParams(provider="OLLAMA")).summarise("p")


def test_ollama_empty_content_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Response:
        status_code = 200
        text = "{}"

        def json(self) -> dict[str, object]:
            return {"message": {"content": ""}}

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Response())
    with pytest.raises(LLMError, match="empty message content"):
        OllamaClient(LLMParams(provider="OLLAMA")).summarise("p")


def test_with_fallback_succeeds_on_primary() -> None:
    call = with_fallback(lambda p: "primary", lambda p: "fallback")
    assert call("p") == "primary"


def test_with_fallback_switches_on_auth_error() -> None:
    def primary(p: str) -> str:
        raise LLMAuthenticationError("bad key")

    call = with_fallback(primary, lambda p: "fallback")
    assert call("p") == "fallback"


def test_with_fallback_raises_when_both_fail() -> None:
    def primary(p: str) -> str:
        raise LLMAuthenticationError("bad key")

    def secondary(p: str) -> str:
        raise LLMNetworkError("ollama down")

    call = with_fallback(primary, secondary)
    with pytest.raises(LLMNetworkError, match="fallback also failed"):
        call("p")
