"""Redact secrets and PII from free text before it leaves a collector (D9, standard 03 §3.4).

Placeholders name the kind of value removed so diagnostic text stays readable:
`[REDACTED:jwt|bearer|email|ip|secret]`. Hostnames, ports, error codes and service names are
left alone; they are the evidence. Redaction is idempotent. The LLM stage reuses this module.
"""

from __future__ import annotations

import re
from typing import Any

_JWT = re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+")
_BEARER = re.compile(r"\bBearer\s+(?!\[REDACTED:)[\w.~+/=-]+", re.IGNORECASE)
# The lookbehind lets a match start only at the beginning of a run of local-part characters.
# Without it the scan restarts at every character of a long token and is quadratic, which would
# stall a collector on one huge log line (a base64 blob, say).
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_SECRET_PAIR = re.compile(
    r"\b(password|passwd|pwd|secret|token|api[_-]?key|authorization)(\s*[=:]\s*)(?!\[REDACTED:)\S+",
    re.IGNORECASE,
)


def redact(text: str) -> str:
    """Return `text` with JWTs, bearer tokens, emails, IPv4 addresses and secret pairs replaced."""
    text = _JWT.sub("[REDACTED:jwt]", text)
    text = _BEARER.sub("[REDACTED:bearer]", text)
    text = _EMAIL.sub("[REDACTED:email]", text)
    text = _IPV4.sub("[REDACTED:ip]", text)
    return _SECRET_PAIR.sub(r"\1\2[REDACTED:secret]", text)


def redact_mapping(values: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of `values` with every string value (recursively) redacted; keys are kept."""
    return {
        key: redact(value)
        if isinstance(value, str)
        else redact_mapping(value)
        if isinstance(value, dict)
        else value
        for key, value in values.items()
    }
