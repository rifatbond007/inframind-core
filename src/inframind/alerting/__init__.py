"""Alerting: deliver deduplicated, actionable incident notifications.

The RCA stage ranks; this stage notifies. It consumes the ranked candidate list
**in-process** (no new Redis stream key — see D20 and `docs/surface-map.md`) and renders
an alert payload with the top candidate, blast radius, and the LLM summary (redacted
again on the way out, D9).

Public surface:

* `AlertingParams`, `load_alerting_params` — config.
* `DedupTracker` — one alert per open incident; severity escalation re-arms.
* `build_payload` — the alert dict (pure function).
* `AlertRouter` — the object the RCA worker calls; fans out to the configured sinks.
* `sinks` — `console` (always in dev) and `webhook` (opt-in).

Hard rules:

* The LLM summary is **presentation only**; the ranked order is passed through unchanged.
* `summary_text` is re-redacted before it lands in the payload — the model's output is
  not trusted verbatim (D9).
* This module may not import `storage` or `api` (DAG, standards 01 §1.3).
"""

from __future__ import annotations

from . import sinks
from .config import AlertingParams, load_alerting_params
from .dedup import DedupTracker
from .payload import build_payload
from .router import AlertRouter, DispatchResult

__all__ = [
    "AlertRouter",
    "AlertingParams",
    "DedupTracker",
    "DispatchResult",
    "build_payload",
    "load_alerting_params",
    "sinks",
]
