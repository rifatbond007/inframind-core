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
├── kind/              (cluster.yaml + bootstrap.sh)
├── obs/               (install.sh + jaeger-nodeports.yaml)
├── helm-values/       (kube-prometheus-stack, loki, alloy, jaeger, chaos-mesh)
├── boutique/          (Online Boutique kustomize + memory/tracing patches)
├── chaos/             (5 Chaos Mesh CRs + install.sh)
└── smoke.sh           (the P1 -> P2 gate)
```

`make up` runs the whole chain: kind, obs, boutique, chaos, then Compose.

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
| 6 | Re-create `testbed/chaos/pod-kill.yaml`, wait up to 90s | Prometheus shows a **newer** `kube_pod_created` for the frontend (pod-kill replaces the pod, so the restart counter never moves) **and** Loki has logs from the killed pod and its replacement (Alloy ships pod logs) **and** Jaeger has `frontend` traces (smoke sends a few requests through a port-forward, because the load generator is scaled to 0) |

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

**Each row is prefixed with the slice that introduces the file. Slices 1–5 are
landed on `dev-rifat`; rows from those slices apply today.**

| Slice | Symptom | Likely cause | Fix |
|-------|---------|--------------|-----|
| [S2] `make kind` | `make kind` times out during `kind create cluster` | Not enough free RAM (need ~3 GiB for the bootstrap) | `docker system prune -af`; close other apps; drop to a 1c/0w `cluster.yaml` if desperate |
| [S2] `make up` (re-run) | `make up` fails with `free memory is 1864 MiB; minimum is 2048 MiB` even though the cluster already exists | The bootstrap pre-flight runs the RAM check before the cluster-exists check, so a re-run of `make up` after `make obs` (which fills the host with chart images) hits a misleading "free memory" error | The bootstrap now runs docker-reachability + cluster-exists *before* the RAM check; the RAM check only gates the create path. The error means the cluster is genuinely being created and you really are low on RAM — `docker system prune -af`. |
| [S3] `make obs` | `UPGRADE FAILED: no matches for kind "Alertmanager" in version "monitoring.coreos.com/v1" ensure CRDs are installed first` (also `Prometheus`, `PrometheusRule`, `ServiceMonitor`, `PodMonitor`, `Probe`, `ThanosRuler`, `AlertmanagerConfig`, `PrometheusAgent`, `ScrapeConfig`) | kube-prometheus-stack chart 48+ split the prometheus-operator CRDs into a separate subchart at `charts/crds/`; the main chart's `crds.enabled: true` flag is leftover and no longer auto-installs them | `testbed/obs/install.sh` now installs the subchart as a separate release (`kube-prometheus-stack-crds`) *before* the main chart. The subchart's `crds/` dir ships the 10 real `kind: CustomResourceDefinition` YAMLs; Helm 3 auto-loads them. If the upgrade fails, run `make down && make up` — partial state is not safe to debug. |
| [S4] `make chaos` | `ImagePullBackOff` on `chaos-daemon-*` | The image tag is `2.7.2` (no `v`); ghcr.io publishes chaos-mesh images with a `v` prefix (e.g. `v2.7.2`) and the chart's default tag is `v2.7.2` | `testbed/helm-values/chaos-mesh.yaml` pins `chaosDaemon.image.tag: v2.7.2`. Do not "fix" by removing the `v` — it must be there. |
| [S4] `make chaos` | `error validating: unknown field "spec.scheduler"` (or `no matches for kind "Workflow"`) when applying a CR | chaos-mesh 2.7.x **removed the `scheduler` field** from `PodChaos`/`StressChaos`/`NetworkChaos`; scheduling moved to a separate `Workflow` resource | All 5 CRs in `testbed/chaos/` no longer set `spec.scheduler.cron`. They fire once on apply (bounded by `duration: 5m`). P7 evaluation will wrap the CRs in `Workflow` resources for repeating-fault eval. |
| [S4] `make chaos` | `error validating: spec.target.mode: Unsupported value: ""` on a NetworkChaos CR | NetworkChaos 2.7.2 made `spec.target.mode` REQUIRED (enum: `one`/`all`/`fixed`/`fixed-percent`/`random-max-percent`) | `testbed/chaos/network-delay.yaml` sets `target.mode: one` (sibling of `target.selector`). Use `mode: all` to inject into every destination pod instead of just one. |
| [S4] `chaos-mesh` | `chaos-daemon` is `CrashLoopBackOff` | The kind node's in-container containerd socket isn't visible to the chaos-daemon Pod | `testbed/kind/cluster.yaml` `extraMounts` bind-mounts `/run/containerd/containerd.sock` to `/var/run/docker.sock` inside the kind node container; `testbed/helm-values/chaos-mesh.yaml` reads it via `chaosDaemon.runtime=docker` + `socketPath=/var/run/docker.sock`. If the mount is missing, `make kind` will fail loudly (D2 readiness check, exit 1). On a SELinux-enforcing host, also try `selinuxRelabel: true`. |
| [S4] `chaos-mesh` | `chaos-controller-manager` pod is `SysctlForbidden` (admission rejects `fs.nr-open`) | The kind cluster's pod security admission enforces the "baseline" policy in modern kind versions, and `fs.nr-open` is an "unsafe" sysctl | The `controllerManager.securityContext.sysctls: fs.nr-open` block is intentionally **not** set. The combination of `targetNamespace: default`, the 4 enabled controllers, and the 4 enabled webhooks keeps the controller's fd usage under the chart's default limit. If a future chart upgrade re-introduces the fd pressure, raise the controller's `GOGC` env to 200 (long-running soak) rather than re-introducing the sysctl — see the header comment in `testbed/helm-values/chaos-mesh.yaml`. |
| [S3] `kube-prometheus-stack` | Prometheus pod is `OOMKilled` | The 768 MiB cap is too tight for your scrape volume | Drop to 512 MiB in `testbed/helm-values/kube-prometheus-stack.yaml` and reduce `retention` to 4h. The header comment in that file documents the levers. |
| [S3] `loki` | Loki pod is `OOMKilled` | WAL growth because `compactor.replicas=0` | Acceptable for short P1 dev runs. For longer ones, enable the compactor in the values file. |
| [S3] `loki` | `curl http://localhost:30100/ready` returns 404 | The chart's gateway nginx doesn't proxy `/ready` (it tries to serve it as a static file). Loki itself is ready — verify with `kubectl get pod -n monitoring loki-0 -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'` (returns `True`) or `curl http://localhost:30100/loki/api/v1/status/buildinfo` (returns the version). The slice 5 smoke test uses the buildinfo path. |
| [S2] host | Pods CrashLoop with `too many open files` (Chaos Mesh controller, Prometheus, Loki) | The host's `fs.inotify.max_user_instances` (default 128) is exhausted by the kind nodes plus every pod's watchers | `sudo sysctl -w fs.inotify.max_user_instances=1024 fs.inotify.max_user_watches=524288`; persist in `/etc/sysctl.d/99-kind.conf`. `bootstrap.sh` fails fast below 512. |
| [S4] `chaos-mesh` | CRs are accepted but nothing happens (empty `.status`, no events, no pod killed) | Either `enabledControllers` was restricted (disables Chaos Mesh's shared lifecycle controllers) or `clusterScoped: false` (controller ignores CRs outside `targetNamespace`) | Keep both at the chart defaults / `clusterScoped: true`, as in `helm-values/chaos-mesh.yaml`. |
| [S3] `loki` | `/loki/api/v1/labels` returns `too many unhealthy instances in the ring` | Single binary without `commonConfig.replication_factor: 1` | Set in `helm-values/loki.yaml`. `buildinfo` passes even when this is broken. |
| [S3] `jaeger` | Services send traces but the UI shows none, or `connection refused` on 4317 | (a) collector and query as separate pods with in-memory storage do not share spans; (b) OTLP receiver bound to localhost | Jaeger runs allInOne with `COLLECTOR_OTLP_*_HOST_PORT=0.0.0.0:*`; NodePorts come from `obs/jaeger-nodeports.yaml`. Migrating an old release needs `helm uninstall jaeger -n monitoring` first. |
| (always) | `make up` fails with `connection refused` / `EOF` right after "cluster already exists" | Node containers stopped or wedged (host reboot, containerd sandbox reservations) | `bootstrap.sh` restarts the nodes and waits; if still wedged: `make down && make up`. |
| (always) | `kubectl` complains about the wrong context | You ran a command without `--context kind-inframind` | Always pass `--context kind-inframind`, or `kubectl config use-context kind-inframind` once per session. This is the **most-violated rule** (standard 09 §9.5). |
| [S4] `boutique` | Online Boutique pods `Pending` or `OOMKilled` | Insufficient cluster resources, or uncapped services hitting the 7 GiB ceiling | Drop the per-service limits in `testbed/boutique/memory-patches.yaml`. Slice 4 caps all 11 services. |
| [S4] `chaos-mesh` | `oom-kill` CR fires but the wrong container is killed | The CR targets `app=frontend` without an explicit `containerNames`; a future OTel sidecar would be the first container in the Pod and would be killed instead of `server` | `testbed/chaos/oom-kill.yaml` pins `containerNames: [server]`. The deterministic container-kill is distinct from `stress-memory` (which holds memory until the kernel OOM-killer fires — that one is non-deterministic about *which* process is killed and uses `oomScoreAdj: 1000` to bias the kernel toward the stress-allocator). |
| [S2] `cluster.yaml` | Port already in use (9090/3100/16686/9093/4317/4318) | A previous run, a sibling project, or a host service | Stop the other consumer, or change the host-side `extraPortMappings` in `testbed/kind/cluster.yaml`. 4317/4318 (OTLP) are added in slice 2. |
| [S3] `jaeger` | Jaeger UI loads but `curl /api/services` is empty | Boutique's OTel sidecar is exporting OTLP but the Jaeger allInOne image is not configured to accept it | Verify the chart values file sets the OTLP receiver config. Slice 3 documents the exact path. |
| [S5] `make smoke-testbed` | C6 prom fails: no new frontend pod after 90s | The pod-kill CR fires once on apply and was not reconciled (see the Chaos Mesh row above), or the chaos-daemon is down | `kubectl -n chaos-testing describe podchaos boutique-frontend-pod-kill` (look for `Injected`); `kubectl -n chaos-mesh get pods`. |

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
