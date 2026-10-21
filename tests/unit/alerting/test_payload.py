"""Tests for `alerting.payload.build_payload` — pure function, no I/O.

Asserts the surface-map shape, the D9 re-redaction of the LLM summary, and that the
ranking order from the ranker is preserved (the alerting layer never reorders).
"""

from __future__ import annotations

from datetime import UTC, datetime

from inframind.alerting.payload import build_payload
from inframind.common.incident import Incident
from inframind.common.signal import Severity
from inframind.rca.evidence import Candidate, Evidence


def _incident() -> Incident:
    return Incident(
        id="inc_test_001",
        revision=1,
        state="open",
        severity=Severity.WARNING,
        title="cart latency spike",
        fingerprint="0123abcd",
        opened_at=datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC),
        services=["frontend"],
        signals=[],
        anomalies=[],
    )


def _candidate(
    service: str, score: float, *, with_evidence: list[Evidence] | None = None
) -> Candidate:
    return Candidate(
        service=service,
        score=score,
        depth=1 if service != "frontend" else 0,
        change_correlation=0.4,
        confidence=0.7,
        evidence=with_evidence or [],
    )


def test_build_payload_carries_top_candidate_and_blast_radius() -> None:
    """`top_candidate` is the best candidate; `blast_radius` is the rest, best-first."""
    e1 = Evidence(kind="metric", summary="latency p99 2.1s", ref="sig_x", weight=1.0)
    primary = _candidate("paymentservice", 0.91, with_evidence=[e1])
    secondary = _candidate("cartservice", 0.42)
    payload = build_payload(_incident(), [primary, secondary])

    assert payload["top_candidate"]["service"] == "paymentservice"
    assert payload["top_candidate"]["evidence_ids"] == [e1.id]
    assert payload["blast_radius"] == ["cartservice"]


def test_build_payload_reredsactes_summary_text() -> None:
    """D9 belt-and-braces: secrets in the LLM summary are redacted a second time."""
    leak = "Server returned 401 for token=sk-LIVE-abc123def456ghi789jkl012mno"
    payload = build_payload(_incident(), [_candidate("a", 0.5)], summary_text=leak)
    assert payload["summary_text"] is not None
    assert "sk-LIVE" not in payload["summary_text"]  # type: ignore[operator]


def test_build_payload_returns_none_for_top_when_no_candidates() -> None:
    """An empty candidate list yields a `None` top, not a crash."""
    payload = build_payload(_incident(), [])
    assert payload["top_candidate"] is None
    assert payload["blast_radius"] == []


def test_build_payload_does_not_reorder_candidates() -> None:
    """The ranker's order is the alerting order; we mirror, not re-rank."""
    ranked = [_candidate("cartservice", 0.99), _candidate("paymentservice", 0.10)]
    payload = build_payload(_incident(), ranked)
    assert payload["top_candidate"]["service"] == "cartservice"
    assert payload["blast_radius"] == ["paymentservice"]


def test_build_payload_includes_audit_id_when_provided() -> None:
    """`audit_id` round-trips into the payload for forensic correlation."""
    payload = build_payload(_incident(), [_candidate("a", 0.5)], audit_id="audit_42")
    assert payload["audit_id"] == "audit_42"


def test_build_payload_links_are_placeholders() -> None:
    """Dashboard / runbook URLs are P8 work; payload shape must still be stable."""
    payload = build_payload(_incident(), [_candidate("a", 0.5)])
    assert payload["links"] == {"dashboard": None, "runbook": None}


def test_build_payload_serialises_opened_at_as_iso() -> None:
    """`opened_at` is a string (json), not a datetime object."""
    payload = build_payload(_incident(), [_candidate("a", 0.5)])
    assert isinstance(payload["opened_at"], str)
    assert payload["opened_at"].startswith("2026-10-21T12:00:00")
