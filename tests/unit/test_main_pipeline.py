"""`inframind.main.IncidentPipeline`: the post-RCA chain the deployed rca process runs (D28).

Storage and the LLM are replaced with recorders so the test pins the *order and conditions*
of the chain: every revision is stored; the LLM runs only when an alert is due; a closed
incident re-arms dedup; an LLM failure never costs the alert (D1: presentation only); a
storage failure propagates so the bus redelivers the incident.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

from inframind import main as pipeline_module
from inframind.alerting import AlertingParams, AlertRouter
from inframind.common.incident import Incident
from inframind.common.signal import Severity
from inframind.llm import LLMParams, LLMSummary
from inframind.llm.validator import ValidationResult
from inframind.main import IncidentPipeline
from inframind.rca.evidence import Candidate, Evidence
from inframind.rca.worker import IncidentAnalysis
from inframind.storage import StorageUnavailable

T0 = datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC)


def _incident(
    *, revision: int = 1, state: str = "open", severity: Severity = Severity.WARNING
) -> Incident:
    closed = state in ("resolved", "expired")
    return Incident(
        id="inc_live",
        revision=revision,
        state=state,
        severity=severity,
        title="frontend errors",
        fingerprint="0123abcd",
        opened_at=T0,
        updated_at=T0 + timedelta(seconds=30),
        closed_at=T0 + timedelta(seconds=30) if closed else None,
        services=["frontend"],
        signals=[],
        anomalies=[],
    )


def _candidate() -> Candidate:
    return Candidate(
        service="productcatalogservice",
        score=0.9,
        depth=1,
        change_correlation=0.0,
        confidence=0.8,
        evidence=[Evidence(id="ev_1", kind="metric", summary="p95 up", ref="sig_1", weight=0.9)],
    )


def _analysis(candidates: list[Candidate] | None = None) -> IncidentAnalysis:
    return IncidentAnalysis(
        incident_id="inc_live",
        graph_version=1,
        window=(T0, T0 + timedelta(seconds=30)),
        candidates=[_candidate()] if candidates is None else candidates,
    )


class Recorder:
    """Stands in for the storage functions `inframind.main` imported."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail: str | None = None

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def make(self, name: str) -> Any:
        async def fn(pool: Any, *args: Any, **kwargs: Any) -> Any:
            if self.fail == name:
                raise StorageUnavailable(f"{name} failed")
            self.calls.append((name, {"args": args, **kwargs}))
            return True

        return fn


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    rec = Recorder()
    for name in ("write_incident", "record_summary", "record_alerts"):
        monkeypatch.setattr(pipeline_module, name, rec.make(name))
    return rec


@pytest.fixture
def console() -> Any:
    with patch("inframind.alerting.sinks.console.send", lambda payload: ("al_1", "delivered")):
        yield


def _explain_ok(calls: list[int]) -> Any:
    def fake(incident: Incident, candidates: list[Candidate], **kwargs: Any) -> Any:
        calls.append(1)
        summary = LLMSummary(summary_text="productcatalogservice is the root cause")
        return summary, ValidationResult(summary=summary, raw='{"ok": 1}', status="accepted")

    return fake


def _pipeline(explain_fn: Any) -> IncidentPipeline:
    return IncidentPipeline(
        object(),  # the pool is only passed through to the (patched) storage functions
        AlertRouter(AlertingParams(console_enabled=True)),
        llm_params=LLMParams(ollama_fallback=False),
        explain_fn=explain_fn,
    )


async def test_first_revision_is_stored_explained_alerted_and_recorded(
    store: Recorder, console: Any
) -> None:
    calls: list[int] = []
    await _pipeline(_explain_ok(calls))(_incident(), _analysis())

    assert store.names() == ["write_incident", "record_summary", "record_alerts"]
    assert calls == [1]
    summary_call = store.calls[1][1]
    assert summary_call["summary_text"] == "productcatalogservice is the root cause"
    assert summary_call["provenance"]["status"] == "accepted"
    alerts_call = store.calls[2][1]
    assert alerts_call["outcomes"] == [("al_1", "console", "delivered")]
    assert alerts_call["payload"]["summary_text"] == "productcatalogservice is the root cause"


async def test_later_revisions_are_stored_but_not_re_explained(
    store: Recorder, console: Any
) -> None:
    calls: list[int] = []
    pipeline = _pipeline(_explain_ok(calls))
    await pipeline(_incident(revision=1), _analysis())
    await pipeline(_incident(revision=2), _analysis())
    await pipeline(_incident(revision=3), _analysis())

    assert calls == [1]  # one LLM call per alert, not per revision
    assert store.names().count("write_incident") == 3
    assert store.names().count("record_alerts") == 1


async def test_escalation_to_critical_explains_and_alerts_again(
    store: Recorder, console: Any
) -> None:
    calls: list[int] = []
    pipeline = _pipeline(_explain_ok(calls))
    await pipeline(_incident(severity=Severity.WARNING), _analysis())
    await pipeline(_incident(revision=2, severity=Severity.CRITICAL), _analysis())
    assert calls == [1, 1]
    assert store.names().count("record_alerts") == 2


async def test_a_closed_incident_is_stored_and_re_armed_without_an_alert(
    store: Recorder, console: Any
) -> None:
    calls: list[int] = []
    pipeline = _pipeline(_explain_ok(calls))
    await pipeline(_incident(), _analysis())
    await pipeline(_incident(revision=2, state="resolved"), _analysis())
    assert store.names()[-1] == "write_incident"
    assert calls == [1]
    # Re-armed: a new open revision of the same id would alert again.
    await pipeline(_incident(revision=3), _analysis())
    assert calls == [1, 1]


async def test_an_llm_failure_still_alerts_with_the_template(store: Recorder, console: Any) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("provider exploded")

    await _pipeline(broken)(_incident(), _analysis())

    assert store.names() == ["write_incident", "record_summary", "record_alerts"]
    provenance = store.calls[1][1]["provenance"]
    assert provenance["status"] == "template_fallback"
    assert "provider exploded" in provenance["error"]
    assert "productcatalogservice" in store.calls[1][1]["summary_text"]


async def test_no_candidates_means_no_llm_call_but_still_one_alert(
    store: Recorder, console: Any
) -> None:
    calls: list[int] = []
    await _pipeline(_explain_ok(calls))(_incident(), _analysis(candidates=[]))
    assert calls == []
    assert store.names() == ["write_incident", "record_alerts"]
    assert store.calls[1][1]["payload"]["summary_text"] is None


async def test_a_storage_failure_propagates_for_redelivery(store: Recorder, console: Any) -> None:
    store.fail = "write_incident"
    calls: list[int] = []
    with pytest.raises(StorageUnavailable):
        await _pipeline(_explain_ok(calls))(_incident(), _analysis())
    assert calls == []  # nothing is explained or alerted for an incident that was not stored


async def test_the_llm_sees_the_ranking_unchanged(store: Recorder, console: Any) -> None:
    """D1: the explainer receives the candidates in the ranker's order, nothing else decides."""
    seen: list[list[str]] = []

    def spy(incident: Incident, candidates: list[Candidate], **kwargs: Any) -> Any:
        seen.append([c.service for c in candidates])
        summary = LLMSummary(summary_text="x")
        return summary, None

    second = Candidate(
        service="currencyservice",
        score=0.3,
        depth=2,
        change_correlation=0.0,
        confidence=0.2,
        evidence=[],
    )
    await _pipeline(spy)(_incident(), _analysis([_candidate(), second]))
    assert seen == [["productcatalogservice", "currencyservice"]]
    assert store.calls[1][1]["provenance"]["status"] == "cache_hit"
