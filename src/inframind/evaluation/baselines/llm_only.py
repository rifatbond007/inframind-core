"""`llm_only` baseline (D7, D1's control).

Ask the LLM to name the root cause from the evidence alone, with no graph, no ranker.
Parse the service name out of the response and put it first.

D1 says the LLM never picks the root cause in InfraMind's pipeline — it explains a ranking
the graph already made. This baseline exists to *measure* the gap: it should score near
chance. If it scores high, that is a finding about the eval, not a bug — report it rather
than deleting the row.

The provider is reached through the same `explain` pipeline the real stage uses, so the
prompt, redaction, and validation are identical. Only the *input* differs: no ranked
candidates, so the model has to decide for itself.
"""

from __future__ import annotations

import re

from .base import BaselineInput, _validate_nonempty

#: Matches a service name anywhere in the LLM's free-text reply. Kebab-case tokens only,
#: so ordinary English words ("the", "was") never match.
_SERVICE_RE = re.compile(r"\b([a-z][a-z0-9]*(?:-[a-z0-9]+)+)\b")


def extract_service(summary: str, known: frozenset[str] | set[str]) -> str | None:
    """Return the first service in `known` mentioned in `summary`, else None.

    Prefers an exact word-boundary match of a known service; falls back to scanning the
    generic kebab-case pattern in case the model wrote a compound name.
    """
    for match in _SERVICE_RE.finditer(summary.lower()):
        token = match.group(1)
        if token in known:
            return token
    lowered = summary.lower()
    for service in sorted(known):
        if service in lowered:
            return service
    return None


class LLmOnlyBaseline:
    """Rank by whatever service the LLM names first; fall back to alphabetical order."""

    name = "llm_only"

    def rank(self, input: BaselineInput, *, summary_text: str | None = None) -> list[str]:
        """Return the services with the LLM's pick first, then the rest alphabetically.

        Args:
            input: the baseline input (must be non-empty).
            summary_text: the LLM's reply. If omitted, `input.summary_text` is used.
        """
        _validate_nonempty(input.services)
        text = summary_text if summary_text is not None else input.summary_text
        if not text:
            return sorted(input.services)

        picked = extract_service(text, input.service_set)
        if picked is None:
            return sorted(input.services)
        return [picked, *sorted(s for s in input.services if s != picked)]


__all__ = ["LLmOnlyBaseline", "extract_service"]
