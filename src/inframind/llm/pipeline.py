"""The end-to-end explainer: cache lookup, redacted prompt, provider call, validation.

`explain()` is the single entry point the RCA worker (or the evaluation harness) uses. It
composes the stages defined in standards 07.2-07.7 in the order the standard requires:

    cache lookup -> build prompt -> validate -> call provider -> validate (x2) -> cache set

Two invariants hold on every call:

* **D9** — the prompt is redacted by `prompt.build_prompt` before the adapter sees it. The
  adapter never redacts; a single choke point means there is exactly one place to audit.
* **D1** — the LLM never picks. The ranked candidate order is passed through untouched;
  validation only checks citations.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence

from inframind.common.incident import Incident
from inframind.common.logging import get_logger
from inframind.metrics import observe_llm_call
from inframind.rca.evidence import Candidate

from .cache import SummaryCache, key_for_incident
from .client import (
    LLMError,
    LLMNetworkError,
    OllamaClient,
    OpenAIClient,
    with_fallback,
)
from .config import LLMParams, load_llm_params, should_fallback_to_ollama
from .prompt import PROMPT_VERSION, build_prompt
from .types import LLMSummary
from .validator import ValidationResult, summarise_with_validation, template_summary

logger = get_logger(__name__)

# Process-wide cache. Standard 07.7 wants the same (scenario, version, bundle, model) to
# return the same summary; an evaluation run shares this across scenarios.
_CACHE = SummaryCache()


def explain(
    incident: Incident,
    candidates: Sequence[Candidate],
    *,
    scenario_id: str,
    params: LLMParams | None = None,
    cache: SummaryCache | None = None,
) -> tuple[LLMSummary, ValidationResult | None]:
    """Summarise `candidates` for `incident`, returning (summary, provenance).

    `provenance` is None when the summary came from the cache, else the `ValidationResult`
    describing what the provider returned and whether it passed. Callers that write the
    audit row (standard 07.4.5) store `provenance.raw` and its status.

    The returned summary is always valid: the accepted LLM summary, or the deterministic
    template summary if both attempts fail or the provider is unreachable.
    """
    params = params or load_llm_params()
    store = cache if cache is not None else _CACHE

    if not candidates:
        raise ValueError("explain requires at least one ranked candidate")

    model = params.ollama_model if should_fallback_to_ollama(params) else params.openai_model
    key = key_for_incident(
        scenario_id=scenario_id,
        prompt_version=PROMPT_VERSION,
        candidates=candidates,
        model=model,
    )

    cached = store.get(key)
    if cached is not None:
        logger.info("llm summary served from cache", extra={"key": key, "scenario": scenario_id})
        # A cache hit still consumes wall-clock time in this process, but it is not an LLM
        # call. Recording it here keeps the histogram's `_count` equal to "explain() calls",
        # not "provider round-trips", which is the number the paper's latency column reports.
        observe_llm_call(0.0)
        return cached, None

    call = _build_call(params)
    prompt = build_prompt(incident, candidates, top_k=params.top_k)

    started = time.perf_counter()
    try:
        result = summarise_with_validation(
            lambda p, hint: call(prompt if hint is None else f"{prompt}\n\n{hint}"),
            prompt,
            candidates,
            provider=params.provider,
        )
    except LLMNetworkError as e:
        # Every provider path failed (both adapters, or one with no fallback available).
        # Degrade to the template summary rather than dropping the explanation (07.4.4).
        logger.warning(
            "llm provider unreachable; using deterministic template summary",
            extra={"provider": params.provider, "error": str(e), "scenario": scenario_id},
        )
        result = ValidationResult(
            summary=template_summary(candidates),
            raw="",
            status="template_fallback",
        )

    store.set(key, result.summary)
    observe_llm_call(time.perf_counter() - started)
    return result.summary, result


def _build_call(params: LLMParams) -> Callable[[str], str]:
    """Return a zero-arg-free callable `(prompt) -> str` honouring the D9 fallback.

    When the primary provider is OpenAI and the fallback condition holds (no API key), or
    the OpenAI adapter fails at runtime, Ollama is attempted as fallback. When Ollama is
    also unavailable the original error propagates for `explain` to degrade on.
    """
    import os

    env = os.environ

    if params.provider == "OLLAMA":
        client = OllamaClient(params)
        return client.summarise

    primary = OpenAIClient(params, env.get("OPENAI_API_KEY", "").strip())
    if should_fallback_to_ollama(params, env):
        # No key configured: skip the doomed primary call entirely.
        logger.info("no OPENAI_API_KEY; using the Ollama fallback (standard 07.6)")
        return OllamaClient(params).summarise
    return with_fallback(primary.summarise, OllamaClient(params).summarise)


__all__ = ["explain"]


# Re-exported for callers that catch adapter errors alongside the pipeline.
_LLM_ERRORS = (LLMError, LLMNetworkError)
