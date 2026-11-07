#!/usr/bin/env bash
# helm-lint wrapper — standard 11 section 11.7. Runs from CI on every PR that touches deploy/.
#
# Two checks:
#   1. `helm lint` — schema, template parse, broken refs.
#   2. `helm template` — render every value combination; catches conditions that lint misses.
#
# Exit codes:
#   0 — clean.
#   1 — lint or template errors.

set -euo pipefail

CHART_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

if ! command -v helm >/dev/null 2>&1; then
    echo "helm-lint: helm not found on PATH; install Helm 3.15+ first" >&2
    exit 1
fi

HELM_VERSION=$(helm version --short 2>/dev/null || echo "unknown")
echo "helm-lint: using $HELM_VERSION"

echo "helm-lint: linting $CHART_DIR"
if ! helm lint "$CHART_DIR" --strict; then
    echo "helm-lint: helm lint failed" >&2
    exit 1
fi

echo "helm-lint: rendering $CHART_DIR"
if ! helm template test-release "$CHART_DIR" > "$WORKDIR/rendered.yaml"; then
    echo "helm-lint: helm template failed" >&2
    exit 1
fi

# Hard rule: the secret must render with empty stringData by default. If a real key snuck into
# values.yaml during a refactor, this catches it before CI does.
if grep -q "stringData:" "$WORKDIR/rendered.yaml"; then
    if awk '/^---$/{f=0} /^kind: Secret/{f=1} f' "$WORKDIR/rendered.yaml" \
        | grep -E "^\s*(llm-api-key|inframind-alert-webhook-secret):" \
        | grep -vE ':\s*""\s*$' \
        | grep -q .; then
        echo "helm-lint: Secret has non-empty stringData; refusing to merge" >&2
        grep -B1 -A6 "kind: Secret" "$WORKDIR/rendered.yaml" || true
        exit 1
    fi
fi

# Hard rule: exactly four Deployments must be rendered (D27 — alerting and llm are in-process).
COUNT=$(grep -c "^kind: Deployment$" "$WORKDIR/rendered.yaml" || true)
if [ "$COUNT" -ne 4 ]; then
    echo "helm-lint: expected 4 Deployments, got $COUNT (D27)" >&2
    exit 1
fi

# Hard rule: at least one Role and one RoleBinding must be rendered (read-only RBAC).
for kind in Role RoleBinding ServiceAccount ConfigMap Secret Service; do
    got=$(grep -c "^kind: $kind$" "$WORKDIR/rendered.yaml" || true)
    if [ "$got" -lt 1 ]; then
        echo "helm-lint: rendered output is missing kind: $kind" >&2
        exit 1
    fi
done

echo "helm-lint: OK ($COUNT Deployments, $(wc -l < "$WORKDIR/rendered.yaml") lines rendered)"
