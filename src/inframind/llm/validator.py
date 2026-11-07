"""Response validation and the deterministic fallback (standard 07.4).

The validator is where the "the LLM does not pick the root cause" rule (D1) and the "every
claim cites real evidence" rule become mechanical. It never softens a hard rule: a claim
citing an evidence ID that is not in the bundle is **rejected**, not warned about.

Flow (standard 07.4):

1. JSON-schema validate the response against `LLMSummary`.
2. For each claim, check its `evidence_id` is in the bundle.
3. If a claim cites a missing ID, reject and retry once with a hint naming the bad ID.
4. If the retry also fails, fall back to a deterministic template summary.
5. Log every rejection at WARNING, with the provider and the rejected response.

The audit table stores the raw LLM output regardless of outcome (standard 07.4.5); that
write is the pipeline's job, not the validator's, so the validator stays a pure function
of (response, bundle) apart from logging.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Literal

from pydantic import ValidationError

from inframind.common.logging import get_logger
from inframind.rca.evidence import Candidate

from .types import LLMSummary

logger = get_logger(__name__)

_VALIDATION = Literal["accepted", "rejected", "template_fallback"]


class ValidationResult:
    """The outcome of validating one LLM response, plus the artifact to store.

    `summary` is always populated: either the accepted `LLMSummary` or the deterministic
    template summary. `raw` is always the provider's raw text (standard 07.4.5 — the audit
    table stores it regardless). `retry_hint` is populated only when the caller should
    retry, i.e. on a rejection.
    """

    def __init__(
        self,
        *,
        summary: LLMSummary,
        raw: str,
        status: _VALIDATION,
        rejected_ids: Sequence[str] = (),
        retry_hint: str | None = None,
    ) -> None:
        self.summary = summary
        self.raw = raw
        self.status = status
        self.rejected_ids = tuple(rejected_ids)
        self.retry_hint = retry_hint

    @property
    def accepted(self) -> bool:
        """True when the provider's own summary passed validation."""
        return self.status == "accepted"

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"ValidationResult(status={self.status!r}, claims={len(self.summary.claims)})"


def bundle_evidence_ids(candidates: Sequence[Candidate]) -> set[str]:
    """Return every evidence id across all candidates — the set claims may cite."""
    return {item.id for candidate in candidates for item in candidate.evidence}


def parse_response(raw: str) -> LLMSummary:
    """Parse `raw` into an `LLMSummary`, tolerating a markdown fence around the JSON.

    Raises:
        ValueError: `raw` is not JSON, or not a valid `LLMSummary`.
    """
    text = raw.strip()
    if text.startswith("```"):
        # Strip a ```json ... ``` fence if the model added one despite the prompt.
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip("`").strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM response is not valid JSON: {e}") from e
    try:
        return LLMSummary.model_validate(payload)
    except ValidationError as e:
        raise ValueError(f"LLM response does not match LLMSummary: {e}") from e


def find_missing_ids(summary: LLMSummary, allowed: set[str]) -> list[str]:
    """Return the cited evidence IDs that are NOT in `allowed`, de-duplicated in order."""
    missing: list[str] = []
    for claim in summary.claims:
        if claim.evidence_id not in allowed and claim.evidence_id not in missing:
            missing.append(claim.evidence_id)
    return missing


def build_retry_hint(missing_ids: Sequence[str], allowed: set[str]) -> str:
    """Build the hint sent with the single retry (standard 07.4.3)."""
    bad = ", ".join(missing_ids)
    valid = ", ".join(sorted(allowed)) or "(none)"
    return (
        f"your previous response cited evidence ID(s) {bad} that are not in the bundle. "
        f"Every claim must cite one of these IDs exactly: {valid}. "
        f"Re-answer using only these IDs, or drop the claims you cannot support."
    )


def template_summary(candidates: Sequence[Candidate]) -> LLMSummary:
    """Return the deterministic single-sentence summary used when both attempts fail.

    Derived only from the top candidate and its evidence list — no LLM text survives here,
    which is what makes the fallback reproducible (standard 07.4.4).
    """
    top = candidates[0]
    evidence = ", ".join(item.summary for item in top.evidence[:3]) or "no bundled evidence"
    text = (
        f"Deterministic fallback: the ranked top candidate is {top.service} "
        f"(score={top.score:.3f}, depth={top.depth}), supported by: {evidence}."
    )
    from .types import LLMClaim

    claims = [LLMClaim(text=item.summary, evidence_id=item.id) for item in top.evidence[:3]]
    return LLMSummary(summary_text=text, claims=claims)


def validate_response(
    raw: str,
    candidates: Sequence[Candidate],
    *,
    provider: str = "unknown",
    is_retry: bool = False,
) -> ValidationResult:
    """Validate one raw LLM response against the bundle (standard 07.4).

    Args:
        raw: the provider's raw text reply.
        candidates: the ranked candidates whose evidence bundle was sent to the LLM.
        provider: the provider name, for the WARNING log (standard 07.4.5).
        is_retry: True when this is the second (final) attempt after a rejection. On a
            second failure the result is the deterministic template fallback instead of
            another retry hint.

    Returns:
        A `ValidationResult`. `status` is one of accepted / rejected / template_fallback.
    """
    allowed = bundle_evidence_ids(candidates)

    try:
        summary = parse_response(raw)
    except ValueError as e:
        logger.warning(
            "llm response failed schema validation",
            extra={"provider": provider, "error": str(e), "raw": raw[:500]},
        )
        if is_retry:
            return ValidationResult(
                summary=template_summary(candidates),
                raw=raw,
                status="template_fallback",
            )
        return ValidationResult(
            summary=template_summary(candidates),
            raw=raw,
            status="rejected",
            retry_hint=(
                "your previous response was not valid JSON matching the required schema "
                "(summary_text: string, claims: list of {text, evidence_id}). "
                "Return a single JSON object and nothing else."
            ),
        )

    missing = find_missing_ids(summary, allowed)
    if missing:
        logger.warning(
            "llm response cited evidence ids not present in the bundle",
            extra={
                "provider": provider,
                "rejected_ids": missing,
                "allowed_count": len(allowed),
                "raw": raw[:500],
            },
        )
        if is_retry:
            return ValidationResult(
                summary=template_summary(candidates),
                raw=raw,
                status="template_fallback",
                rejected_ids=missing,
            )
        return ValidationResult(
            summary=template_summary(candidates),
            raw=raw,
            status="rejected",
            rejected_ids=missing,
            retry_hint=build_retry_hint(missing, allowed),
        )

    return ValidationResult(summary=summary, raw=raw, status="accepted")


def summarise_with_validation(
    call: Callable[[str, str | None], str],
    prompt: str,
    candidates: Sequence[Candidate],
    *,
    provider: str = "unknown",
) -> ValidationResult:
    """Call `call(prompt, retry_hint)` up to twice, validating each response.

    `call` takes the prompt and an optional retry hint (or None on the first attempt).
    This is the retry orchestration from standard 07.4; it is separated from
    `validate_response` so the latter stays a pure function that is trivial to test.

    Returns the first accepted result, or the template-fallback result if both attempts
    are rejected.
    """
    first_raw = call(prompt, None)
    first = validate_response(first_raw, candidates, provider=provider, is_retry=False)
    if first.accepted:
        return first

    second_raw = call(prompt, first.retry_hint)
    second = validate_response(second_raw, candidates, provider=provider, is_retry=True)
    return second


__all__ = [
    "ValidationResult",
    "build_retry_hint",
    "bundle_evidence_ids",
    "find_missing_ids",
    "parse_response",
    "summarise_with_validation",
    "template_summary",
    "validate_response",
]
