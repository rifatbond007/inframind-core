"""Live runner: applies Chaos Mesh, records signals, scores a real run (standard 10.5).

This module is the **only** place in `evaluation/` that calls `kubectl`. Two guards hold the
PUKU.md rules:

* **Context must start with `kind-`.** Reading `~/.kube/config` is forbidden; we read the
  configured kubeconfig (which the testbed puts at `testbed/.kube/config`) and refuse any
  context that does not look like a local kind cluster.
* **Blocked scenarios need an explicit flag.** With `D2-Chaos-runtime` still PROPOSED, four
  scenarios need StressChaos/NetworkChaos, which the docker runtime does not inject against
  a containerd node. We default to refusing them.

Because no docker daemon exists on this dev box, this module ships **unexecuted**; every
test against it is `pytest.skip`-style. The replay path in `replay.py` is the gate that
runs green on this host.

Recording (writing fixtures during a live run) is a follow-up with the first real P7 run.
For now `live.py` reads and runs, never writes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from inframind.common.logging import get_logger

from ..config import EvalParams
from ..scenarios import BLOCKED_REASON, fixture_dir_for
from ..schema import ScenarioSpec

log = get_logger(__name__)

KIND_CONTEXT_PREFIX = "kind-"
KUBECONFIG_ENV = "KUBECONFIG"

__all__ = [
    "KIND_CONTEXT_PREFIX",
    "BlockedScenarioError",
    "LiveRunner",
    "WrongKubeContext",
]


class WrongKubeContext(RuntimeError):
    """The active kubeconfig context is not a `kind-*` cluster (PUKU.md)."""


class BlockedScenarioError(RuntimeError):
    """The scenario is blocked by D2-Chaos-runtime and `allow_blocked` is False."""


class LiveRunner:
    """The (read-only) chaos-injecting runner; `replay.py` is the deterministic counterpart.

    Construction parses the kubeconfig context. `run_scenario` is the only public method;
    it is intentionally heavy and synchronous because `kubectl` commands are.
    """

    def __init__(self, kubeconfig_path: Path | None = None) -> None:
        self._kubeconfig = Path(kubeconfig_path) if kubeconfig_path else self._resolve_kubeconfig()
        self._context = self._parse_context()

    @property
    def context(self) -> str:
        """The active cluster context. Always starts with `kind-` or construction raises."""
        return self._context

    def _resolve_kubeconfig(self) -> Path:
        """Pick the kubeconfig the testbed owns, never `~/.kube/config`."""
        explicit = os.environ.get(KUBECONFIG_ENV, "").strip()
        if explicit:
            return Path(explicit)
        repo_local = Path("testbed/.kube/config")
        if repo_local.exists():
            return repo_local.resolve()
        raise WrongKubeContext(
            "no KUBECONFIG and testbed/.kube/config missing; refuse to touch ~/.kube/config"
        )

    def _parse_context(self) -> str:
        """Read the current-context from kubeconfig; raise unless it starts with `kind-`."""
        if shutil.which("kubectl") is None:
            raise WrongKubeContext("kubectl not on PATH; refusing to run live scenarios")
        if not self._kubeconfig.exists():
            raise WrongKubeContext(f"{self._kubeconfig} does not exist")
        proc = subprocess.run(
            [
                "kubectl",
                f"--kubeconfig={self._kubeconfig}",
                "config",
                "current-context",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise WrongKubeContext(f"kubectl config current-context failed: {proc.stderr.strip()}")
        context = proc.stdout.strip()
        if not context.startswith(KIND_CONTEXT_PREFIX):
            raise WrongKubeContext(
                f"context {context!r} is not a kind cluster (must start with {KIND_CONTEXT_PREFIX!r})"
            )
        return context

    def run_scenario(
        self,
        spec: ScenarioSpec,
        *,
        env: Mapping[str, str] | None = None,
        record_to: Path | None = None,
        allow_blocked: bool = False,
    ) -> None:
        """Apply the chaos, wait the warmup, capture, then remove the chaos. (NO-OP until fixtures exist.)

        Args:
            spec: the scenario to run.
            env: optional env mapping; defaults to `os.environ`.
            record_to: directory to write a new fixture record (not implemented yet).
            allow_blocked: pass True to run scenarios blocked by D2-Chaos-runtime anyway.

        Raises:
            BlockedScenarioError: `spec.is_blocked` and `allow_blocked` is False.
            WrongKubeContext: context check failed during construction.

        Notes:
            Recording fixtures during a live run is a follow-up. This method currently
            raises `NotImplementedError` with a message explaining why — its job is to lock
            down the context + block guards so the first real run inherits correct behaviour.
        """
        if spec.is_blocked and not allow_blocked:
            raise BlockedScenarioError(
                f"{spec.scenario_id} is blocked ({spec.block_reason or BLOCKED_REASON}); "
                "set INFRAMIND_EVAL_ALLOW_BLOCKED=1 to override."
            )

        fixtures = fixture_dir_for(spec, EvalParams())
        if not fixtures.exists():
            log.warning(
                "live_run_no_fixture_yet",
                scenario_id=spec.scenario_id,
                fixture_dir=str(fixtures),
            )

        raise NotImplementedError(
            "Live chaos injection + fixture recording lands with the first real P7 run. "
            "Context and blocked-scenario guards are wired so the recording path inherits "
            "them; replay.py is the gate that runs on this dev box."
        )


def list_blocked_reasons(scenarios: Sequence[ScenarioSpec]) -> dict[str, str]:
    """Return `{scenario_id: block_reason}` for every blocked scenario in `scenarios`."""
    return {spec.scenario_id: spec.block_reason for spec in scenarios if spec.is_blocked}
