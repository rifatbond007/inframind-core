"""The incident fingerprint: stable for a flapping fault, different for a different one."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from inframind.correlation.fingerprint import fingerprint

BUCKET = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)  # a 300 s bucket boundary


def test_it_is_deterministic_and_16_lowercase_hex_characters() -> None:
    first = fingerprint("frontend", BUCKET, 300.0)
    assert first == fingerprint("frontend", BUCKET, 300.0)
    assert re.fullmatch(r"[0-9a-f]{16}", first)


def test_a_flapping_fault_keeps_its_fingerprint_inside_the_bucket() -> None:
    early = fingerprint("frontend", BUCKET + timedelta(seconds=10), 300.0)
    late = fingerprint("frontend", BUCKET + timedelta(seconds=250), 300.0)
    assert early == late


def test_a_different_service_or_the_next_bucket_differs() -> None:
    base = fingerprint("frontend", BUCKET, 300.0)
    assert fingerprint("cartservice", BUCKET, 300.0) != base
    assert fingerprint("frontend", BUCKET + timedelta(seconds=300), 300.0) != base


def test_the_service_name_is_normalised() -> None:
    assert fingerprint("FrontEnd", BUCKET, 300.0) == fingerprint("frontend", BUCKET, 300.0)


def test_the_window_length_changes_the_bucket() -> None:
    at = BUCKET + timedelta(seconds=150)
    assert fingerprint("frontend", at, 300.0) != fingerprint("frontend", at, 100.0)


def test_a_known_value_is_pinned() -> None:
    """Changing how fingerprints are made changes every downstream dedup key; do it on purpose."""
    assert fingerprint("frontend", BUCKET, 300.0) == "637cd58030666c2d"
