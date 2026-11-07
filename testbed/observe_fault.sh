#!/usr/bin/env bash
# testbed/observe_fault.sh — watch what the four ingestion paths see while one chaos fault runs.
#
# Usage: observe_fault.sh <chaos-cr.yaml> [baseline_s=90] [fault_s=150] [recovery_s=90]
#
# Starts the API and the Prometheus/Loki/Jaeger collectors, drives steady traffic through the
# frontend, injects the fault, and prints a per-30s timeline (edge latency, span p95, CPU,
# errors, alerts). Output lands in testbed/.observe/<run-id>/ (git-ignored).
#
# Why it never hangs: it does not delete and re-apply the repo's CR by name (a finalizer on a
# finished NetworkChaos blocked `kubectl delete` for ~7 minutes). It applies a *copy* with a
# unique name and a `duration` of fault_s, so the fault ends by itself; the copy is removed
# with `--wait=false` and the wait for it to disappear is capped at 120 s and only reported.
#
# Exit codes: 0 report printed; 1 pre-flight or injection failed; 2 usage error.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=/dev/null
source "$REPO_ROOT/scripts/lib/log.sh"
INFRAMIND_SCRIPT_NAME="observe-fault"

CR_FILE="${1:-}"
BASELINE_S="${2:-90}"
FAULT_S="${3:-150}"
RECOVERY_S="${4:-90}"
PY="${PY:-python3}"
CTX="${TESTBED_CONTEXT:-kind-inframind}"
export KUBECONFIG="${TESTBED_KUBECONFIG:-$REPO_ROOT/testbed/.kube/config}"
K="kubectl --context $CTX"
REDIS_CONTAINER="${REDIS_CONTAINER:-inframind-redis}"
API_PORT="${INFRAMIND_API_PORT:-8080}"
FWD_PORT=18082

if [ -z "$CR_FILE" ] || [ ! -f "$CR_FILE" ]; then
  log_fail "usage: observe_fault.sh <chaos-cr.yaml> [baseline_s] [fault_s] [recovery_s]"
  exit 2
fi
for n in "$BASELINE_S" "$FAULT_S" "$RECOVERY_S"; do
  case "$n" in '' | *[!0-9]*) log_fail "durations must be whole seconds, got '$n'"; exit 2 ;; esac
done

port_free() { ! (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

RUN_ID="$(date +%Y%m%d-%H%M%S)"
SUFFIX="$(date +%H%M%S)"
OUT="$REPO_ROOT/testbed/.observe/$RUN_ID"
CR_COPY="$OUT/cr.yaml"
PIDS=()

cleanup() {
  for p in "${PIDS[@]:-}"; do
    [ -n "$p" ] || continue
    pkill -P "$p" 2>/dev/null   # children first: a port-forward or curl started by a loop
    kill "$p" 2>/dev/null
  done
  if [ -f "$CR_COPY" ]; then $K delete -f "$CR_COPY" --wait=false --ignore-not-found >/dev/null 2>&1; fi
}
trap cleanup EXIT

# --- pre-flight ---
$K get nodes >/dev/null 2>&1 || { log_fail "cluster $CTX not reachable; run 'make up'"; exit 1; }
docker exec "$REDIS_CONTAINER" redis-cli ping >/dev/null 2>&1 || { log_fail "Redis ($REDIS_CONTAINER) not reachable"; exit 1; }
for p in "$API_PORT" "$FWD_PORT"; do
  port_free "$p" || { log_fail "port $p is in use"; exit 1; }
done
KIND="$(sed -nE 's/^kind: (.+)$/\1/p' "$CR_FILE" | head -1)"
if [ "$KIND" = "NetworkChaos" ]; then
  # kind shares the host kernel; without these modules Chaos Mesh cannot apply (or recover) a
  # NetworkChaos, so the fault silently never happens and delete hangs on a finalizer.
  missing=""
  for m in sch_netem ip_set_hash_ipport ip_set_hash_net; do
    lsmod | grep -q "^$m " || missing="$missing $m"
  done
  if [ -n "$missing" ]; then
    log_fail "NetworkChaos needs kernel modules not loaded on this host:$missing"
    log_info "fix: sudo modprobe sch_netem ip_set_hash_ipport ip_set_hash_net"
    log_info "persist: printf 'sch_netem\\nip_set_hash_ipport\\nip_set_hash_net\\n' | sudo tee /etc/modules-load.d/inframind-chaos.conf"
    exit 1
  fi
fi
mkdir -p "$OUT"

# --- the CR copy: unique name, self-expiring duration ---
sed -E "0,/^  name: (.+)\$/s//  name: \1-$SUFFIX/; s/^  duration: .*/  duration: \"${FAULT_S}s\"/" "$CR_FILE" >"$CR_COPY"
NAME="$(sed -nE '0,/^  name: (.+)$/s//\1/p' "$CR_COPY" | head -1)"
if [ "${#NAME}" -gt 63 ]; then
  log_fail "generated CR name '$NAME' exceeds 63 characters; shorten the CR's metadata.name"
  exit 1
fi
$K apply --dry-run=server -f "$CR_COPY" >/dev/null 2>&1 || { log_fail "server-side dry-run rejected $CR_COPY"; exit 1; }

# --- start the pipeline ---
last_id() { docker exec "$REDIS_CONTAINER" redis-cli XREVRANGE "$1" + - COUNT 1 | head -1; }
SIG_AFTER="$(last_id stream:signals)"; SIG_AFTER="${SIG_AFTER:-0-0}"
GRA_AFTER="$(last_id stream:graph-updates)"; GRA_AFTER="${GRA_AFTER:-0-0}"
START="$(date +%s)"

INFRAMIND_ALERT_WEBHOOK_SECRET="${SMOKE_WEBHOOK_SECRET:-dev-secret}" INFRAMIND_API_PORT="$API_PORT" \
  "$PY" -m inframind.api.app >"$OUT/api.log" 2>&1 & PIDS+=($!)
PROM_INTERVAL_S=10 "$PY" -m inframind.ingestion.prometheus >"$OUT/prometheus.log" 2>&1 & PIDS+=($!)
LOKI_INTERVAL_S=10 "$PY" -m inframind.ingestion.loki >"$OUT/loki.log" 2>&1 & PIDS+=($!)
JAEGER_INTERVAL_S=10 "$PY" -m inframind.ingestion.jaeger >"$OUT/jaeger.log" 2>&1 & PIDS+=($!)
# A port-forward dies with the pod it is bound to (a pod-kill fault ends it), so reconnect.
forward() { while true; do $K port-forward svc/frontend "$FWD_PORT:80" >/dev/null 2>&1; sleep 1; done; }
forward & PIDS+=($!)
sleep 4

traffic() {
  local ids=(OLJCESPC7Z 66VCHSJNUP 1YMWWN1N4O L9ECAV7KIM 2ZYFJ3GM2N 0PUK6V6EV0)
  while true; do
    curl -s -o /dev/null -m 8 "localhost:$FWD_PORT/"
    curl -s -o /dev/null -m 8 "localhost:$FWD_PORT/product/${ids[RANDOM % 6]}"
    sleep 0.5
  done
}
traffic & PIDS+=($!)

log_info "baseline ${BASELINE_S}s (run $RUN_ID, output $OUT)"
sleep "$BASELINE_S"

# --- the fault ---
if ! $K apply -f "$CR_COPY" >"$OUT/apply.log" 2>&1; then
  log_fail "could not apply the fault: $(tail -1 "$OUT/apply.log")"
  exit 1
fi
INJECT="$(date +%s)"
injected=""
for _ in $(seq 1 15); do
  injected="$($K -n chaos-testing get "$KIND" "$NAME" -o jsonpath='{.status.conditions[?(@.type=="AllInjected")].status}' 2>/dev/null)"
  [ "$injected" = "True" ] && break
  sleep 2
done
if [ "$injected" != "True" ]; then
  log_fail "fault $NAME was accepted but never reported AllInjected (see: $K -n chaos-testing describe $KIND $NAME)"
  exit 1
fi
log_ok "fault $NAME injected for ${FAULT_S}s"
sleep "$FAULT_S"
FAULT_END="$(date +%s)"

# Remove the copy without waiting on its finalizer; then wait (capped) for it to disappear.
$K delete -f "$CR_COPY" --wait=false --ignore-not-found >/dev/null 2>&1
gone=0
for _ in $(seq 1 24); do
  if ! timeout 10 $K -n chaos-testing get -f "$CR_COPY" >/dev/null 2>&1; then gone=1; break; fi
  sleep 5
done
if [ "$gone" -eq 1 ]; then log_ok "fault object removed"; else log_warn "fault object still terminating after 120s; continuing. Inspect: $K -n chaos-testing describe $KIND $NAME"; fi

log_info "recovery ${RECOVERY_S}s"
sleep "$RECOVERY_S"
END="$(date +%s)"

printf 'start=%s\ninject=%s\nfault_end=%s\nend=%s\nsig_after=%s\ngra_after=%s\n' \
  "$START" "$INJECT" "$FAULT_END" "$END" "$SIG_AFTER" "$GRA_AFTER" >"$OUT/markers.env"

"$PY" "$REPO_ROOT/testbed/observe_fault_report.py" "$OUT" | tee "$OUT/timeline.txt"
