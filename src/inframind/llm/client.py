"""LLM provider adapters (standard 07.6, 07.7).

The contract every adapter implements:

    def summarise(self, prompt: str) -> str: ...

The validator runs *after* the adapter — never inside it (standard 07.6). Adapters
**never** apply redaction; that is the pipeline's job, after the prompt is built and
before the adapter is called (so a prompt that comes from a test fixture without
redaction would still call the adapter over redacted text).

Provider selection:

* ``OPENAI`` — calls the OpenAI API. Requires ``OPENAI_API_KEY``. ``temperature=0``.
* ``OLLAMA`` — calls a local Ollama endpoint. No API key. ``temperature=0``.

The fallback path (standard 07.6) is implemented in `pipeline.py`. This module is
deliberately side-effect free at import: the OpenAI SDK is imported lazily inside the
adapter, so unit tests that mock the adapter do not require ``openai`` to be installed.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Protocol

import httpx

from .config import LLMParams

# `temperature=0` is a hard rule of standard 07.7 and lives here as a constant so a
# reviewer can grep for it.
TEMPERATURE = 0.0


class LLMError(RuntimeError):
    """Base class for adapter-side failures. The pipeline catches these to drive fallback."""


class LLMAuthenticationError(LLMError):
    """OpenAI rejected the API key (HTTP 401). Triggers Ollama fallback (standard 07.6)."""


class LLMNetworkError(LLMError):
    """The provider endpoint was unreachable or timed out. Triggers fallback."""


class LLMClient(Protocol):
    """The contract every provider adapter implements."""

    model: str

    def summarise(self, prompt: str) -> str:
        """Return the raw text reply for `prompt`; validation is downstream."""
        ...


# --- OpenAI adapter --------------------------------------------------------------


class OpenAIClient:
    """OpenAI adapter. Lazily imports the official SDK so unit tests do not need it."""

    def __init__(self, params: LLMParams, api_key: str) -> None:
        self._params = params
        self._api_key = api_key
        self.model = params.openai_model

    def summarise(self, prompt: str) -> str:
        if not self._api_key:
            raise LLMAuthenticationError("OPENAI_API_KEY is empty; refusing to call OpenAI")
        try:
            from openai import AuthenticationError as _OpenAIAuthError
            from openai import OpenAI
        except ImportError as e:  # pragma: no cover - exercised only when SDK is missing
            raise LLMNetworkError("the openai package is not installed") from e

        client = OpenAI(
            api_key=self._api_key,
            base_url=self._params.openai_base_url,
            timeout=self._params.request_timeout_s,
        )
        try:
            response = client.chat.completions.create(
                model=self.model,
                temperature=TEMPERATURE,
                messages=[{"role": "user", "content": prompt}],
            )
        except _OpenAIAuthError as e:
            raise LLMAuthenticationError("OpenAI rejected the API key") from e
        except Exception as e:  # openai SDK raises broad exceptions on network errors
            raise LLMNetworkError(f"OpenAI call failed: {e}") from e

        if not response.choices:
            raise LLMError("OpenAI returned an empty choices list")
        return response.choices[0].message.content or ""


# --- Ollama adapter --------------------------------------------------------------


class OllamaClient:
    """Ollama adapter. Uses httpx directly; no extra SDK needed."""

    def __init__(self, params: LLMParams) -> None:
        self._params = params
        self.model = params.ollama_model

    def summarise(self, prompt: str) -> str:
        url = f"{self._params.ollama_base_url.rstrip('/')}/api/chat"
        payload = {
            "model": self.model,
            "stream": False,
            "options": {"temperature": TEMPERATURE},
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            response = httpx.post(url, json=payload, timeout=self._params.request_timeout_s)
        except httpx.HTTPError as e:
            raise LLMNetworkError(f"Ollama call failed: {e}") from e
        if response.status_code == 401 or response.status_code == 403:
            # Ollama does not auth, but mirror the shape so a future auth model is uniform.
            raise LLMAuthenticationError(f"Ollama rejected the request: {response.status_code}")
        if response.status_code >= 500:
            raise LLMNetworkError(f"Ollama returned {response.status_code}: {response.text[:200]}")
        if response.status_code >= 400:
            raise LLMError(f"Ollama returned {response.status_code}: {response.text[:200]}")
        try:
            body = response.json()
        except json.JSONDecodeError as e:
            raise LLMNetworkError(f"Ollama returned non-JSON: {e}") from e
        message = body.get("message") or {}
        content = message.get("content")
        if not isinstance(content, str) or not content:
            raise LLMError("Ollama returned an empty message content")
        return content


# --- Factory --------------------------------------------------------------------


def build_client(params: LLMParams, env: dict[str, str] | None = None) -> LLMClient:
    """Return the right adapter for `params.provider`.

    `env` lets tests inject an env mapping; defaults to ``os.environ``.
    """
    import os

    source = os.environ if env is None else env
    if params.provider == "OPENAI":
        api_key = source.get("OPENAI_API_KEY", "").strip()
        return OpenAIClient(params, api_key)
    if params.provider == "OLLAMA":
        return OllamaClient(params)
    raise ValueError(f"unknown provider {params.provider!r}; expected OPENAI or OLLAMA")


def with_fallback(
    primary: LLMClient | Callable[[str], str],
    fallback: LLMClient | Callable[[str], str],
) -> Callable[[str], str]:
    """Wrap `primary` so LLMAuthenticationError/NetworkError triggers `fallback`.

    Accepts either an `LLMClient` adapter or a bare `str -> str` callable (a bound
    `summarise`), because the pipeline passes bound methods.

    The fallback result is returned. If the fallback also fails, the original primary
    error is re-raised so the validator can decide between retry and template fallback.
    """

    def _as_callable(client: LLMClient | Callable[[str], str]) -> Callable[[str], str]:
        # Duck-typed rather than isinstance: LLMClient is a Protocol and a bare callable
        # is the common case (a bound `summarise`).
        summarise = getattr(client, "summarise", None)
        return summarise if callable(summarise) else client  # type: ignore[return-value]

    primary_call: Callable[[str], str] = _as_callable(primary)
    fallback_call: Callable[[str], str] = _as_callable(fallback)

    def call(prompt: str) -> str:
        try:
            return primary_call(prompt)
        except (LLMAuthenticationError, LLMNetworkError) as primary_error:
            try:
                return fallback_call(prompt)
            except (LLMAuthenticationError, LLMNetworkError) as fallback_error:
                raise LLMNetworkError(
                    f"primary failed ({primary_error}); fallback also failed ({fallback_error})"
                ) from fallback_error

    return call


__all__ = [
    "TEMPERATURE",
    "LLMAuthenticationError",
    "LLMClient",
    "LLMError",
    "LLMNetworkError",
    "OllamaClient",
    "OpenAIClient",
    "build_client",
    "with_fallback",
]
