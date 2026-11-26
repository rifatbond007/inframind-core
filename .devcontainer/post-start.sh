#!/usr/bin/env bash
# Runs on every codespace start (D31). kind + Chaos Mesh + Prometheus exhaust the default
# inotify limit (testbed/kind/bootstrap.sh refuses to start below 512), and the limit resets
# when the codespace stops.
set -uo pipefail

sudo sysctl -q -w fs.inotify.max_user_instances=1024 fs.inotify.max_user_watches=524288 \
  || echo "[post-start] WARN: could not raise the inotify limits; make kind will refuse to start"

# Prefer the CI-built image from GHCR over a local build (falls back to building in `make up`).
cd "$(dirname "$0")/.." && make -s image-pull || true
