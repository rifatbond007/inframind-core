# Kubernetes testbed (Phase P1)

The testbed is the runtime every other phase hits. P3 collectors scrape it,
P5 ranks over the trace-derived service graph, P7 injects faults into it.
**If the testbed is not green, no later phase is green.**

This runbook is the operational entry point. The authority is
[`docs/standards/09-testbed-ops.md`](../standards/09-testbed-ops.md). If they
disagree, the standard wins.

## 1. Layout

```
testbed/
├── README.md          (this file — operational runbook)
├── kind/              (cluster.yaml + bootstrap.sh — lands in P1 slice 2)
├── helm-values/       (per-component values — lands in P1 slice 3 + 4)
├── boutique/          (Online Boutique kustomize — lands in P1 slice 4)
└── chaos/             (Chaos Mesh CRs — lands in P1 slice 4)
```

The full `make up` chain is the P1 slice 5 deliverable.

## 2. Prerequisites

You need three CLI tools **on your `PATH`** before any of the `make` targets
in this runbook will work. The Makefile has `*-check` targets that verify
each one with the pinned version.

| Tool    | Pinned version | Install |
|---------|---------------|---------|
| `kind`  | `v0.24.0`     | [kind releases](https://github.com/kubernetes-sigs/kind/releases) — pick the `linux-amd64` asset and place it on your `PATH` |
| `kubectl` | `v1.30.0`   | [kubectl install](https://kubernetes.io/docs/tasks/tools/install-kubectl-linux/) — pinned at the minor we test against |
| `helm`  | `v3.15.4`     | [helm install](https://helm.sh/docs/intro/install/) — `curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 \| bash` then `helm version` to confirm |

If you don't have write access to `/usr/local/bin`, install to `~/.local/bin`
(it is on the default `PATH` for this dev box) and add the check:

```bash
make kind-check helm-check kubectl-check
```

Each target prints the exact install command if the tool is missing or the
version is wrong. **Do not skip the version pin** — chart and image
compatibility is sensitive.

You also need `docker` (this dev box has 29.x; ≥ 24 is fine) and ≥ 6 GiB
of free RAM for the slim default profile, or ≥ 16 GiB for the
`make testbed-full` profile.

## 3. Quick start (slim default, ≤ 5.5 GiB)

```bash
# from the repo root
make up            # bring up Compose (Redis + Postgres) AND the full testbed chain
make smoke-testbed # prove the P1 → P2 gate
make down          # tear everything down
```

> **Slice 5 status (2026-10-11):** `make up` chains the full testbed
> (kind → observability → Online Boutique → Chaos Mesh → Compose) and
> `make smoke-testbed` runs the 6-check P1 → P2 gate. If you run `make up`
> and `make smoke-testbed` returns 6/6 green, the testbed is ready for
> P2 (schema + event bus). If any check fails, follow the hint printed
> by the failing check (or see §6 below).

The slim default is designed for this dev box (7 GiB total RAM). It runs a
1c1w kind cluster, single-replica observability pods with hard memory caps,
and Online Boutique with all 11 services capped at conservative limits
(documented in slice 4). If a pod OOMs, see [§6 Troubleshooting](#6-troubleshooting)
before opening a ticket.

## 4. The P1 → P2 gate

`make smoke-testbed` runs the verification table from standard 09 §9.2 plus
the pod-kill chaos assertion from the P1 → P2 phase gate:

| # | Check | Expected |
|---|-------|----------|
| 1 | `docker compose ps` | redis + postgres Up |
| 2 | `kubectl get pods -A` (all namespaces) | no Pods in `Pending`, `CrashLoopBackOff`, `ImagePullBackOff`, or `Error` |
| 3 | Prometheus `/api/v1/status/runtimeinfo` | 200 OK |
| 4 | Loki `/loki/api/v1/status/buildinfo` (through gateway) | 200 with `version` field (the gateway's nginx doesn't proxy `/ready`; the pod's own readiness probe uses `/ready` directly and is passing — verified by `kubectl get pod loki-0 -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'` = `True`) |
| 5 | Jaeger UI `/` | HTML page loads |
| 6 | Apply `testbed/chaos/pod-kill.yaml`, wait 30s | Prometheus records a `kube_pod_container_status_restarts_total` increment **and** Loki shows a `Killing` log line **and** Jaeger shows a `frontend` trace ending in error within the last 5 min |

A green smoke test is the precondition for any later phase work. If any
check is red, `make down` and start over — do not investigate mid-state
(standard 09 §9.8).

## 5. Teardown

```bash
make down
```

This deletes the `inframind` kind cluster and brings down the Compose stack
**including volumes** (the Postgres and Redis volumes are gitignored, so
`make down` is the clean reset). If you don't have `kind` on your `PATH`,
only the Compose stack is torn down — install kind and re-run to fully
clean up.

## 6. Troubleshooting

**The rows below reference files that land in slices 2-5 of P1. Each row is prefixed
with the slice that introduces the file.** Today (after slice 1 only) most of these
fixes won't apply — the files don't exist yet.

| Slice | Symptom | Likely cause | Fix |
|-------|---------|--------------|-----|
| [S2] `make kind` | `make kind` times out during `kind create cluster` | Not enough free RAM (need ~3 GiB for the bootstrap) | `docker system prune -af`; close other apps; drop to a 1c/0w `cluster.yaml` if desperate |
| [S4] `chaos-mesh` | `chaos-daemon` is `CrashLoopBackOff` | `chaosDaemon.runtime` is not set to `containerd`, **or** the kind node's containerd socket is not exposed to the chaos-daemon Pod (cluster.yaml needs `extraMounts` for `/run/containerd/containerd.sock`) | The values file in `testbed/helm-values/chaos-mesh.yaml` sets `runtime=containerd`. The `cluster.yaml` extraMounts is the second half — slice 4 documents the exact patch. |
| [S3] `kube-prometheus-stack` | Prometheus pod is `OOMKilled` | The 768 MiB cap is too tight for your scrape volume | Drop to 512 MiB in `testbed/helm-values/kube-prometheus-stack.yaml` and reduce `retention` to 4h. The header comment in that file documents the levers. |
| [S3] `loki` | Loki pod is `OOMKilled` | WAL growth because `compactor.replicas=0` | Acceptable for short P1 dev runs. For longer ones, enable the compactor in the values file. |
| [S3] `loki` | `curl http://localhost:30100/ready` returns 404 | The chart's gateway nginx doesn't proxy `/ready` (it tries to serve it as a static file). Loki itself is ready — verify with `kubectl get pod -n monitoring loki-0 -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'` (returns `True`) or `curl http://localhost:30100/loki/api/v1/status/buildinfo` (returns the version). The slice 5 smoke test uses the buildinfo path. |
| (always) | `kubectl` complains about the wrong context | You ran a command without `--context kind-inframind` | Always pass `--context kind-inframind`, or `kubectl config use-context kind-inframind` once per session. This is the **most-violated rule** (standard 09 §9.5). |
| [S4] `boutique` | Online Boutique pods `Pending` or `OOMKilled` | Insufficient cluster resources, or uncapped services hitting the 7 GiB ceiling | Drop the per-service limits in `testbed/boutique/memory-patches.yaml`. Slice 4 caps all 11 services. |
| [S2] `cluster.yaml` | Port already in use (9090/3100/16686/9093/4317/4318) | A previous run, a sibling project, or a host service | Stop the other consumer, or change the host-side `extraPortMappings` in `testbed/kind/cluster.yaml`. 4317/4318 (OTLP) are added in slice 2. |
| [S3] `jaeger` | Jaeger UI loads but `curl /api/services` is empty | Boutique's OTel sidecar is exporting OTLP but the Jaeger allInOne image is not configured to accept it | Verify the chart values file sets the OTLP receiver config. Slice 3 documents the exact path. |

For deeper landmines, read [`docs/standards/09-testbed-ops.md`](../standards/09-testbed-ops.md) §9.4 in full.

## 7. Memory profiles

| Profile | When to use | What it costs |
|---------|-------------|---------------|
| **slim default** (the default) | This dev box, 7-8 GiB RAM | 1c1w, replicas=1, 6h retention, ≤ 5.5 GiB working set |
| **`make testbed-full`** (commented in the Makefile) | 16-32 GiB machine, supervisor or eval runs | 2c2w, replicas=2, 24h retention, ≤ 12 GiB working set |

Switching profiles is a value-file change plus a `make down && make up`
cycle. Do not edit values files mid-run.

## 8. Cross-reference

- Standard: [`docs/standards/09-testbed-ops.md`](../standards/09-testbed-ops.md) — the authority
- Phase gate: [`.claude/skills/phase-gates/SKILL.md`](../skills/phase-gates/SKILL.md) — the P1 → P2 checklist
- D-decisions: [`docs/decision-tree.md`](../decision-tree.md) — D5 (Online Boutique), D21 (service graph contract), D22 (K8s change watcher), D23 (`scripts/`), D24 (pre-commit hooks)
- Testbed engineer agent: [`.claude/agents/build/testbed-engineer.md`](../agents/build/testbed-engineer.md) — owned scope

## 9. Known risks (design review, 2026-10-11)

The following design risks were identified in a P1 review and are tracked
into the slices that fix them. They are **not** blockers for slice 1
(tooling + runbook), but they will be the first three red checks on a
fresh `make up` if not addressed.

| # | Risk | Slice that fixes it |
|---|------|---------------------|
| D1 | Online Boutique exports **OTLP** (gRPC 4317, HTTP 4318); the Jaeger `allInOne` chart needs an explicit OTLP receiver config + port-forwards in `cluster.yaml` | S2 (port maps) + S3 (chart values) |
| D2 | Chaos Mesh `chaosDaemon` runs inside the kind cluster and needs the **kind node's** `/run/containerd/containerd.sock` (not the host's). `cluster.yaml` must mount the kind node container's rootfs at that path. | S2 (cluster.yaml extraMounts) + S4 (chaos-mesh values) |
| D3 | Online Boutique's 11 services are not all capped upstream. The Java `ad` service and the Go `frontend` are the heaviest; on a 7 GiB host uncapped services OOM-loop. | S4 (rename `cartservice-patch.yaml` → `memory-patches.yaml`, add limits to all 11) |
| D4 | `make smoke-testbed` will curl `localhost:9090/3100/16686/9093`, but kube-prometheus-stack, Loki, and Jaeger expose `ClusterIP` by default. The services must be `NodePort` and the `extraPortMappings` must map to the nodePort range. | S2 (port maps) + S3 (NodePort services) |
