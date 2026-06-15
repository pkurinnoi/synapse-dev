#!/bin/bash
# Archives a task's monitor log + Agent transcript, kills its tmux window.
# Idempotent.
#
# Usage: bash scripts/monitor-close.sh <slug>
#
# Environment:
#   AGENT_TEAM_SESSION       tmux session (default: agent-team)
#   AGENT_TEAM_LOG_DIR       live log dir (default: /tmp/agent-team)
#   AGENT_TEAM_ARCHIVE_DIR   archive dir (default: .claude/monitors/archive)

set -euo pipefail

SESSION="${AGENT_TEAM_SESSION:-agent-team}"
LOG_DIR="${AGENT_TEAM_LOG_DIR:-/tmp/agent-team}"

# Resolve default archive dir relative to workspace root (parent of this script's dir)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ARCHIVE_DIR="${AGENT_TEAM_ARCHIVE_DIR:-$WORKSPACE_ROOT/.claude/monitors/archive}"

usage() {
    echo "usage: monitor-close.sh <slug>" >&2
    exit 2
}

[ "$#" -eq 1 ] || usage
SLUG="$1"

DATE="$(date +%Y-%m-%d)"
mkdir -p "$ARCHIVE_DIR"

# Audit hook (Q6.1=c): if no matching monitor-open.sh was seen, log it non-blocking.
# The open script creates $LOG_DIR/.opened-<slug>; absence means orphan close.
if [ ! -f "$LOG_DIR/.opened-$SLUG" ]; then
    echo "$(date -Iseconds) monitor-close called without matching open: $SLUG" \
        >> "$ARCHIVE_DIR/.audit.log"
fi

# Archive live files if present (mv — ignore missing)
for ext in log jsonl; do
    live="$LOG_DIR/$SLUG.$ext"
    if [ -f "$live" ]; then
        archived="$ARCHIVE_DIR/$DATE-$SLUG.$ext"
        # If destination exists (double-close of same slug same day), append suffix
        if [ -e "$archived" ]; then
            archived="$ARCHIVE_DIR/$DATE-$SLUG-$(date +%H%M%S).$ext"
        fi
        mv "$live" "$archived" || {
            echo "ERROR: failed to archive $live → $archived" >&2
            exit 5
        }
    fi
done

# Remove marker
rm -f "$LOG_DIR/.opened-$SLUG"

# Kill window (ignore if already gone)
tmux kill-window -t "$SESSION:$SLUG" 2>/dev/null || true

echo "OK: closed '$SLUG' (archive=$ARCHIVE_DIR)"
