#!/usr/bin/env bash
# Drive the InfraMind testbed codespace from the laptop (D31). Called by scripts/remote.mk.
#
#   codespace.sh run <make goals and VAR=value ...>   run `make ...` in the codespace
#   codespace.sh create | status | stop | delete | ssh | ports | logs | fetch
#
# The codespace runs the PUSHED state of the current branch: before each run it fast-forwards
# its checkout to origin/<branch>. Uncommitted or unpushed laptop changes are not there, and
# the script says so. `make down` in remote mode also stops the codespace.
set -euo pipefail

REPO="${CODESPACE_REPO:-rifatbond007/inframind-core}"
DISPLAY_NAME="${CODESPACE_DISPLAY_NAME:-inframind-testbed}"
MACHINE="${CODESPACE_MACHINE:-standardLinux32gb}" # 4 cores / 16 GB
IDLE_TIMEOUT="${CODESPACE_IDLE_TIMEOUT:-240m}"     # GitHub's maximum
WORKDIR="/workspaces/inframind-core"
FORWARD_PORTS=(30300 30090 30093 30686 30100 8080)

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/lib/log.sh
. "$ROOT/scripts/lib/log.sh"
INFRAMIND_SCRIPT_NAME="codespace"

need_gh() {
  command -v gh >/dev/null || { log_fail "gh (GitHub CLI) is not installed"; exit 1; }
  if ! gh codespace list --repo "$REPO" >/dev/null 2>&1; then
    log_fail "gh cannot manage codespaces (missing the 'codespace' scope?)"
    log_warn "  run once: gh auth refresh -h github.com -s codespace"
    exit 1
  fi
}

branch() { git -C "$ROOT" rev-parse --abbrev-ref HEAD; }

# The codespace for this repo with our display name, or empty.
find_cs() {
  gh codespace list --repo "$REPO" --json name,displayName \
    -q ".[] | select(.displayName == \"$DISPLAY_NAME\") | .name" | head -n1
}

state_of() {
  gh codespace list --repo "$REPO" --json name,state -q ".[] | select(.name == \"$1\") | .state"
}

create_cs() { # progress goes to stderr; gh prints the new name on stdout
  local b
  b="$(branch)"
  log_info >&2 "creating codespace '$DISPLAY_NAME' on $REPO@$b ($MACHINE, idle timeout $IDLE_TIMEOUT)"
  gh codespace create --repo "$REPO" --branch "$b" --machine "$MACHINE" \
    --idle-timeout "$IDLE_TIMEOUT" --display-name "$DISPLAY_NAME" --default-permissions
}

ensure_cs() {
  local cs
  cs="$(find_cs)"
  if [ -z "$cs" ]; then
    create_cs >/dev/null
    cs="$(find_cs)"
    [ -n "$cs" ] || { log_fail "codespace creation did not return a codespace"; exit 1; }
    log_ok "created $cs (first start runs .devcontainer/post-create.sh, a few minutes)" >&2
  fi
  echo "$cs" # the only stdout line: callers capture it with $(ensure_cs)
}

warn_unsynced() {
  local b
  b="$(branch)"
  if [ -n "$(git -C "$ROOT" status --porcelain --untracked-files=no)" ]; then
    log_warn "uncommitted laptop changes are NOT in the codespace (it runs origin/$b)"
  fi
  git -C "$ROOT" fetch -q origin "$b" 2>/dev/null || true
  if [ -n "$(git -C "$ROOT" log --oneline "origin/$b..HEAD" 2>/dev/null)" ]; then
    log_warn "unpushed commits are NOT in the codespace: git push origin $b"
  fi
}

# Block until .devcontainer/post-create.sh has finished (it writes ~/.inframind-ready). A fresh
# codespace accepts ssh before its setup is done, and a failed devcontainer build leaves
# GitHub's recovery container, which has none of the tools.
wait_ready() {
  local cs="$1" rc=0
  remote "$cs" '[ -f ~/.inframind-ready ]' 2>/dev/null && return 0
  log_info >&2 "waiting for the codespace setup (.devcontainer/post-create.sh) to finish..."
  remote "$cs" 'L=/workspaces/.codespaces/.persistedshare/creation.log
    for _ in $(seq 1 120); do
      [ -f ~/.inframind-ready ] && exit 0
      grep -q "Creating recovery container" "$L" 2>/dev/null && exit 3
      sleep 10
    done
    exit 4' || rc=$?
  case "$rc" in
    0) log_ok >&2 "codespace setup finished" ;;
    3) log_fail "the devcontainer failed to build; GitHub started its recovery container"
       log_warn "  see: make codespace-ssh, then grep -i error /workspaces/.codespaces/.persistedshare/creation.log"
       log_warn "  after fixing .devcontainer/ and pushing: gh codespace rebuild --full -c $cs"
       exit 1 ;;
    *) log_fail "codespace setup did not finish within 20 minutes"; exit 1 ;;
  esac
}

remote() { # remote <codespace> <shell command>
  gh codespace ssh --codespace "$1" -- "bash -lc $(printf '%q' "$2")"
}

cmd_run() {
  local cs b args detached=0 goal
  cs="$(ensure_cs)"
  wait_ready "$cs"
  b="$(branch)"
  warn_unsynced
  args="$(printf '%q ' "$@")"
  for goal in "$@"; do
    [ "$goal" = "eval-live" ] && detached=1
  done
  log_info "codespace $cs: make $* (origin/$b)"
  local sync="cd $WORKDIR && git fetch -q origin && git checkout -q $b && git merge -q --ff-only origin/$b"
  local venv=". .venv/bin/activate"
  if [ "$detached" = 1 ]; then
    # A recording takes hours: run it detached so a dropped laptop connection cannot kill it.
    local log="testbed/.observe/remote-eval-live-\$(date -u +%Y%m%dT%H%M%SZ).log"
    remote "$cs" "$sync && $venv && mkdir -p testbed/.observe && L=$log && setsid nohup make $args > \"\$L\" 2>&1 < /dev/null & echo \"[codespace] recording started; log: \$L\""
    log_ok "follow it with: make codespace-logs"
  else
    remote "$cs" "$sync && $venv && make $args"
  fi
  for goal in "$@"; do
    if [ "$goal" = "down" ]; then
      log_info "stopping codespace $cs (make down in remote mode stops the codespace too)"
      gh codespace stop --codespace "$cs"
      log_ok "codespace stopped; no compute is billed while stopped"
    fi
  done
}

cmd_status() {
  need_gh
  local cs
  cs="$(find_cs)"
  if [ -z "$cs" ]; then echo "no '$DISPLAY_NAME' codespace for $REPO"; return; fi
  gh codespace list --repo "$REPO" --json name,displayName,state,gitStatus,machineName \
    -q ".[] | select(.name == \"$cs\") | \"\(.displayName)  \(.name)  \(.state)  \(.machineName)  branch=\(.gitStatus.ref)\""
}

cmd_ports() {
  local cs
  cs="$(ensure_cs)"
  local pairs=()
  for p in "${FORWARD_PORTS[@]}"; do pairs+=("$p:$p"); done
  log_info "forwarding ${FORWARD_PORTS[*]} to localhost (Ctrl+C to stop); Grafana: http://localhost:30300"
  gh codespace ports forward "${pairs[@]}" --codespace "$cs"
}

cmd_logs() {
  local cs
  cs="$(ensure_cs)"
  remote "$cs" "cd $WORKDIR && L=\$(ls -t testbed/.observe/remote-eval-live-*.log 2>/dev/null | head -n1); [ -n \"\$L\" ] || { echo 'no remote recording log'; exit 0; }; echo \"== \$L\"; tail -n 40 -f \"\$L\""
}

cmd_fetch() {
  local cs
  cs="$(ensure_cs)"
  log_info "copying evaluation/fixtures and evaluation/results from $cs to the laptop"
  gh codespace cp --codespace "$cs" -e -r "remote:$WORKDIR/evaluation/fixtures" "$ROOT/evaluation/"
  gh codespace cp --codespace "$cs" -e -r "remote:$WORKDIR/evaluation/results" "$ROOT/evaluation/"
  log_ok "fetched; review with git status, commit on the laptop"
}

main() {
  local sub="${1:-}"
  shift || true
  case "$sub" in
    run) need_gh; cmd_run "$@" ;;
    create) need_gh; [ -n "$(find_cs)" ] && { log_ok "already exists: $(find_cs)"; exit 0; }; create_cs ;;
    status) cmd_status ;;
    stop) need_gh; cs="$(find_cs)"; [ -n "$cs" ] && gh codespace stop --codespace "$cs" || echo "no codespace" ;;
    delete) need_gh; cs="$(find_cs)"; [ -n "$cs" ] && gh codespace delete --codespace "$cs" || echo "no codespace" ;;
    ssh) need_gh; gh codespace ssh --codespace "$(ensure_cs)" ;;
    ports) need_gh; cmd_ports ;;
    logs) need_gh; cmd_logs ;;
    fetch) need_gh; cmd_fetch ;;
    *) echo "usage: $0 run <goals...> | create | status | stop | delete | ssh | ports | logs | fetch" >&2; exit 2 ;;
  esac
}

main "$@"
