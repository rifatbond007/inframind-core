#!/usr/bin/env bash
# testbed/chaos/install.sh — idempotent install of Chaos Mesh onto
# the `kind-inframind` cluster. Sources the D23-permitted shell
# helper. Run by `make chaos`.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPTS_LIB="$REPO_ROOT/scripts/lib/log.sh"

# shellcheck source=/dev/null
source "$SCRIPTS_LIB"

INFRAMIND_SCRIPT_NAME="chaos-install"

# --- knobs (env-overridable for tests) ---
CONTEXT_NAME="${TESTBED_CONTEXT:-kind-inframind}"
KUBECONFIG_PATH="${TESTBED_KUBECONFIG:-$REPO_ROOT/testbed/.kube/config}"
CHAOS_MESH_CHART_VERSION="${CHAOS_MESH_CHART_VERSION:-2.7.2}"
CHAOS_NAMESPACE="${CHAOS_TESTING_NS:-chaos-testing}"
CHAOS_MESH_NAMESPACE="${CHAOS_MESH_NS:-chaos-mesh}"
CHAOS_RELEASE="${CHAOS_RELEASE:-chaos-mesh}"
VALUES_DIR="$REPO_ROOT/testbed/helm-values"
CHAOS_CR_DIR="$REPO_ROOT/testbed/chaos"
WAIT_TIMEOUT="${CHAOS_WAIT_TIMEOUT:-600s}"

log_info "chaos-mesh install"
log_info "  context         : $CONTEXT_NAME"
log_info "  chart version   : $CHAOS_MESH_CHART_VERSION"
log_info "  values dir      : $VALUES_DIR"
log_info "  chaos-mesh ns   : $CHAOS_MESH_NAMESPACE"
log_info "  experiments ns  : $CHAOS_NAMESPACE"
log_info "  CRs (after helm): $CHAOS_CR_DIR"

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

# --- idempotency: create the namespaces once ---
log_info "ensuring namespaces exist"
for ns in "$CHAOS_MESH_NAMESPACE" "$CHAOS_NAMESPACE"; do
  if ! kubectl --kubeconfig "$KUBECONFIG_PATH" --context "$CONTEXT_NAME" \
       get namespace "$ns" >/dev/null 2>&1; then
    kubectl --kubeconfig "$KUBECONFIG_PATH" --context "$CONTEXT_NAME" \
      create namespace "$ns"
    log_ok "namespace '$ns' created"
  else
    log_ok "namespace '$ns' already exists"
  fi
done

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
helm_repo_add_if_missing chaos-mesh https://charts.chaos-mesh.org
helm repo update >/dev/null

# --- install: chaos-mesh ---
log_info "installing chaos-mesh chart $CHAOS_MESH_CHART_VERSION"
helm upgrade --install "$CHAOS_RELEASE" \
  chaos-mesh/chaos-mesh \
  --kube-context "$CONTEXT_NAME" \
  --kubeconfig "$KUBECONFIG_PATH" \
  --namespace "$CHAOS_MESH_NAMESPACE" \
  --version "$CHAOS_MESH_CHART_VERSION" \
  --values "$VALUES_DIR/chaos-mesh.yaml" \
  --wait --timeout "$WAIT_TIMEOUT"
log_ok "chaos-mesh ready"

# --- apply the 4 chaos CRs (in the experiments namespace) ---
# The CRs target `default` namespace pods but live in `chaos-testing`
# to keep experiment bookkeeping separate from the SUT.
log_info "applying 4 chaos CRs from $CHAOS_CR_DIR"
for cr in pod-kill.yaml network-delay.yaml cpu-stress.yaml stress-memory.yaml; do
  kubectl --kubeconfig "$KUBECONFIG_PATH" --context "$CONTEXT_NAME" \
    apply -f "$CHAOS_CR_DIR/$cr" -n "$CHAOS_NAMESPACE"
  log_ok "  applied $cr"
done

# --- summary ---
log_ok "chaos-mesh + 4 CRs are up"
log_info "  kubectl --context $CONTEXT_NAME get pods -n $CHAOS_MESH_NAMESPACE"
log_info "  kubectl --context $CONTEXT_NAME get podchaos,networkchaos,stresschaos -n $CHAOS_NAMESPACE"
log_info "  next: 'make smoke-testbed' (Slice 5) verifies the P1 → P2 gate end-to-end"
