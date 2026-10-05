# syntax=docker/dockerfile:1.7
# Multi-stage image for InfraMind services.
# Step 0: builds the package and runs an empty placeholder.
# Later phases will switch the CMD per service.

ARG PY_VERSION=3.11

FROM python:${PY_VERSION}-slim AS builder
WORKDIR /build
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
COPY src ./src
RUN pip install --prefix=/install .

FROM python:${PY_VERSION}-slim AS runtime
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/usr/local/bin:${PATH}"
COPY --from=builder /install /usr/local
COPY src ./src

# Default entrypoint — a placeholder. Each service overrides CMD in Step 8+.
CMD ["python", "-m", "inframind.main"]
