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
#   ./scripts/webui.sh                 # run in the foreground
#   ./scripts/webui.sh set-password    # create/replace the login password
#   ./scripts/webui.sh hash-password   # print a hash without touching .env
#   ./scripts/webui.sh --background    # detach, logging to $AGENT_TEAM_LOG_DIR/webui.log

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
SERVER="$ROOT/webui/server.py"
LOG_DIR="${AGENT_TEAM_LOG_DIR:-/tmp/agent-team}"

command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }
[ -f "$SERVER" ] || { echo "missing $SERVER" >&2; exit 1; }

# Load scripts/.env so WEBUI_* (and the tokens the panel manages) are present.
if [ -f "$HERE/.env" ]; then
  set -a; source "$HERE/.env"; set +a
fi

case "${1:-}" in
  set-password|passwd)   exec python3 "$SERVER" set-password "${2:-}" ;;
  hash-password|hash)    exec python3 "$SERVER" hash-password "${2:-}" ;;
  --background|-b)
    mkdir -p "$LOG_DIR"
    nohup python3 "$SERVER" >>"$LOG_DIR/webui.log" 2>&1 &
    echo "$!" > "$LOG_DIR/webui.pid"
    echo "web ui started in the background (pid $!) — log: $LOG_DIR/webui.log"
    ;;
  --stop)
    if [ -f "$LOG_DIR/webui.pid" ] && kill "$(cat "$LOG_DIR/webui.pid")" 2>/dev/null; then
      echo "stopped (pid $(cat "$LOG_DIR/webui.pid"))"; rm -f "$LOG_DIR/webui.pid"
    else
      pkill -f "webui/server.py" && echo "stopped" || echo "not running"
    fi
    ;;
  -h|--help)             exec python3 "$SERVER" --help ;;
  *)                     exec python3 "$SERVER" ;;
esac
