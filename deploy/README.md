# Deployment packaging (Phase P8)

Docker images and the Helm chart for InfraMind workers + API. See `docs/standards/11-helm-packaging.md` and the `helm-packaging` skill.

Layout:

- `docker/` — per-service Dockerfiles (when split from root `Dockerfile`)
- `helm/inframind/` — chart for the full InfraMind stack
