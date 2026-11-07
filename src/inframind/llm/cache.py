"""Reproducibility cache for LLM summaries (standard 07.7).

Cache key: ``(scenario_id, prompt_version, evidence_bundle_hash, model)``. A hit returns
the cached summary without re-calling the LLM, so the same key always yields the same
summary regardless of when it ran. This is what makes the paper's LLM-summary numbers
reproducible.

The cache is an in-process dict for now. That is deliberate: the evaluation harness (P7)
runs scenarios in repeated subprocesses within one run, so a process-local cache captures
the reuse that matters while keeping the cache trivially correct (no invalidation, no
serialisation format, no staleness). If cross-process reuse is ever needed, the
`get`/`set` interface below is the seam to back with Redis or Postgres (D20: storage is the
source of truth, the bus is the conduit — a cache is neither).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from inframind.rca.evidence import Candidate

from .types import LLMSummary


def cache_key(
    *,
    scenario_id: str,
    prompt_version: str,
    evidence_bundle_hash: str,
    model: str,
) -> str:
    """Return the composite cache key as a hex digest (standard 07.7)."""
    blob = json.dumps(
        [scenario_id, prompt_version, evidence_bundle_hash, model],
        separators=(",", ":"),
    )
    return hashlib.blake2b(blob.encode(), digest_size=16).hexdigest()


@dataclass
class SummaryCache:
    """A process-local LLM summary cache keyed by the standard's four-part key."""

    _entries: dict[str, LLMSummary] = field(default_factory=dict, repr=False)
    hits: int = 0
    misses: int = 0

    def get(self, key: str) -> LLMSummary | None:
        """Return the cached summary for `key`, or None; updates the hit/miss counters."""
        found = self._entries.get(key)
        if found is None:
            self.misses += 1
            return None
        self.hits += 1
        return found

    def set(self, key: str, summary: LLMSummary) -> None:
        """Store `summary` under `key`."""
        self._entries[key] = summary

    def clear(self) -> None:
        """Drop every entry and reset the counters (test helper)."""
        self._entries.clear()
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        return len(self._entries)


def key_for_incident(
    *,
    scenario_id: str,
    prompt_version: str,
    candidates: Sequence[Candidate],
    model: str,
) -> str:
    """Convenience wrapper: build the key for a ranked candidate list."""
    from .prompt import evidence_bundle_hash

    return cache_key(
        scenario_id=scenario_id,
        prompt_version=prompt_version,
        evidence_bundle_hash=evidence_bundle_hash(candidates),
        model=model,
    )


__all__ = ["SummaryCache", "cache_key", "key_for_incident"]
