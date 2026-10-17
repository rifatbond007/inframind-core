#!/usr/bin/env bash
# testbed/kind/bootstrap.sh — idempotent create-or-reuse for the
# kind-inframind cluster (P1 slice 2). Sources the D23-permitted
# shell helper from scripts/lib for color/log output.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPTS_LIB="$REPO_ROOT/scripts/lib/log.sh"

# shellcheck source=/dev/null
source "$SCRIPTS_LIB"

INFRAMIND_SCRIPT_NAME="kind-bootstrap"

# --- knobs (env-overridable for tests) ---
# The kind cluster's *own* name (passed to `kind create cluster --name`) and
# the kubectl *context* name are different. kind's convention is to prefix
# the cluster name with `kind-` when generating the context. To keep the
# canonical context `kind-inframind` we name the cluster `inframind` and let
# kind produce the `kind-` prefix for us. Override CLUSTER_NAME and/or
# CONTEXT_NAME if you want both under your control (e.g. for tests).
CLUSTER_NAME="${TESTBED_CLUSTER_NAME:-inframind}"
CONTEXT_NAME="${TESTBED_CONTEXT:-kind-inframind}"
KUBECONFIG_PATH="${TESTBED_KUBECONFIG:-$REPO_ROOT/testbed/.kube/config}"
CLUSTER_YAML="$REPO_ROOT/testbed/kind/cluster.yaml"
FREE_MIN_MIB="${FREE_MIN_MIB:-2048}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-180s}"

log_info "kind cluster bootstrap"
log_info "  cluster    : $CLUSTER_NAME"
log_info "  context    : $CONTEXT_NAME"
log_info "  kubeconfig : $KUBECONFIG_PATH"
log_info "  config     : $CLUSTER_YAML"

# --- pre-flight 1: kind on PATH ---
if ! command -v kind >/dev/null 2>&1; then
  log_fail "kind not found on PATH. Run: make kind-check"
  exit 1
fi
log_ok "kind $(kind version | awk '{print $2}')"

# --- pre-flight 2: docker reachable (cheap, do before memory) ---
if ! docker info >/dev/null 2>&1; then
  log_fail "docker not reachable. Start Docker and retry."
  exit 1
fi
log_ok "docker reachable"

# --- idempotency: skip if cluster already exists ---
# If the cluster is up, no new memory needs to be allocated; skip the
# RAM check so a re-run of `make up` after `make obs` (which fills the
# host with chart images) doesn't fail with a misleading "free memory"
# error. The RAM check only gates the *create* path below.
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  log_ok "cluster '$CLUSTER_NAME' already exists; skip create"
  log_info "next: make kind-status"
  exit 0
fi

# --- pre-flight 3: free RAM (only for the create path) ---
AVAIL_MIB="$(free -m | awk '/Mem:/{print $7}')"
if [ "${AVAIL_MIB:-0}" -lt "$FREE_MIN_MIB" ]; then
  log_fail "free memory is ${AVAIL_MIB} MiB; minimum is $FREE_MIN_MIB MiB."
  log_warn "  Try: docker system prune -af   (frees ~1-2 GiB)"
  exit 1
fi
log_ok "${AVAIL_MIB} MiB available (>= $FREE_MIN_MIB)"

# --- create ---
mkdir -p "$(dirname "$KUBECONFIG_PATH")"
chmod 700 "$(dirname "$KUBECONFIG_PATH")"

log_info "creating cluster '$CLUSTER_NAME' (this pulls the kindest/node image; may take 2-5 min on first run)"

# --retain keeps the cluster's Docker containers around if creation
# fails partway, so we can `docker logs` them to diagnose.
kind create cluster \
  --name "$CLUSTER_NAME" \
  --config "$CLUSTER_YAML" \
  --kubeconfig "$KUBECONFIG_PATH" \
  --retain

# chmod the kubeconfig to a tight mode (kind writes 644 by default).
chmod 600 "$KUBECONFIG_PATH"

# --- wait for both nodes Ready ---
# Node objects (and Docker containers) are named `<cluster-name>-<role>`,
# so with cluster name `inframind` we get `inframind-control-plane` and
# `inframind-worker`. The kubectl context, however, is `kind-inframind`
# because kind prefixes the cluster name with `kind-` for contexts.
log_info "waiting for control-plane node (timeout $WAIT_TIMEOUT)..."
kubectl --kubeconfig "$KUBECONFIG_PATH" --context "$CONTEXT_NAME" \
  wait --for=condition=Ready "node/${CLUSTER_NAME}-control-plane" \
  --timeout="$WAIT_TIMEOUT"

log_info "waiting for worker node (timeout $WAIT_TIMEOUT)..."
kubectl --kubeconfig "$KUBECONFIG_PATH" --context "$CONTEXT_NAME" \
  wait --for=condition=Ready "node/${CLUSTER_NAME}-worker" \
  --timeout="$WAIT_TIMEOUT"

# --- D2 readiness check (Chaos Mesh containerd socket for slice 4) ---
# The kind node container is itself a Docker container. We mounted
# its in-node containerd socket onto /var/run/docker.sock so the
# chaos-daemon Pod can read it as a docker socket (see cluster.yaml).
# Verify the control-plane node container has that mount. Without it,
# chaos-mesh is dead — promote to a hard fail so the user doesn't
# chase a chaos-mesh CrashLoop 10 minutes later with no clear root
# cause.
if docker exec "${CLUSTER_NAME}-control-plane" sh -c 'ls -l /var/run/docker.sock' >/dev/null 2>&1; then
  log_ok "D2 readiness: /var/run/docker.sock present inside the kind node container"
else
  log_fail "D2 readiness check failed: /var/run/docker.sock not visible inside the kind control-plane container."
  log_fail "  Chaos Mesh cannot inject faults without this mount."
  log_fail "  Check testbed/kind/cluster.yaml extraMounts block on the control-plane node."
  log_fail "  If you're on a SELinux-enforcing host, you may also need selinuxRelabel: true."
  exit 1
fi

# --- summary ---
log_ok "cluster '$CLUSTER_NAME' is up (context: $CONTEXT_NAME)"
log_info "  kubectl --context $CONTEXT_NAME get nodes"
log_info "  next: make kind-status    (verify)"
log_info "        slice 3 -> make obs (installs observability)"
