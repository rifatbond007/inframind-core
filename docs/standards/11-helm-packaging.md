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
│   ├── deployment-api.yaml
│   ├── deployment-ingestion.yaml
│   ├── deployment-detection.yaml
│   ├── deployment-correlation.yaml
│   ├── deployment-rca.yaml
│   ├── deployment-llm.yaml
│   ├── deployment-alerting.yaml
│   ├── service.yaml
│   ├── servicemonitor.yaml     # /metrics scrape config for Prometheus
│   └── podmonitor.yaml
└── tests/
    └── helm-lint.sh
```

One `Deployment` per pipeline stage keeps the failure domains separate. A detector crash
does not take the API down with it.

## 11.3 `values.yaml` — the defaults

```yaml
image:
  repository: ghcr.io/bayust-cse/inframind
  tag: "0.0.0"
  pullPolicy: IfNotPresent

resources:
  requests:
    cpu: 200m
    memory: 256Mi
  limits:
    cpu: 1
    memory: 1Gi

envFrom:
  - configMapRef:
      name: inframind-config

secretRef:
  LLM_API_KEY: ""               # set via `helm install --set secretRef.LLM_API_KEY=...`

redis:
  url: "redis://inframind-redis:6379/0"

postgres:
  url: "postgresql://inframind:inframind@inframind-postgres:5432/inframind"

prometheus:
  url: "http://kube-prometheus-stack-prometheus:9090"
loki:
  url: "http://loki:3100"
jaeger:
  url: "http://jaeger-query:16686"
alertmanager:
  webhookUrl: "http://inframind-api:8080/alertmanager"
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

The `Role` does not include write or exec. A writer that needs `pods/exec` is wrong.

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
  the PR.
- **Read-only RBAC.** A writer that needs write or exec is wrong.
- **`helm lint` clean.** CI enforces.
- **One Deployment per stage.** A combined `Deployment` with all stages is wrong — failure
  isolation matters.

## 11.11 Cross-reference

- **Skill:** `.claude/skills/helm-packaging/SKILL.md`.
- **Locked decisions:** D8 (out of scope), D9 (redaction), D20 (storage is truth).
- **Testbed bring-up:** `09-testbed-ops.md` in this set — `make up` includes the chart once
  InfraMind itself is Helm-packaged (P8).
- **Image:** `deploy/docker/` — per-service Dockerfiles.
