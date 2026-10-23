"""`random_service` baseline (D7).

A uniform pick from the service set, with a `seed`. The chance floor; proves that any
method beating it is doing more than guessing. Deterministic given the seed so the
ranking is reproducible across runs.
"""

from __future__ import annotations

import hashlib

from .base import BaselineInput, _validate_nonempty


class RandomServiceBaseline:
    """Uniform random selection, but stable for a given seed."""

    name = "random_service"

    def __init__(self, *, seed: int = 0) -> None:
        self._seed = seed

    def rank(self, input: BaselineInput) -> list[str]:
        """Return the services in a seed-stable pseudo-random order."""
        _validate_nonempty(input.services)
        scrambled = sorted(
            input.services,
            key=lambda s: (
                hashlib.blake2b(f"{self._seed}:{s}".encode(), digest_size=8).hexdigest(),
                s,
            ),
        )
        return scrambled


__all__ = ["RandomServiceBaseline"]
