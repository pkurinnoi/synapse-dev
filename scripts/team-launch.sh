#!/bin/bash
# Agent Bot Team — tmux launcher (per-task monitor windows)
#
# Starts a tmux session with a single CTO window. The CTO spawns per-task monitor
# windows on demand via scripts/monitor-open.sh. This is a multi-repo tree (the
# team root /srv/agent-team is not itself a git repo), so NO per-dev worktrees are
# provisioned — each dev works directly in the relevant sub-repo on a feature branch.
#
# Usage: ./scripts/team-launch.sh [--replace]
#   --replace  kill an existing session first
#
# Env (all optional):
#   AGENT_TEAM_SESSION  tmux session name (default: agent-team)
#   AGENT_TEAM_DIR      project root (default: parent of this script's dir = /srv/agent-team)
#   AGENT_TEAM_LOG_DIR  per-task monitor log dir (default: /tmp/agent-team)

set -euo pipefail

SESSION="${AGENT_TEAM_SESSION:-agent-team}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${AGENT_TEAM_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
LOG_DIR="${AGENT_TEAM_LOG_DIR:-/tmp/agent-team}"
ARCHIVE_DIR="$ROOT_DIR/.claude/monitors/archive"
JQ_PROGRAM="$SCRIPT_DIR/format-transcript.jq"

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; NC='\033[0m'

REPLACE=false
[ "${1:-}" = "--replace" ] && REPLACE=true

echo -e "${CYAN}Agent Bot Team Launcher${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  session: $SESSION"
echo "  root:    $ROOT_DIR  (multi-repo; no worktrees)"

# ─── Preflight ──────────────────────────────────────────────────────────────
command -v claude  >/dev/null 2>&1 || { echo -e "${RED}Error: claude CLI not found${NC}"; exit 1; }
command -v tmux    >/dev/null 2>&1 || { echo -e "${RED}Error: tmux not found${NC}"; exit 1; }
command -v jq      >/dev/null 2>&1 || { echo -e "${RED}Error: jq not found${NC}"; exit 1; }
command -v python3 >/dev/null 2>&1 || echo -e "${YELLOW}Warning: python3 not found — issuebot window will be skipped${NC}"

[ -f "$JQ_PROGRAM" ] || { echo -e "${RED}Error: $JQ_PROGRAM not found${NC}"; exit 1; }
echo '{}' | jq -rf "$JQ_PROGRAM" >/dev/null 2>&1 \
    || { echo -e "${RED}Error: $JQ_PROGRAM has a syntax error${NC}"; exit 1; }

[ -f "$ROOT_DIR/.claude/agents/cto.md" ] || {
    echo -e "${YELLOW}Warning: $ROOT_DIR/.claude/agents/cto.md not found —${NC}"
    echo -e "${YELLOW}  'claude --agent cto' will fail unless you create it.${NC}"
}

# ─── Existing session handling ──────────────────────────────────────────────
if tmux has-session -t "$SESSION" 2>/dev/null; then
    if [ "$REPLACE" = true ]; then
        echo -e "${YELLOW}Killing existing session '$SESSION'...${NC}"
        tmux kill-session -t "$SESSION"; sleep 1
    else
        echo -e "${YELLOW}Session '$SESSION' already running. Use --replace to kill it.${NC}"
        exit 0
    fi
fi

# ─── Orphan sweep ───────────────────────────────────────────────────────────
shopt -s nullglob dotglob
ORPHANS=("$LOG_DIR"/*)
shopt -u nullglob dotglob
if [ ${#ORPHANS[@]} -gt 0 ]; then
    DATE="$(date +%Y-%m-%d)"
    ORPHAN_DIR="$ARCHIVE_DIR/orphaned-$DATE"
    [ -d "$ORPHAN_DIR" ] && ORPHAN_DIR="$ORPHAN_DIR-$(date +%H%M%S)"
    mkdir -p "$ORPHAN_DIR"
    mv "${ORPHANS[@]}" "$ORPHAN_DIR"/ \
        || echo -e "${YELLOW}Warning: some orphan files could not be archived${NC}"
    echo -e "Orphaned per-task logs moved to ${GREEN}$ORPHAN_DIR${NC}"
fi
mkdir -p "$LOG_DIR"

# ─── Launch tmux with a CTO-only window ─────────────────────────────────────
cd "$ROOT_DIR"
echo ""
echo "Creating tmux session '$SESSION' (CTO-only)..."
tmux new-session -d -s "$SESSION" -n cto -c "$ROOT_DIR"
tmux send-keys -t "$SESSION:cto" 'claude --agent cto' Enter

# ─── Launch issuebot window (Telegram /issue bot) ────────────────────────────
# Load .env to check the required vars before starting the window.
_ISSUEBOT_ENV="$SCRIPT_DIR/.env"
_ISSUEBOT_TOKEN="${TELEGRAM_TOKEN:-}"
_ISSUEBOT_CHAT="${TELEGRAM_CHAT_ID:-}"
if [ -z "$_ISSUEBOT_TOKEN" ] && [ -f "$_ISSUEBOT_ENV" ]; then
    _ISSUEBOT_TOKEN="$(grep -E '^TELEGRAM_TOKEN=' "$_ISSUEBOT_ENV" 2>/dev/null | cut -d= -f2- | tr -d '"'"'"' | head -1)"
fi
if [ -z "$_ISSUEBOT_CHAT" ] && [ -f "$_ISSUEBOT_ENV" ]; then
    _ISSUEBOT_CHAT="$(grep -E '^TELEGRAM_CHAT_ID=' "$_ISSUEBOT_ENV" 2>/dev/null | cut -d= -f2- | tr -d '"'"'"' | head -1)"
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo -e "${YELLOW}Skipping issuebot window (python3 not found)${NC}"
elif [ -z "$_ISSUEBOT_TOKEN" ] || [ -z "$_ISSUEBOT_CHAT" ]; then
    echo -e "${YELLOW}Skipping issuebot window (TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set in env or .env)${NC}"
else
    tmux new-window -t "$SESSION" -n issuebot -c "$ROOT_DIR"
    tmux send-keys -t "$SESSION:issuebot" "python3 $SCRIPT_DIR/tg-issue-bot.py" Enter
    echo -e "  issuebot window started (${GREEN}python3 scripts/tg-issue-bot.py${NC})"
fi

echo ""
echo -e "${CYAN}Agent Bot Team launched!${NC}"
echo "  Session: $SESSION"
echo "  Windows: cto, issuebot (more created per-task via monitor-open.sh)"
echo "  Navigation: Ctrl-b 0 → cto; Ctrl-b w → window list"
echo ""

[ -z "${TMUX:-}" ] && tmux attach -t "$SESSION"
