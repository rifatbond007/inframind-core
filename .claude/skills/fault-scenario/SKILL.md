---
name: fault-scenario
description: How to author a fault-injection scenario YAML for the evaluation harness. Every scenario is reproducible from scenario_id + seed.
---

# Fault scenario

Every scenario is a YAML under `evaluation/scenarios/<scenario_id>/scenario.yaml`. It must be reproducible from `scenario_id + seed`.

## Schema

```yaml
scenario_id: "001-pod-kill-frontend"
seed: 42
description: "Kill one frontend pod and observe RCA."

# --- ground truth ---
fault_type: "pod_kill"
target_service: "frontend"
target_namespace: "default"
duration_s: 300
expected_root_cause: "frontend"
expected_affected_services:
  - "frontend"
  - "checkoutservice"

# --- Chaos Mesh recipe (or kubectl one-liner) ---
chaos:
  kind: "PodChaos"
  spec:
    action: "pod-kill"
    mode: "one"
    selector:
      namespaces: ["default"]
      labelSelectors:
        app: "frontend"

# --- observation window ---
warmup_s: 60          # wait for steady state before injecting
cooldown_s: 120       # wait after injection ends before recording "end of incident"

# --- evaluation hints ---
metrics_to_assert:
  - "MTTD < 60s"
  - "RCA top-1 = frontend"
repeats: 5
```

## Required fields

- `scenario_id` — kebab-case, globally unique.
- `seed` — used by every component that has randomness (Chaos Mesh pod selection, traffic load generator).
- `fault_type` — one of: `pod_kill`, `pod_restart_loop`, `cpu_stress`, `memory_pressure`, `network_delay`, `packet_loss`, `http_500`, `slow_downstream`, `db_unavailable`, `env_misconfig`, `log_error_spike`, `trace_latency`, `multi_fault`.
- `target_service` — the canonical service name (matches Online Boutique's service names).
- `expected_root_cause` — the service that the deterministic ranker should output as top-1.
- `expected_affected_services` — services that should appear in the blast radius.
- `repeats` — number of times to run this scenario (default 5 per D6).

## Forbidden

- A scenario whose fault is not actually a fault (`log_error_spike` is OK because it changes the system, but `no-op` is not).
- A scenario whose ground truth is ambiguous (e.g. CPU stress on a shared node).
- A scenario without a seed.

## How to author a new scenario

1. Pick a `scenario_id` (kebab-case).
2. Pick a `seed`. Don't reuse another scenario's seed.
3. Write the YAML. Use the schema as a starting point.
4. Run `make eval SCENARIO=<scenario_id>` to smoke-test it.
5. Confirm the deterministic ranker outputs the expected root cause.
6. Commit the YAML and a tiny integration test.

## Commit

- `eval(scenario): add <scenario_id>`
- Update `docs/PROGRESS.md` with the new row.