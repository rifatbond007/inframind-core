"""Walk from the symptom service **toward its callees** (D2, standard 06.3).

The graph edges are ``caller -> callee``. A failure in a callee propagates to its callers, so
the root cause is found by moving *away* from the symptom, following the direction the failure
travelled. The walk is a bounded breadth-first search; ``max_depth`` is pinned by
`docs/standards/06-rca-scoring.md` (default 4) and must not be hard-coded differently here.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from inframind.common.signal import normalise_service

from .config import RCAParams
from .graph import ServiceGraph


@dataclass(frozen=True)
class Candidate:
    """One service reached by the walk, with the depth at which it was reached.

    `depth` is the shortest ``caller -> callee`` distance from the symptom. Depth 0 is the
    symptom service itself.
    """

    service: str
    depth: int
    # The path of services traversed to reach this candidate, symptom-first.
    path: tuple[str, ...]


def walk_toward_callees(
    graph: ServiceGraph,
    symptom: str,
    *,
    max_depth: int | None = None,
    params: RCAParams | None = None,
) -> list[Candidate]:
    """Return the walkable services reachable from `symptom` along ``caller -> callee`` edges.

    Only edges that have met ``GRAPH_MIN_OBSERVATIONS`` are traversed (standard 13.4); an
    under-observed edge is invisible to the walk. Cycles are handled by the visited set, so a
    cyclic topology terminates. Results are ordered by ``(depth, service)`` for determinism.

    The symptom service itself is included as the depth-0 candidate; every other candidate is
    strictly deeper. `max_depth` bounds how far the walk travels.
    """
    params = params or RCAParams()
    depth_cap = max_depth if max_depth is not None else params.max_depth
    root = normalise_service(symptom)

    candidates: list[Candidate] = [Candidate(service=root, depth=0, path=(root,))]
    visited = {root}
    queue: deque[Candidate] = deque([candidates[0]])

    while queue:
        current = queue.popleft()
        if current.depth >= depth_cap:
            continue
        for callee in graph.successors(current.service):
            if not graph.is_walkable(current.service, callee):
                continue
            if callee in visited:
                continue
            visited.add(callee)
            next_candidate = Candidate(
                service=callee,
                depth=current.depth + 1,
                path=(*current.path, callee),
            )
            candidates.append(next_candidate)
            queue.append(next_candidate)

    # Deterministic order: shallowest first, then alphabetical within a depth.
    candidates.sort(key=lambda c: (c.depth, c.service))
    return candidates
