# Test fixtures

## `quiet_cluster_signals.ndjson`

Real Prometheus signals recorded from the kind testbed: `cpu_cores` and
`memory_working_set_bytes` for the eleven Online Boutique workloads, one `Signal` JSON per line,
sorted by `ts`. 3516 signals, 2026-10-18 18:46:07 to 20:14:48 UTC (89 minutes, 30-second poll).

During those minutes the restart counters never increased (no pod was replaced), but the traffic
generator was started and stopped repeatedly, so CPU rises and falls. That makes it a hard quiet
baseline: the detectors must stay silent on idle-versus-busy swings.

It is a **regression guard on the noise floors, not an accuracy claim.** Whether the detectors
find real faults is measured on the evaluation scenarios (P7), never here.

Regenerate (testbed up, collectors having run for a while):

```python
import json, subprocess
from inframind.common.signal import Signal

raw = subprocess.run(
    [
        "docker",
        "exec",
        "inframind-redis",
        "redis-cli",
        "--json",
        "XRANGE",
        "stream:signals",
        "-",
        "+",
    ],
    capture_output=True,
    text=True,
    check=True,
).stdout
signals = [Signal.model_validate(json.loads(fields[1])) for _, fields in json.loads(raw)]
keep = sorted(
    (
        s
        for s in signals
        if s.source.value == "prometheus"
        and s.attrs["metric"] in ("cpu_cores", "memory_working_set_bytes")
    ),
    key=lambda s: s.ts,
)
open("tests/fixtures/quiet_cluster_signals.ndjson", "w").write(
    "\n".join(s.model_dump_json() for s in keep) + "\n"
)
```

Choose a stretch in which `container_restarts_total` does not increase for any pod, and keep
the file under 5 MB.
