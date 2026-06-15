#!/bin/bash
# Agent Bot Team — stop + cleanup
#
# Archives any live monitor logs and kills the tmux session. (No worktrees to
# remove — This is multi-repo and the team does not provision per-dev worktrees.)
#
# Usage: ./scripts/team-stop.sh
#
# Env mirrors team-launch.sh (AGENT_TEAM_SESSION / _DIR / _LOG_DIR).

set -euo pipefail

SESSION="${AGENT_TEAM_SESSION:-agent-team}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${AGENT_TEAM_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
LOG_DIR="${AGENT_TEAM_LOG_DIR:-/tmp/agent-team}"
ARCHIVE_DIR="$ROOT_DIR/.claude/monitors/archive"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

echo -e "${YELLOW}Stopping Agent Bot Team${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ─── Archive any live monitor logs before killing tmux ──────────────────────
shopt -s nullglob
ARCHIVE_FILES=("$LOG_DIR"/*.log "$LOG_DIR"/*.jsonl)
shopt -u nullglob
if [ ${#ARCHIVE_FILES[@]} -gt 0 ]; then
    DATE="$(date +%Y-%m-%d)"
    mkdir -p "$ARCHIVE_DIR"
    for f in "${ARCHIVE_FILES[@]}"; do
        stem="$(basename "$f")"
        mv "$f" "$ARCHIVE_DIR/stop-$DATE-$stem" 2>/dev/null \
            && echo -e "  archived ${GREEN}$stem${NC} → stop-$DATE-$stem"
    done
fi
rm -f "$LOG_DIR"/.opened-*

# ─── Kill tmux session ──────────────────────────────────────────────────────
if tmux has-session -t "$SESSION" 2>/dev/null; then
    tmux kill-session -t "$SESSION"
    echo -e "tmux session '$SESSION' ${GREEN}killed${NC}"
else
    echo "tmux session '$SESSION' not running"
fi

echo ""
echo -e "${GREEN}Done.${NC}"
