# scripts/lib/log.sh — shared logging helpers for top-level scripts/
# Source from a script with:  source "$(dirname "$0")/../lib/log.sh"
# Used by the D24 pre-commit hook wrappers and by future testbed bootstrap
# helpers. Stdlib only (no third-party deps) so it works on a fresh checkout
# before the Python venv exists.

# Avoid double-sourcing.
if [ -n "${INFRAMIND_LOG_SOURCED:-}" ]; then
  return 0 2>/dev/null || true
fi
INFRAMIND_LOG_SOURCED=1

# Colors only when stdout is a TTY. Set NO_COLOR=1 to force disable.
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  _C_RED='\033[31m'
  _C_YEL='\033[33m'
  _C_GRN='\033[32m'
  _C_CYA='\033[36m'
  _C_RST='\033[0m'
else
  _C_RED=''; _C_YEL=''; _C_GRN=''; _C_CYA=''; _C_RST=''
fi

# Prefix every line with the script name if INFRAMIND_SCRIPT_NAME is set.
_log_prefix() {
  if [ -n "${INFRAMIND_SCRIPT_NAME:-}" ]; then
    printf '[%s] ' "$INFRAMIND_SCRIPT_NAME"
  fi
}

log_info() {
  printf '%b%s%b\n' "$_C_CYA" "$(_log_prefix)$*" "$_C_RST"
}

log_warn() {
  printf '%b%s%b\n' "$_C_YEL" "$(_log_prefix)WARN: $*" "$_C_RST" >&2
}

log_fail() {
  printf '%b%s%b\n' "$_C_RED" "$(_log_prefix)FAIL: $*" "$_C_RST" >&2
}

log_ok() {
  printf '%b%s%b\n' "$_C_GRN" "$(_log_prefix)OK: $*" "$_C_RST"
}
