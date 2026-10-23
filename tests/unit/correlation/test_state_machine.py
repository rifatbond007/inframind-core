"""The incident lifecycle: legal transitions only, and which states keep baselines frozen (D4)."""

from __future__ import annotations

import pytest

from inframind.common.exceptions import InfraMindError
from inframind.correlation.state_machine import (
    InvalidTransition,
    is_active,
    is_terminal,
    transition,
)

LEGAL = [
    ("open", "acknowledge", "acknowledged"),
    ("open", "resolve", "resolved"),
    ("open", "expire", "expired"),
    ("acknowledged", "resolve", "resolved"),
    ("acknowledged", "expire", "expired"),
]
ILLEGAL = [
    ("acknowledged", "acknowledge"),
    ("resolved", "acknowledge"),
    ("resolved", "resolve"),
    ("resolved", "expire"),
    ("expired", "acknowledge"),
    ("expired", "resolve"),
    ("expired", "expire"),
]


@pytest.mark.parametrize(("state", "event", "expected"), LEGAL)
def test_legal_transitions(state: str, event: str, expected: str) -> None:
    assert transition(state, event) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(("state", "event"), ILLEGAL)
def test_illegal_transitions_raise_and_name_both(state: str, event: str) -> None:
    with pytest.raises(InvalidTransition, match=f"{event}.*{state}|{state}.*{event}"):
        transition(state, event)  # type: ignore[arg-type]


def test_invalid_transition_is_a_domain_error() -> None:
    assert issubclass(InvalidTransition, InfraMindError)


@pytest.mark.parametrize(
    ("state", "active", "terminal"),
    [
        ("open", True, False),
        ("acknowledged", True, False),
        ("resolved", False, True),
        ("expired", False, True),
    ],
)
def test_active_states_keep_baselines_frozen_and_terminal_states_are_final(
    state: str, active: bool, terminal: bool
) -> None:
    assert is_active(state) is active  # type: ignore[arg-type]
    assert is_terminal(state) is terminal  # type: ignore[arg-type]
