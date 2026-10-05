# 9. Testbed ops — bring up, verify, tear down, hard rules
> standards · Process · InfraMind. Pairs with the `testbed-ops` skill in `.claude/skills/testbed-ops/SKILL.md`. This file is the rationale and the common-issue index.

## 9.1 Bring up — the single command

```bash
make up
```

This is the single command to rule them all. It brings up, in order:

1. **Docker Compose** — Redis 7 and PostgreSQL 16 (in `inframind-net`).
2. **`kind` cluster** — 1 control + 2 workers (`kind-inframind`).
3. **Helm — kube-prometheus-stack** — Prometheus + Alertmanager + Grafana.
4. **Helm — Loki** — log storage with Promtail / Grafana Alloy.
5. **Helm — Jaeger / OTel Collector** — distributed tracing.
6. **Helm — Chaos Mesh** — fault injection (P7+).
7. **`kubectl apply` — Online Boutique + load generator** — system under test.
8. **`kubectl apply -f deploy/helm/inframind/`** — InfraMind itself, once the Helm chart lands
   in P8.

The order matters: dependencies must be up before dependents. Chaos Mesh is added at step 6
because it is only useful once an SUT is running.

## 9.2 Verify

```bash
make smoke-testbed
```

The smoke-test runs per-component health checks:

| Check | Expected |
|---|---|
| `redis-cli ping` | `PONG` |
| `psql` connect | connects, `inframind` DB exists |
| `kubectl get pods -A` | all pods `Running` |
| `curl http://localhost:9090/-/ready` | Prometheus ready |
| `curl http://localhost:3100/ready` | Loki ready |
| `curl http://localhost:16686/` | Jaeger UI up |
| Chaos Mesh pod-kill injected | Prometheus, Loki, Jaeger all see it |

A green smoke-test is the precondition for any evaluation run. If a component is not ready,
fix the bring-up; do not start the eval.

## 9.3 Tear down

```bash
make down
```

Brings everything down and removes volumes. Use this when:

- Switching test app.
- Tearing down the cluster for a CI step.

`make down` does not delete the kind Docker container by default; pass `DOWN_VOLUMES=1` to also
wipe the volumes. The Postgres and Redis volumes are gitignored.

## 9.4 Common issues

| Symptom | Cause | Fix |
|---|---|---|
| Chaos Mesh pods `CrashLoopBackOff` | Runtime not containerd. | Set `chaosDaemon.runtime=containerd` in the Chaos Mesh Helm values. |
| Prometheus OOMKilled | The default retention + scrape density is too high. | Reduce `--retention-size=10GB` and `--retention-time=6h` in the Helm values. |
| Loki OOMKilled | Same. | Reduce ingestion rate; switch to a smaller schema. |
| Memory pressure on host | Online Boutique + Prometheus + Loki + Jaeger + Chaos Mesh is tight on 16 GB. | 32 GB is recommended. Local VMs are fine; laptops struggle. |
| Port collision on 6379 / 5432 / 9090 / 3100 / 16686 / 9093 | A previous run, a sibling project, or a system service. | Free the port, or edit the docker-compose / Helm values to remap. |
| `kubectl` against the wrong context | The default context may not be `kind-inframind`. | Always pass `--context kind-inframind`. Or set `kubectl config use-context kind-inframind` for the session. |
| Jaeger shows no traces | The OTel Collector is mis-piped, or the Online Boutique app is not instrumented. | Check the Online Boutique sidecar / DaemonSet. Online Boutique ships with OTel instrumentation; if the env vars are not set, no traces. |
| Chaos Mesh web UI unreachable | Port-forward needed. | `kubectl port-forward -n chaos-mesh svc/chaos-dashboard 2333:2333 --context kind-inframind` |
| Online Boutique pods pending | Insufficient cluster resources. | Reduce replicas. |

## 9.5 The contexts rule

Every `kubectl` / `helm` invocation in InfraMind runs against a `kind-*` context. Never against
production or a shared cluster. Hard-coded pre-commit and CI checks assume `kind-inframind` is
the default during the bring-up; the alternative is to pass `--context kind-inframind` on every
invocation.

**The most-violated rule.** A reviewer who sees `kubectl apply` without a context argument in a
PR rejects it. The repo has shipped against the wrong context once. It will not again.

## 9.6 What never goes in the testbed

- **Real secrets.** `INFRAMIND_LLM_API_KEY` is set from the host env, not committed to a
  ConfigMap. `--set secretRef.LLM_API_KEY=$OPENAI_API_KEY` is the install path.
- **Real customer data.** Online Boutique is a sample app. It carries no PII.
- **Production-looking ingress.** No public LB. Port-forwards only.

## 9.7 The OS-side checklist (a brief checklist)

Before any evaluation run:

- [ ] `make up` completed without error.
- [ ] `make smoke-testbed` is green.
- [ ] Chaos Mesh web UI is reachable.
- [ ] The scenario YAML's `seed` is set.
- [ ] No production context is current (`kubectl config current`).

## 9.8 Hard rules

- **`kubectl`/`helm` only against `kind-*` contexts.** Never against the default context. Never
  against a production cluster.
- **Never commit kubeconfigs.** `.gitignore` blocks `kubeconfig*`, `*.kubeconfig`.
- **Bring up down.** If `make up` partially succeeds, `make down` and start over; do not
  investigate mid-state.
- **The smoke-test is the precondition.** No eval run without it.

## 9.9 Cross-reference

- **Skill:** `.claude/skills/testbed-ops/SKILL.md`.
- **Compose:** `docker-compose.yml` at the repo root.
- **Helm:** `11-helm-packaging.md` in this set — the InfraMind chart itself.
- **Evaluation:** `10-evaluation.md` in this set — how the testbed becomes numbers.
