"""The contract between the LLM stage and the rest of InfraMind (standard 07.3, 07.4).

`LLMSummary` is what the LLM is expected to produce; every `LLMClaim.text` MUST cite an
`evidence_id` that appears in the bundle. The validator (see `validator.py`) enforces that
on the wire — a model whose schema is too lax would let the rule slip.

The models are frozen pydantic, matching the rest of `common/` and `rca/`. The shape is
deliberately small: a single summary string and a list of cited claims. Anything richer
(lists of mitigation steps, suggested runbook links, etc.) belongs in a later phase and
should land as additional fields, not as untyped strings.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class LLMClaim(BaseModel):
    """One factual statement in the LLM summary, grounded in an evidence item.

    `evidence_id` must reference an evidence item the validator can find in the bundle
    that was sent to the LLM. A claim without a resolvable ID is rejected (standard 07.4).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)


class LLMSummary(BaseModel):
    """The structured output of an LLM summary call.

    `summary_text` is the human-readable narrative (markdown is fine but not required);
    `claims` is the list of cited facts the validator pinned it to.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary_text: str = Field(min_length=1)
    claims: list[LLMClaim] = Field(default_factory=list)


__all__ = ["LLMClaim", "LLMSummary"]
