# 11. Helm packaging — `deploy/helm/inframind/`
> standards · Process · InfraMind. Pairs with the `helm-packaging` skill in `.claude/skills/helm-packaging/SKILL.md`. This file is the rationale.

## 11.1 The scope

InfraMind is packaged as a Helm chart under `deploy/helm/inframind/`. **Production deployment
is out of scope (D8)** — this is a research artifact. The chart exists so a fresh machine can
run `make up` and have InfraMind up alongside Online Boutique, the observability stack, and
Chaos Mesh.

The chart is reproducible, lint-clean, and ships with a default `values.yaml` that contains
**no real secrets**.

## 11.2 Chart layout

```
deploy/helm/inframind/
├── Chart.yaml
├── values.yaml                 # defaults — no real secrets
├── templates/
│   ├── _helpers.tpl
│   ├── configmap.yaml
│   ├── secret.yaml             # LLM_API_KEY, POSTGRES_PASSWORD (override only)
│   ├── serviceaccount.yaml
│   ├── rbac.yaml               # read-only K8s API access
│   ├── deployment-api.yaml     # inframind.api.app
│   ├── deployment-ingestion.yaml   # 3 containers: prom / loki / jaeger collectors
│   ├── deployment-detection.yaml   # inframind.correlation.worker
│   ├── deployment-rca.yaml     # inframind.rca.worker + emptyDir /app/data
│   ├── service.yaml            # ClusterIP for the api
│   ├── ingress.yaml            # disabled by default; D8 says no public Ingress
│   ├── servicemonitor.yaml     # api /metrics on 8080
│   └── podmonitor.yaml         # worker /metrics on 9100
└── tests/
    └── helm-lint.sh
```

**Four Deployments, not seven (D27).** `standard 11` historically listed a
`deployment-llm.yaml` and a `deployment-alerting.yaml` alongside the five
stage Deployments. Those two are **deferred**: alerting runs inside the `api`
process as `api.app.AlertRouter`, and the LLM explainer runs inside the `rca`
worker as a synchronous `llm.explain()` call. See
[D27-InProcessAlertingLlm](../decision-tree.md#d27-inprocessalertingllm-alerting-and-the-llm-explainer-stay-in-process).

One `Deployment` per pipeline stage keeps the failure domains separate. A
detector crash does not take the API down with it.

### Why the ingestion Deployment has three containers

`deployment-ingestion.yaml` is a single Deployment with three containers
sharing one Pod, one ServiceAccount token, one emptyDir, and one scheduling
decision. This is "one Deployment per stage" applied one stage further: the
prometheus / loki / jaeger collectors *are* the ingestion stage, and they all
poll the same observability-stack pods, so they share a failure domain. Giving
each its own Deployment would add two pods to `kubectl get pods` whose health
carries no independent information.

### Why detection and correlation are one Deployment

`deployment-detection.yaml` runs `python -m inframind.correlation.worker`,
which reads `stream:signals`, runs the `DetectionEngine`, and writes
incidents back to the bus. Detection and correlation share state (the
`BaselineFreezeCoordinator` lease table that D4 owns), so splitting them into
two Deployments would require a coordination channel that neither stage has a
reason to open. The `DetectionEngine` is instantiated inside the correlation
worker; there is no separate detection entrypoint.

### The RCA Deployment pins one replica

`deployment-rca.yaml` is the only Deployment that must run exactly one
replica. The RCA worker holds the single-writer / single-reader in-memory
`ServiceGraph` (D21, `13-service-graph-contract.md`); a second replica would
duplicate the ranking work and split the stream. Its `/app/data` is an
`emptyDir` (medium `Memory`, 64 Mi) that implements D21's cold start: the
jaeger collector writes `service-graph.json` within seconds, and a missing
file is a cold graph — never a crash.

## 11.3 `values.yaml` — the defaults

`image.repository` defaults to `ghcr.io/rifatbond007/inframind-core`, which is
derived from `pyproject.toml`'s `[project.urls] Homepage`. A
`test_helm_chart_values_repository_matches_pyproject` test asserts the two
agree — the previously committed `ghcr.io/bayust-cse/inframind` was a drift
between this file and the package metadata.

```yaml
image:
  repository: ghcr.io/rifatbond007/inframind-core
  tag: "0.0.0"
  pullPolicy: IfNotPresent

resources:
  requests:
    cpu: 200m
    memory: 256Mi
  limits:
    cpu: 1
    memory: 1Gi

resourcesRca:                   # the RCA worker holds the in-memory ServiceGraph
  requests:
    cpu: 200m
    memory: 512Mi
  limits:
    cpu: 1
    memory: 2Gi

config:                         # rendered into the ConfigMap, consumed via envFrom
  REDIS_URL: "redis://inframind-redis:6379/0"
  POSTGRES_URL: "postgresql://inframind:inframind@inframind-postgres:5432/inframind"
  PROMETHEUS_URL: "http://kube-prometheus-stack-prometheus:9090"
  LOKI_URL: "http://loki:3100"
  JAEGER_URL: "http://jaeger-query:16686"
  ALERTMANAGER_WEBHOOK_URL: "http://inframind-api:8080/webhooks/alertmanager"
  METRICS_PORT: "9100"
  INFRAMIND_RCA_GRAPH_PATH: "/app/data/service-graph.json"
  INFRAMIND_WATCH_NAMESPACES: "boutique,inframind"

secretRef:
  LLM_API_KEY: ""               # set via `helm install --set secretRef.LLM_API_KEY=...`
  INFRAMIND_ALERT_WEBHOOK_SECRET: ""

existingSecret: ""              # point the chart at a Secret you manage yourself

rca:
  replicaCount: 1               # pinned: D21 single-writer graph
  graphData:
    medium: Memory
    sizeLimit: 64Mi
```

Defaults are sane for a `kind` cluster. Production-scale values are out of scope (D8).

## 11.4 The secret discipline

- **No real secrets in `values.yaml`.** `LLM_API_KEY` and `POSTGRES_PASSWORD` are placeholders.
- **Real values are passed at install time** via `--set secretRef.LLM_API_KEY=$OPENAI_API_KEY`,
  or via a sealed-secret / external-secrets operator.
- **`secret.yaml` is a `helm template` placeholder.** The chart ships a `Secret` resource
  with `stringData: { llm-api-key: "" }`; the operator populates it.
- **No `sealed-secrets` CRDs in the chart.** A team that wants sealed secrets adds them on
  top of the chart, not inside it.

## 11.5 RBAC

`rbac.yaml` defines a `ServiceAccount` and a `Role` with **read-only** access to:

- `pods`, `pods/log` (for change correlation)
- `events` (for K8s rollouts, ConfigMap changes)
- `configmaps` (read-only)
- `secrets` (read-only, D22's extension — the watcher records *that* a Secret
  changed, never its value)
- `services`, `endpoints` (service discovery for the graph's cold start)
- `apps`: `deployments`, `statefulsets`, `daemonsets`, `replicasets`
- `autoscaling`: `horizontalpodautoscalers`

Every verb is `get` / `list` / `watch`. The `Role` does not include write or
exec. A writer that needs `pods/exec` is wrong.

`rbac.watchNamespaces` widens the read scope beyond the release namespace by
emitting an additional `Role` + `RoleBinding` **pair per namespace** — a
namespaced `Role` cannot grant access outside its own namespace, so a
cross-namespace watch needs a binding in each watched namespace. This stays
read-only and namespaced; the chart never emits a `ClusterRole`.

## 11.6 `servicemonitor.yaml` and `podmonitor.yaml`

The chart ships both. They let the bundled Prometheus scrape InfraMind's own `/metrics`
endpoint. Every stage exports Prometheus metrics:

- `inframind_signals_received_total` (per source, per type)
- `inframind_anomalies_emitted_total` (per detector)
- `inframind_incidents_opened_total` (per service)
- `inframind_rca_ranking_duration_seconds` (histogram)
- `inframind_llm_call_duration_seconds` (histogram)
- `inframind_alerts_sent_total` (per channel, per severity)

The `ServiceMonitor` selector matches the chart's labels.

## 11.7 Build and lint

```bash
helm lint deploy/helm/inframind
helm template inframind deploy/helm/inframind > /tmp/rendered.yaml
helm package deploy/helm/inframind -d dist/
```

`helm lint` is run in CI. A red lint blocks merge. The CI step is a single shell command in
`.github/workflows/ci.yml`; the script is `deploy/helm/inframind/tests/helm-lint.sh`.

## 11.8 Install (dev)

```bash
helm install inframind deploy/helm/inframind \
  --namespace inframind \
  --create-namespace \
  --set secretRef.LLM_API_KEY=$OPENAI_API_KEY
```

The `--set` overrides the placeholder at install time. Nothing is committed with the real
value. The team policy: real values come from `~/.config/inframind/secrets.env` or from
CI secrets, never from a committed file.

## 11.9 What never goes in the chart

- Real API keys, passwords, kubeconfigs.
- A public-facing `Ingress` (D8: no production deployment).
- An autoscaler (`HPA`) — resource requests are sized for a single replica; the cluster is
  `kind`, not prod.
- A `PodDisruptionBudget` is fine; a `HorizontalPodAutoscaler` is out of scope.

## 11.10 Hard rules

- **No real secrets in `values.yaml`.** A reviewer who sees a non-empty `stringData` rejects
  the PR. The `helm-lint.sh` script enforces this mechanically, and
  `test_helm_chart_ships_no_real_secret_in_secret_yaml` enforces it in pytest.
- **Read-only RBAC.** A writer that needs write or exec is wrong. No `ClusterRole`.
- **`helm lint` clean.** CI enforces, via `deploy/helm/inframind/tests/helm-lint.sh`.
- **One Deployment per failure domain.** A combined `Deployment` with all stages is wrong.
  Note this is *not* the same as "one Deployment per stage": per D27, alerting and
  the llm share a process with an existing Deployment rather than getting their own.
- **Exactly four Deployments.** `helm-lint.sh` counts `kind: Deployment` in the
  rendered output and fails on any number other than 4, so a re-added
  `deployment-llm.yaml` cannot merge without a D-change.

## 11.11 Cross-reference

- **Skill:** `.claude/skills/helm-packaging/SKILL.md`.
- **Locked decisions:** D8 (out of scope), D9 (redaction), D20 (storage is truth),
  D21 (single-writer graph), D22 (change correlation's K8s watch),
  **D27-InProcessAlertingLlm** (four Deployments, not seven).
- **Testbed bring-up:** `09-testbed-ops.md` in this set — `make up` includes the chart once
  InfraMind itself is Helm-packaged (P8). `make up-app` is the P8 entry point: it builds
  the image, loads it into kind, and installs the chart. `make up` is unchanged.
- **Metrics:** `src/inframind/metrics.py` — the six families in section 11.12.
- **Dockerfile:** the repo-root `Dockerfile`. There is no `deploy/docker/` directory — one
  image serves every process, and the orchestrator picks the entrypoint via `command:`.

## 11.12 The six metrics and how the chart scrapes them

`src/inframind/metrics.py` declares exactly six families against a **private**
`CollectorRegistry`. The names are pinned here, in `Chart.yaml`'s annotations,
and in `tests/unit/test_metrics.py`.

| Metric | Type | Labels | Increment site |
|---|---|---|---|
| `inframind_signals_received_total` | Counter | `source`, `type` | `common/bus.py::publish` |
| `inframind_anomalies_emitted_total` | Counter | `detector` | `detection/engine.py::process` |
| `inframind_incidents_opened_total` | Counter | `service` | `correlation/tracker.py::_open` |
| `inframind_rca_ranking_duration_seconds` | Histogram | none | `rca/rank.py::rank_candidates` |
| `inframind_llm_call_duration_seconds` | Histogram | none | `llm/pipeline.py::explain` |
| `inframind_alerts_sent_total` | Counter | `channel`, `severity` | `alerting/router.py::dispatch` |

Three rules about the instrumentation:

1. **It must never raise into a caller's control flow.** Every `record_*` /
   `observe_*` helper wraps its increment in `contextlib.suppress(Exception)`.
   A metrics failure must not take down a collector.
2. **`bus.publish` counts only routable signals.** A signal dropped for an
   unresolved service returns `None` and does not increment — the counter
   means "reached the bus", not "was constructed".
3. **A cache hit in `llm.explain` observes `0.0`,** not the real elapsed time.
   The histogram's `_count` is then "explain calls", and the latency
   distribution is not polluted by sub-microsecond cache reads.

### Two scrape paths, on purpose

- **`ServiceMonitor` → the `api` Service, port 8080.** The `api` process serves
  `/healthz`, `/readyz`, the REST surface *and* `/metrics` from one uvicorn
  server on one port. Its `/metrics` body is **two concatenated blocks**: the
  dependency-free `inframind_up` / `inframind_uptime_seconds` head that P6's
  testbed already scrapes (byte-identical — an existing contract), then
  `metrics.render_extra()` for the six families. `test_metrics_head_comes_before_the_pipeline_block`
  pins the order.
- **`PodMonitor` → every worker pod, port 9100.** `prometheus_client.start_http_server(9100)`
  runs in each worker's `amain` on a daemon thread, leaving the event loop
  untouched so no worker needs an extra HTTP dependency. A `PodMonitor` rather
  than a `Service` target because Prometheus needs *all* replicas, and a
  worker pod's IP changes on every rolling restart. The port is overridable per
  deployment via `METRICS_PORT`.

Both monitors ship **disabled by default** (`serviceMonitor.enabled`,
`podMonitor.enabled`) because the CRDs only exist when
`kube-prometheus-stack` is installed; enabling them on a cluster without the
`monitoring.coreos.com` API group would make `helm install` fail on an unknown
kind. `make up` installs the observability stack first, so a testbed run can
opt in with `--set serviceMonitor.enabled=true --set podMonitor.enabled=true`.

A bind failure on 9100 is logged as a structured warning and swallowed. A
worker that cannot expose metrics is still a worker that processes signals —
losing observability is not a reason to lose the pipeline.
