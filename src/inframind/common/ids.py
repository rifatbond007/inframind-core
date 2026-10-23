"""ULID generator for `Signal.id`, `Incident.id` and friends.

A ULID is 48 bits of millisecond timestamp plus 80 bits of randomness, encoded as 26
Crockford base32 characters. It sorts lexicographically by creation time.
"""

from __future__ import annotations

import secrets
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ULID_LENGTH = 26


def new_ulid() -> str:
    """Return a new 26-character ULID string."""
    value = (time.time_ns() // 1_000_000) << 80 | secrets.randbits(80)
    chars = []
    for _ in range(ULID_LENGTH):
        chars.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))
