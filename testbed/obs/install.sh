#!/usr/bin/env bash
# testbed/obs/install.sh — idempotent install of the observability
# stack (kube-prometheus-stack + Loki + Jaeger) onto the
# `kind-inframind` cluster. Sources the D23-permitted shell helper.
# Run by `make obs`.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPTS_LIB="$REPO_ROOT/scripts/lib/log.sh"

# shellcheck source=/dev/null
source "$SCRIPTS_LIB"

INFRAMIND_SCRIPT_NAME="obs-install"

# --- knobs (env-overridable for tests) ---
CONTEXT_NAME="${TESTBED_CONTEXT:-kind-inframind}"
KUBECONFIG_PATH="${TESTBED_KUBECONFIG:-$REPO_ROOT/testbed/.kube/config}"
KPS_CHART_VERSION="${KPS_CHART_VERSION:-55.7.0}"
LOKI_CHART_VERSION="${LOKI_CHART_VERSION:-6.21.0}"
JAEGER_CHART_VERSION="${JAEGER_CHART_VERSION:-3.3.0}"
MONITORING_NAMESPACE="${TESTBED_NS:-monitoring}"
KPS_RELEASE="${KPS_RELEASE:-kube-prometheus-stack}"
LOKI_RELEASE="${LOKI_RELEASE:-loki}"
JAEGER_RELEASE="${JAEGER_RELEASE:-jaeger}"
VALUES_DIR="$REPO_ROOT/testbed/helm-values"
WAIT_TIMEOUT="${OBS_WAIT_TIMEOUT:-600s}"

log_info "observability install"
log_info "  context     : $CONTEXT_NAME"
log_info "  namespace   : $MONITORING_NAMESPACE"
log_info "  values dir  : $VALUES_DIR"
log_info "  versions    : kps=$KPS_CHART_VERSION, loki=$LOKI_CHART_VERSION, jaeger=$JAEGER_CHART_VERSION"

# --- pre-flight: kind on PATH ---
if ! command -v kind >/dev/null 2>&1; then
  log_fail "kind not found on PATH. Run: make kind-check"
  exit 1
fi
log_ok "kind $(kind version | awk '{print $2}')"

# --- pre-flight: helm on PATH ---
if ! command -v helm >/dev/null 2>&1; then
  log_fail "helm not found on PATH. Run: make helm-check"
  exit 1
fi
log_ok "helm $(helm version --short)"

# --- pre-flight: cluster reachable ---
if ! kind get clusters 2>/dev/null | grep -qx "inframind"; then
  log_fail "kind cluster 'inframind' not found. Run: make kind"
  exit 1
fi
log_ok "kind cluster 'inframind' is up"

# --- idempotency: create the namespace once ---
log_info "ensuring namespace '$MONITORING_NAMESPACE' exists"
if ! kubectl --kubeconfig "$KUBECONFIG_PATH" --context "$CONTEXT_NAME" \
     get namespace "$MONITORING_NAMESPACE" >/dev/null 2>&1; then
  kubectl --kubeconfig "$KUBECONFIG_PATH" --context "$CONTEXT_NAME" \
    create namespace "$MONITORING_NAMESPACE"
  log_ok "namespace '$MONITORING_NAMESPACE' created"
else
  log_ok "namespace '$MONITORING_NAMESPACE' already exists"
fi

# --- helm repos (add only if missing) ---
log_info "ensuring helm repos"
helm_repo_add_if_missing() {
  local name="$1"
  local url="$2"
  if helm repo list 2>/dev/null | awk '{print $1}' | grep -qx "$name"; then
    log_ok "  repo '$name' already present"
  else
    helm repo add "$name" "$url" >/dev/null
    log_ok "  repo '$name' added ($url)"
  fi
}
helm_repo_add_if_missing prometheus-community https://prometheus-community.github.io/helm-charts
helm_repo_add_if_missing grafana              https://grafana.github.io/helm-charts
helm_repo_add_if_missing jaegertracing        https://jaegertracing.github.io/helm-charts
# Jaeger 3.3.0 depends on the incubator/cassandra and bitnami/elasticsearch
# sub-charts (gated by provisionDataStore.{cassandra,elasticsearch}). Even
# when we disable those, helm still pulls them to resolve dependencies.
helm_repo_add_if_missing incubator            https://charts.helm.sh/incubator
helm_repo_add_if_missing bitnami              https://charts.bitnami.com/bitnami
helm repo update >/dev/null

# --- install: kube-prometheus-stack ---
log_info "installing kube-prometheus-stack chart $KPS_CHART_VERSION"
helm upgrade --install "$KPS_RELEASE" \
  prometheus-community/kube-prometheus-stack \
  --kube-context "$CONTEXT_NAME" \
  --kubeconfig "$KUBECONFIG_PATH" \
  --namespace "$MONITORING_NAMESPACE" \
  --version "$KPS_CHART_VERSION" \
  --values "$VALUES_DIR/kube-prometheus-stack.yaml" \
  --wait --timeout "$WAIT_TIMEOUT"
log_ok "kube-prometheus-stack ready"

# --- install: Loki ---
log_info "installing loki chart $LOKI_CHART_VERSION (single-binary mode)"
helm upgrade --install "$LOKI_RELEASE" \
  grafana/loki \
  --kube-context "$CONTEXT_NAME" \
  --kubeconfig "$KUBECONFIG_PATH" \
  --namespace "$MONITORING_NAMESPACE" \
  --version "$LOKI_CHART_VERSION" \
  --values "$VALUES_DIR/loki.yaml" \
  --wait --timeout "$WAIT_TIMEOUT"
log_ok "loki ready"

# --- install: Jaeger ---
log_info "installing jaeger chart $JAEGER_CHART_VERSION (allInOne strategy)"
helm upgrade --install "$JAEGER_RELEASE" \
  jaegertracing/jaeger \
  --kube-context "$CONTEXT_NAME" \
  --kubeconfig "$KUBECONFIG_PATH" \
  --namespace "$MONITORING_NAMESPACE" \
  --version "$JAEGER_CHART_VERSION" \
  --values "$VALUES_DIR/jaeger.yaml" \
  --wait --timeout "$WAIT_TIMEOUT"
log_ok "jaeger ready"

# --- summary ---
log_ok "observability stack is up"
log_info "  kubectl --context $CONTEXT_NAME get pods -n $MONITORING_NAMESPACE"
log_info "  Prometheus  : http://localhost:30090 (NodePort)"
log_info "  Grafana     : http://localhost:30300 (NodePort)"
log_info "  Alertmanager: http://localhost:30093 (NodePort)"
log_info "  Loki ready  : http://localhost:30100/ready"
log_info "  Jaeger UI   : http://localhost:30686 (NodePort)"
log_info "  Jaeger OTLP : localhost:30317 (gRPC), localhost:30318 (HTTP)  (D1)"
log_info "  next: slice 4 -> make boutique (Online Boutique) and make chaos (Chaos Mesh)"