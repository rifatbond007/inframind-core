"""LLM-stage redaction (D9, standard 07.5).

Two layers, composed:

1. `inframind.common.redact` — the collector-stage redaction for JWTs, bearer tokens,
   emails, IPv4 and secret pairs. Every collector already runs it.
2. This module — the LLM-stage additional patterns: K8s ``kind: Secret`` blocks, hashed
   pod / container / node names, the ``data:`` payload of any Secret-shaped YAML, and a
   conservative long-random-string fallback that catches things the patterns above miss.

The functions are pure and idempotent: ``redact(redact(x)) == redact(x)``. A unit test
asserts the full pattern set.

Why a separate module: ``common/redact`` is consumed by every collector and runs in the
hot path; if we folded the LLM-only patterns into it, every collector would pay the
extra cost. The LLM stage runs once per ranked incident, so the cost matters there too —
but the addition is justified by the wider net.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from inframind.common.redact import redact as _common_redact

_KIND_SECRET = re.compile(r"(?ms)\bkind:\s*Secret\b.*?(?=^\S|\Z)")
_DATA_PAYLOAD = re.compile(r"(?ms)^\s*data:\s*\n((?:\s+[^\n]+\n)+)")
# Long base64-ish tokens. Anchored at >= 40 chars so ordinary words and short IDs are
# untouched. Conservative on purpose: false negatives are preferred to false positives
# (a missing redaction is a leak; a spurious one just costs a citation).
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_\-]{40,}\b")
# K8s-style name fields where the value is a runtime identity, not evidence.
_POD_NAME = re.compile(
    r"\b(?:pod|name):\s*([a-z0-9]([-a-z0-9]*[a-z0-9])?(?:\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*)"
)
_CONTAINER_IMAGE = re.compile(r"\bimage:\s*([^\s]+)")
_NODE_NAME = re.compile(r"\bnode(?:-name)?:\s*([a-z0-9]([-a-z0-9]*[a-z0-9])?)(?:\s|$)")


def _stable_hash(value: str, salt: str, length: int = 8) -> str:
    """Return a stable short hash of `value` for diagnostics; deterministic across runs."""
    digest = hashlib.blake2b(f"{salt}:{value}".encode(), digest_size=8).hexdigest()
    return f"[REDACTED:hash:{digest[:length]}]"


def redact_text(
    text: str,
    *,
    pod_salt: str = "inframind-llm-pod",
    node_salt: str = "inframind-llm-node",
) -> str:
    """Redact `text` with the LLM-stage pattern set on top of `common.redact.redact`.

    The function is pure and idempotent. Pinned to a stable hash so the same pod name
    across two redacted summaries reveals it is the same pod without leaking the name.
    """
    text = _common_redact(text)
    text = _KIND_SECRET.sub("[REDACTED:k8s-secret-block]", text)
    text = _DATA_PAYLOAD.sub("data:\n  [REDACTED:k8s-secret-data]\n", text)

    # Hash runtime identities so an alert can still cite "pod X" without naming it.
    def _hash_pod(match: re.Match[str]) -> str:
        name = match.group(1)
        return f"{match.group(0)[: match.start(1) - match.start(0)]}{_stable_hash(name, pod_salt)}"

    text = _POD_NAME.sub(_hash_pod, text)
    text = _CONTAINER_IMAGE.sub("[REDACTED:image]", text)

    def _hash_node(match: re.Match[str]) -> str:
        prefix = match.group(0)[: match.start(1) - match.start(0)]
        return f"{prefix}{_stable_hash(match.group(1), node_salt)}"

    text = _NODE_NAME.sub(_hash_node, text)

    # Long random strings — last-resort heuristic for blobs the patterns above missed.
    text = _LONG_TOKEN.sub("[REDACTED:long-token]", text)
    return text


def redact(value: Any) -> Any:
    """Return `value` with all string content redacted for LLM transmission.

    Strings are redacted with `redact_text`; mappings have every string value redacted
    recursively (keys are kept); sequences are mapped element-wise. Anything else is
    returned unchanged.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


__all__ = ["redact", "redact_text"]
