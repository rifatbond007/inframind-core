---
name: testbed-ops
description: Operational runbook for the InfraMind testbed — kind cluster, observability backends, Chaos Mesh, Online Boutique.
---

# Testbed ops

## Bring up

```bash
make up
```

This is the single command to rule them all. It brings up (in order):

1. Docker Compose: Redis, Postgres (in `inframind-net`).
2. `kind` cluster: 1 control + 2 workers (`kind-inframind`).
3. Helm: kube-prometheus-stack (Prometheus + Alertmanager + Grafana).
4. Helm: Loki + Promtail / Grafana Alloy.
5. Helm: Jaeger / OTel Collector.
6. Helm: Chaos Mesh.
7. kubectl apply: Online Boutique + load generator.
9. InfraMind: `kubectl apply -f deploy/helm/inframind/` once Helm chart lands in P8.

## Verify

```bash
make smoke-testbed
```

Runs the per-component health checks:

- `redis-cli ping` → `PONG`
- `psql` → connects.
- `kubectl get pods -A` → all pods `Running`.
- `curl http://localhost:9090/-/ready` → Prometheus ready.
- `curl http://localhost:3100/ready` → Loki ready.
- `curl http://localhost:16686/` → Jaeger UI up.
- Inject a pod-kill fault, confirm Prometheus, Loki, and Jaeger all see it.

## Tear down

```bash
make down
```

Brings everything down and removes volumes.

## Common issues

- **Chaos Mesh kind runtime** — Chaos Mesh requires the containerd socket. Configure `chaosDaemon.runtime=containerd` in the Helm values.
- **Memory pressure** — Online Boutique + Prometheus + Loki + Jaeger + Chaos Mesh + Redis + Postgres is tight on 16 GB. Recommend 32 GB or a VM.
- **Port collisions** — if any of 6379 / 5432 / 9090 / 3100 / 16686 / 9093 are already taken, the bring-up fails fast. Either free the port or edit the docker-compose / Helm values.

## Hard rules

- `kubectl` / `helm` only against `kind-inframind` (or `kind-*` prefix). Never against production or shared clusters.
- Never commit kubeconfigs. `.gitignore` blocks `kubeconfig*`, `*.kubeconfig`.
- Critically: **never run `kubectl` against the default context**. Always pass `--context kind-inframind`.