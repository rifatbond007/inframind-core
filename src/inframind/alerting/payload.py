"""The alert payload: what a human actually reads when the page fires.

`build_payload` is a pure function. It takes the incident, the ranked candidates, the
optional LLM summary, and returns a dict shaped per surface-map's `Alert.payload`. The
rules:

* **`summary_text` is redacted again before it goes out.** The LLM stage already
  redacted before *sending* to the model, but the model's *output* is new text and must
  not be trusted verbatim — the redactor runs again here (D9, belt-and-braces).
* **`top_candidate` carries the evidence ids**, so a reader can click through to the
  evidence rows in Postgres. It never carries raw log lines.
* **`blast_radius`** is the set of services downstream of the primary (they will feel
  the pain) — computed from the candidates' depths, best-first. The service that itself
  is the primary has depth 0 and is not in its own blast radius.
* **No ranking is performed here.** The candidates arrive already ranked from
  `rca/rank.py`; the payload mirrors that order verbatim. The LLM summary (D1) also
  never changes the ranking — it is presentation only.

`links` is a placeholder dict for P8 (dashboard / runbook URLs are not yet deployed).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from inframind.common.incident import Incident
from inframind.common.logging import get_logger
from inframind.llm.redact import redact_text
from inframind.rca.evidence import Candidate

logger = get_logger(__name__)


def _top_candidate_payload(candidate: Candidate | None) -> dict[str, Any] | None:
    if candidate is None:
        return None
    return {
        "service": candidate.service,
        "score": round(candidate.score, 4),
        "depth": candidate.depth,
        "confidence": round(candidate.confidence, 4),
        "change_correlation": round(candidate.change_correlation, 4),
        "evidence_ids": [item.id for item in candidate.evidence],
    }


def _blast_radius(candidates: Sequence[Candidate]) -> list[str]:
    """Services downstream of the primary, best-first, excluding the primary itself."""
    return [c.service for c in candidates[1:]]


def build_payload(
    incident: Incident,
    candidates: Sequence[Candidate],
    *,
    summary_text: str | None = None,
    audit_id: str | None = None,
) -> dict[str, Any]:
    """Return the alert payload for one incident.

    Args:
        incident: the incident being alerted on.
        candidates: the ranked candidates from `rca/rank.py`, best first.
        summary_text: the LLM summary, if the explainer ran. Redacted here.
        audit_id: the `audit_log` row id for this alert, for forensics.

    Returns:
        A JSON-serialisable dict matching surface-map's `Alert.payload` shape:
        `incident_id`, `state`, `severity`, `services`, `top_candidate`,
        `blast_radius`, `summary_text`, `links`, `audit_id`, `opened_at`.
    """
    primary = candidates[0] if candidates else None
    safe_summary = redact_text(summary_text) if summary_text else None
    return {
        "incident_id": incident.id,
        "state": incident.state,
        "severity": incident.severity,
        "title": incident.title,
        "services": list(incident.services),
        "top_candidate": _top_candidate_payload(primary),
        "blast_radius": _blast_radius(candidates),
        "summary_text": safe_summary,
        "opened_at": incident.opened_at.isoformat(),
        "closed_at": incident.closed_at.isoformat() if incident.closed_at else None,
        # P8 will populate real dashboard / runbook URLs; empty placeholders for now so
        # the shape is stable across the phases.
        "links": {"dashboard": None, "runbook": None},
        "audit_id": audit_id,
    }


__all__ = ["build_payload"]
