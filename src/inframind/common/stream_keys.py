"""The pinned Redis stream key names. Renaming one is a D-change (see `docs/decision-tree.md`)."""

from __future__ import annotations

from typing import Final

SIGNALS: Final = "stream:signals"
INCIDENTS: Final = "stream:incidents"
GRAPH_UPDATES: Final = "stream:graph-updates"
