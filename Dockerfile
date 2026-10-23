# syntax=docker/dockerfile:1.7
# InfraMind — single image, many entrypoints (standard 11 §11.2, D27).
#
# One image serves every InfraMind process. The chart's Deployments each set their own
# `command:`; `docker-compose.yml` does the same. The image ships no opinion about which
# stage it is running — that is the orchestrator's job.
#
# Stages:
#   builder — compiles wheels for every dependency into /install.
#   runtime — copies /install, runs as a non-root uid, no build toolchain.

ARG PY_VERSION=3.11

FROM python:${PY_VERSION}-slim AS builder
WORKDIR /build
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
# build-essential is a meta-package whose version tracks the Debian release rather than
# anything we control. Pinning it would break on every base-image bump without making the
# build any more reproducible -- PY_VERSION is the real pin.
# hadolint ignore=DL3008
RUN apt-get update \
 # hadolint ignore=DL3008: build-essential is a meta-package whose version tracks the Debian
 # release, not a pin we control. Pinning it would break on every base-image bump and would
 # not make the build any more reproducible -- the real inputs are pinned by PY_VERSION.
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --prefix=/install .

FROM python:${PY_VERSION}-slim AS runtime
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/usr/local/bin:${PATH}"

COPY --from=builder /install /usr/local

# uid 10001 — a fixed, non-root identity. The chart's securityContext references it
# numerically so a cluster with an existing `inframind` user still schedules cleanly.
RUN groupadd --gid 10001 inframind \
 && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin inframind \
 && mkdir -p /app/data \
 && chown -R inframind:inframind /app

# Source tree, for the RCA worker's editable-ish graph snapshot path and for tracebacks
# that name a real file rather than a zip entry.
COPY --from=builder --chown=inframind:inframind /build/src ./src

# /app/data is an emptyDir in the chart (D21 cold start: the Jaeger collector populates
# service-graph.json within seconds). Nothing is committed here.
USER 10001:10001

# Placeholder. Every chart Deployment and compose service overrides this.
CMD ["python", "-m", "inframind.main"]
