"""`inframind.llm.prompt` renders the pinned template with a fixture bundle.

Three things are enforced here (standard 07.2, 07.7):

1. The template renders — no placeholder is left un-substituted.
2. The ranked order is authoritative and survives into the prompt, unchanged (D1).
3. `evidence_bundle_hash` is deterministic and score-noise tolerant, because it is half
   of the cache key.
"""

from __future__ import annotations

import pytest

from inframind.common.incident import Incident
from inframind.llm.prompt import (
    PROMPT_VERSION,
    build_prompt,
    evidence_bundle_hash,
    format_candidates,
    format_evidence,
)
from inframind.rca.evidence import Candidate, Evidence


def _candidate(service: str, score: float, ev_id: str) -> Candidate:
    return Candidate(
        service=service,
        score=score,
        depth=1,
        change_correlation=0.5,
        confidence=1.0,
        evidence=[
            Evidence(
                id=ev_id,
                kind="metric",
                summary=f"error_spike fired on {service} (score=1.00)",
                ref="sig_x",
                weight=1.0,
            )
        ],
    )


def test_renders_with_fixture_bundle(incident: Incident, ranked_candidate: Candidate) -> None:
    prompt = build_prompt(incident, [ranked_candidate])
    assert incident.id in prompt
    assert "paymentservice" in prompt
    # No un-substituted placeholder survived.
    assert "{{" not in prompt
    assert "}}" not in prompt


def test_ranked_order_is_preserved(incident: Incident, ranked_candidate: Candidate) -> None:
    second = _candidate("currencyservice", 0.5, "ev_gamma")
    prompt = build_prompt(incident, [ranked_candidate, second])
    # paymentservice (rank 1) must appear before currencyservice (rank 2).
    assert prompt.index("paymentservice") < prompt.index("currencyservice")


def test_top_k_is_respected(incident: Incident, ranked_candidate: Candidate) -> None:
    candidates = [ranked_candidate] + [_candidate(f"svc{i}", 0.1 * i, f"ev_{i}") for i in range(5)]
    prompt = build_prompt(incident, candidates, top_k=2)
    rendered = format_candidates(candidates[:2])
    assert rendered in prompt
    # svc4 is beyond top_k and must not appear.
    assert "svc4" not in prompt


def test_evidence_ids_are_printed_unredacted(ranked_candidate: Candidate) -> None:
    rendered = format_evidence(ranked_candidate)
    assert "ev_alpha" in rendered
    assert "ev_beta" in rendered
    assert "ev_alpha | metric |" in rendered


def test_empty_evidence_renders_a_placeholder(ranked_candidate: Candidate) -> None:
    bare = ranked_candidate.model_copy(update={"evidence": []})
    assert format_evidence(bare) == "(no evidence bundled)"


def test_retry_hint_is_appended(incident: Incident, ranked_candidate: Candidate) -> None:
    base = build_prompt(incident, [ranked_candidate])
    with_hint = build_prompt(
        incident, [ranked_candidate], retry_hint="your previous response was invalid"
    )
    assert with_hint.startswith(base)
    assert "## Correction" in with_hint
    assert "your previous response was invalid" in with_hint


def test_empty_candidates_is_rejected(incident: Incident) -> None:
    with pytest.raises(ValueError, match="at least one ranked candidate"):
        build_prompt(incident, [])


def test_evidence_summary_is_redacted(incident: Incident) -> None:
    leaky = Evidence(
        id="ev_leak",
        kind="log_ref",
        summary="failed for alice@example.com with token=eyJhbGciOiJIUzI1NiJ9.body.sig",
        ref="sig_leak",
        weight=1.0,
    )
    candidate = Candidate(
        service="paymentservice",
        score=1.0,
        depth=1,
        change_correlation=0.5,
        confidence=1.0,
        evidence=[leaky],
    )
    prompt = build_prompt(incident, [candidate])
    assert "alice@example.com" not in prompt
    assert "ev_leak" in prompt  # the citation handle survives


def test_bundle_hash_is_deterministic(ranked_candidate: Candidate) -> None:
    a = evidence_bundle_hash([ranked_candidate])
    b = evidence_bundle_hash([ranked_candidate])
    assert a == b


def test_bundle_hash_ignores_float_noise(ranked_candidate: Candidate) -> None:
    noisy = ranked_candidate.model_copy(update={"score": ranked_candidate.score + 1e-12})
    assert evidence_bundle_hash([noisy]) == evidence_bundle_hash([ranked_candidate])


def test_bundle_hash_changes_with_order(ranked_candidate: Candidate) -> None:
    other = _candidate("currencyservice", 0.5, "ev_gamma")
    assert evidence_bundle_hash([ranked_candidate, other]) != evidence_bundle_hash(
        [other, ranked_candidate]
    )


def test_prompt_version_is_pinned() -> None:
    assert PROMPT_VERSION == "v1"
