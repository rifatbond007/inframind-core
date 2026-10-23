"""Tests for `alerting.router.AlertRouter` — dedup + fan-out contract.

Sinks are patched so the dispatcher's own behaviour is what's under test: dedup
suppression, sink fan-out, and outcome aggregation. `pytest` runs in `asyncio_mode=auto`
(see pyproject.toml), so the async test bodies are awaited directly.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest

from inframind.alerting.config import AlertingParams
from inframind.alerting.router import AlertRouter
from inframind.common.incident import Incident
from inframind.common.signal import Severity
from inframind.rca.evidence import Candidate


def _incident(severity: Severity = Severity.WARNING) -> Incident:
    return Incident(
        id="inc_abc",
        revision=1,
        state="open",
        severity=severity,
        title="checkout latency",
        fingerprint="0123abcd",
        opened_at=datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC),
        services=["frontend"],
        signals=[],
        anomalies=[],
    )


def _candidate() -> Candidate:
    return Candidate(
        service="paymentservice",
        score=0.91,
        depth=3,
        change_correlation=0.7,
        confidence=0.8,
        evidence=[],
    )


def _webhook_stub(status: str) -> Callable[..., Awaitable[tuple[str, str]]]:
    """Return an async stub producing `(alert_id, status)` for the webhook sink."""
    counter = {"n": 0}

    async def _send(
        payload: dict[str, Any], *, url: str, timeout_s: float = 5.0
    ) -> tuple[str, str]:
        counter["n"] += 1
        return f"alert_{counter['n']}", status

    return _send


async def test_first_dispatch_fires_configured_sinks() -> None:
    """Both sinks fire on the first dispatch when console and webhook are enabled."""
    router = AlertRouter(AlertingParams(console_enabled=True, webhook_url="http://hook"))
    calls: list[str] = []

    def fake_console(payload: dict[str, Any]) -> tuple[str, str]:
        calls.append("console")
        return "alert_console", "delivered"

    async def fake_webhook(payload: dict[str, Any], *, url: str, **_: Any) -> tuple[str, str]:
        calls.append("webhook")
        return "alert_webhook", "delivered"

    with (
        patch("inframind.alerting.sinks.console.send", fake_console),
        patch("inframind.alerting.sinks.webhook.send", fake_webhook),
    ):
        result = await router.dispatch(_incident(), [_candidate()])

    assert result.alerted is True
    assert sorted(calls) == ["console", "webhook"]
    assert result.delivered_any is True
    assert sorted(result.outcomes) == [
        ("alert_console", "console", "delivered"),
        ("alert_webhook", "webhook", "delivered"),
    ]


async def test_dispatch_is_deduped_within_window() -> None:
    """The second dispatch inside the window is suppressed and fires no sinks."""
    router = AlertRouter(AlertingParams(dedup_window_s=300.0, console_enabled=True))
    calls: list[dict[str, Any]] = []

    # console.send is synchronous — the router wraps it in asyncio.to_thread.
    def fake_console(payload: dict[str, Any]) -> tuple[str, str]:
        calls.append(payload)
        return "alert_console", "delivered"

    with patch("inframind.alerting.sinks.console.send", fake_console):
        first = await router.dispatch(_incident(), [_candidate()])
        second = await router.dispatch(_incident(), [_candidate()])

    assert first.alerted is True
    assert second.alerted is False
    assert second.outcomes == []
    assert len(calls) == 1  # only the first dispatch reached the sink


async def test_escalation_rearms_within_window() -> None:
    """A critical escalation re-arms the tracker and fires a second alert."""
    router = AlertRouter(AlertingParams(dedup_window_s=300.0, console_enabled=False))
    with patch("inframind.alerting.sinks.webhook.send", _webhook_stub("delivered")):
        first = await router.dispatch(_incident(Severity.WARNING), [_candidate()])
        second = await router.dispatch(_incident(Severity.CRITICAL), [_candidate()])

    assert first.alerted is True
    assert second.alerted is True


async def test_console_disabled_skips_the_console_sink() -> None:
    """With `console_enabled=False`, only the webhook sink fires."""
    router = AlertRouter(AlertingParams(console_enabled=False, webhook_url="http://hook"))
    calls: list[str] = []

    def fake_console(payload: dict[str, Any]) -> tuple[str, str]:
        calls.append("console")
        return "alert_console", "delivered"

    async def fake_webhook(payload: dict[str, Any], *, url: str, **_: Any) -> tuple[str, str]:
        calls.append("webhook")
        return "alert_webhook", "delivered"

    with (
        patch("inframind.alerting.sinks.console.send", fake_console),
        patch("inframind.alerting.sinks.webhook.send", fake_webhook),
    ):
        await router.dispatch(_incident(), [_candidate()])

    assert calls == ["webhook"]


async def test_webhook_is_skipped_when_url_is_empty() -> None:
    """An empty webhook URL means the sink is never invoked."""
    router = AlertRouter(AlertingParams(console_enabled=False, webhook_url=""))
    calls: list[str] = []

    async def fake_webhook(payload: dict[str, Any], *, url: str, **_: Any) -> tuple[str, str]:
        calls.append(url)
        return "alert_webhook", "delivered"

    with patch("inframind.alerting.sinks.webhook.send", fake_webhook):
        result = await router.dispatch(_incident(), [_candidate()])

    assert result.alerted is True
    assert result.outcomes == []
    assert calls == []


async def test_no_sinks_configured_yields_no_outcomes() -> None:
    """Both sinks off is a valid (if useless) configuration: alert is recorded, nothing sent."""
    router = AlertRouter(AlertingParams(console_enabled=False, webhook_url=""))
    result = await router.dispatch(_incident(), [_candidate()])

    assert result.alerted is True
    assert result.outcomes == []
    assert result.delivered_any is False


async def test_queued_status_is_not_reported_as_delivered() -> None:
    """`queued` means "we tried, retry on the bus" — it is not a delivery."""
    router = AlertRouter(AlertingParams(console_enabled=False, webhook_url="http://hook"))
    with patch("inframind.alerting.sinks.webhook.send", _webhook_stub("queued")):
        result = await router.dispatch(_incident(), [_candidate()])

    assert result.outcomes == [("alert_1", "webhook", "queued")]
    assert result.delivered_any is False


async def test_failed_status_is_surfaced_not_swallowed() -> None:
    """A transport failure is reported so the caller can persist `sink_status='failed'`."""
    router = AlertRouter(AlertingParams(console_enabled=False, webhook_url="http://hook"))
    with patch("inframind.alerting.sinks.webhook.send", _webhook_stub("failed")):
        result = await router.dispatch(_incident(), [_candidate()])

    assert result.outcomes == [("alert_1", "webhook", "failed")]
    assert result.delivered_any is False


@pytest.mark.parametrize("candidates", [[], None])
async def test_dispatch_without_candidates_still_alerts(candidates: Any) -> None:
    """An incident with no ranked candidates still pages (an unexplained alert is a page)."""
    router = AlertRouter(AlertingParams(console_enabled=False, webhook_url="http://hook"))
    with patch("inframind.alerting.sinks.webhook.send", _webhook_stub("delivered")):
        result = await router.dispatch(_incident(), candidates or [])

    assert result.alerted is True
    assert result.delivered_any is True
