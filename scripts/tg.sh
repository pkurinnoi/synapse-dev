#!/usr/bin/env bash
# scripts/tg.sh — send an HTML-formatted message to the team Telegram chat.
# Used by the CTO (interactive) to report issue lifecycle events, the same way
# the team reports. The autonomous issue-loop has its own inline tg().
#
# Usage:   bash scripts/tg.sh "✅ <b>Issue #12</b> (api-gateway) complete"
#          bash scripts/tg.sh "⏸️ paused …" --mr <project_id> <mr_iid> [mr_url]
# The --mr form attaches inline Merge / Close buttons (handled by tg-issue-bot.py)
# so a held MR can be resolved with a tap instead of typing /merge.
# Requires: TELEGRAM_TOKEN and TELEGRAM_CHAT_ID (from env or scripts/.env).

set -euo pipefail

MSG="${1:?usage: tg.sh \"<message>\" [--mr <project_id> <mr_iid> [mr_url]]}"

# Optional inline keyboard: --mr <project_id> <mr_iid> [mr_url]
REPLY_MARKUP=""
if [ "${2:-}" = "--mr" ]; then
  MR_PID="${3:?--mr requires <project_id>}"
  MR_IID="${4:?--mr requires <mr_iid>}"
  MR_URL="${5:-}"
  if [ -n "$MR_URL" ] && [[ "$MR_URL" == http* ]]; then
    REPLY_MARKUP=$(printf '{"inline_keyboard":[[{"text":"✅ Merge MR","callback_data":"mr:merge:%s:%s"},{"text":"❌ Close MR","callback_data":"mr:close:%s:%s"}],[{"text":"🔗 Open MR !%s","url":"%s"}]]}' \
      "$MR_PID" "$MR_IID" "$MR_PID" "$MR_IID" "$MR_IID" "$MR_URL")
  else
    REPLY_MARKUP=$(printf '{"inline_keyboard":[[{"text":"✅ Merge MR","callback_data":"mr:merge:%s:%s"},{"text":"❌ Close MR","callback_data":"mr:close:%s:%s"}]]}' \
      "$MR_PID" "$MR_IID" "$MR_PID" "$MR_IID")
  fi
fi

# Load scripts/.env if the Telegram vars are not already in the environment.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -z "${TELEGRAM_TOKEN:-}" ] && [ -f "$HERE/.env" ]; then
  set -a; source "$HERE/.env"; set +a
fi

: "${TELEGRAM_TOKEN:?TELEGRAM_TOKEN is not set (export it or put it in scripts/.env)}"
: "${TELEGRAM_CHAT_ID:?TELEGRAM_CHAT_ID is not set (export it or put it in scripts/.env)}"

if [ -n "$REPLY_MARKUP" ]; then
  curl -s -X POST \
    "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${MSG}" \
    --data-urlencode "parse_mode=HTML" \
    --data-urlencode "reply_markup=${REPLY_MARKUP}" \
    > /dev/null 2>&1 || true
else
  curl -s -X POST \
    "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${MSG}" \
    --data-urlencode "parse_mode=HTML" \
    > /dev/null 2>&1 || true
fi
