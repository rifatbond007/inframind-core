#!/usr/bin/env bash
# testbed/kind/watcher-kubeconfig.sh — give the compose `rca` container read-only access to
# the kind cluster for the K8s change watcher (D22, standard 14.6).
#
# Applies watcher-rbac.yaml (a ServiceAccount with get/list/watch only), mints a token for
# it, and writes testbed/.kube/watcher/config: a kubeconfig that reaches the API server
# over the `kind` docker network (https://<cluster>-control-plane:6443). Compose mounts the
# directory at /kube. The file is gitignored (testbed/.kube/). It is world-readable because
# the container runs as uid 10001; the token is read-only and expires after TOKEN_DURATION.
# Re-run (`make watcher-kubeconfig`) after the token expires or the cluster is recreated.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=/dev/null
source "$REPO_ROOT/scripts/lib/log.sh"
INFRAMIND_SCRIPT_NAME="watcher-kubeconfig"

CLUSTER_NAME="${TESTBED_CLUSTER_NAME:-inframind}"
CONTEXT_NAME="${TESTBED_CONTEXT:-kind-inframind}"
KUBECONFIG_PATH="${TESTBED_KUBECONFIG:-$REPO_ROOT/testbed/.kube/config}"
OUT_DIR="${WATCHER_KUBECONFIG_DIR:-$REPO_ROOT/testbed/.kube/watcher}"
TOKEN_DURATION="${TOKEN_DURATION:-720h}"
NAMESPACE="default"
SA="inframind-change-watcher"

case "$CONTEXT_NAME" in
  kind-*) ;;
  *) log_fail "context $CONTEXT_NAME is not a kind cluster; refusing"; exit 1 ;;
esac

kc() { kubectl --kubeconfig "$KUBECONFIG_PATH" --context "$CONTEXT_NAME" "$@"; }

kc apply -f "$REPO_ROOT/testbed/kind/watcher-rbac.yaml" >/dev/null
token="$(kc -n "$NAMESPACE" create token "$SA" --duration="$TOKEN_DURATION")"
ca="$(kc config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')"
server="https://${CLUSTER_NAME}-control-plane:6443"

mkdir -p "$OUT_DIR"
umask 022
cat >"$OUT_DIR/config.tmp" <<KUBECONFIG
apiVersion: v1
kind: Config
current-context: change-watcher
clusters:
  - name: ${CLUSTER_NAME}
    cluster:
      server: ${server}
      certificate-authority-data: ${ca}
users:
  - name: ${SA}
    user:
      token: ${token}
contexts:
  - name: change-watcher
    context:
      cluster: ${CLUSTER_NAME}
      user: ${SA}
      namespace: ${NAMESPACE}
KUBECONFIG
chmod 0644 "$OUT_DIR/config.tmp"
mv "$OUT_DIR/config.tmp" "$OUT_DIR/config"

# The token must actually be read-only: it can list Deployments and cannot change them.
check() { kc auth can-i "$@" --as="system:serviceaccount:${NAMESPACE}:${SA}" -n "$NAMESPACE"; }
if [ "$(check watch deployments.apps)" != "yes" ] || [ "$(check update deployments.apps || true)" != "no" ]; then
  log_fail "the watcher ServiceAccount is not read-only as expected"
  exit 1
fi
log_ok "watcher kubeconfig written to $OUT_DIR/config (server $server, token valid $TOKEN_DURATION)"
