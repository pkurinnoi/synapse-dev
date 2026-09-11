#!/usr/bin/env bash
# scripts/webui.sh — start the Claude Dev Team web control panel.
#
# Reads its configuration from scripts/.env (same file as the rest of the kit):
#   WEBUI_USER            login name (default: admin)
#   WEBUI_PASSWORD_HASH   pbkdf2 hash — set it with:  ./scripts/webui.sh set-password
#   WEBUI_HOST            bind address (default: 0.0.0.0)
#   WEBUI_PORT            port (default: 9000)
#   WEBUI_SESSION_TTL     login lifetime in seconds (default: 43200)
#   WEBUI_TLS             1 when served behind an HTTPS proxy (marks cookies Secure)
#
# Usage:
#   ./scripts/webui.sh                      # run in the foreground on WEBUI_PORT
#   ./scripts/webui.sh --port 9001          # run on a port just for this launch
#   ./scripts/webui.sh --background         # detach, logging to $AGENT_TEAM_LOG_DIR/webui.log
#   ./scripts/webui.sh port                 # show the configured port
#   ./scripts/webui.sh port 9001            # save that port to scripts/.env
#   ./scripts/webui.sh set-password [user]  # create/replace the login password
#   ./scripts/webui.sh hash-password        # print a hash without touching .env
#   ./scripts/webui.sh --stop               # stop a backgrounded panel
#
# --host works the same way as --port, for either form.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
SERVER="$ROOT/webui/server.py"
LOG_DIR="${AGENT_TEAM_LOG_DIR:-/tmp/agent-team}"

command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }
[ -f "$SERVER" ] || { echo "missing $SERVER" >&2; exit 1; }

# Print the header comment block (everything between the shebang and the first
# line of real code) as the help text.
usage() { awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "${BASH_SOURCE[0]}"; }

# Reject anything that is not a usable TCP port before we bother the server.
check_port() {
  case "$1" in
    ''|*[!0-9]*) echo "not a port number: $1" >&2; exit 2 ;;
  esac
  if [ "$1" -lt 1 ] || [ "$1" -gt 65535 ]; then
    echo "port out of range (1-65535): $1" >&2; exit 2
  fi
  if [ "$1" -lt 1024 ] && [ "$(id -u)" != 0 ]; then
    echo "note: ports below 1024 need root" >&2
  fi
}

# ─── Argument parsing ────────────────────────────────────────────────────────
CMD=""; BACKGROUND=false; PORT=""; HOST=""; ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    -p|--port)   PORT="${2:?--port needs a number}"; shift 2 ;;
    --port=*)    PORT="${1#*=}"; shift ;;
    --host)      HOST="${2:?--host needs an address}"; shift 2 ;;
    --host=*)    HOST="${1#*=}"; shift ;;
    -b|--background) BACKGROUND=true; shift ;;
    --stop)      CMD="stop"; shift ;;
    -h|--help)   usage; exit 0 ;;
    port|set-password|passwd|hash-password|hash)
                 CMD="$1"; shift ;;
    *)           ARGS+=("$1"); shift ;;
  esac
done

# `port 9001` / `set-password alice` take their value as a plain argument.
[ "$CMD" = "port" ] && [ -z "$PORT" ] && [ ${#ARGS[@]} -gt 0 ] && PORT="${ARGS[0]}"

# Load scripts/.env so WEBUI_* (and the tokens the panel manages) are present.
if [ -f "$HERE/.env" ]; then
  set -a; source "$HERE/.env"; set +a
fi

[ -n "$PORT" ] && check_port "$PORT"

case "$CMD" in
  port)
    if [ -z "$PORT" ]; then
      if [ -n "${WEBUI_PORT:-}" ]; then
        echo "WEBUI_PORT=$WEBUI_PORT   (set in $HERE/.env)"
      else
        echo "WEBUI_PORT=9000   (default — not set in $HERE/.env)"
      fi
      echo "host: ${WEBUI_HOST:-0.0.0.0}"
      exit 0
    fi
    python3 "$SERVER" set-port "$PORT"
    ;;

  set-password|passwd)   exec python3 "$SERVER" set-password "${ARGS[@]:-}" ;;
  hash-password|hash)    exec python3 "$SERVER" hash-password "${ARGS[@]:-}" ;;

  stop)
    if [ -f "$LOG_DIR/webui.pid" ] && kill "$(cat "$LOG_DIR/webui.pid")" 2>/dev/null; then
      echo "stopped (pid $(cat "$LOG_DIR/webui.pid"))"; rm -f "$LOG_DIR/webui.pid"
    elif systemctl is-active --quiet agent-webui 2>/dev/null; then
      # Don't pkill a systemd-managed panel out from under the unit.
      echo "the panel is running under systemd — stop it with: systemctl stop agent-webui"
      exit 1
    else
      pkill -f "webui/server.py" && echo "stopped" || echo "not running"
    fi
    ;;

  *)
    # A --port/--host given here overrides .env for this launch only.
    [ -n "$PORT" ] && export WEBUI_PORT="$PORT"
    [ -n "$HOST" ] && export WEBUI_HOST="$HOST"
    if [ "$BACKGROUND" = true ]; then
      mkdir -p "$LOG_DIR"
      nohup python3 "$SERVER" >>"$LOG_DIR/webui.log" 2>&1 &
      echo "$!" > "$LOG_DIR/webui.pid"
      sleep 1
      if kill -0 "$!" 2>/dev/null; then
        echo "web ui started in the background (pid $!) on ${WEBUI_HOST:-0.0.0.0}:${WEBUI_PORT:-9000}"
        echo "log: $LOG_DIR/webui.log"
      else
        echo "web ui failed to start — last lines of $LOG_DIR/webui.log:" >&2
        tail -5 "$LOG_DIR/webui.log" >&2
        exit 1
      fi
    else
      exec python3 "$SERVER"
    fi
    ;;
esac
