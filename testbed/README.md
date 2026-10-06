# Kubernetes testbed (Phase P1)

kind cluster, observability stack, Online Boutique, Chaos Mesh. See `docs/standards/09-testbed-ops.md` and the `testbed-ops` skill.

Layout:

- `kind/` — cluster config and bootstrap
- `helm-values/` — kube-prometheus-stack, Loki, Jaeger, boutique
- `chaos/` — reusable Chaos Mesh templates
