#!/usr/bin/env bash
# scripts/issue-loop.sh — GitLab *group* issue loop (todo → in-progress → done)
# with headless issue-worker implementation, MR creation, CI auto-fix, and Telegram.
#
# Two-tier backlog:
#   roadmap  = backlog of all planned issues
#   todo     = ready-to-work queue
#
# Selection logic (runs continuously within one invocation):
#   1. Work `todo` issues first. If COUNT > 0, pick the highest-priority todo.
#   2. Only when ZERO todo issues exist: promote the highest-priority `roadmap` issue
#      by adding the `todo` label (+ setting its GitLab milestone), then work it.
#      Promote ONE at a time — never bulk.
#   3. Pause & wait if an MR needs human attention (CTO_AUTO_ACCEPT_MR off,
#      sensitive-path hold, gate not met, or any flagged decision). Write a pause
#      marker, report to Telegram, EXIT — resume when the human resolves it.
#   4. Pause & wait if Claude hits a session/usage limit. Revert the issue to `todo`
#      (not failed), write a session-limit pause marker with RESUME_AFTER epoch,
#      report to Telegram, EXIT — resume automatically once the reset time passes.
#
# Priority ranking (lowest sorts first, used for both todo selection and roadmap promotion):
#   1. epic  = Milestone M0–M7 (from GitLab milestone title ^M([0-7]); unset → 99)
#   2. stage = P<n> label (e.g. "P1:Schema" → 1) or [P<n>] title prefix; unparseable → 99
#   3. tags  = repo-rank (db=0, backend=1, web=2, docs=3)
#              then label-theme alphabetically
#   4. oldest first = lowest iid as final tiebreak
#
# CLI:  issue-loop.sh                          # continuous loop over the board
#       issue-loop.sh repair-mr <pid> <iid>    # repair one MR's pipeline
#       issue-loop.sh select-dry               # dry-run: print ranked todo (and roadmap
#                                              #   promotion candidate) WITHOUT mutating anything

set -euo pipefail

# ── Single instance lock (skipped for read-only subcommands) ─────────────────
LOCKFILE="/var/lock/agent-issue-loop.lock"
# select-dry is read-only — it must not be blocked by a running loop instance.
if [ "${1:-}" != "select-dry" ]; then
  exec 9>"$LOCKFILE"
  if ! flock -n 9; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Already running (lock held) — exit."
    exit 0
  fi
fi

# ── Config ────────────────────────────────────────────────────────────────────
GROUP="${GITLAB_GROUP:-your-group}"      # GitLab group path (override via GITLAB_GROUP env)
GROUP_ENCODED="$GROUP"                   # url-encoded group path
GITLAB_API="https://gitlab.com/api/v4"
TEAM_BASE="/srv/agent-team"             # holds the sibling project repos
LOG_DIR="/var/log/agent-team"
STATE_FILE="/var/run/agent-issue-loop.state"
MAX_TURNS=200
MAX_PARALLEL=1                      # issues per pass (kept serial for safety)
MAX_ISSUES_PER_RUN=20              # safety cap: max issues worked in one invocation
PIPELINE_TIMEOUT=600               # max seconds to wait on a running pipeline
PIPELINE_POLL=15
NO_PIPELINE_GRACE=75               # if no pipeline appears within this, treat as "no CI gate"
MAX_FIX_ATTEMPTS=3
STUCK_TIMEOUT=1800                 # reset in-progress issues untouched this long
SESSION_LIMIT_FALLBACK=14400       # fallback cooldown if reset time can't be parsed (4h in seconds)
SESSION_LIMIT_MARGIN=120           # safety margin added to parsed reset time (seconds)

# Board labels (Kanban columns)
LBL_TODO="todo"
LBL_ROADMAP="roadmap"
LBL_PROGRESS="in-progress"
LBL_DONE="done"
LBL_FAILED="failed"
LBL_PIPE_FAILED="pipeline-failed"
# The full set of workflow labels — used to move an issue between columns without
# clobbering its other labels (priority P0–P6, roadmap, etc.).
WORKFLOW_LABELS="$LBL_TODO,$LBL_PROGRESS,$LBL_DONE,$LBL_FAILED,$LBL_PIPE_FAILED"

# Pause marker (written when an MR needs human attention, or a session limit is hit;
# cleared when the MR is resolved or the reset time passes)
PAUSE_FILE="/var/run/agent-issue-loop.paused"
# Manual stop flag — set by the Telegram bot's /stop command, cleared by /start.
# When present the loop performs no work and exits immediately. Distinct from the
# auto-managed PAUSE_FILE (MR / session-limit holds): this one is operator-driven
# and is only ever cleared by an explicit /start.
STOP_FILE="/var/run/agent-issue-loop.stopped"
# Throttle repeated Telegram pings while paused (seconds between pings)
PAUSE_PING_INTERVAL=3600

# MRs whose changed files match this (case-insensitive) are NEVER auto-merged by
# the loop — they require manual security review (auth/session/tokens,
# credentials/secrets, payments/billing, schema/migrations, audit, access-control,
# and dependency manifests/lockfiles).
SENSITIVE_PATH_RE='(oidc|oauth|jwt|session|auth|token|credential|secret|billing|payment|subscription|schema|migrat|audit|rls|access[-_]control|package\.json|package-lock|pnpm-lock|yarn\.lock|go\.sum|requirements\.txt|composer\.lock|Cargo\.lock)'

mkdir -p "$LOG_DIR"

export PATH="$PATH:/root/.local/bin:/usr/local/bin"
[ -f /root/.nvm/nvm.sh ] && source /root/.nvm/nvm.sh 2>/dev/null || true

# Load scripts/.env if tokens are not already set
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$HERE/.env" ] && { [ -z "${GITLAB_TOKEN:-}" ] || [ -z "${CTO_AUTO_ACCEPT_MR:-}" ]; }; then
  set -a; source "$HERE/.env"; set +a
fi

: "${GITLAB_TOKEN:?GITLAB_TOKEN is not set}"
: "${TELEGRAM_TOKEN:?TELEGRAM_TOKEN is not set}"
: "${TELEGRAM_CHAT_ID:?TELEGRAM_CHAT_ID is not set}"

# ── Logging (stderr so command substitutions capture only the result line) ────
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >&2; }

# ── Telegram ──────────────────────────────────────────────────────────────────
tg() {
  # $1 = HTML text; optional $2 = reply_markup JSON (inline keyboard).
  if [ -n "${2:-}" ]; then
    curl -s -X POST \
      "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
      --data-urlencode "text=$1" \
      --data-urlencode "parse_mode=HTML" \
      --data-urlencode "reply_markup=$2" \
      > /dev/null 2>&1 || true
  else
    curl -s -X POST \
      "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
      --data-urlencode "text=$1" \
      --data-urlencode "parse_mode=HTML" \
      > /dev/null 2>&1 || true
  fi
}

# Build the Merge/Close inline keyboard JSON for a held-MR pause message.
# $1 = numeric project id, $2 = MR iid, $3 = MR url (optional).
# Emits nothing when pid/iid are missing or the iid is unknown ("?"), so tg()
# falls back to a plain message with no buttons.
mr_buttons_json() {
  local pid="$1" iid="$2" url="${3:-}"
  [ -z "$pid" ] && return 0
  [ -z "$iid" ] && return 0
  [ "$iid" = "?" ] && return 0
  if [ -n "$url" ] && [[ "$url" == http* ]]; then
    printf '{"inline_keyboard":[[{"text":"✅ Merge MR","callback_data":"mr:merge:%s:%s"},{"text":"❌ Close MR","callback_data":"mr:close:%s:%s"}],[{"text":"🔗 Open MR !%s","url":"%s"}]]}' \
      "$pid" "$iid" "$pid" "$iid" "$iid" "$url"
  else
    printf '{"inline_keyboard":[[{"text":"✅ Merge MR","callback_data":"mr:merge:%s:%s"},{"text":"❌ Close MR","callback_data":"mr:close:%s:%s"}]]}' \
      "$pid" "$iid" "$pid" "$iid"
  fi
}

# ── GitLab REST ───────────────────────────────────────────────────────────────
gl_api() {
  curl -sf \
    --header "PRIVATE-TOKEN: $GITLAB_TOKEN" \
    --header "Content-Type: application/json" \
    "$@"
}

# Move an issue across board columns WITHOUT clobbering its other labels.
# Args: pid iid "new,workflow,labels". Adds the requested workflow label(s) and
# removes only the other workflow labels; priority/roadmap labels are preserved.
gl_set_labels() {
  local pid="$1" iid="$2" want="$3" remove="" w
  IFS=',' read -ra _WL <<< "$WORKFLOW_LABELS"
  for w in "${_WL[@]}"; do
    case ",$want," in *",$w,"*) ;; *) remove="$remove,$w" ;; esac
  done
  remove="${remove#,}"
  # Pass as query params (workflow labels are URL-safe) so the json content-type
  # header on gl_api doesn't conflict with a form body.
  gl_api --request PUT \
    "$GITLAB_API/projects/$pid/issues/$iid?add_labels=${want}&remove_labels=${remove}" \
    > /dev/null 2>&1 || true
}

# Post a note on an issue. Args: pid iid body
gl_comment() {
  gl_api --request POST \
    "$GITLAB_API/projects/$1/issues/$2/notes" \
    --data "$(jq -nc --arg b "$3" '{body:$b}')" > /dev/null 2>&1 || true
}

# ── Ranking & promotion helpers ───────────────────────────────────────────────

# repo_rank_from_url: derive integer repo rank from issue web_url slug.
# db=0, backend=1, web=2, docs=3, unknown=4
repo_rank_from_url() {
  local url="$1" slug
  slug="${url#https://gitlab.com/$GROUP/}"
  slug="${slug%%/-/*}"
  case "$slug" in
    db)      echo 0 ;;
    backend) echo 1 ;;
    web)   echo 2 ;;
    docs)   echo 3 ;;
    *)               echo 4 ;;
  esac
}

# rank_issues: given a JSON array of GitLab issues on stdin, emit them sorted by
# (epic=milestone#, stage=P#, repo-rank, iid) ascending — best candidate first.
# Uses python3 for reliable sorting.
# Usage: echo "$json_array" | rank_issues
rank_issues() {
  python3 -c '
import json, re, sys

issues = json.loads(sys.stdin.read())

def repo_rank(web_url):
    m = re.search(r"gitlab\.com/[^/]+/([^/]+)", web_url or "")
    slug = m.group(1) if m else ""
    ranks = {"db": 0, "backend": 1, "web": 2, "docs": 3}
    return ranks.get(slug, 4)

def milestone_rank(issue):
    ms = issue.get("milestone") or {}
    title = ms.get("title", "") or ""
    m = re.match(r"^M([0-7])", title)
    return int(m.group(1)) if m else 99

def stage_rank(issue):
    for lbl in (issue.get("labels") or []):
        m = re.match(r"^P([0-6])(?:[:\s]|$)", lbl)
        if m:
            return int(m.group(1))
    m = re.search(r"\[P([0-6])\]", issue.get("title", "") or "")
    if m:
        return int(m.group(1))
    return 99

def sort_key(issue):
    return (
        milestone_rank(issue),
        stage_rank(issue),
        repo_rank(issue.get("web_url", "")),
        issue.get("iid", 99999),
    )

issues.sort(key=sort_key)
print(json.dumps(issues))
'
}

# Look up a GitLab group milestone by title matching ^M([0-7]). Echoes milestone id or empty.
# Args: milestone_title_pattern (e.g. "M0" or "M1")
group_milestone_id() {
  local want="$1"
  gl_api "$GITLAB_API/groups/$GROUP_ENCODED/milestones?per_page=100" 2>/dev/null \
    | jq -r --arg w "$want" '.[] | select(.title | test("^" + $w)) | .id' \
    | head -1
}

# Infer the best milestone for an issue from its P-stage label (P0→M0, P1→M0, P2→M1, etc.)
# This is a rough best-effort mapping; the founder can always set milestones manually.
# Returns: milestone title string (e.g. "M0") or empty.
infer_milestone_from_stage() {
  local stage="$1"
  case "$stage" in
    0|1) echo "M0" ;;    # P0:Foundation, P1:Schema → M0 Foundations
    2)   echo "M1" ;;    # P2:Onboarding → M1 Onboarding
    3)   echo "M2" ;;    # P3:Operate → M2 Operate
    4)   echo "M3" ;;    # P4:Build → M3 Build
    5)   echo "M4" ;;    # P5:Monetise → M4 Monetise
    6)   echo "M5" ;;    # P6:Scale → M5 Scale
    *)   echo ""   ;;
  esac
}

# Promote a roadmap issue: add the `todo` label (preserving roadmap + P-labels)
# and set its milestone if one can be determined. Args: pid iid stage web_url
promote_roadmap_issue() {
  local pid="$1" iid="$2" stage="$3" web_url="$4"
  local repo ms_title ms_id

  # Add todo label (gl_set_labels removes other workflow labels but preserves roadmap/P-labels)
  gl_set_labels "$pid" "$iid" "$LBL_TODO"

  # Determine milestone to set
  repo="${web_url#https://gitlab.com/$GROUP/}"
  repo="${repo%%/-/*}"

  # Try to infer milestone from stage
  ms_title="$(infer_milestone_from_stage "$stage")"
  if [ -n "$ms_title" ]; then
    ms_id="$(group_milestone_id "$ms_title")"
    if [ -n "$ms_id" ]; then
      gl_api --request PUT \
        "$GITLAB_API/projects/$pid/issues/$iid" \
        --data "$(jq -nc --argjson mid "$ms_id" '{milestone_id: $mid}')" \
        > /dev/null 2>&1 || true
      log "Promoted #$iid ($repo): roadmap → todo, set milestone $ms_title (id=$ms_id) from stage=$stage"
      tg "⬆️ <b>Promoted #${iid}</b> (${repo}) <code>roadmap</code> → <code>todo</code>
🏁 Milestone set to <b>${ms_title}</b> (inferred from P${stage})."
    else
      log "Promoted #$iid ($repo): roadmap → todo (no group milestone found for $ms_title)"
      tg "⬆️ <b>Promoted #${iid}</b> (${repo}) <code>roadmap</code> → <code>todo</code>
⚠️ Could not find group milestone <b>${ms_title}</b> — please set manually."
    fi
  else
    log "Promoted #$iid ($repo): roadmap → todo (no milestone inferred from stage=$stage)"
    tg "⬆️ <b>Promoted #${iid}</b> (${repo}) <code>roadmap</code> → <code>todo</code>
ℹ️ Stage unclear — set milestone manually if needed."
  fi
}

# ── Map an issue web_url to the local repo dir. e.g.
#   https://gitlab.com/$GROUP/api-gateway/-/issues/3  →  /srv/agent-team/api-gateway
repo_dir_from_url() {
  local url="$1" path slug
  path="${url#https://gitlab.com/}"      # your-group/api-gateway/-/issues/3
  path="${path%%/-/*}"                    # your-group/api-gateway
  slug="${path##*/}"                      # api-gateway
  echo "$TEAM_BASE/$slug"
}

# Project default branch (fallback main). Arg: pid
get_default_branch() {
  local b
  b=$(gl_api "$GITLAB_API/projects/$1" 2>/dev/null | jq -r '.default_branch // "main"')
  echo "${b:-main}"
}

# Create an MR (REST; glab not required). Args: pid source target title desc
# Echoes the MR web_url (or the existing one if it already exists).
create_mr() {
  local pid="$1" src="$2" tgt="$3" title="$4" desc="$5" existing url
  existing=$(gl_api \
    "$GITLAB_API/projects/$pid/merge_requests?source_branch=$src&state=opened&per_page=1" \
    2>/dev/null | jq -r '.[0].web_url // empty')
  if [ -n "$existing" ]; then echo "$existing"; return 0; fi
  url=$(gl_api --request POST "$GITLAB_API/projects/$pid/merge_requests" \
    --data "$(jq -nc --arg s "$src" --arg t "$tgt" --arg ti "$title" --arg d "$desc" \
      '{source_branch:$s, target_branch:$t, title:$ti, description:$d, remove_source_branch:true}')" \
    2>/dev/null | jq -r '.web_url // empty')
  echo "$url"
}

# Changed file paths for an MR (new + old), deduped. Args: pid mr_iid
mr_changed_paths() {
  gl_api "$GITLAB_API/projects/$1/merge_requests/$2/changes" 2>/dev/null \
    | jq -r '.changes[]? | .new_path, .old_path' 2>/dev/null | sort -u
}

# ── CTO auto-accept (merge) an MR when the gated power is ON. Args: pid issue_iid mr_url ─
# Delegates the CTO_AUTO_ACCEPT_MR toggle + merge gates to cto-accept-mr.sh (the
# enforcement boundary). The toggle is read here ONLY to route messaging — and is
# loaded from scripts/.env at the top of this script (see the sourcing block).
# Security-sensitive MRs (SENSITIVE_PATH_RE) are NEVER auto-merged here; they are
# left open for manual security review. Fail-safe: if the changed-file list can't
# be determined, auto-merge is skipped.
#
# Outcome is signalled via the global LAST_MERGE_OUTCOME:
#   merged   — MR was successfully merged; loop may continue.
#   held     — MR held for human attention (sensitive-path, power off, fail-safe);
#              loop MUST pause and wait.
#   declined — A merge gate not met (draft, conflicts, red pipeline, etc.);
#              loop MUST pause and wait.
#   no-mr    — No MR url provided; loop may continue (issue closed without MR).
LAST_MERGE_OUTCOME="no-mr"
cto_accept_mr() {
  local pid="$1" issue_iid="$2" mr_url="$3" mr_iid rc toggle paths hits hit_list
  LAST_MERGE_OUTCOME="no-mr"
  [ -n "$mr_url" ] || { log "No MR url — skipping CTO accept."; LAST_MERGE_OUTCOME="no-mr"; return 0; }
  mr_iid="${mr_url##*/}"
  case "$mr_iid" in ''|*[!0-9]*)
    log "Could not parse MR iid from '$mr_url' — skipping accept."
    LAST_MERGE_OUTCOME="no-mr"; return 0 ;;
  esac

  toggle="$(printf '%s' "${CTO_AUTO_ACCEPT_MR:-0}" | tr '[:upper:]' '[:lower:]')"
  case "$toggle" in
    1|true|yes|on) ;;
    *)
      log "CTO_AUTO_ACCEPT_MR is OFF — leaving MR !$mr_iid open for the PM-gated workflow."
      LAST_MERGE_OUTCOME="held"
      return 0 ;;
  esac

  # Security gate: never auto-merge auth/payments/schema/dependency MRs.
  paths=$(mr_changed_paths "$pid" "$mr_iid")
  if [ -z "$paths" ]; then
    log "Could not determine changed files for MR !$mr_iid — skipping auto-merge (fail-safe)."
    tg "🔒 <b>MR !${mr_iid} held</b> — changed files undeterminable, auto-merge skipped (fail-safe).
🔗 ${mr_url}"
    LAST_MERGE_OUTCOME="held"
    return 0
  fi
  hits=$(echo "$paths" | grep -iE "$SENSITIVE_PATH_RE" || true)
  if [ -n "$hits" ]; then
    hit_list=$(echo "$hits" | tr '\n' ' ' | cut -c1-300)
    log "MR !$mr_iid touches security-sensitive paths — skipping auto-merge: $hit_list"
    gl_comment "$pid" "$issue_iid" "Auto-merge skipped — this MR touches security-sensitive paths ($hit_list) and requires manual security review before merge."
    tg "🔒 <b>MR !${mr_iid} held for security review</b> — touches sensitive paths, auto-merge skipped.
🔗 ${mr_url}"
    LAST_MERGE_OUTCOME="held"
    return 0
  fi

  log "CTO accept-MR for !$mr_iid (project $pid) ..."
  set +e
  bash "$HERE/cto-accept-mr.sh" "$pid" "$mr_iid" >> "${CLAUDE_LOG:-/dev/null}" 2>&1
  rc=$?
  set -e
  case $rc in
    0)
      log "✓ CTO merged MR !$mr_iid (cto-accept-mr.sh reports its own Telegram success)."
      LAST_MERGE_OUTCOME="merged" ;;
    2)
      log "CTO_AUTO_ACCEPT_MR is OFF — leaving MR !$mr_iid open for the PM-gated workflow."
      LAST_MERGE_OUTCOME="held" ;;
    *)
      log "CTO declined MR !$mr_iid (a merge gate not met, rc=$rc) — left open."
      tg "⏸️ <b>MR !${mr_iid} left open</b> — CTO auto-accept declined (a merge gate not met).
🔗 ${mr_url}"
      LAST_MERGE_OUTCOME="declined" ;;
  esac
  return 0
}

# ── Track / clear active issue (survives restarts for resume) ─────────────────
state_set_issue() { printf 'PID=%s\nIID=%s\nWORKER=%s\nSTARTED=%s\n' "$1" "$2" "$$" "$(date -Iseconds)" > "$STATE_FILE"; }
state_clear()     { rm -f "$STATE_FILE"; }

# ── Run claude (headless) without set -e killing the script ───────────────────
# Always invoked from $TEAM_BASE so `--agent issue-worker` resolves from
# /srv/agent-team/.claude/agents/. The prompt cd's into the target repo.
run_claude() {
  cd "$TEAM_BASE"
  set +e
  claude "$@" >> "${CLAUDE_LOG:-/dev/null}" 2>&1
  local RC=$?
  set -e
  return $RC
}

# ── Session-limit detection ───────────────────────────────────────────────────
#
# claude_session_limited <logfile>
#   Scans the last 100 lines of <logfile> for a Claude session/usage-limit message.
#   If found: prints the human-readable reset string (e.g. "4:10pm (UTC)") to stdout
#             and returns 0.
#   If not found: prints nothing and returns 1.
#
#   Patterns detected (case-insensitive):
#     "session limit", "usage limit", "limit reached", "hit your * limit"
#   AND at least one of these indicates it is a Claude capacity message — confirmed
#   by presence of a "resets" line in the same log tail.
#
#   Reset-time parsing (python3):
#     Handles: HH:MMam/pm [(TZ)], Ham/pm [(TZ)], HH:MM 24h [(TZ)]
#     Timezone: only UTC is explicitly handled; others fall back to UTC arithmetic.
#     If parsing succeeds and the computed time is already past today, advances to
#     tomorrow. Adds SESSION_LIMIT_MARGIN seconds of safety margin.
#     Falls back to SESSION_LIMIT_FALLBACK seconds from now if parsing fails.
#
#   Sets global SESSION_LIMIT_RESET_STR and SESSION_LIMIT_RESUME_AFTER as a side-effect
#   (in addition to the stdout print) so callers can use them without re-running.
SESSION_LIMIT_RESET_STR=""
SESSION_LIMIT_RESUME_AFTER=0

claude_session_limited() {
  local logfile="$1"
  SESSION_LIMIT_RESET_STR=""
  SESSION_LIMIT_RESUME_AFTER=0

  [ -f "$logfile" ] || return 1

  # Grab the last 100 lines for inspection (cheap).
  local tail_content
  tail_content=$(tail -100 "$logfile" 2>/dev/null || true)
  [ -z "$tail_content" ] && return 1

  # Check for a session/usage limit indicator (case-insensitive).
  if ! echo "$tail_content" | grep -qiE '(session limit|usage limit|limit reached|hit your .* limit)'; then
    return 1
  fi

  # Secondary confirmation: a "resets" (or "reset at") line must also be present
  # (distinguishes real capacity limits from unrelated "limit" words in code/errors).
  if ! echo "$tail_content" | grep -qiE 'resets?'; then
    return 1
  fi

  # Extract the reset-time string and compute RESUME_AFTER via python3.
  local result
  result=$(python3 - "$tail_content" "$SESSION_LIMIT_FALLBACK" "$SESSION_LIMIT_MARGIN" <<'PYEOF'
import sys, re, time, datetime

content   = sys.argv[1]
fallback  = int(sys.argv[2])
margin    = int(sys.argv[3])
now       = time.time()

# Find the line containing "resets" or "reset at"
reset_str = ""
for line in content.splitlines():
    if re.search(r'resets?', line, re.IGNORECASE):
        # Extract the portion from "resets[s]?" onward
        m = re.search(r'resets?\s*(?:at\s*)?(.+)', line, re.IGNORECASE)
        if m:
            reset_str = m.group(1).strip()
        break

if not reset_str:
    # Fallback: use now + fallback seconds
    resume = int(now) + fallback
    print("RESET_STR=unknown")
    print("RESUME_AFTER=" + str(resume))
    print("USED_FALLBACK=1")
    sys.exit(0)

# Try to parse the time from reset_str
# Patterns (in order of specificity):
#   H:MMam/pm [(TZ)]
#   Ham/pm [(TZ)]
#   H:MM 24h [(TZ)]
parsed_h = None
parsed_m = 0
parsed_ampm = None
parsed_tz = "UTC"

m = re.match(r'^(\d{1,2}):(\d{2})\s*(am|pm)\s*(?:\((\w+)\))?', reset_str, re.IGNORECASE)
if m:
    parsed_h, parsed_m, parsed_ampm, tz = int(m.group(1)), int(m.group(2)), m.group(3).lower(), m.group(4)
    if tz:
        parsed_tz = tz
else:
    m = re.match(r'^(\d{1,2})\s*(am|pm)\s*(?:\((\w+)\))?', reset_str, re.IGNORECASE)
    if m:
        parsed_h, parsed_ampm, tz = int(m.group(1)), m.group(2).lower(), m.group(3)
        if tz:
            parsed_tz = tz
    else:
        m = re.match(r'^(\d{1,2}):(\d{2})\s*(?:\((\w+)\))?', reset_str, re.IGNORECASE)
        if m:
            parsed_h, parsed_m, tz = int(m.group(1)), int(m.group(2)), m.group(3)
            if tz:
                parsed_tz = tz

if parsed_h is None:
    resume = int(now) + fallback
    print("RESET_STR=" + reset_str)
    print("RESUME_AFTER=" + str(resume))
    print("USED_FALLBACK=1")
    sys.exit(0)

# Convert 12h to 24h
if parsed_ampm == 'am':
    if parsed_h == 12:
        parsed_h = 0
elif parsed_ampm == 'pm':
    if parsed_h != 12:
        parsed_h += 12

# We only handle UTC explicitly; any other TZ name falls back to treating it as UTC
# (conservative — the margin and fallback cover small TZ errors).
# Build a UTC datetime for today at the parsed time.
now_utc = datetime.datetime.utcfromtimestamp(now)
candidate = now_utc.replace(hour=parsed_h, minute=parsed_m, second=0, microsecond=0)

# If that time is already past (or within the margin), advance to tomorrow.
candidate_epoch = (candidate - datetime.datetime(1970,1,1)).total_seconds()
if candidate_epoch <= now + margin:
    candidate += datetime.timedelta(days=1)
    candidate_epoch = (candidate - datetime.datetime(1970,1,1)).total_seconds()

resume = int(candidate_epoch) + margin
print("RESET_STR=" + reset_str)
print("RESUME_AFTER=" + str(resume))
print("USED_FALLBACK=0")
PYEOF
  )

  if [ -z "$result" ]; then
    # python3 itself failed — use fallback
    SESSION_LIMIT_RESET_STR="unknown"
    SESSION_LIMIT_RESUME_AFTER=$(( $(date +%s) + SESSION_LIMIT_FALLBACK ))
    log "claude_session_limited: python3 parse failed — using ${SESSION_LIMIT_FALLBACK}s fallback"
    echo "$SESSION_LIMIT_RESET_STR"
    return 0
  fi

  SESSION_LIMIT_RESET_STR=$(echo "$result" | grep '^RESET_STR=' | cut -d= -f2-)
  SESSION_LIMIT_RESUME_AFTER=$(echo "$result" | grep '^RESUME_AFTER=' | cut -d= -f2-)
  local used_fallback
  used_fallback=$(echo "$result" | grep '^USED_FALLBACK=' | cut -d= -f2-)

  if [ "$used_fallback" = "1" ]; then
    log "claude_session_limited: could not parse reset time from '${SESSION_LIMIT_RESET_STR}' — using ${SESSION_LIMIT_FALLBACK}s fallback (resume at $(date -d "@${SESSION_LIMIT_RESUME_AFTER}" '+%Y-%m-%d %H:%M:%S UTC' 2>/dev/null || echo "epoch ${SESSION_LIMIT_RESUME_AFTER}"))"
  else
    log "claude_session_limited: session limit detected, reset='${SESSION_LIMIT_RESET_STR}', resume at $(date -d "@${SESSION_LIMIT_RESUME_AFTER}" '+%Y-%m-%d %H:%M:%S UTC' 2>/dev/null || echo "epoch ${SESSION_LIMIT_RESUME_AFTER}")"
  fi

  echo "$SESSION_LIMIT_RESET_STR"
  return 0
}

# ── Latest pipeline for a ref. Args: pid ref → "status<TAB>id<TAB>url" or empty ─
get_latest_pipeline() {
  gl_api "$GITLAB_API/projects/$1/pipelines?ref=$2&order_by=id&sort=desc&per_page=1" \
    2>/dev/null | jq -r '.[0] | select(.) | [.status, .id, .web_url] | @tsv'
}

# ── Wait for a CI pipeline. Args: pid ref [notify]. Echoes one of:
#    success | <status>:<id> | timeout | none   (none = no pipeline ever appeared)
wait_for_pipeline() {
  local pid="$1" ref="$2" notify="${3:-true}" elapsed=0 seen=0
  log "Waiting for pipeline on $ref (project $pid) ..."
  [ "$notify" = "true" ] && tg "⏳ <b>Pipeline</b> on <code>$ref</code>"
  sleep 10
  while [ $elapsed -lt $PIPELINE_TIMEOUT ]; do
    local p status id url
    p=$(gl_api "$GITLAB_API/projects/$pid/pipelines?ref=$ref&order_by=id&sort=desc&per_page=1" \
      2>/dev/null | jq -r '.[0] // empty')
    if [ -z "$p" ]; then
      if [ $seen -eq 0 ] && [ $elapsed -ge $NO_PIPELINE_GRACE ]; then
        log "No pipeline on $ref after ${elapsed}s — treating as no CI gate."
        echo "none"; return 0
      fi
      sleep $PIPELINE_POLL; elapsed=$((elapsed + PIPELINE_POLL)); continue
    fi
    seen=1
    status=$(echo "$p" | jq -r '.status'); id=$(echo "$p" | jq -r '.id'); url=$(echo "$p" | jq -r '.web_url')
    log "Pipeline #$id: $status"
    case "$status" in
      success)
        [ "$notify" = "true" ] && tg "✅ <b>Pipeline passed</b>
🔗 $url"
        echo "success"; return 0 ;;
      failed|canceled|skipped)
        [ "$notify" = "true" ] && tg "❌ <b>Pipeline $status</b>
🔗 $url"
        echo "$status:$id"; return 0 ;;
      *) sleep $PIPELINE_POLL; elapsed=$((elapsed + PIPELINE_POLL)) ;;
    esac
  done
  [ "$notify" = "true" ] && tg "⏰ <b>Pipeline timeout</b> on <code>$ref</code>"
  echo "timeout"
}

# ── Failed-job traces for a pipeline. Args: pid pipeline_id ──────────────────
get_pipeline_failures() {
  local pid="$1" pl="$2" jobs
  jobs=$(gl_api "$GITLAB_API/projects/$pid/pipelines/$pl/jobs" 2>/dev/null || echo "[]")
  echo "$jobs" | jq -r '.[] | select(.status=="failed") | .id' | while read -r jid; do
    [ -z "$jid" ] && continue
    local name trace
    name=$(gl_api "$GITLAB_API/projects/$pid/jobs/$jid" 2>/dev/null | jq -r '.name // "unknown"')
    trace=$(curl -sf --header "PRIVATE-TOKEN: $GITLAB_TOKEN" \
      "$GITLAB_API/projects/$pid/jobs/$jid/trace" 2>/dev/null | tail -100 || echo "(no trace)")
    echo "=== FAILED JOB: $name ==="; echo "$trace"; echo "=== END: $name ==="
  done
}

# ── Auto-fix a branch pipeline. Args: pid repo_dir branch iid logfile title ───
# Returns 0 if green (or no CI gate), 1 on failure/timeout.
repair_pipeline() {
  local pid="$1" repo_dir="$2" branch="$3" iid="$4" logfile="$5" title="$6"
  local attempt=0 result plid logs rc ctx=""
  [ -n "$iid" ] && ctx=" for issue #${iid}"
  log "Repairing pipeline on ${branch}${ctx} (project $pid) ..."

  while [ $attempt -le $MAX_FIX_ATTEMPTS ]; do
    result=$(wait_for_pipeline "$pid" "$branch" "$([ $attempt -eq 0 ] && echo true || echo false)" | tail -1)

    [ "$result" = "success" ] && { log "Pipeline green on ${branch}."; return 0; }
    [ "$result" = "none" ]    && { log "No CI gate on ${branch} — accepting."; return 0; }
    [ "$result" = "timeout" ] && { log "Pipeline timed out on ${branch}."; return 1; }

    attempt=$((attempt + 1))
    plid=$(echo "$result" | cut -d: -f2)

    if [ $attempt -gt $MAX_FIX_ATTEMPTS ]; then
      log "Pipeline unfixable on ${branch} after ${MAX_FIX_ATTEMPTS} attempts."
      if [ -n "$iid" ]; then
        gl_set_labels "$pid" "$iid" "$LBL_FAILED,$LBL_PIPE_FAILED"
        gl_comment "$pid" "$iid" "The agent failed to fix the pipeline after ${MAX_FIX_ATTEMPTS} attempts. Manual intervention needed."
      fi
      tg "❌ <b>Pipeline unfixable</b>${ctx}
Branch: <code>${branch}</code> · after ${MAX_FIX_ATTEMPTS} attempts."
      return 1
    fi

    log "Pipeline failed on ${branch} — fix attempt ${attempt}/${MAX_FIX_ATTEMPTS} ..."
    tg "🔧 <b>Fixing pipeline</b>${ctx} (attempt ${attempt}/${MAX_FIX_ATTEMPTS})
🌿 <code>${branch}</code> · <b>${title}</b>"

    logs=$(get_pipeline_failures "$pid" "$plid")
    [ -z "$logs" ] && logs="(No failed job traces — inspect pipeline #${plid} in GitLab.)"

    CLAUDE_LOG="$logfile"
    run_claude --agent issue-worker --print --max-turns "$MAX_TURNS" \
      "Fix CI pipeline failures for branch ${branch} in repo ${repo_dir}.

FAILED JOB LOGS:
${logs}

INSTRUCTIONS:
1. cd ${repo_dir}
2. git fetch origin && git checkout ${branch} && git pull origin ${branch}
3. Read the errors and fix everything (lint, types, tests, build) for this repo's stack.
4. git config user.email 'agent@example.com' && git config user.name 'Agent Bot'
5. git add -A && git commit -m 'fix: pipeline failures${ctx} attempt ${attempt}'
6. git push origin ${branch}

Fix only what the logs indicate. Do not change unrelated code.
Do not commit: scripts/issue-loop.sh, scripts/tg.sh, nohup.out, any .env or secret." || {
        local fix_rc=$?
        # Check for session limit before treating as a plain failure.
        if claude_session_limited "$logfile"; then
          log "Claude session limit hit during pipeline repair${ctx} — pausing loop."
          # Revert issue label to todo (not failed) if we have a valid iid.
          if [ -n "$iid" ]; then
            gl_set_labels "$pid" "$iid" "$LBL_TODO"
            log "Issue #${iid} reverted to todo (session limit — not failed)."
          fi
          state_clear
          session_limit_pause_set "$SESSION_LIMIT_RESET_STR" "$SESSION_LIMIT_RESUME_AFTER" "${iid:-}"
          tg "⏸️ Claude session limit reached — issue-loop paused until ${SESSION_LIMIT_RESET_STR}. Issue #${iid:-N/A} returned to the queue (not failed)."
          exit 0
        fi
        log "Claude pipeline-fix attempt ${attempt} exited nonzero (rc=${fix_rc})"
        tg "⚠️ <b>Pipeline fix agent failed</b>${ctx} (attempt ${attempt}/${MAX_FIX_ATTEMPTS})"
      }
    sleep 5
  done
  return 1
}

# ── Pause marker helpers ──────────────────────────────────────────────────────

# Write a TYPE=mr pause marker. Args: mr_url pid mr_iid
pause_set() {
  printf 'TYPE=mr\nMR_URL=%s\nPID=%s\nMR_IID=%s\nWORKER=%s\nPAUSED_AT=%s\nLAST_PING=0\n' \
    "$1" "$2" "$3" "$$" "$(date -Iseconds)" > "$PAUSE_FILE"
  log "Pause marker written (TYPE=mr): $PAUSE_FILE (MR: $1)"
}

# Write a TYPE=session-limit pause marker.
# Args: reset_str resume_after_epoch iid
session_limit_pause_set() {
  local reset_str="$1" resume_after="$2" iid="${3:-}"
  printf 'TYPE=session-limit\nRESET_STR=%s\nRESUME_AFTER=%s\nIID=%s\nWORKER=%s\nPAUSED_AT=%s\nLAST_PING=0\n' \
    "$reset_str" "$resume_after" "$iid" "$$" "$(date -Iseconds)" > "$PAUSE_FILE"
  log "Pause marker written (TYPE=session-limit): resume after epoch $resume_after (reset: $reset_str)"
}

# Clear the pause marker.
pause_clear() {
  rm -f "$PAUSE_FILE"
  log "Pause marker cleared."
}

# Check the pause marker. Returns 0 (still paused — caller should exit) or
# 1 (resolved/clear — caller should continue).
# Handles two pause types:
#   TYPE=session-limit  — pure time comparison; no API calls; cheap.
#   TYPE=mr (or no type) — queries GitLab MR state; throttled Telegram pings.
# Re-pings Telegram at most once per PAUSE_PING_INTERVAL seconds.
pause_check() {
  [ -f "$PAUSE_FILE" ] || return 1   # no marker → not paused

  local pause_type last_ping now
  pause_type=$(grep '^TYPE=' "$PAUSE_FILE" 2>/dev/null | cut -d= -f2- || echo "mr")
  last_ping=$( grep '^LAST_PING=' "$PAUSE_FILE" | cut -d= -f2-)
  now=$(date +%s)

  # ── Session-limit branch (cheap: one integer comparison, no API call) ──────
  if [ "$pause_type" = "session-limit" ]; then
    local resume_after reset_str
    resume_after=$(grep '^RESUME_AFTER=' "$PAUSE_FILE" | cut -d= -f2-)
    reset_str=$(   grep '^RESET_STR='    "$PAUSE_FILE" | cut -d= -f2-)

    if [ "$now" -ge "$resume_after" ]; then
      log "Session-limit pause elapsed (reset: ${reset_str}) — clearing marker and resuming."
      pause_clear
      return 1   # resolved → caller continues
    fi

    # Still waiting: throttle Telegram to at most once per hour
    if [ $(( now - last_ping )) -ge "$PAUSE_PING_INTERVAL" ]; then
      local remaining=$(( resume_after - now ))
      tg "⏸️ <b>Loop paused</b> — Claude session limit, waiting until ${reset_str} (${remaining}s remaining)."
      sed -i "s/^LAST_PING=.*/LAST_PING=${now}/" "$PAUSE_FILE" 2>/dev/null || true
    fi

    log "Loop paused (session-limit) — resuming after epoch ${resume_after} (reset: ${reset_str}). Exiting."
    return 0   # still paused → caller exits
  fi

  # ── MR branch (existing behaviour) ───────────────────────────────────────
  local mr_url pid mr_iid mr_state
  mr_url=$(  grep '^MR_URL='   "$PAUSE_FILE" | cut -d= -f2-)
  pid=$(     grep '^PID='      "$PAUSE_FILE" | cut -d= -f2-)
  mr_iid=$(  grep '^MR_IID='  "$PAUSE_FILE" | cut -d= -f2-)

  # Query the MR state via API
  mr_state=$(gl_api "$GITLAB_API/projects/$pid/merge_requests/$mr_iid" 2>/dev/null \
    | jq -r '.state // "unknown"')

  case "$mr_state" in
    merged|closed)
      log "Paused MR !$mr_iid is now $mr_state — clearing pause marker and resuming."
      pause_clear
      return 1 ;;    # resolved → caller should continue
  esac

  # Still open/unmerged: throttle Telegram pings
  if [ $(( now - last_ping )) -ge "$PAUSE_PING_INTERVAL" ]; then
    tg "⏸️ <b>Loop paused</b> — waiting for human to resolve MR !${mr_iid}.
Merge or close it (tap a button below, or reply <code>/merge</code>) to resume.
🔗 ${mr_url}" \
      "$(mr_buttons_json "$pid" "$mr_iid" "$mr_url")"
    # Update last_ping in the marker
    sed -i "s/^LAST_PING=.*/LAST_PING=${now}/" "$PAUSE_FILE" 2>/dev/null || true
  fi

  log "Loop still paused — MR !$mr_iid is $mr_state. Exiting to wait."
  return 0   # still paused → caller should exit
}

# ── Fetch and rank open issues by label ───────────────────────────────────────

# fetch_ranked_issues: fetch group issues filtered by label, exclude workflow
# columns (in-progress/done/failed), rank them, echo the ranked JSON array.
# Args: label [extra_exclude_label]
# The optional extra_exclude_label lets us skip roadmap issues already carrying `todo`.
fetch_ranked_issues() {
  local label="$1" exclude_extra="${2:-}"
  local raw filtered
  raw=$(gl_api \
    "$GITLAB_API/groups/$GROUP_ENCODED/issues?state=opened&labels=${label}&per_page=100" \
    2>/dev/null || echo "[]")

  # Build the jq select expression: always exclude in-progress/done/failed/pipe-failed
  local jq_filter
  jq_filter='[.[] | select(
    (.labels | index("'"$LBL_PROGRESS"'") | not) and
    (.labels | index("'"$LBL_DONE"'")     | not) and
    (.labels | index("'"$LBL_FAILED"'")   | not) and
    (.labels | index("'"$LBL_PIPE_FAILED"'") | not)'

  if [ -n "$exclude_extra" ]; then
    jq_filter="${jq_filter} and
    (.labels | index(\"${exclude_extra}\") | not)"
  fi
  jq_filter="${jq_filter})]"

  filtered=$(echo "$raw" | jq "$jq_filter")
  echo "$filtered" | rank_issues
}

# ── Work a single issue end-to-end. Args: pid iid web_url title body [mode] ───
work_issue() {
  local pid="$1" iid="$2" url="$3" title="$4" body="$5" mode="${6:-}"
  local repo_dir branch logfile default_branch mr_url start elapsed
  repo_dir="$(repo_dir_from_url "$url")"
  branch="fix/issue-${iid}"
  logfile="${LOG_DIR}/issue-${pid}-${iid}.log"
  start=$(date +%s)

  if [ ! -d "$repo_dir/.git" ]; then
    log "✗ Repo dir $repo_dir not found for issue #${iid}"
    tg "❌ <b>Issue #${iid}</b> — local repo <code>${repo_dir##*/}</code> not found at ${repo_dir}. Skipping."
    return 1
  fi

  default_branch="$(get_default_branch "$pid")"

  if [ "$mode" = "resume" ]; then
    log "→ Resuming issue #${iid} (${repo_dir##*/}): ${title}"
    tg "♻️ <b>Resuming issue #${iid}</b> (${repo_dir##*/})
<b>${title}</b>
🔗 ${url} · 🌿 <code>${branch}</code>"
    { echo ""; echo "=== resumed $(date -Iseconds) ==="; } >> "$logfile"
  else
    > "$logfile"
    log "→ Starting issue #${iid} (${repo_dir##*/}): ${title}"
    tg "🚀 <b>Issue #${iid} started</b> (${repo_dir##*/})
<b>${title}</b>
🔗 ${url} · 🌿 <code>${branch}</code>"
  fi

  state_set_issue "$pid" "$iid"
  gl_set_labels "$pid" "$iid" "$LBL_PROGRESS"

  # ── Implement ──────────────────────────────────────────────────────────────
  CLAUDE_LOG="$logfile"
  run_claude --agent issue-worker --print --max-turns "$MAX_TURNS" \
    "You are working headlessly on GitLab issue #${iid} in the GitLab group, repo ${repo_dir}.

ISSUE TITLE: ${title}

ISSUE BODY:
${body}

INSTRUCTIONS — follow every step. Never ask questions. Never pause.
1. cd ${repo_dir}
2. git fetch origin && git checkout ${default_branch} && git pull origin ${default_branch}
3. git checkout -b ${branch} 2>/dev/null || git checkout ${branch}
4. Read existing code to learn the repo's patterns and stack, then implement everything in the issue.
5. Write or update tests; run this repo's lint + tests until green (see .claude/TEAM.md and the repo README).
6. git config user.email 'agent@example.com' && git config user.name 'Agent Bot'
7. git add -A && git commit -m 'fix: #${iid} ${title}'
8. git push origin ${branch}
Do NOT push to ${default_branch}. Do NOT open the MR (the loop does that).
Do NOT commit: scripts/issue-loop.sh, scripts/tg.sh, nohup.out, .claude/agents/issue-worker.md, any .env or secret, any *.log."
  local impl_rc=$?

  if [ $impl_rc -ne 0 ]; then
    # ── Session-limit check — MUST happen before marking failed ───────────
    if claude_session_limited "$logfile"; then
      log "Claude session limit hit during implementation of #${iid} — pausing loop."
      # Revert: remove in-progress, set back to todo (not failed).
      gl_set_labels "$pid" "$iid" "$LBL_TODO"
      log "Issue #${iid} reverted to todo (session limit — not failed)."
      state_clear
      session_limit_pause_set "$SESSION_LIMIT_RESET_STR" "$SESSION_LIMIT_RESUME_AFTER" "$iid"
      tg "⏸️ Claude session limit reached — issue-loop paused until ${SESSION_LIMIT_RESET_STR}. Issue #${iid} returned to the queue (not failed)."
      exit 0
    fi

    log "✗ Implementation failed for #${iid}"
    gl_set_labels "$pid" "$iid" "$LBL_FAILED"
    local tail_log; tail_log=$(tail -5 "$logfile" 2>/dev/null | tr '\n' ' ' | cut -c1-280)
    tg "❌ <b>Issue #${iid} implementation failed</b> (${repo_dir##*/})
<b>${title}</b>
📄 <code>${logfile}</code>
💬 <code>${tail_log}</code>"
    state_clear; return 1
  fi

  log "✓ Implementation done for #${iid} — opening MR..."
  mr_url=$(create_mr "$pid" "$branch" "$default_branch" "fix: #${iid} ${title}" "Closes #${iid}

Implemented headlessly by the issue-worker. See branch \`${branch}\`.")
  [ -n "$mr_url" ] && gl_comment "$pid" "$iid" "Implementation complete on \`${branch}\`. MR: ${mr_url}"
  log "MR: ${mr_url:-<none>}"

  if repair_pipeline "$pid" "$repo_dir" "$branch" "$iid" "$logfile" "$title"; then
    elapsed=$(( $(date +%s) - start ))
    gl_set_labels "$pid" "$iid" "$LBL_DONE"
    log "✓ Issue #${iid} done in $((elapsed/60))m $((elapsed%60))s"
    tg "✅ <b>Issue #${iid} complete</b> (${repo_dir##*/})
<b>${title}</b>
⏱ $((elapsed/60))m $((elapsed%60))s
🔗 ${url}
🔀 ${mr_url:-<no MR>}"
    cto_accept_mr "$pid" "$iid" "$mr_url"
    state_clear; return 0
  fi

  gl_set_labels "$pid" "$iid" "$LBL_FAILED,$LBL_PIPE_FAILED"
  tg "❌ <b>Issue #${iid} — pipeline not resolved</b> (${repo_dir##*/})
<b>${title}</b> · will retry next loop.
🔀 ${mr_url:-<no MR>}"
  state_clear; return 1
}

# ── Reset issues stuck in-progress too long (group-wide) ──────────────────────
reset_stuck_issues() {
  local stuck
  stuck=$(gl_api \
    "$GITLAB_API/groups/$GROUP_ENCODED/issues?state=opened&labels=$LBL_PROGRESS&per_page=50" \
    2>/dev/null || echo "[]")
  echo "$stuck" | jq -c '.[]' 2>/dev/null | while read -r issue; do
    local pid iid updated age
    pid=$(echo "$issue" | jq -r '.project_id'); iid=$(echo "$issue" | jq -r '.iid')
    updated=$(echo "$issue" | jq -r '.updated_at')
    age=$(( $(date +%s) - $(date -d "$updated" +%s 2>/dev/null || echo 0) ))
    if [ "$age" -gt "$STUCK_TIMEOUT" ]; then
      log "Resetting stuck issue $pid#$iid (stuck ${age}s)"
      gl_set_labels "$pid" "$iid" "$LBL_TODO"
      tg "♻️ <b>Issue #${iid}</b> was stuck in-progress $((age/60))m — reset to <code>todo</code>."
    fi
  done
}

# ── CLI dispatch (must come after all function definitions) ───────────────────

# CLI: repair a specific MR. Args: pid iid
if [ "${1:-}" = "repair-mr" ]; then
  : "${2:?Usage: $0 repair-mr <project_id> <issue_iid>}"
  : "${3:?Usage: $0 repair-mr <project_id> <issue_iid>}"
  PID="$2"; IID="$3"; BRANCH="fix/issue-${IID}"
  ISSUE=$(gl_api "$GITLAB_API/projects/$PID/issues/$IID" 2>/dev/null || echo "{}")
  URL=$(echo "$ISSUE" | jq -r '.web_url // empty')
  TITLE=$(echo "$ISSUE" | jq -r '.title // "Pipeline repair"')
  REPO_DIR="$(repo_dir_from_url "$URL")"
  gl_set_labels "$PID" "$IID" "$LBL_PROGRESS,$LBL_PIPE_FAILED"
  repair_pipeline "$PID" "$REPO_DIR" "$BRANCH" "$IID" "${LOG_DIR}/mr-${PID}-${IID}.log" "$TITLE"
  exit $?
fi

# CLI: select-dry — read-only ranking preview (no mutations, no Telegram)
# Fetches the live board and prints the ranked todo list (and, if todo is empty,
# which roadmap issue WOULD be promoted). Does NOT add labels, set milestones,
# run claude, or send Telegram messages. Safe to run at any time.
if [ "${1:-}" = "select-dry" ]; then
  echo "=== select-dry: ranked todo issues ==="
  DRY_TODO=$(fetch_ranked_issues "$LBL_TODO")
  DRY_TODO_COUNT=$(echo "$DRY_TODO" | jq 'length')
  echo "Found $DRY_TODO_COUNT open todo issue(s):"
  echo "$DRY_TODO" | jq -r '
    to_entries[] |
    .value as $i |
    [ (.key + 1 | tostring),
      ("#" + ($i.iid | tostring)),
      ("milestone=" + (($i.milestone.title) // "none")),
      ("labels=" + ($i.labels | join(","))),
      ("repo=" + ($i.web_url | capture("/(?<r>[^/]+)/-/").r // "unknown")),
      $i.title
    ] | join("  ")
  ' 2>/dev/null || echo "(none)"

  echo ""
  if [ "$DRY_TODO_COUNT" -eq 0 ]; then
    echo "=== No todo issues — roadmap promotion candidate ==="
    DRY_ROADMAP=$(fetch_ranked_issues "$LBL_ROADMAP" "$LBL_TODO")
    DRY_ROADMAP_COUNT=$(echo "$DRY_ROADMAP" | jq 'length')
    if [ "$DRY_ROADMAP_COUNT" -eq 0 ]; then
      echo "No roadmap issues available either — nothing to promote."
    else
      echo "Would promote the following issue (rank 1 of $DRY_ROADMAP_COUNT):"
      echo "$DRY_ROADMAP" | jq -r '
        .[0] as $i |
        "  #" + ($i.iid | tostring) + "  milestone=" + (($i.milestone.title) // "none") +
        "  labels=" + ($i.labels | join(",")) +
        "  repo=" + ($i.web_url | capture("/(?<r>[^/]+)/-/").r // "unknown") +
        "\n  title: " + $i.title
      ' 2>/dev/null
      echo ""
      echo "Full roadmap ranking ($DRY_ROADMAP_COUNT issue(s)):"
      echo "$DRY_ROADMAP" | jq -r '
        to_entries[] |
        .value as $i |
        [ (.key + 1 | tostring),
          ("#" + ($i.iid | tostring)),
          ("milestone=" + (($i.milestone.title) // "none")),
          ("labels=" + ($i.labels | join(","))),
          ("repo=" + ($i.web_url | capture("/(?<r>[^/]+)/-/").r // "unknown")),
          $i.title
        ] | join("  ")
      ' 2>/dev/null
    fi
  else
    echo "=== (Todo queue non-empty — no roadmap promotion would occur) ==="
    echo "Roadmap ranking shown for reference:"
    DRY_ROADMAP=$(fetch_ranked_issues "$LBL_ROADMAP" "$LBL_TODO")
    DRY_ROADMAP_COUNT=$(echo "$DRY_ROADMAP" | jq 'length')
    echo "$DRY_ROADMAP" | jq -r '
      to_entries[] |
      .value as $i |
      [ (.key + 1 | tostring),
        ("#" + ($i.iid | tostring)),
        ("milestone=" + (($i.milestone.title) // "none")),
        ("labels=" + ($i.labels | join(","))),
        ("repo=" + ($i.web_url | capture("/(?<r>[^/]+)/-/").r // "unknown")),
        $i.title
      ] | join("  ")
    ' 2>/dev/null || echo "(none)"
  fi
  echo ""
  echo "=== select-dry complete (no mutations made) ==="
  exit 0
fi

# ── Main ──────────────────────────────────────────────────────────────────────
log "Issue loop starting (max ${MAX_ISSUES_PER_RUN} issues this run)."

# (a0) Manual stop flag (Telegram /stop) — do no work until /start clears it.
if [ -f "$STOP_FILE" ]; then
  log "Manual stop flag present ($STOP_FILE) — issue loop is stopped. Exiting. (Send Telegram /start to resume.)"
  exit 0
fi

# (a) Reset stuck issues once at the top of each run.
reset_stuck_issues

ISSUES_WORKED=0

while true; do
  # Safety cap — avoid runaway.
  if [ "$ISSUES_WORKED" -ge "$MAX_ISSUES_PER_RUN" ]; then
    log "Safety cap reached (${MAX_ISSUES_PER_RUN} issues worked this run). Stopping."
    tg "⚠️ <b>Dev team</b> — safety cap hit (${MAX_ISSUES_PER_RUN} issues). Restart the loop manually."
    break
  fi

  # (b0) Manual stop flag — if /stop arrived mid-run, halt before the next issue
  #      (the issue already in flight, if any, has finished by this point).
  if [ -f "$STOP_FILE" ]; then
    log "Manual stop flag detected ($STOP_FILE) — halting before next issue. Exiting."
    exit 0
  fi

  # (b) Pause check: if a marker exists, re-check whether it has resolved.
  #     TYPE=session-limit: exits if now < RESUME_AFTER; clears if elapsed.
  #     TYPE=mr: queries GitLab; exits if MR still open; clears if merged/closed.
  if pause_check; then
    # Still paused — exit without starting new work.
    exit 0
  fi

  # (c) Select next issue — two-tier.
  log "Selecting next issue (tier 1: todo) ..."
  RANKED_TODO=$(fetch_ranked_issues "$LBL_TODO")
  TODO_COUNT=$(echo "$RANKED_TODO" | jq 'length')

  if [ "$TODO_COUNT" -gt 0 ]; then
    # Work the top-ranked todo issue.
    ISSUE=$(echo "$RANKED_TODO" | jq -c '.[0]')
    log "Found $TODO_COUNT todo issue(s) — picking highest-priority."
  else
    log "No todo issues — checking roadmap (tier 2) ..."
    RANKED_ROADMAP=$(fetch_ranked_issues "$LBL_ROADMAP" "$LBL_TODO")
    ROADMAP_COUNT=$(echo "$RANKED_ROADMAP" | jq 'length')

    if [ "$ROADMAP_COUNT" -eq 0 ]; then
      # (d) Nothing in either tier.
      log "Nothing to do (no todo or roadmap issues pending)."
      break
    fi

    # Promote the top-ranked roadmap issue.
    ISSUE=$(echo "$RANKED_ROADMAP" | jq -c '.[0]')
    R_PID=$(  echo "$ISSUE" | jq -r '.project_id')
    R_IID=$(  echo "$ISSUE" | jq -r '.iid')
    R_URL=$(  echo "$ISSUE" | jq -r '.web_url')
    R_TITLE=$(echo "$ISSUE" | jq -r '.title')

    # Determine stage for milestone inference
    R_STAGE=99
    for lbl in $(echo "$ISSUE" | jq -r '.labels[]?' 2>/dev/null); do
      if echo "$lbl" | grep -qE '^P([0-6])'; then
        R_STAGE=$(echo "$lbl" | grep -oE '^P([0-6])' | tr -d 'P')
        break
      fi
    done
    # Also try title [P<n>]
    if [ "$R_STAGE" -eq 99 ]; then
      R_STAGE_T=$(echo "$R_TITLE" | grep -oE '\[P([0-6])\]' | grep -oE '[0-9]' || true)
      [ -n "$R_STAGE_T" ] && R_STAGE="$R_STAGE_T"
    fi

    log "Promoting roadmap issue #$R_IID: '$R_TITLE'"
    promote_roadmap_issue "$R_PID" "$R_IID" "$R_STAGE" "$R_URL"

    # Re-fetch the now-todo issue so ISSUE has the updated labels.
    ISSUE=$(gl_api "$GITLAB_API/projects/$R_PID/issues/$R_IID" 2>/dev/null \
      | jq -c '.' || echo "$ISSUE")
  fi

  # (e) Work the selected issue.
  PID=$(  echo "$ISSUE" | jq -r '.project_id')
  IID=$(  echo "$ISSUE" | jq -r '.iid')
  URL=$(  echo "$ISSUE" | jq -r '.web_url')
  TITLE=$(echo "$ISSUE" | jq -r '.title')
  BODY=$( echo "$ISSUE" | jq -r '.description // "No description provided."')

  LAST_MERGE_OUTCOME="no-mr"
  work_issue "$PID" "$IID" "$URL" "$TITLE" "$BODY" || true
  ISSUES_WORKED=$(( ISSUES_WORKED + 1 ))

  # Evaluate outcome: continue vs pause.
  case "$LAST_MERGE_OUTCOME" in
    merged|no-mr)
      log "Issue #$IID outcome: $LAST_MERGE_OUTCOME — continuing loop."
      ;;
    held|declined)
      # Need human attention — write pause marker and exit.
      # Recover the MR url from the last work_issue run (may be empty if pipeline failed).
      HELD_MR_URL=""
      HELD_MR_IID=""
      # Try to find the open MR for this branch.
      HELD_MR_URL=$(gl_api \
        "$GITLAB_API/projects/$PID/merge_requests?source_branch=fix/issue-${IID}&state=opened&per_page=1" \
        2>/dev/null | jq -r '.[0].web_url // empty' || true)
      HELD_MR_IID="${HELD_MR_URL##*/}"
      if [ -z "$HELD_MR_URL" ]; then
        HELD_MR_URL="(MR url unavailable — check project $PID issue #$IID)"
        HELD_MR_IID="?"
      fi
      log "Issue #$IID outcome: $LAST_MERGE_OUTCOME — pausing loop. MR: $HELD_MR_URL"
      pause_set "$HELD_MR_URL" "$PID" "$HELD_MR_IID"
      tg "⏸️ <b>Loop paused</b> — MR needs human attention (outcome: ${LAST_MERGE_OUTCOME}).
Issue: <b>#${IID}</b> ${TITLE}
🔗 MR: ${HELD_MR_URL}
Merge or close the MR (tap a button below, or reply <code>/merge</code>) to resume." \
        "$(mr_buttons_json "$PID" "$HELD_MR_IID" "$HELD_MR_URL")"
      exit 0
      ;;
    *)
      log "Issue #$IID outcome: $LAST_MERGE_OUTCOME — unknown; stopping loop as precaution."
      break
      ;;
  esac
done

state_clear
log "Loop complete ($ISSUES_WORKED issue(s) worked)."
[ "$ISSUES_WORKED" -gt 0 ] && tg "🏁 <b>Dev team</b> — loop complete (${ISSUES_WORKED} issue(s) worked)."
