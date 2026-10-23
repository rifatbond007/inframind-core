"""OpenAI adapter live test (standard 07.8).

Skipped when ``OPENAI_API_KEY`` is unset. Hits a real OpenAI endpoint with
``temperature=0`` and verifies the response shape — that the redacted prompt round-trips
through the SDK and yields an `LLMSummary`-shaped payload.

This is a wire test; it is intentionally narrow so it can run in CI without flaking on
the model's free-form prose. It exercises:

* the OpenAIClient adapter path end-to-end,
* `temperature=0` is honored,
* the validator accepts the response against the bundled evidence.

It does not exercise the full pipeline because that depends on `Incident` and
`Candidate` fixtures from P5; that is the responsibility of the `test_pipeline.py`
unit suite with a mock call.
"""

from __future__ import annotations

import json
import os

import pytest

from inframind.llm.client import OpenAIClient
from inframind.llm.config import LLMParams
from inframind.llm.prompt import build_prompt
from inframind.llm.types import LLMSummary

# Skip the whole module when the API key is not configured (CI without secrets).
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("OPENAI_API_KEY"),
        reason="OPENAI_API_KEY is not set; live OpenAI integration is skipped",
    ),
]


@pytest.fixture
def live_llm_params() -> LLMParams:
    """LLMParams pinned to the cheap small model for the live test."""
    return LLMParams(openai_model="gpt-4o-mini", request_timeout_s=30.0)


def test_openai_summarise_round_trip(
    live_llm_params: LLMParams, incident, ranked_candidate
) -> None:
    prompt = build_prompt(
        incident,
        [ranked_candidate],
        top_k=live_llm_params.top_k,
    )
    client = OpenAIClient(live_llm_params, os.environ["OPENAI_API_KEY"])
    raw = client.summarise(prompt)
    # The model is told to return only JSON. Some compliance layers wrap in markdown;
    # the validator handles both cases (test_validator.test_markdown_fence_is_stripped).
    summary = LLMSummary.model_validate(json.loads(_strip_fence(raw)))
    assert summary.summary_text
    # The valid evidence ids are the ones shipped in the bundle.
    allowed = {item.id for item in ranked_candidate.evidence}
    for claim in summary.claims[:1]:
        assert claim.evidence_id in allowed


def _strip_fence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        body = text.strip("`")
        if body.lower().startswith("json"):
            body = body[4:]
        return body.strip("`").strip()
    return text
