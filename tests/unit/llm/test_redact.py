"""`inframind.llm.redact` covers the full pattern set of standard 07.5 / D9.

Each table row in the standard gets a focused assertion. The point of these tests is not
to catch every regex detail — they enforce the *coverage* of the pattern set, so a future
edit that drops a row is caught at `pytest` time.

Idempotency is asserted explicitly because the redactor runs twice for every prompt
(builder + audit log write); a non-idempotent redact would compose to garbled output.
"""

from __future__ import annotations

from inframind.llm.redact import redact, redact_text


def test_redacts_jwt() -> None:
    token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjMifQ.abcDEFghij"
    out = redact_text(f"token={token}")
    assert token not in out
    assert "[REDACTED:jwt]" in out


def test_redacts_bearer_header() -> None:
    out = redact_text("Authorization: Bearer abc.def-GHI_123~+/=")
    assert "Bearer abc.def" not in out
    assert "[REDACTED:bearer]" in out


def test_redacts_email() -> None:
    out = redact_text("contact alice@example.com for support")
    assert "alice@example.com" not in out
    assert "[REDACTED:email]" in out


def test_redacts_internal_ipv4() -> None:
    out = redact_text("connection from 10.0.0.42 via 192.168.1.1 and 8.8.8.8")
    # All four dotted-quads match the regex on purpose; the rule is broad by design.
    assert "10.0.0.42" not in out
    assert "192.168.1.1" not in out
    assert "8.8.8.8" not in out
    assert out.count("[REDACTED:ip]") == 3


def test_redacts_secret_pair_with_password() -> None:
    out = redact_text("password=hunter2 PASSWORD = hunter2 again")
    assert "hunter2" not in out
    assert "[REDACTED:secret]" in out


def test_redacts_secret_pair_with_api_key() -> None:
    out = redact_text("api_key=sk_live_abcdef api-key: shh")
    assert "sk_live_abcdef" not in out
    assert "shh" not in out
    assert "[REDACTED:secret]" in out


def test_redacts_kind_secret_block() -> None:
    text = (
        "apiVersion: v1\n"
        "kind: Secret\n"
        "metadata:\n"
        "  name: paymentservice-secrets\n"
        "data:\n"
        "  api-key: YXNkZg==\n"
        "  password: cGFzc3dvcmQ=\n"
    )
    out = redact_text(text)
    assert "kind: Secret" not in out
    assert "[REDACTED:k8s-secret-block]" in out


def test_redacts_k8s_data_payload() -> None:
    text = "data:\n  username: YWRtaW4=\n  password: cGFzcw==\n"
    out = redact_text(text)
    assert "YWRtaW4=" not in out
    assert "[REDACTED:k8s-secret-data]" in out


def test_hashes_pod_name() -> None:
    out = redact_text("pod: paymentservice-7d4f9b8c6d-x2k4q")
    assert "paymentservice-7d4f9b8c6d-x2k4q" not in out
    assert "[REDACTED:hash:" in out


def test_redacts_container_image() -> None:
    out = redact_text("image: ghcr.io/acme/paymentservice:v1.2.3")
    assert "ghcr.io/acme/paymentservice:v1.2.3" not in out
    assert "[REDACTED:image]" in out


def test_redacts_long_random_string() -> None:
    long_blob = "a" * 60
    out = redact_text(f"blob: {long_blob}")
    assert long_blob not in out
    assert "[REDACTED:long-token]" in out


def test_redaction_is_idempotent() -> None:
    text = "contact alice@example.com token=eyJhbGciOiJIUzI1NiJ9.payload.sig password=hunter2"
    once = redact_text(text)
    twice = redact_text(once)
    assert once == twice


def test_redact_walks_nested_structures() -> None:
    payload = {
        "summary": "error for user alice@example.com",
        "context": {"token": "Bearer abc.def-GHI_123~+/=", "ok": True, "count": 3},
        "items": ["password=secret1", "no secrets here"],
    }
    out = redact(payload)
    assert isinstance(out, dict)
    assert "alice@example.com" not in out["summary"]
    assert "Bearer abc.def" not in out["context"]["token"]
    assert out["context"]["ok"] is True
    assert out["context"]["count"] == 3
    assert "secret1" not in out["items"][0]
    assert out["items"][1] == "no secrets here"


def test_short_strings_are_left_alone() -> None:
    text = "service=paymentservice hostname=checkout-0 pod_name=web-1"
    out = redact_text(text)
    # pod_name matches the regex but the value is short enough to be a normal identifier,
    # confirming the long-token heuristic does not over-redact.
    assert "[REDACTED:hash:" not in out
