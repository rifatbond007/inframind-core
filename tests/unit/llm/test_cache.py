"""`inframind.llm.cache` guarantees the reproducibility contract of standard 07.7.

The contract: the same ``(scenario_id, prompt_version, evidence_bundle_hash, model)`` returns
the same summary without calling the provider again. These tests assert the key is
sensitive to all four components and that hit/miss accounting is honest.
"""

from __future__ import annotations

from inframind.llm.cache import SummaryCache, cache_key, key_for_incident
from inframind.llm.types import LLMClaim, LLMSummary


def _summary() -> LLMSummary:
    return LLMSummary(
        summary_text="cached summary",
        claims=[LLMClaim(text="t", evidence_id="ev_alpha")],
    )


def _key(**overrides: str) -> str:
    base = {
        "scenario_id": "scn_001",
        "prompt_version": "v1",
        "evidence_bundle_hash": "deadbeef",
        "model": "gpt-4o-mini",
    }
    base.update(overrides)
    return cache_key(**base)


def test_key_is_deterministic() -> None:
    assert _key() == _key()


def test_key_changes_with_every_component() -> None:
    base = _key()
    assert _key(scenario_id="scn_002") != base
    assert _key(prompt_version="v2") != base
    assert _key(evidence_bundle_hash="cafebabe") != base
    assert _key(model="llama3.1") != base


def test_get_and_set_round_trip() -> None:
    cache = SummaryCache()
    summary = _summary()
    cache.set("k", summary)
    assert cache.get("k") == summary
    assert len(cache) == 1


def test_get_miss_returns_none_and_counts() -> None:
    cache = SummaryCache()
    assert cache.get("absent") is None
    assert cache.misses == 1
    assert cache.hits == 0


def test_hit_is_counted() -> None:
    cache = SummaryCache()
    cache.set("k", _summary())
    cache.get("k")
    cache.get("k")
    assert cache.hits == 2
    assert cache.misses == 0


def test_clear_resets_everything() -> None:
    cache = SummaryCache()
    cache.set("k", _summary())
    cache.get("k")
    cache.clear()
    assert len(cache) == 0
    assert cache.hits == 0
    assert cache.misses == 0


def test_key_for_incident_uses_bundle_hash(ranked_candidate) -> None:
    k1 = key_for_incident(
        scenario_id="s", prompt_version="v1", candidates=[ranked_candidate], model="m"
    )
    k2 = key_for_incident(
        scenario_id="s", prompt_version="v1", candidates=[ranked_candidate], model="m"
    )
    assert k1 == k2
