"""Live change-watcher check on the kind testbed (D22, standard 14.10).

Needs `make up` (kind + the compose stack with a running change watcher). Creates a
throwaway ConfigMap in `default`, changes its data, deletes it, and asserts exactly one
`configmap_change` reaches Postgres through `GET /change-events`, and that it moves the probe
service's change-correlation score from 0 to above 0.5. The ConfigMap is labelled
`app=inframind-watcher-probe`, never a real service, so a probe that runs before a live
recording cannot leak into a fixture as a change on a Boutique service. Skips when the cluster
or the API is unreachable. kubectl only ever targets the `kind-inframind` context.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from inframind.common.ids import new_ulid
from inframind.common.time import utcnow
from inframind.rca.change_correlation import ChangeEvent, change_correlation

pytestmark = pytest.mark.integration

CONTEXT = "kind-inframind"
KUBECONFIG = Path(__file__).resolve().parents[2] / "testbed" / ".kube" / "config"
PROBE_SERVICE = "inframind-watcher-probe"
API = os.environ.get("INFRAMIND_API_URL", "http://localhost:8080")


def _kubectl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", f"--kubeconfig={KUBECONFIG}", f"--context={CONTEXT}", "-n", "default", *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _events(start, end) -> list[ChangeEvent]:  # type: ignore[no-untyped-def]
    response = httpx.get(
        f"{API}/change-events",
        params={"start": start.isoformat(), "end": end.isoformat()},
        timeout=5,
    )
    response.raise_for_status()
    return [ChangeEvent.model_validate(e) for e in response.json()["items"]]


@pytest.fixture
def testbed() -> None:
    if shutil.which("kubectl") is None or not KUBECONFIG.is_file():
        pytest.skip("no kubectl or testbed kubeconfig")
    if _kubectl("get", "configmaps", "-o", "name").returncode != 0:
        pytest.skip("kind-inframind is not reachable")
    try:
        _events(utcnow() - timedelta(seconds=1), utcnow())
    except (httpx.HTTPError, ValueError):
        pytest.skip("the InfraMind API (with /change-events) is not reachable")


def test_a_configmap_data_change_lands_in_postgres(testbed: None) -> None:
    name = f"inframind-watcher-probe-{new_ulid().lower()[-8:]}"
    start = utcnow()
    try:
        assert _kubectl("create", "configmap", name, "--from-literal=k=v1").returncode == 0
        assert _kubectl("label", "configmap", name, f"app={PROBE_SERVICE}").returncode == 0
        assert _kubectl("annotate", "configmap", name, "note=x").returncode == 0  # not a change
        time.sleep(1)
        changed = _kubectl("patch", "configmap", name, "-p", '{"data":{"k":"v2"}}')
        assert changed.returncode == 0
        deadline = time.monotonic() + 5
        mine: list[ChangeEvent] = []
        while time.monotonic() < deadline and not mine:
            time.sleep(0.5)
            mine = [e for e in _events(start, utcnow()) if e.resource == f"cm/{name}"]
    finally:
        _kubectl("delete", "configmap", name, "--ignore-not-found")

    assert len(mine) == 1, "expected exactly the data change (create/label/annotate are not)"
    event = mine[0]
    assert (event.service, event.kind) == (PROBE_SERVICE, "configmap_change")
    window = (event.occurred_at, event.occurred_at + timedelta(minutes=5))
    assert change_correlation(PROBE_SERVICE, window, []) == 0.0
    assert change_correlation(PROBE_SERVICE, window, mine) > 0.5
