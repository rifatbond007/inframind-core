"""The incident lifecycle: `open -> acknowledged -> resolved | expired`.

States follow the surface map. "Updating" is not a state: a republish of the same incident
bumps `revision`. `open` and `acknowledged` are *active*: D4 keeps every service in an active
incident frozen. `resolved` (quiet for the cooldown) and `expired` (open for `max_age_s`) are
terminal.
"""

from __future__ import annotations

from typing import Literal

from inframind.common.exceptions import InfraMindError
from inframind.common.incident import IncidentState

Event = Literal["acknowledge", "resolve", "expire"]

_TRANSITIONS: dict[tuple[IncidentState, Event], IncidentState] = {
    ("open", "acknowledge"): "acknowledged",
    ("open", "resolve"): "resolved",
    ("open", "expire"): "expired",
    ("acknowledged", "resolve"): "resolved",
    ("acknowledged", "expire"): "expired",
}


class InvalidTransition(InfraMindError):
    """An event is not allowed in the incident's current state."""


def transition(state: IncidentState, event: Event) -> IncidentState:
    """Return the state after `event`, or raise `InvalidTransition`."""
    try:
        return _TRANSITIONS[(state, event)]
    except KeyError:
        raise InvalidTransition(f"cannot {event} an incident that is {state}") from None


def is_active(state: IncidentState) -> bool:
    """Return whether baselines stay frozen in this state (D4): open or acknowledged."""
    return state in ("open", "acknowledged")


def is_terminal(state: IncidentState) -> bool:
    """Return whether no further event can change this state."""
    return state in ("resolved", "expired")
