"""The incident fingerprint: a stable identity for downstream deduplication.

It is fixed when the incident opens, from the *opening* service and the time bucket, and never
changes while the incident lives, even as other services join (a growing set of services could
not give a stable value). A fault that flaps and re-opens inside the same bucket keeps its
fingerprint, so alerting can suppress the repeat. The incident `id` stays a fresh ULID.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from inframind.common.signal import normalise_service


def fingerprint(first_service: str, opened_at: datetime, window_s: float) -> str:
    """Return 16 hex characters from the opening service and the `window_s` bucket of `opened_at`."""
    bucket = int(opened_at.timestamp() // window_s) * int(window_s)
    key = f"{normalise_service(first_service)}|{bucket}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]
