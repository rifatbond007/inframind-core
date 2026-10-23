"""Alerting tunables and their `INFRAMIND_ALERTING_<PARAM>` overrides.

Three knobs:

* `dedup_window_s` — how long a single alert stays "fresh" for an open incident. After
  the window the dedup tracker forgets the incident and a new alert can fire. The default
  matches an SRE's first-look SLA: 5 minutes. A longer window means fewer alerts but
  more risk of "the page is stale by the time you read it".
* `console_enabled` — emit every alert to the JSON logger. Useful for `kubectl logs`
  and for the paper's tracing screenshots. Off in prod; on by default in dev so the
  human can watch the system work without a webhook target.
* `webhook_url` / `webhook_timeout_s` — the webhook sink is optional. Empty URL means
  "skipped" and the router never tries to POST.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from inframind.common.config import load_prefixed


@dataclass(frozen=True)
class AlertingParams:
    """Alerting tunables (P6, surface-map, alerting-storage-engineer spec)."""

    dedup_window_s: float = 300.0
    console_enabled: bool = True
    webhook_url: str = ""
    webhook_timeout_s: float = 5.0


def load_alerting_params(env: Mapping[str, str] | None = None) -> AlertingParams:
    """Build `AlertingParams` from env (`os.environ` when `env` is None)."""
    return load_prefixed(AlertingParams, "INFRAMIND_ALERTING", env)


__all__ = ["AlertingParams", "load_alerting_params"]
