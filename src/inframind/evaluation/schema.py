"""The frozen result schema for P7 (standard 10.2).

Everything the paper can report is derived from `ResultRow` — one row per
(scenario, seed, repeat, method). The dataclass is **frozen**: a rename or a dropped field
becomes a paper bug, so the shape is pinned here and P9's `results-to-latex` reads these
names verbatim.

Three types live here:

* `ScenarioSpec` — one `scenario.yaml`, validated. A malformed scenario is a load error,
  never a silent skip.
* `ResultRow` — one measured run. The only thing scoring reads.
* `FixtureMeta` — the provenance of a replayed fixture directory.

The dataclasses are frozen and slotted rather than pydantic models on purpose: nothing
crosses a wire here, and `dataclasses.asdict` is the CSV serializer.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

ScenarioSet = Literal["dev", "test"]
MethodKind = Literal["infra", "baseline", "ablation"]


@dataclass(frozen=True, slots=True)
class ChaosSpec:
    """The Chaos Mesh resource a scenario injects. `kind` empty means no chaos at all."""

    kind: str = ""
    action: str = ""
    mode: str = ""
    selector: dict[str, str] = field(default_factory=dict)

    def to_yaml(self) -> dict[str, Any]:
        """Return the nested mapping a `chaos:` block would hold."""
        if not self.kind:
            return {}
        spec: dict[str, Any] = {"action": self.action, "mode": self.mode}
        if self.selector:
            spec["selector"] = dict(self.selector)
        return {"kind": self.kind, "spec": spec}


@dataclass(frozen=True, slots=True)
class ScenarioSpec:
    """One fault scenario. `expected_root_cause` is the label scoring compares against."""

    scenario_id: str
    set: ScenarioSet
    seed: int
    fault_type: str
    target_service: str
    target_namespace: str
    duration_s: float
    warmup_s: float
    cooldown_s: float
    expected_root_cause: str
    expected_affected_services: tuple[str, ...]
    chaos: ChaosSpec
    metrics_to_assert: tuple[str, ...]
    repeats: int
    status: Literal["runnable", "blocked"] = "runnable"
    block_reason: str = ""

    @property
    def is_blocked(self) -> bool:
        """True when this scenario cannot run until a locked decision resolves."""
        return self.status == "blocked"

    @property
    def fixture_dirname(self) -> str:
        """The fixture directory a replay of this scenario would live in."""
        return self.scenario_id


@dataclass(frozen=True, slots=True)
class ResultRow:
    """One measured run: (scenario, seed, repeat, method) -> did it find the root cause.

    `run_id` ties the row back to the raw fixture that produced it. A row without a
    `run_id` is rejected by `report.py` — that is the mechanical half of PUKU.md's
    "never invent a number".
    """

    scenario_id: str
    seed: int
    repeat: int
    method: str
    method_kind: MethodKind
    detected: bool
    top1_correct: bool
    top3_correct: bool
    reciprocal_rank: float
    rank: int
    detection_latency_s: float | None
    false_positive: bool
    scenario_set: ScenarioSet
    run_id: str
    expected_root_cause: str
    predicted_root_cause: str | None
    blocked: bool = False
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict, for the CSV/JSON writers."""
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ResultRow:
        """Rebuild a row from `as_dict` output. Raises `KeyError` on a missing field."""
        return cls(**raw)

    def as_csv_row(self) -> dict[str, Any]:
        """Return the row flattened for CSV: lists joined, None rendered as empty."""
        flat: dict[str, Any] = {}
        for key, value in self.as_dict().items():
            flat[key] = "" if value is None else value
        return flat


@dataclass(frozen=True, slots=True)
class FixtureMeta:
    """Provenance for one `evaluation/fixtures/<scenario>/` directory."""

    scenario_id: str
    seed: int
    repeat: int
    onset_ts: datetime
    window_s: float
    recorded_at: datetime | None = None

    @classmethod
    def from_json(cls, path: Path) -> FixtureMeta:
        """Parse `meta.json`. Raises `ValueError` when a required key is absent."""
        raw = json.loads(path.read_text())
        missing = {"scenario_id", "seed", "repeat", "onset_ts", "window_s"} - raw.keys()
        if missing:
            raise ValueError(f"{path} is missing required key(s): {sorted(missing)}")
        onset = raw["onset_ts"]
        recorded = raw.get("recorded_at")
        return cls(
            scenario_id=str(raw["scenario_id"]),
            seed=int(raw["seed"]),
            repeat=int(raw["repeat"]),
            onset_ts=_parse_ts(onset),
            window_s=float(raw["window_s"]),
            recorded_at=_parse_ts(recorded) if recorded else None,
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict."""
        return {
            "scenario_id": self.scenario_id,
            "seed": self.seed,
            "repeat": self.repeat,
            "onset_ts": self.onset_ts.isoformat(),
            "window_s": self.window_s,
            "recorded_at": self.recorded_at.isoformat() if self.recorded_at else None,
        }


def _parse_ts(value: Any) -> datetime:
    """Parse an ISO-8601 timestamp into an aware UTC datetime."""
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp {value!r} must be timezone-aware")
    return parsed.astimezone(UTC)


__all__ = [
    "ChaosSpec",
    "FixtureMeta",
    "MethodKind",
    "ResultRow",
    "ScenarioSet",
    "ScenarioSpec",
]
