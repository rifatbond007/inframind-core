#!/usr/bin/env bash
# testbed/smoke.sh — P1 -> P2 phase gate.
#
# Runs the 6 checks from testbed/README.md §4 and prints a pass/fail
# line per check. Per standard 09 §9.8 ("fix the bring-up; do not
# start the eval"): the script accumulates failures (one bad check
# doesn't skip the rest), prints a per-check remediation hint, and
# exits non-zero iff any check failed.
#
# Idempotent: the pod-kill assertion compares t1 > t0 (not t1 == 1),
# so prior runs' accumulated restart-counter values don't false-positive.
# Checks 1-5 are read-only.
#
# Run by `make smoke-testbed`. Sourced helpers: scripts/lib/log.sh.
#
# Exit codes:
#   0  — all 6 checks passed
#   1  — one or more checks failed (see output for which)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS_LIB="$REPO_ROOT/scripts/lib/log.sh"

# shellcheck source=/dev/null
source "$SCRIPTS_LIB"

INFRAMIND_SCRIPT_NAME="smoke-testbed"

# --- knobs (env-overridable for tests) ---
CONTEXT_NAME="${TESTBED_CONTEXT:-kind-inframind}"
KUBECONFIG_PATH="${TESTBED_KUBECONFIG:-$REPO_ROOT/testbed/.kube/config}"
MONITORING_NS="${TESTBED_NS:-monitoring}"
CHAOS_NS="${CHAOS_TESTING_NS:-chaos-testing}"
PROM_URL="${SMOKE_PROM_URL:-http://localhost:30090}"
LOKI_URL="${SMOKE_LOKI_URL:-http://localhost:30100}"
JAEGER_URL="${SMOKE_JAEGER_URL:-http://localhost:30686}"
COMPOSE_FILE="${COMPOSE_FILE:-$REPO_ROOT/docker-compose.yml}"
POD_KILL_WAIT_SECONDS="${SMOKE_POD_KILL_WAIT_SECONDS:-90}"
POD_KILL_POLL_SECONDS="${SMOKE_POD_KILL_POLL_SECONDS:-10}"
POD_KILL_QUERY='sum(kube_pod_container_status_restarts_total{namespace="default",pod=~"frontend-.*"})'

FAIL_COUNT=0

# --- pre-flight: kind cluster reachable ---
if ! command -v kubectl >/dev/null 2>&1; then
  log_fail "kubectl not found on PATH. Run: make kubectl-check"
  exit 1
fi
if ! kubectl --kubeconfig "$KUBECONFIG_PATH" --context "$CONTEXT_NAME" \
    get nodes >/dev/null 2>&1; then
  log_fail "kind cluster '$CONTEXT_NAME' not reachable via $KUBECONFIG_PATH"
  log_warn "  run: make kind   (then make up)"
  exit 1
fi
log_ok "kind cluster '$CONTEXT_NAME' reachable"

log_info "P1 -> P2 phase gate"
log_info "  context     : $CONTEXT_NAME"
log_info "  prom        : $PROM_URL"
log_info "  loki        : $LOKI_URL"
log_info "  jaeger      : $JAEGER_URL"
log_info "  compose     : $COMPOSE_FILE"

# =========================================================================
# C1: docker compose — redis + postgres up
# =========================================================================
check_compose() {
  if ! command -v docker >/dev/null 2>&1; then
    log_fail "C1 compose: docker not on PATH"
    log_warn "  install docker (see testbed/README.md §2)"
    FAIL_COUNT=$((FAIL_COUNT + 1))
    return
  fi
  local compose_state=""
  compose_state="$(docker compose -f "$COMPOSE_FILE" ps \
    --format '{{.Name}}\t{{.State}}' 2>/dev/null || true)"
  if [ -z "$compose_state" ]; then
    log_fail "C1 compose: docker compose ps returned nothing (is Compose down?)"
    log_warn "  run: make up   (Compose is the first step of the chain)"
    FAIL_COUNT=$((FAIL_COUNT + 1))
    return
  fi
  local missing=()
  for svc in inframind-redis inframind-postgres; do
    # `docker compose ps --format '{{.State}}'` returns `running` for a
    # healthy container but `running (healthy)` after the healthcheck
    # passes (and `running (unhealthy)` on a probe failure). We accept
    # `running` (alone) or `running (healthy)`, but reject
    # `running (unhealthy)`, `exited`, `restarting`, `dead`.
    if ! printf '%s\n' "$compose_state" | awk -F'\t' -v s="$svc" '$1==s && ($2 == "running" || $2 == "running (healthy)") {found=1} END{exit !found}'; then
      missing+=("$svc")
    fi
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    log_fail "C1 compose: services not running: ${missing[*]}"
    log_warn "  run: make up   (or: docker compose -f $COMPOSE_FILE up -d)"
    FAIL_COUNT=$((FAIL_COUNT + 1))
    return
  fi
  log_ok "C1 compose: redis + postgres running"
}

# =========================================================================
# C2: no pods in Pending / CrashLoopBackOff / ImagePullBackOff / Error
# =========================================================================
check_pods_pending() {
  local bad_lines=""
  bad_lines="$(kubectl --kubeconfig "$KUBECONFIG_PATH" --context "$CONTEXT_NAME" \
    get pods -A --no-headers 2>/dev/null \
    | awk '$4 ~ /^(Pending|CrashLoopBackOff|ImagePullBackOff|Error)$/' \
    || true)"
  if [ -n "$bad_lines" ]; then
    log_fail "C2 pods: pods in bad phase:"
    printf '%s\n' "$bad_lines" | while IFS= read -r line; do
      log_warn "    $line"
    done
    log_warn "  fix: see testbed/README.md §6 (troubleshooting). If partial: make down && make up"
    FAIL_COUNT=$((FAIL_COUNT + 1))
    return
  fi
  log_ok "C2 pods: all Running"
}

# =========================================================================
# C3: Prometheus runtimeinfo
# =========================================================================
check_prometheus() {
  local body_file="/tmp/inframind-smoke-prom.$$"
  local code=""
  code="$(curl -sS -o "$body_file" -w '%{http_code}' --max-time 10 \
    "$PROM_URL/api/v1/status/runtimeinfo" || true)"
  if [ "$code" != "200" ]; then
    log_fail "C3 prom: HTTP $code (expected 200) from $PROM_URL/api/v1/status/runtimeinfo"
    log_warn "  check: kubectl -n $MONITORING_NS get pods | grep prometheus"
    log_warn "  fix:   make obs   (reinstall the observability stack)"
    rm -f "$body_file"
    FAIL_COUNT=$((FAIL_COUNT + 1))
    return
  fi
  if ! jq -e '.data == "success"' "$body_file" >/dev/null 2>&1; then
    log_fail "C3 prom: 200 but .data != 'success'"
    cat "$body_file" | head -c 200 | while IFS= read -r line; do
      log_warn "    $line"
    done
    rm -f "$body_file"
    FAIL_COUNT=$((FAIL_COUNT + 1))
    return
  fi
  rm -f "$body_file"
  log_ok "C3 prom: runtimeinfo == success"
}

# =========================================================================
# C4: Loki buildinfo (gateway does NOT proxy /ready — see runbook §6)
# =========================================================================
check_loki() {
  local body_file="/tmp/inframind-smoke-loki.$$"
  local code=""
  code="$(curl -sS -o "$body_file" -w '%{http_code}' --max-time 10 \
    "$LOKI_URL/loki/api/v1/status/buildinfo" || true)"
  if [ "$code" != "200" ]; then
    log_fail "C4 loki: HTTP $code (expected 200) from $LOKI_URL/loki/api/v1/status/buildinfo"
    log_warn "  note: gateway does NOT proxy /ready — use /loki/api/v1/status/buildinfo"
    log_warn "  fix:  make obs   (reinstall the observability stack)"
    rm -f "$body_file"
    FAIL_COUNT=$((FAIL_COUNT + 1))
    return
  fi
  if ! jq -e '.version | type == "string" and length > 0' "$body_file" >/dev/null 2>&1; then
    log_fail "C4 loki: 200 but no .version field in buildinfo"
    cat "$body_file" | head -c 200 | while IFS= read -r line; do
      log_warn "    $line"
    done
    rm -f "$body_file"
    FAIL_COUNT=$((FAIL_COUNT + 1))
    return
  fi
  rm -f "$body_file"
  log_ok "C4 loki: buildinfo has .version"
}

# =========================================================================
# C5: Jaeger UI HTML
# =========================================================================
check_jaeger() {
  local body_file="/tmp/inframind-smoke-jaeger.$$"
  local code=""
  code="$(curl -sS -o "$body_file" -w '%{http_code}' --max-time 10 \
    "$JAEGER_URL/" || true)"
  if [ "$code" != "200" ]; then
    log_fail "C5 jaeger: HTTP $code (expected 200) from $JAEGER_URL/"
    log_warn "  check: kubectl -n $MONITORING_NS get pods | grep jaeger"
    log_warn "  fix:   make obs   (reinstall the observability stack)"
    rm -f "$body_file"
    FAIL_COUNT=$((FAIL_COUNT + 1))
    return
  fi
  if ! grep -qi '<title>Jaeger' "$body_file" 2>/dev/null; then
    log_fail "C5 jaeger: 200 but body does not contain '<title>Jaeger'"
    log_warn "  jaeger UI may not have finished starting; wait 30s and retry"
    rm -f "$body_file"
    FAIL_COUNT=$((FAIL_COUNT + 1))
    return
  fi
  rm -f "$body_file"
  log_ok "C5 jaeger: UI HTML returned"
}

# =========================================================================
# C6: pod-kill assertion — prom restart counter, loki Killing line, jaeger trace
# =========================================================================
check_pod_kill() {
  if ! kubectl --kubeconfig "$KUBECONFIG_PATH" --context "$CONTEXT_NAME" \
      get podchaos -n "$CHAOS_NS" boutique-frontend-pod-kill \
      >/dev/null 2>&1; then
    log_fail "C6 pod-kill: boutique-frontend-pod-kill not found in ns=$CHAOS_NS"
    log_warn "  fix: make chaos   (re-applies the 4 baseline CRs)"
    FAIL_COUNT=$((FAIL_COUNT + 1))
    return
  fi

  # --- prom restart counter: snapshot t0, poll for t1 > t0 ---
  local t0="0"
  t0="$(curl -sS --max-time 10 --get \
    --data-urlencode "query=$POD_KILL_QUERY" \
    "$PROM_URL/api/v1/query" \
    | jq -r '.data.result[0].value[1] // "0"' 2>/dev/null || echo "0")"
  if ! [[ "$t0" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
    t0="0"
  fi

  log_info "C6 prom: t0 restart counter = $t0 (polling every ${POD_KILL_POLL_SECONDS}s up to ${POD_KILL_WAIT_SECONDS}s)"

  local elapsed=0
  local t1="$t0"
  while [ "$elapsed" -lt "$POD_KILL_WAIT_SECONDS" ]; do
    sleep "$POD_KILL_POLL_SECONDS"
    elapsed=$((elapsed + POD_KILL_POLL_SECONDS))
    t1="$(curl -sS --max-time 10 --get \
      --data-urlencode "query=$POD_KILL_QUERY" \
      "$PROM_URL/api/v1/query" \
      | jq -r '.data.result[0].value[1] // "0"' 2>/dev/null || echo "0")"
    if ! [[ "$t1" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
      t1="0"
    fi
    # Compare numerically (t0 may be "0" or a float)
    if awk -v a="$t1" -v b="$t0" 'BEGIN{exit !(a+0 > b+0)}'; then
      log_ok "C6 prom: restart counter t1=$t1 > t0=$t0 (kill observed)"
      break
    fi
  done
  if ! awk -v a="$t1" -v b="$t0" 'BEGIN{exit !(a+0 > b+0)}'; then
    log_fail "C6 prom: restart counter did not increase after ${POD_KILL_WAIT_SECONDS}s (t0=$t0, t1=$t1)"
    log_warn "  the pod-kill CR scheduler is '@every 1m' — first fire is at t=0 on apply"
    log_warn "  if just installed: wait 60s and re-run make smoke-testbed"
    log_warn "  if persistent:    check chaos-daemon logs (kubectl logs -n chaos-mesh -l app.kubernetes.io/name=chaos-daemon)"
    FAIL_COUNT=$((FAIL_COUNT + 1))
    return
  fi

  # --- loki Killing line in the last 2 min ---
  local loki_body="/tmp/inframind-smoke-loki-kill.$$"
  local now_ns="$(date +%s)000000000"
  local start_ns="$((now_ns - 120000000000))"  # 2 min ago, ns
  local loki_code=""
  loki_code="$(curl -sS -o "$loki_body" -w '%{http_code}' --max-time 15 --get \
    --data-urlencode 'query={namespace="default",pod=~"frontend-.*"} |= "Killing"' \
    --data-urlencode "start=$start_ns" \
    --data-urlencode "end=$now_ns" \
    --data-urlencode 'limit=1' \
    "$LOKI_URL/loki/api/v1/query_range" || true)"
  if [ "$loki_code" != "200" ] \
      || ! jq -e '.data.result | length >= 1' "$loki_body" >/dev/null 2>&1; then
    log_fail "C6 loki: no 'Killing' log line in last 2 min (HTTP $loki_code)"
    log_warn "  Promtail may not be scraping the frontend pod; check Promtail ds in $MONITORING_NS"
    rm -f "$loki_body"
    FAIL_COUNT=$((FAIL_COUNT + 1))
    return
  fi
  rm -f "$loki_body"
  log_ok "C6 loki: 'Killing' line present in last 2 min"

  # --- jaeger frontend trace in the last 5 min (best-effort: OTel may not be wired) ---
  local jaeger_body="/tmp/inframind-smoke-jaeger-trace.$$"
  local jcode=""
  jcode="$(curl -sS -o "$jaeger_body" -w '%{http_code}' --max-time 15 \
    "$JAEGER_URL/api/traces?service=frontend&lookback=5m&limit=1" || true)"
  if [ "$jcode" != "200" ]; then
    log_warn "C6 jaeger: HTTP $jcode from /api/traces — OTel sidecar may not be wired (best-effort)"
    rm -f "$jaeger_body"
  elif ! jq -e '.data | length > 0' "$jaeger_body" >/dev/null 2>&1; then
    log_warn "C6 jaeger: 200 but no frontend traces in last 5 min (best-effort — OTel not wired?)"
    rm -f "$jaeger_body"
  else
    rm -f "$jaeger_body"
    log_ok "C6 jaeger: frontend traces present in last 5 min"
  fi
}

# --- run all 6 checks ---
check_compose
check_pods_pending
check_prometheus
check_loki
check_jaeger
check_pod_kill

# --- summary ---
if [ "$FAIL_COUNT" -eq 0 ]; then
  log_ok "P1 -> P2 gate: 6/6 checks passed"
  log_info "  next: phase P2 (schema + event bus). See docs/PROGRESS.md."
  exit 0
else
  log_fail "P1 -> P2 gate: $FAIL_COUNT/6 check(s) failed"
  log_warn "  fix the bring-up: run 'make down' then 'make up' (do not investigate mid-state)."
  log_warn "  see testbed/README.md §6 (troubleshooting) and docs/standards/09-testbed-ops.md §9.8."
  exit 1
fi
