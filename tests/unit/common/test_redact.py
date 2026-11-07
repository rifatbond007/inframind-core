"""Redaction of secrets and PII (D9, standard 03 section 3.4): what goes, and what must stay."""

from __future__ import annotations

import time

import pytest

from inframind.common.redact import redact, redact_mapping

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.abc_DEF-123"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("auth Bearer abc.def-123", "auth [REDACTED:bearer]"),
        (f"token was {JWT} ok", "token was [REDACTED:jwt] ok"),
        ("contact a.b@example.com now", "contact [REDACTED:email] now"),
        ("dial tcp 10.244.1.12:5432 refused", "dial tcp [REDACTED:ip]:5432 refused"),
        (
            "password=hunter2 token: abc123 api_key=XYZ",
            "password=[REDACTED:secret] token: [REDACTED:secret] api_key=[REDACTED:secret]",
        ),
        ("PASSWORD=Hunter2", "PASSWORD=[REDACTED:secret]"),
        ("Authorization: Bearer abc123", "Authorization: [REDACTED:bearer]"),
        (f"Bearer {JWT}", "Bearer [REDACTED:jwt]"),
    ],
)
def test_sensitive_values_are_replaced(text: str, expected: str) -> None:
    assert redact(text) == expected


@pytest.mark.parametrize(
    "evidence",
    [
        "could not retrieve product: rpc error: code = NotFound desc = no product with ID NOPE-9",
        "dial tcp: lookup productcatalogservice on 10.96.0.10:53: no such host".replace(
            "10.96.0.10", "kube-dns"
        ),
        "request error: connection refused to cartservice:7070",
        "",
    ],
)
def test_diagnostic_evidence_survives(evidence: str) -> None:
    assert redact(evidence) == evidence


@pytest.mark.parametrize(
    "text",
    ["password=hunter2 a@b.co 10.0.0.1", f"Bearer {JWT}", "token: x Authorization: Bearer y"],
)
def test_redaction_is_idempotent(text: str) -> None:
    assert redact(redact(text)) == redact(text)


def test_redact_mapping_redacts_string_values_only() -> None:
    values = {
        "summary": "pod up",
        "description": "login for a@b.com from 10.1.2.3 failed",
        "count": 3,
        "nested": {"note": "token=abc"},
    }
    assert redact_mapping(values) == {
        "summary": "pod up",
        "description": "login for [REDACTED:email] from [REDACTED:ip] failed",
        "count": 3,
        "nested": {"note": "token=[REDACTED:secret]"},
    }
    assert values["description"].startswith("login for a@b.com")  # input is not mutated


@pytest.mark.parametrize(
    "text",
    [
        "A" * 30_000,  # one long token, e.g. a base64 blob
        "eyJ" * 10_000,
        "a@" * 15_000,
        "1." * 15_000,
        "token=" * 5_000,
        "a." * 15_000,
    ],
    ids=[
        "word-run",
        "repeated-jwt-prefix",
        "repeated-at",
        "dotted-digits",
        "repeated-key",
        "dotted-word",
    ],
)
def test_redaction_stays_fast_on_pathological_lines(text: str) -> None:
    """A single huge log line must not stall a collector (redaction runs before truncation)."""
    started = time.perf_counter()
    redact(text)
    assert time.perf_counter() - started < 1.0
