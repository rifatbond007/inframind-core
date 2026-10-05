---
name: helm-packaging
description: How to package InfraMind as a Helm chart — `deploy/helm/inframind/`. Reproducible packaging, not production-scale.
---

# Helm packaging

InfraMind is packaged as a Helm chart under `deploy/helm/inframind/`. **Production deployment is out of scope (D8)** — this is a research artifact.

## Chart layout

```
deploy/helm/inframind/
├── Chart.yaml
├── values.yaml           # defaults — no real secrets
├── templates/
│   ├── _helpers.tpl
│   ├── configmap.yaml
│   ├── secret.yaml       # LLM_API_KEY, POSTGRES_PASSWORD (override only)
│   ├── serviceaccount.yaml
│   ├── rbac.yaml         # read-only K8s API access
│   ├── deployment-api.yaml
│   ├── deployment-ingestion.yaml
│   ├── deployment-detection.yaml
│   ├── deployment-correlation.yaml
│   ├── deployment-rca.yaml
│   ├── deployment-llm.yaml
│   ├── deployment-alerting.yaml
│   ├── service.yaml
│   ├── servicemonitor.yaml   # /metrics scrape config
│   └── podmonitor.yaml
└── tests/
    └── helm-lint.sh
```

## values.yaml (defaults)

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
  LLM_API_KEY: ""        # set via `helm install --set secretRef.LLM_API_KEY=...`

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

## Hard rules

- **No real secrets in `values.yaml`.** Use `helm install --set` or a sealed-secret / external-secrets operator.
- Resource requests/limits for every container.
- `servicemonitor.yaml` and `podmonitor.yaml` so InfraMind scrapes itself with Prometheus.
- Read-only RBAC for the K8s API.

## Build & lint

```bash
helm lint deploy/helm/inframind
helm template inframind deploy/helm/inframind > /tmp/rendered.yaml
helm package deploy/helm/inframind -d dist/
```

## Install (dev)

```bash
helm install inframind deploy/helm/inframind \
  --namespace inframind \
  --create-namespace \
  --set secretRef.LLM_API_KEY=$OPENAI_API_KEY
```

## Commit

- `chore(helm): <change>`
- Run `helm lint` before committing.
- Update `docs/PROGRESS.md` with the new row.