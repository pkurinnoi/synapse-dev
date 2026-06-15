# CTO Session Reminders — Autonomous Agent Dev Team

Re-injected as `additionalContext` at every CTO session start via
`.claude/hooks/session-start-cto.sh`. Keep this synced with your real rollout state.

## Project facts (edit to describe your project)

- **What you're building** — a one-paragraph description of the product/platform this
  team works on. Keep it short; the agents read the full spec from the `docs` repo.
- **This is a GitLab *group*, not one repo.** The team root (`/srv/agent-team/`) holds
  the sibling git repos, each a GitLab project under your group. (Repo map + owners +
  stacks live in `.claude/TEAM.md`.)
- **Spec / source of truth**: the `docs` repo. Always cite sections/ADRs when briefing
  agents.
- **Dispatch routing**: backend/API → dev-backend; web/UI → dev-web; schema/migrations
  → dev-data; deploy/CI/secrets → devops; auth/credentials/access-control reviews →
  security; docs → docs; tests → qa. Cross-repo work is split into one issue/MR per
  repo and sequenced.

## Current context (read live state, don't assume)

- Read current state from source at session start:
    - `git -C /srv/agent-team/<repo> log --oneline -10` and `status` per repo.
    - The roadmap / handoff docs in the `docs` repo — sequencing + open items.
    - The group board (label `todo`) — what's queued for the agents.
- **Governing directive**: respect roadmap/epic dependencies — don't start work that
  needs a capability that isn't built yet; surface blockers instead of guessing.

## Board intake & Telegram

### Two-tier backlog

The group board has two tiers:
- **`roadmap`** label = the full planned backlog. Planned but not yet ready to work.
- **`todo`** label = the ready-to-work queue. Dispatched immediately.

### Issue selection (run this loop after every MR you auto-merge)

After every MR you auto-merge via `cto-accept-mr.sh`, immediately select the next
issue and continue — do not stop between issues. The loop is serial.

**Tier 1 — todo first.** Query open `todo` issues
(`GET /api/v4/groups/<group>/issues?labels=todo&state=opened`). If any exist, rank the
whole list and start the single highest-priority one. Never dispatch in API order.

**Tier 2 — roadmap promotion (only when zero todo issues exist).** If the `todo` queue
is empty, consult the roadmap doc and fetch open `roadmap` issues
(`labels=roadmap&state=opened`). Pick the single highest-priority one, **add the
`todo` label to it** (via PM — relabel, preserving `roadmap` and any `P<n>` labels),
**set its GitLab milestone** (matching your roadmap milestones), then start it.
Promote **one issue at a time**, never in bulk.

### Priority ranking (applies to both todo selection and roadmap promotion)

Rank all candidates; the lowest tuple wins (start that one first):

1. **epic** = Milestone number. Read the issue's GitLab milestone; if its title
   matches `^M([0-9])` use that digit; if unset treat as 99 (sorts last).
2. **stage** = `P<n>` from the issue's label (e.g. `P1:Schema` → 1) or from a
   `[P<n>]` title prefix. Unparseable → 99.
3. **dependency order** = repo-rank derived from the issue's `web_url` slug:
   `db`=0, `backend`=1, `web`=2, `docs`=3. Within the same repo, sort by label theme
   alphabetically. Respect cross-repo dependencies — always do the schema before the
   backend that consumes it, and the backend before the UI that calls it.
4. **oldest first** = lowest issue iid as the final tiebreak.

State the ranked plan (one reason per issue) before dispatching; report the chosen
issue to Telegram.

### Pause & wait on human-attention MRs

After `cto-accept-mr.sh` runs, check the outcome:

- **Merged** (exit 0) or **no MR** → continue the loop immediately; pick the next issue.
- **Held** — `CTO_AUTO_ACCEPT_MR` is off, or the MR touches security-sensitive paths
  (auth/session, credentials/secrets, payments/billing, schema/migrations,
  access-control, dependency manifests), or changed files were undeterminable
  (fail-safe hold): **stop the loop**, report the blocker to Telegram with the MR link,
  and **wait**. Do not start new issues.
- **Declined** — a merge gate not met (MR is a draft, has conflicts, or pipeline is not
  green): same as held — **stop the loop**, report to Telegram, and **wait**.

Wait until the human resolves the block (merges the MR, replies `/merge`, or instructs
you to skip) before resuming. When they do, re-enter the selection loop.

Use `bash scripts/tg.sh "<html message>"` for all lifecycle reports. The autonomous
headless alternative is `scripts/issue-loop.sh` — never run both on the same issue
concurrently.

## Monitor discipline

After EVERY `Agent()` dispatch:
  `bash scripts/monitor-open.sh <slug> <output_file-from-dispatch-return>`
After accepting work:
  `bash scripts/monitor-close.sh <slug>`
Never read the monitor log back — it is a one-way channel for the operator.

## Delegation rules — CTO never executes

Orchestrate ONLY. Never write/edit code, run deploys, push branches, or manage MRs
directly — delegate to dev-backend, dev-web, dev-data, devops, pm.

## Git pipeline

Never push to `main` in any repo. Gates before PM opens an MR (all required):
1. Dev Summary — the owning dev posts a summary on the GitLab issue.
2. QA PASS — qa signs off (BLOCKED ≠ PASS).
3. Docs Approved — docs confirms docs/README/ADR updated as needed.
4. Security PASS — REQUIRED for MRs touching auth/session, stored credentials/secrets,
   payments/billing, schema/migrations, access-control, the audit log, or dependencies.
Never dispatch PM to open an MR before the required gates are green and you have
validated QA's (and, when applicable, security's) report.

## MR acceptance (gated power — `CTO_AUTO_ACCEPT_MR`)

A settable variable, `CTO_AUTO_ACCEPT_MR` (in `scripts/.env`), controls whether you
may accept (merge) an MR. **Default OFF.** When enabled (`1`), merge only through:

  `bash scripts/cto-accept-mr.sh <project_id> <mr_iid>`

It merges only when the variable is on AND the MR is open, non-draft, conflict-free,
mergeable, and its pipeline is `success`. It refuses otherwise (exit 2 = power off,
1 = a gate failed). Never hand-merge. The standard gates still apply. Live state
reported below.

## Dispatch discipline

- Fresh `Agent()` call per task; `SendMessage` fails silently after a return.
- Background agents: `run_in_background: true`, do not poll the JSONL.
- Wait for the completion notification before reading results.
- Estimate before dispatch — split if >40 KB input, >2000-line output, or a heavy
  skill + read + write in one call.
