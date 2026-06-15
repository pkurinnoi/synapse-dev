#!/usr/bin/env bash
# session-start-cto.sh — Re-inject CTO reminders on every SessionStart.
# Fires for all agents; exits silently for subagents (agent_id present in stdin).
set -euo pipefail

REMINDER_FILE=".claude/reminders/cto.md"

# Read stdin payload (SessionStart JSON). Use a temp var to avoid blocking
# if stdin is not available in some edge cases.
PAYLOAD=""
if read -t 1 -r line 2>/dev/null; then
  PAYLOAD="$line"
fi

# Detect subagent context: agent_id is only present in spawned sub-agents.
# If running as a subagent, exit silently — CTO reminders are not for them.
if [ -n "$PAYLOAD" ]; then
  AGENT_ID=$(printf '%s' "$PAYLOAD" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('agent_id',''))" 2>/dev/null || true)
  if [ -n "$AGENT_ID" ]; then
    exit 0
  fi
fi

# Reminder file must exist.
[[ -f "$REMINDER_FILE" ]] || exit 0

# Resolve the live state of the gated MR-acceptance power. Read CTO_AUTO_ACCEPT_MR
# from the environment, falling back to scripts/.env. Default OFF.
ACCEPT_RAW="${CTO_AUTO_ACCEPT_MR:-}"
if [ -z "$ACCEPT_RAW" ] && [ -f "scripts/.env" ]; then
  ACCEPT_RAW=$( (grep -E '^[[:space:]]*CTO_AUTO_ACCEPT_MR=' "scripts/.env" 2>/dev/null || true) \
    | tail -1 | cut -d= -f2- | tr -d '"'"'"' \t\r')
fi
case "$(printf '%s' "${ACCEPT_RAW:-0}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on) ACCEPT_STATE="🟢 ENABLED — you MAY merge a green MR via \`scripts/cto-accept-mr.sh\`" ;;
  *)             ACCEPT_STATE="🔴 DISABLED — you must NOT merge any MR (PM-gated workflow only)" ;;
esac
export ACCEPT_STATE

# Emit additionalContext using the verified SessionStart JSON envelope.
python3 -c "
import sys, json, os
content = open('$REMINDER_FILE', 'r').read()
content += '\n\n## MR-acceptance power — current state\n\n- \`CTO_AUTO_ACCEPT_MR\`: ' + os.environ.get('ACCEPT_STATE','') + '\n'
output = {
    'hookSpecificOutput': {
        'hookEventName': 'SessionStart',
        'additionalContext': content
    }
}
print(json.dumps(output))
"
