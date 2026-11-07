"""The six Prometheus metrics (standard 11 section 11.12).

These tests are deliberately registry-tolerant: `inframind.metrics` registers its families at
import time against a module-level registry, so incrementing a counter in one test changes the
value another test sees. Every assertion below is therefore *relative* (delta, not absolute) or
*structural* (the family is present), never "the counter equals exactly N".
"""

from __future__ import annotations

from inframind import metrics


def test_module_owns_exactly_the_six_pinned_metrics() -> None:
    """The six metric names are pinned by standard 11 section 11.12. Adding a seventh is a
    standards change, not a code change."""
    assert len(metrics.ALL_METRICS) == 6
    names = {m._name for m in metrics.ALL_METRICS}
    assert names == {
        "inframind_signals_received",
        "inframind_anomalies_emitted",
        "inframind_incidents_opened",
        "inframind_rca_ranking_duration_seconds",
        "inframind_llm_call_duration_seconds",
        "inframind_alerts_sent",
    }


def test_render_extra_always_contains_all_six_families() -> None:
    """A scrape finding a metric *missing* should mean "never registered" (a bug), not
    "nothing has happened yet". So the families are present before any sample."""
    body = metrics.render_extra()
    for name in (
        "inframind_signals_received",
        "inframind_anomalies_emitted",
        "inframind_incidents_opened",
        "inframind_rca_ranking_duration_seconds",
        "inframind_llm_call_duration_seconds",
        "inframind_alerts_sent",
    ):
        assert name in body, f"render_extra() is missing {name}"


def test_record_signal_increments_the_signal_counter() -> None:
    before = _counter("inframind_signals_received", {"source": "prometheus", "type": "metric"})
    metrics.record_signal("prometheus", "metric")
    after = _counter("inframind_signals_received", {"source": "prometheus", "type": "metric"})
    assert after == before + 1


def test_record_anomaly_and_incident_and_alert_increment() -> None:
    assert _bump("anomaly")
    assert _bump("incident")
    assert _bump("alert")


def test_observe_helpers_record_without_raising_on_bad_input() -> None:
    # A negative duration is nonsense but must not raise into the caller's control flow.
    metrics.observe_ranking(-1.0)
    metrics.observe_llm_call(0.0)
    assert "inframind_rca_ranking_duration_seconds_count" in metrics.render_extra()
    assert "inframind_llm_call_duration_seconds_count" in metrics.render_extra()


def test_record_helpers_are_idempotent_under_duplicate_registration() -> None:
    """Re-running a counter increment with the same labels must not raise or double-count
    beyond the increment itself (the registry caches child metrics by label set).

    Asserted as a delta, not an absolute: the registry is module-level, so another test in
    this file may already have incremented the same label set.
    """
    before = metrics.ANOMALIES_EMITTED.labels(detector="zscore")._value.get()
    metrics.record_anomaly("zscore")
    metrics.record_anomaly("zscore")
    after = metrics.ANOMALIES_EMITTED.labels(detector="zscore")._value.get()
    assert after == before + 2


def test_start_http_server_survives_a_bad_port() -> None:
    """A worker that cannot bind metrics must still be a worker. Returns None, does not raise."""
    assert metrics.start_http_server(port=-1) is None


def _counter(metric_name: str, labels: dict[str, str]) -> float:
    """Read the current value of a labelled counter from the private registry."""
    return metrics.SIGNALS_RECEIVED.labels(**labels)._value.get()


def _bump(kind: str) -> bool:
    """Increment a counter once and assert the value moved by exactly one."""
    if kind == "anomaly":
        before = metrics.ANOMALIES_EMITTED.labels(detector="unit")._value.get()
        metrics.record_anomaly("unit")
        after = metrics.ANOMALIES_EMITTED.labels(detector="unit")._value.get()
    elif kind == "incident":
        before = metrics.INCIDENTS_OPENED.labels(service="unit")._value.get()
        metrics.record_incident_opened("unit")
        after = metrics.INCIDENTS_OPENED.labels(service="unit")._value.get()
    else:
        before = metrics.ALERTS_SENT.labels(channel="console", severity="INFO")._value.get()
        metrics.record_alert("console", "INFO")
        after = metrics.ALERTS_SENT.labels(channel="console", severity="INFO")._value.get()
    return after == before + 1
