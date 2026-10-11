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
# CLUSTER_NAME is the *kind cluster name* (what `kind create/delete cluster
# --name` accepts). CONTEXT_NAME is the kubectl context (kind adds the
# `kind-` prefix automatically). They differ: cluster=inframind,
# context=kind-inframind. The Makefile threads TESTBED_CLUSTER_NAME +
# TESTBED_CONTEXT; this script defaults to the standard names if unset.
CLUSTER_NAME="${TESTBED_CLUSTER_NAME:-inframind}"
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
if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  log_fail "kind cluster '$CLUSTER_NAME' not found. Run: make kind"
  exit 1
fi
log_ok "kind cluster '$CLUSTER_NAME' is up"

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

# --- install: kube-prometheus-stack CRDs (subchart) ---
# Chart 48+ split the prometheus-operator CRDs out of the main chart
# into a separate subchart (charts/crds in the helm package). The
# main chart's `crds.enabled: true` flag is a leftover from chart
# 47.x and no longer auto-installs the CRDs — you have to install
# the subchart as its own release BEFORE the main chart or the
# operator will fail with `no matches for kind "Prometheus" in
# version "monitoring.coreos.com/v1"`.
#
# Mechanism: the subchart at $CHART_DIR/charts/crds/ ships the 10
# CRD YAMLs (one per kind — Alertmanager, AlertmanagerConfig,
# PodMonitor, Probe, Prometheus, PrometheusAgent, PrometheusRule,
# ScrapeConfig, ServiceMonitor, ThanosRuler) under its own crds/
# directory. Helm 3 auto-loads files under a chart's `crds/` dir
# as `kind: CustomResourceDefinition` resources; we install the
# subchart as its own release so its CRDs are managed independently
# of the main chart (so `helm uninstall kube-prometheus-stack`
# won't drop the CRDs, and CRD upgrades can be done in isolation).
# Ref: https://github.com/prometheus-community/helm-charts/blob/main/charts/kube-prometheus-stack/UPGRADE.md
KPS_CRDS_RELEASE="${KPS_CRDS_RELEASE:-kube-prometheus-stack-crds}"
log_info "installing kube-prometheus-stack CRDs (subchart, release=$KPS_CRDS_RELEASE)"
KPS_CHART_CACHE_DIR="$REPO_ROOT/.cache/kube-prometheus-stack-charts"
KPS_CHART_DIR="$KPS_CHART_CACHE_DIR/kube-prometheus-stack"
if [ ! -d "$KPS_CHART_DIR/charts/crds" ]; then
  log_info "  pulling chart kube-prometheus-stack@$KPS_CHART_VERSION to $KPS_CHART_DIR"
  mkdir -p "$KPS_CHART_CACHE_DIR"
  # `helm pull --untar` extracts the chart into a directory named after
  # the chart (here: kube-prometheus-stack/) inside --untardir. So the
  # crds subchart ends up at $KPS_CHART_DIR/charts/crds/.
  helm pull prometheus-community/kube-prometheus-stack \
    --version "$KPS_CHART_VERSION" --untar --untardir "$KPS_CHART_CACHE_DIR" >/dev/null
fi
# shellcheck disable=SC2086
helm upgrade --install "$KPS_CRDS_RELEASE" "$KPS_CHART_DIR/charts/crds" \
  --kube-context "$CONTEXT_NAME" \
  --kubeconfig "$KUBECONFIG_PATH" \
  --namespace "$MONITORING_NAMESPACE" \
  --wait --timeout "$WAIT_TIMEOUT"
log_ok "kube-prometheus-stack CRDs ready"

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
