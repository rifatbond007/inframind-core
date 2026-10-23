"""End-to-end `inframind.llm.pipeline.explain` (standard 07).

The pipeline composes cache, prompt, validation, and provider fallback. Two contracts
matter:

* An identical second call returns the cached summary without re-calling the provider.
* When both attempts fail, the deterministic template summary is returned.
"""

from __future__ import annotations

import json

from inframind.llm import explain
from inframind.llm.cache import SummaryCache
from inframind.llm.config import LLMParams
from inframind.llm.pipeline import _build_call
from inframind.llm.validator import ValidationResult


def test_explain_returns_accepted_summary(
    incident, ranked_candidate, llm_params, monkeypatch: __import__("pytest").MonkeyPatch
) -> None:
    raw = json.dumps(
        {
            "summary_text": "paymentservice is the root cause",
            "claims": [
                {"text": "critical errors fired", "evidence_id": "ev_alpha"},
                {"text": "a deployment rolled out", "evidence_id": "ev_beta"},
            ],
        }
    )

    calls = {"n": 0}

    def fake_call(prompt: str) -> str:
        calls["n"] += 1
        return raw

    monkeypatch.setattr("inframind.llm.pipeline._build_call", lambda p: fake_call)

    cache = SummaryCache()
    summary, provenance = explain(
        incident, [ranked_candidate], scenario_id="scn_001", params=llm_params, cache=cache
    )

    assert calls["n"] == 1
    assert isinstance(provenance, ValidationResult)
    assert provenance.accepted
    assert "paymentservice" in summary.summary_text
    assert cache.hits == 0
    assert cache.misses == 1


def test_explain_hits_cache_on_second_call(
    incident, ranked_candidate, llm_params, monkeypatch: __import__("pytest").MonkeyPatch
) -> None:
    raw = json.dumps(
        {
            "summary_text": "cached",
            "claims": [{"text": "x", "evidence_id": "ev_alpha"}],
        }
    )
    calls = {"n": 0}

    def fake_call(prompt: str) -> str:
        calls["n"] += 1
        return raw

    monkeypatch.setattr("inframind.llm.pipeline._build_call", lambda p: fake_call)

    cache = SummaryCache()
    explain(incident, [ranked_candidate], scenario_id="scn_002", params=llm_params, cache=cache)
    summary, provenance = explain(
        incident, [ranked_candidate], scenario_id="scn_002", params=llm_params, cache=cache
    )
    assert calls["n"] == 1
    assert provenance is None
    assert cache.hits == 1
    assert "cached" in summary.summary_text


def test_explain_falls_back_to_template_when_provider_is_unreachable(
    incident, ranked_candidate, llm_params, monkeypatch: __import__("pytest").MonkeyPatch
) -> None:
    from inframind.llm.client import LLMNetworkError

    def boom(prompt: str) -> str:
        raise LLMNetworkError("simulated outage")

    monkeypatch.setattr("inframind.llm.pipeline._build_call", lambda p: boom)

    cache = SummaryCache()
    summary, provenance = explain(
        incident, [ranked_candidate], scenario_id="scn_003", params=llm_params, cache=cache
    )
    assert provenance is not None
    assert provenance.status == "template_fallback"
    assert "Deterministic fallback" in summary.summary_text


def test_build_call_skips_primary_when_no_api_key(
    monkeypatch: __import__("pytest").MonkeyPatch,
) -> None:

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    params = LLMParams(provider="OPENAI")
    called = {"primary": 0, "fallback": 0}

    class _PrimaryClient:
        def summarise(self, _prompt: str) -> str:
            called["primary"] += 1
            return "primary"

    class _FallbackClient:
        def summarise(self, _prompt: str) -> str:
            called["fallback"] += 1
            return "fallback"

    # When no key is set, the pipeline must short-circuit the primary entirely.
    monkeypatch.setattr("inframind.llm.pipeline.OpenAIClient", lambda params, key: _PrimaryClient())
    monkeypatch.setattr("inframind.llm.pipeline.OllamaClient", lambda params: _FallbackClient())

    fn = _build_call(params)
    out = fn("anything")
    assert out == "fallback"
    assert called["primary"] == 0
    assert called["fallback"] == 1


def test_build_call_swaps_to_fallback_on_runtime_auth_error(
    monkeypatch: __import__("pytest").MonkeyPatch,
) -> None:

    monkeypatch.setenv("OPENAI_API_KEY", "sk-stale")
    params = LLMParams(provider="OPENAI")
    called = {"primary": 0, "fallback": 0}

    class _PrimaryClient:
        def summarise(self, _prompt: str) -> str:
            called["primary"] += 1
            from inframind.llm.client import LLMAuthenticationError

            raise LLMAuthenticationError("key rejected")

    class _FallbackClient:
        def summarise(self, _prompt: str) -> str:
            called["fallback"] += 1
            return "fallback-text"

    monkeypatch.setattr("inframind.llm.pipeline.OpenAIClient", lambda params, key: _PrimaryClient())
    monkeypatch.setattr("inframind.llm.pipeline.OllamaClient", lambda params: _FallbackClient())

    fn = _build_call(params)
    assert fn("p") == "fallback-text"
    assert called["primary"] == 1
    assert called["fallback"] == 1


def test_explain_rejects_empty_candidates(incident) -> None:
    import pytest as _pytest

    with _pytest.raises(ValueError, match="at least one ranked candidate"):
        explain(incident, [], scenario_id="scn_x")
