"""Shared fixtures for the LLM explainer unit tests.

The scenario is the canonical P5 one: `frontend` is the symptom and `paymentservice` is
the deepest, most severe, earliest-firing candidate. Evidence ids are fixed strings
(`ev_alpha`, `ev_beta`) so the validator tests can cite them verbatim — a real ULID would
work too, but a readable id makes a failing assertion obvious at a glance.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from inframind.common.anomaly import Anomaly
from inframind.common.incident import Incident
from inframind.common.signal import Severity
from inframind.llm.config import LLMParams
from inframind.rca.evidence import Candidate, Evidence


def _t() -> datetime:
    return datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def fixed_anomaly() -> Anomaly:
    """One critical error-spike anomaly on the canonical root-cause service."""
    return Anomaly(
        id="anom_001",
        detector="error_spike",
        service="paymentservice",
        severity=Severity.CRITICAL,
        score=5.0,
        ts=_t(),
        signal_id="sig_001",
    )


@pytest.fixture
def evidence_items() -> list[Evidence]:
    """Two evidence items with stable ids so the validator tests can cite them."""
    return [
        Evidence(
            id="ev_alpha",
            kind="metric",
            summary="error_spike fired on paymentservice (score=5.00)",
            ref="sig_001",
            weight=5.0,
        ),
        Evidence(
            id="ev_beta",
            kind="change",
            summary="Deployment on paymentservice at 2026-10-20T12:00:00+00:00",
            ref="paymentservice",
            weight=1.0,
        ),
    ]


@pytest.fixture
def ranked_candidate(evidence_items: list[Evidence]) -> Candidate:
    """The deterministic top-of-ranking candidate used by the validator tests."""
    return Candidate(
        id="cand_root",
        service="paymentservice",
        score=0.9,
        depth=2,
        change_correlation=0.7,
        confidence=1.0,
        evidence=list(evidence_items),
    )


@pytest.fixture
def incident(fixed_anomaly: Anomaly) -> Incident:
    """One open incident whose symptom is `frontend` and root-cause is `paymentservice`."""
    return Incident(
        id="inc_p5b_001",
        revision=1,
        state="open",
        opened_at=_t(),
        updated_at=_t(),
        closed_at=None,
        title="frontend errors, paymentservice suspected",
        services=["frontend", "paymentservice"],
        signals=["sig_001"],
        anomalies=[fixed_anomaly],
        severity=Severity.CRITICAL,
        fingerprint="deadbeef00000001",
    )


@pytest.fixture
def llm_params() -> LLMParams:
    """Default LLMParams for the unit tests."""
    return LLMParams()
