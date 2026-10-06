---
name: helm-packaging
description: How to package InfraMind as a Helm chart — deploy/helm/inframind, values, RBAC, make up integration.
---

# Helm packaging (skill entrypoint)

**Canonical procedure:** [`docs/standards/11-helm-packaging.md`](../../../docs/standards/11-helm-packaging.md)

Before changing `deploy/helm/inframind/`:

1. Read [`docs/PROGRESS.md`](../../../docs/PROGRESS.md) — packaging is Phase P8.
2. Run `helm lint` before commit; no real secrets in `values.yaml`.
3. Log the session in `docs/PROGRESS.md` when done.

Owner: devops-packager agent (`Rifat`).
