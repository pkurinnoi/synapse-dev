#!/bin/bash
# Opens a tmux window for an agent task, with a 2-pane vertical split:
#   left  — live-formatted Agent JSONL transcript
#   right — raw monitor log from the agent's tee -a "$MONITOR_LOG"
#
# Usage: bash scripts/monitor-open.sh <slug> <jsonl-path>
#
# Environment:
#   AGENT_TEAM_SESSION  tmux session name (default: agent-team)
#   AGENT_TEAM_LOG_DIR  monitor log directory (default: /tmp/agent-team)

set -euo pipefail

SESSION="${AGENT_TEAM_SESSION:-agent-team}"
LOG_DIR="${AGENT_TEAM_LOG_DIR:-/tmp/agent-team}"

usage() {
    echo "usage: monitor-open.sh <slug> <jsonl-path>" >&2
    echo "  <slug>       kebab-case task identifier, e.g. iter-50-qa-regression" >&2
    echo "  <jsonl-path> path to Agent tool output_file JSONL transcript" >&2
    exit 2
}

if [ "$#" -ne 2 ]; then
    usage
fi

SLUG="$1"
JSONL="$2"

# Basic slug sanity — kebab-case, max 40 chars
if ! printf '%s' "$SLUG" | grep -qE '^[a-z0-9][a-z0-9-]{0,39}$'; then
    echo "ERROR: invalid slug '$SLUG' (must be kebab-case [a-z0-9-], max 40 chars)" >&2
    exit 2
fi

# Collision check
if tmux list-windows -t "$SESSION" -F '#{window_name}' 2>/dev/null | grep -qx "$SLUG"; then
    echo "ERROR: window '$SLUG' already open — pick a fresh slug or close the old one first" >&2
    exit 3
fi

# jq guard (exercised in Task 8 test)
command -v jq >/dev/null 2>&1 || {
    echo "ERROR: jq required but not installed" >&2
    exit 4
}

# Ensure log dir
mkdir -p "$LOG_DIR"

# Monitor log — touch so tail -F has a file to open
MONITOR_LOG="$LOG_DIR/$SLUG.log"
: > "$MONITOR_LOG"

# Find script directory (for format-transcript.jq resolution)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JQ_PROGRAM="$SCRIPT_DIR/format-transcript.jq"

if [ ! -f "$JQ_PROGRAM" ]; then
    echo "ERROR: format-transcript.jq not found at $JQ_PROGRAM" >&2
    exit 5
fi

# Shell-quote all paths before embedding them in tmux command strings.
# tmux passes the command string verbatim to /bin/sh -c, so single-quote
# interpolation (like '$JSONL') breaks on paths with quotes/spaces/metachars.
# printf '%q' produces POSIX-safe tokens suitable for inline use.
JSONL_Q="$(printf '%q' "$JSONL")"
MONITOR_LOG_Q="$(printf '%q' "$MONITOR_LOG")"
JQ_PROGRAM_Q="$(printf '%q' "$JQ_PROGRAM")"

# Create window with the first command (left pane): formatted transcript
tmux new-window -t "$SESSION" -n "$SLUG" \
    "tail -c +0 -F $JSONL_Q 2>/dev/null | while IFS= read -r line; do printf '%s\n' \"\$line\" | jq -r -f $JQ_PROGRAM_Q 2>/dev/null || echo '⚠️  malformed line skipped'; done"

# Split horizontally (produces side-by-side left/right panes).
# tmux's `-h` flag = "horizontal divider" = vertical layout in common speech.
# Right pane: raw monitor log.
tmux split-window -h -t "$SESSION:$SLUG" \
    "tail -c +0 -F $MONITOR_LOG_Q"

# Mark that we opened this slug — monitor-close.sh looks here. Must match
# AGENT_TEAM_LOG_DIR at close time (no cross-environment tracking by design).
touch "$LOG_DIR/.opened-$SLUG"

# Snap focus back to CTO so operator keeps typing there
tmux select-window -t "$SESSION:cto" 2>/dev/null || true

echo "OK: opened monitor window '$SLUG' (jsonl=$JSONL log=$MONITOR_LOG)"
