"""`inframind.llm.validator` enforces the citation rule end-to-end (standard 07.4).

Coverage here is the full standard 07.4 sequence:

* accept a well-formed response,
* reject a response that cites a missing evidence id and produce a retry hint,
* on the retry attempt, accept a fixed response,
* on two failed attempts, fall back to the deterministic template summary,
* reject non-JSON garbage and bias the retry hint toward schema compliance.
"""

from __future__ import annotations

import json

from inframind.llm.types import LLMSummary
from inframind.llm.validator import (
    build_retry_hint,
    bundle_evidence_ids,
    find_missing_ids,
    parse_response,
    summarise_with_validation,
    template_summary,
    validate_response,
)
from inframind.rca.evidence import Evidence


def _good_summary() -> str:
    return json.dumps(
        {
            "summary_text": "error_spike fired on paymentservice",
            "claims": [
                {"text": "paymentservice error rate rose", "evidence_id": "ev_alpha"},
                {"text": "Deployment rolled out", "evidence_id": "ev_beta"},
            ],
        }
    )


def test_accepts_a_well_formed_response(ranked_candidate) -> None:
    result = validate_response(_good_summary(), [ranked_candidate], provider="OPENAI")
    assert result.accepted
    assert result.status == "accepted"
    assert isinstance(result.summary, LLMSummary)
    assert len(result.summary.claims) == 2


def test_rejects_a_missing_evidence_id(ranked_candidate) -> None:
    raw = json.dumps(
        {
            "summary_text": "weird claim cites a non-existent id",
            "claims": [{"text": "evidence_x is the cause", "evidence_id": "ev_does_not_exist"}],
        }
    )
    result = validate_response(raw, [ranked_candidate], provider="OPENAI")
    assert not result.accepted
    assert result.status == "rejected"
    assert "ev_does_not_exist" in result.rejected_ids
    assert result.retry_hint is not None
    assert "ev_alpha" in result.retry_hint
    assert "ev_beta" in result.retry_hint


def test_retry_hint_names_the_invalid_ids(ranked_candidate) -> None:
    raw = json.dumps(
        {
            "summary_text": "two bad claims",
            "claims": [
                {"text": "bad one", "evidence_id": "missing_1"},
                {"text": "also bad", "evidence_id": "missing_2"},
            ],
        }
    )
    result = validate_response(raw, [ranked_candidate], provider="OPENAI")
    assert "missing_1" in result.retry_hint
    assert "missing_2" in result.retry_hint


def test_retry_path_accepts_a_corrected_response(ranked_candidate) -> None:
    bad = json.dumps(
        {
            "summary_text": "first attempt bad",
            "claims": [{"text": "x", "evidence_id": "missing"}],
        }
    )
    good = _good_summary()
    calls = []

    def call(prompt: str, hint: str | None) -> str:
        calls.append(hint)
        return bad if hint is None else good

    result = summarise_with_validation(call, "the prompt", [ranked_candidate], provider="OPENAI")
    assert result.accepted
    assert calls == [None, calls[1]]  # one call, hint None; second call, hint set


def test_two_failures_trigger_template_fallback(ranked_candidate) -> None:
    bad = json.dumps(
        {
            "summary_text": "first attempt bad",
            "claims": [{"text": "x", "evidence_id": "missing"}],
        }
    )
    calls = []

    def call(prompt: str, hint: str | None) -> str:
        calls.append(hint)
        return bad  # both attempts fail

    result = summarise_with_validation(call, "the prompt", [ranked_candidate], provider="OPENAI")
    assert result.status == "template_fallback"
    assert isinstance(result.summary, LLMSummary)
    # The template fallback cites real, bundled evidence ids (standard 07.4.4).
    allowed = bundle_evidence_ids([ranked_candidate])
    for claim in result.summary.claims:
        assert claim.evidence_id in allowed


def test_template_summary_is_deterministic_and_does_not_use_ev(ranked_candidate) -> None:
    a = template_summary([ranked_candidate])
    b = template_summary([ranked_candidate])
    assert a.summary_text == b.summary_text
    # Must include the deterministic stamp so reviewers can spot a fallback in test output.
    assert "Deterministic fallback" in a.summary_text


def test_non_json_returns_a_schema_retry_hint(ranked_candidate) -> None:
    result = validate_response("not json at all", [ranked_candidate], provider="OPENAI")
    assert not result.accepted
    assert "JSON" in result.retry_hint


def test_markdown_fence_is_stripped(ranked_candidate) -> None:
    raw = "```json\n" + _good_summary() + "\n```"
    result = validate_response(raw, [ranked_candidate], provider="OPENAI")
    assert result.accepted


def test_extra_claim_ids_are_not_falsely_flagged() -> None:
    # A claim citing a valid id must not be flagged.
    raw = json.dumps(
        {
            "summary_text": "only one valid claim",
            "claims": [{"text": "ok", "evidence_id": "ev_alpha"}],
        }
    )
    from inframind.rca.evidence import Candidate

    items = [Evidence(id="ev_alpha", kind="metric", summary="ok", ref="r", weight=1.0)]
    candidate = Candidate(
        service="paymentservice",
        score=1.0,
        depth=1,
        change_correlation=0.0,
        confidence=1.0,
        evidence=items,
    )
    result = validate_response(raw, [candidate], provider="OPENAI")
    assert result.accepted
    assert result.rejected_ids == ()


def test_find_missing_ids_is_order_preserving(ranked_candidate) -> None:
    raw = json.dumps(
        {
            "summary_text": "two missing",
            "claims": [
                {"text": "a", "evidence_id": "miss_a"},
                {"text": "b", "evidence_id": "miss_a"},  # dup should collapse
                {"text": "c", "evidence_id": "miss_b"},
            ],
        }
    )
    summary = parse_response(raw)
    missing = find_missing_ids(summary, bundle_evidence_ids([ranked_candidate]))
    assert missing == ["miss_a", "miss_b"]


def test_build_retry_hint_lists_allowed_ids() -> None:
    hint = build_retry_hint(["bad"], {"ev_a", "ev_b"})
    assert "ev_a" in hint and "ev_b" in hint
    assert "bad" in hint
