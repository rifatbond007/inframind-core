"""LLM tunables and their `INFRAMIND_LLM_<PARAM>` overrides (standard 07).

Defaults come from `docs/standards/07-llm-explainer.md`. Two hard rules govern this file:

* **D7.6: temperature is pinned to 0**. The dataclass has no field for it on purpose — a
  runtime override is impossible because the field does not exist.
* **D9: provider selection**. ``OPENAI`` is the default; ``OLLAMA`` is the local fallback
  when ``OPENAI_API_KEY`` is empty/missing, the OpenAI adapter raises an auth or network
  error, or the operator sets ``LLM_PROVIDER=OLLAMA`` explicitly.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Mapping
from dataclasses import dataclass

from inframind.common.config import load_prefixed


@dataclass(frozen=True)
class LLMParams:
    """LLM explainer tunables (standard 07). All overridable via `INFRAMIND_LLM_<PARAM>`."""

    # Provider. Uppercased at use sites; validation lives in `provider()` below.
    provider: str = "OPENAI"

    # Models. Defaults are the OpenAI / Ollama stable small models at the time of writing
    # (D7.7: pinned for reproducibility). Override only with a paper note.
    openai_model: str = "gpt-4o-mini"
    ollama_model: str = "llama3.1:8b-instruct-q5_K_M"

    # Endpoints. Defaults are the public OpenAI endpoint and the local Ollama default port.
    openai_base_url: str = "https://api.openai.com/v1"
    ollama_base_url: str = "http://localhost:11434"

    # Timeouts and retry policy. The validator already has its own retry-once-with-hint
    # (standard 07.4); this is the transport-layer budget for a single provider call.
    request_timeout_s: float = 15.0
    max_retries: int = 0  # validator retries with a hint; transport retries are off

    # Top-N from `rca/rank.py` that go into the prompt (standard 07.2). Larger bundles
    # cost more tokens but give the LLM more evidence to ground its claims. Tie-break to
    # `rca.config.RCAParams.top_k` (5) by default.
    top_k: int = 5


def load_llm_params(env: Mapping[str, str] | None = None) -> LLMParams:
    """Build `LLMParams` from env (`os.environ` when `env` is None).

    `LLM_PROVIDER` is read directly here (it has no underscore in the field name) so the
    standard `INFRAMIND_LLM_PROVIDER` overload still works — `load_prefixed` lower-cases
    nothing on its own.
    """
    source = os.environ if env is None else env

    # `load_prefixed` will pick up `INFRAMIND_LLM_PROVIDER` because the field is named
    # `provider`; we let it handle the override and validate below.
    params = load_prefixed(LLMParams, "INFRAMIND_LLM", env)

    # `LLM_PROVIDER` (without the `INFRAMIND_` prefix) is the contract from standard 07.6;
    # honour it when set so operators do not need to learn two names.
    explicit = source.get("LLM_PROVIDER")
    if explicit:
        params = dataclasses.replace(params, provider=explicit.strip().upper())

    return _validate(params)


def _validate(params: LLMParams) -> LLMParams:
    """Reject unknown providers and non-positive tunables; raise with a clear message."""
    allowed = {"OPENAI", "OLLAMA"}
    if params.provider not in allowed:
        raise ValueError(
            f"LLM_PROVIDER={params.provider!r} is not supported; use one of {sorted(allowed)}"
        )
    if params.top_k <= 0:
        raise ValueError(f"top_k={params.top_k} must be a positive integer")
    if params.request_timeout_s <= 0:
        raise ValueError(
            f"request_timeout_s={params.request_timeout_s} must be a positive number of seconds"
        )
    if params.max_retries < 0:
        raise ValueError(f"max_retries={params.max_retries} must be >= 0")
    return params


def should_fallback_to_ollama(params: LLMParams, env: Mapping[str, str] | None = None) -> bool:
    """Return True when `OLLAMA` should be tried (standard 07.6).

    Three triggers, OR-ed:

    1. Operator pin: ``LLM_PROVIDER=OLLAMA`` (already reflected in `params.provider`).
    2. No API key: ``OPENAI_API_KEY`` is unset or empty.
    3. The OpenAI adapter raised at runtime — that case is handled by `client.call_*`,
       not here, because it needs the exception class.
    """
    if params.provider == "OLLAMA":
        return True
    source = os.environ if env is None else env
    return not source.get("OPENAI_API_KEY", "").strip()


__all__ = [
    "LLMParams",
    "load_llm_params",
    "should_fallback_to_ollama",
]
