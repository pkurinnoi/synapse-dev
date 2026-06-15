#!/usr/bin/env bash
# scripts/cto-accept-mr.sh — Gated CTO power: accept (merge) an MR, but ONLY when
# its pipeline is green and all tests passed.
#
# This is the ONE place the CTO is permitted to mutate an MR. The gate is enforced
# here in code (not just in prose) so the settable variable genuinely controls the
# behavior. By default the power is OFF.
#
# Settable variable:  CTO_AUTO_ACCEPT_MR
#   unset / 0 / false / no   → power DISABLED, the script refuses and exits non-zero.
#   1 / true / yes / on      → power ENABLED, the script may merge a green MR.
#   Set it in scripts/.env (CTO_AUTO_ACCEPT_MR=1) or export it before launching.
#
# Preconditions checked before merging (all required):
#   1. CTO_AUTO_ACCEPT_MR is enabled.
#   2. The MR is open and not a draft.
#   3. The MR has no merge conflicts and GitLab reports it mergeable.
#   4. The latest pipeline for the MR is `success` (green pipeline = all tests passed).
# If any check fails the script refuses (non-zero) and explains why — it never
# force-merges, never skips the pipeline, never merges a draft.
#
# Usage:    bash scripts/cto-accept-mr.sh <project_id> <mr_iid>
# Requires: GITLAB_TOKEN (api scope). Telegram vars optional (used to report).

set -euo pipefail

PID="${1:?usage: cto-accept-mr.sh <project_id> <mr_iid>}"
IID="${2:?usage: cto-accept-mr.sh <project_id> <mr_iid>}"

GITLAB_API="https://gitlab.com/api/v4"

# ── Load scripts/.env for tokens + the toggle, if not already in the environment ─
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$HERE/.env" ] && { [ -z "${GITLAB_TOKEN:-}" ] || [ -z "${CTO_AUTO_ACCEPT_MR:-}" ]; }; then
  set -a; source "$HERE/.env"; set +a
fi

: "${GITLAB_TOKEN:?GITLAB_TOKEN is not set (export it or put it in scripts/.env)}"

# ── Gate 1: the settable variable must be enabled ─────────────────────────────
case "$(printf '%s' "${CTO_AUTO_ACCEPT_MR:-0}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on) ;;
  *)
    echo "REFUSED: CTO_AUTO_ACCEPT_MR is not enabled — CTO MR-acceptance power is OFF." >&2
    echo "         Set CTO_AUTO_ACCEPT_MR=1 in scripts/.env to enable it." >&2
    exit 2 ;;
esac

gl_api() {
  curl -sf --header "PRIVATE-TOKEN: $GITLAB_TOKEN" \
    --header "Content-Type: application/json" "$@"
}

tg() {  # best-effort Telegram report; no-op if vars/script absent
  [ -x "$HERE/tg.sh" ] || return 0
  bash "$HERE/tg.sh" "$1" >/dev/null 2>&1 || true
}

MR="$GITLAB_API/projects/$PID/merge_requests/$IID"

# ── Fetch the MR ──────────────────────────────────────────────────────────────
mr_json=$(gl_api "$MR" 2>/dev/null || true)
if [ -z "$mr_json" ] || [ "$(echo "$mr_json" | jq -r '.iid // empty')" = "" ]; then
  echo "REFUSED: could not fetch MR !$IID in project $PID (check id/iid/token)." >&2
  exit 1
fi

title=$(echo "$mr_json" | jq -r '.title // "(untitled)"')
web_url=$(echo "$mr_json" | jq -r '.web_url // empty')
state=$(echo "$mr_json" | jq -r '.state // empty')
draft=$(echo "$mr_json" | jq -r '.draft // .work_in_progress // false')
has_conflicts=$(echo "$mr_json" | jq -r '.has_conflicts // false')
detailed=$(echo "$mr_json" | jq -r '.detailed_merge_status // .merge_status // "unknown"')
src_branch=$(echo "$mr_json" | jq -r '.source_branch // empty')

# ── Gate 2: MR open and not a draft ───────────────────────────────────────────
[ "$state" = "opened" ] || { echo "REFUSED: MR !$IID is '$state', not open." >&2; exit 1; }
[ "$draft" = "true" ]   && { echo "REFUSED: MR !$IID is a draft." >&2; exit 1; }

# ── Gate 3: no conflicts / mergeable ──────────────────────────────────────────
[ "$has_conflicts" = "true" ] && { echo "REFUSED: MR !$IID has merge conflicts." >&2; exit 1; }
case "$detailed" in
  mergeable|can_be_merged|"") ;;  # "" = older GitLab without detailed status
  *) echo "REFUSED: MR !$IID is not mergeable (detailed_merge_status=$detailed)." >&2; exit 1 ;;
esac

# ── Gate 4: pipeline is green (all tests passed) ──────────────────────────────
# Prefer head_pipeline; fall back to the latest pipeline on the source branch.
pl_status=$(echo "$mr_json" | jq -r '.head_pipeline.status // empty')
pl_url=$(echo "$mr_json" | jq -r '.head_pipeline.web_url // empty')
if [ -z "$pl_status" ] && [ -n "$src_branch" ]; then
  latest=$(gl_api "$GITLAB_API/projects/$PID/pipelines?ref=$src_branch&order_by=id&sort=desc&per_page=1" 2>/dev/null \
    | jq -r '.[0] // empty')
  pl_status=$(echo "$latest" | jq -r '.status // empty')
  pl_url=$(echo "$latest" | jq -r '.web_url // empty')
fi

if [ "$pl_status" != "success" ]; then
  echo "REFUSED: MR !$IID pipeline is '${pl_status:-none}', not green — tests not all passed." >&2
  [ -n "$pl_url" ] && echo "         Pipeline: $pl_url" >&2
  exit 1
fi

# ── All gates green → merge ───────────────────────────────────────────────────
echo "All gates green for MR !$IID ('$title') — pipeline success. Merging ..." >&2
result=$(gl_api --request PUT "$MR/merge" \
  --data "$(jq -nc '{should_remove_source_branch:true, squash:false}')" 2>/dev/null || true)

merged_state=$(echo "$result" | jq -r '.state // empty')
if [ "$merged_state" = "merged" ]; then
  echo "MERGED: MR !$IID ('$title')." >&2
  [ -n "$web_url" ] && echo "        $web_url" >&2
  tg "✅ <b>MR !${IID} accepted by CTO</b> — green pipeline, all tests passed
🔗 ${web_url}"
  exit 0
fi

echo "FAILED: merge call did not report 'merged' (state='${merged_state:-?}')." >&2
echo "$result" | jq -r '.message // empty' >&2 || true
exit 1
