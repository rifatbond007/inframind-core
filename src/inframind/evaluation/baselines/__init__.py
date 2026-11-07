"""The six D7 baselines, each a pure function over a `BaselineInput`.

| Baseline | Role |
|---|---|
| `raw_alertmanager` | The "what do we already have" floor. |
| `random_service` | The chance floor. |
| `highest_error_service` | The obvious metric heuristic. |
| `deepest_error_span` | The trace-only heuristic; RCA's closest lightweight competitor. |
| `pagerank` | A re-export of the P5 implementation, not a reimplementation. |
| `llm_only` | D1's control: the LLM alone, which *should* score near chance. |

Each is a class with a stable `name` and a `rank(input)` method, so the runner can treat
them uniformly. `pagerank` is special: it needs richer inputs (real `Anomaly` objects, a
symptom list, a window) than `BaselineInput` carries, so the runner uses
`rank_from_pagerank` directly rather than going through the Protocol.
"""

from __future__ import annotations

from .base import Baseline, BaselineInput, EmptyServiceSet
from .deepest_error_span import DeepestErrorSpanBaseline
from .highest_error_service import HighestErrorServiceBaseline
from .llm_only import LLmOnlyBaseline, extract_service
from .pagerank import PAGERANK_METHOD, rank_from_pagerank
from .random_service import RandomServiceBaseline
from .raw_alertmanager import RawAlertmanagerBaseline

#: The six D7 baseline names, in report order. Adding or removing one is a D-change.
BASELINE_NAMES: tuple[str, ...] = (
    "raw_alertmanager",
    "random_service",
    "highest_error_service",
    "deepest_error_span",
    PAGERANK_METHOD,
    "llm_only",
)

__all__ = [
    "BASELINE_NAMES",
    "PAGERANK_METHOD",
    "Baseline",
    "BaselineInput",
    "DeepestErrorSpanBaseline",
    "EmptyServiceSet",
    "HighestErrorServiceBaseline",
    "LLmOnlyBaseline",
    "RandomServiceBaseline",
    "RawAlertmanagerBaseline",
    "extract_service",
    "rank_from_pagerank",
]
