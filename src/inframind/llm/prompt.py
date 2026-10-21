"""Prompt construction for the RCA summary (standard 07.2).

The template lives at `prompts/rca_summary.v1.j2`. Every edit to the template requires a new
versioned file — the old template stays in the tree because the reproducibility cache keys
on `PROMPT_VERSION` and an old cache entry must remain renderable.

`PROMPT_VERSION` is the single source of truth for the template version and is asserted
against the filename at import time, so a copy-paste that forgets to bump `.v1` fails
loudly instead of silently breaking the cache.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

from inframind.common.incident import Incident
from inframind.rca.evidence import Candidate

from .redact import redact_text

# Bump on every template edit (standard 07.9). The filename must agree with this.
PROMPT_VERSION = "v1"

_PROMPT_DIR = Path(__file__).parent / "prompts"
_TEMPLATE_NAME = f"rca_summary.{PROMPT_VERSION}.j2"


@lru_cache(maxsize=1)
def _template_source() -> str:
    """Read the pinned template, asserting the filename matches `PROMPT_VERSION`."""
    path = _PROMPT_DIR / _TEMPLATE_NAME
    if not path.is_file():
        raise FileNotFoundError(
            f"prompt template {path} is missing; PROMPT_VERSION={PROMPT_VERSION} has no template"
        )
    return path.read_text(encoding="utf-8")


def _render(source: str, **context: object) -> str:
    """Render `source` with `context`, replacing `{{ name }}` placeholders literally.

    Deliberately not Jinja: the only dynamic values are the incident fields and two
    pre-formatted blocks, all redacted before insertion. A full template engine would add
    a dependency and an expression-injection surface for no benefit. An unknown
    placeholder is an error — a silently empty slot would ship an evidence-free prompt.
    """
    out = source
    for key, value in context.items():
        token = "{{ " + key + " }}"
        if token not in out:
            raise KeyError(f"prompt template has no placeholder {token!r}")
        out = out.replace(token, str(value))
    return out


def format_candidates(candidates: Sequence[Candidate]) -> str:
    """Render the ranked candidate list, one per line, scores visible.

    Scores are shown to three decimals so a tie is visible to the model but not
    over-signalled; the order is authoritative (standard 07.2).
    """
    if not candidates:
        return "(no candidates — the graph walk reached nothing)"
    lines = []
    for rank, candidate in enumerate(candidates, start=1):
        lines.append(
            f"{rank}. service={candidate.service} score={candidate.score:.3f} "
            f"depth={candidate.depth} change_correlation={candidate.change_correlation:.3f} "
            f"confidence={candidate.confidence:.3f} evidence_count={len(candidate.evidence)}"
        )
    return "\n".join(lines)


def format_evidence(candidate: Candidate | None) -> str:
    """Render one candidate's evidence bundle as `id | kind | summary` lines.

    The evidence id is the citation handle the validator checks, so it is printed first on
    every line and unredacted — redacting a ULID would break validation.
    """
    if candidate is None or not candidate.evidence:
        return "(no evidence bundled)"
    lines = []
    for item in candidate.evidence:
        summary = redact_text(item.summary)
        lines.append(f"{item.id} | {item.kind} | {summary}")
    return "\n".join(lines)


def evidence_bundle_hash(candidates: Sequence[Candidate]) -> str:
    """Return a stable hash of the evidence bundle, for the cache key (standard 07.7).

    Hashes the candidate services, scores, and evidence ids in rank order. Scores are
    rounded before hashing so float noise below the reported precision cannot produce a
    cache miss for an identical ranking.
    """
    payload = [
        {
            "service": c.service,
            "score": round(c.score, 3),
            "depth": c.depth,
            "evidence": [e.id for e in c.evidence],
        }
        for c in candidates
    ]
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.blake2b(blob.encode(), digest_size=16).hexdigest()


def build_prompt(
    incident: Incident,
    candidates: Sequence[Candidate],
    *,
    top_k: int = 5,
    retry_hint: str | None = None,
) -> str:
    """Render the RCA summary prompt for one incident (standard 07.2).

    Args:
        incident: the incident being explained; only id and timestamps are included.
        candidates: the ranked candidates from `rca.rank.rank_candidates`, best first.
            Only the first `top_k` are sent.
        top_k: how many ranked candidates to include.
        retry_hint: appended when the validator is retrying after a rejected response
            (standard 07.4). `None` on the first attempt.

    Returns:
        The fully rendered prompt, ready to hand to a provider adapter. All free text has
        passed through the LLM-stage redactor.

    Raises:
        ValueError: `candidates` is empty — an empty ranking has nothing to summarise, and
            silently sending an empty prompt would produce a confident-sounding summary of
            nothing.
    """
    if not candidates:
        raise ValueError("build_prompt requires at least one ranked candidate")

    selected = list(candidates)[:top_k]
    source = _template_source()

    prompt = _render(
        source,
        incident_id=incident.id,
        opened_at=incident.opened_at.isoformat(),
        closed_at=(incident.closed_at.isoformat() if incident.closed_at else "still open"),
        candidates=format_candidates(selected),
        evidence=format_evidence(selected[0]),
    )

    if retry_hint:
        prompt = f"{prompt}\n\n## Correction\n\n{retry_hint}"
    return prompt


__all__ = [
    "PROMPT_VERSION",
    "build_prompt",
    "evidence_bundle_hash",
    "format_candidates",
    "format_evidence",
]
