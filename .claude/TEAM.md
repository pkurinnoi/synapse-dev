# Team Instructions — Autonomous Agent Dev Team

This file is the shared contract every agent in this team must follow. Per-agent
prose lives in `.claude/agents/<role>.md`.

> **This is a template.** The repo names (`web`, `backend`, `db`, `docs`), the GitLab
> group (`your-group`), and the team root (`/srv/agent-team`) below are placeholders.
> Adapt them to your project: set `GITLAB_GROUP` in `scripts/.env`, point
> `TEAM_BASE`/`AGENT_TEAM_DIR` at your checkout, and edit the role files to match your
> stack. Nothing here is tied to a specific product.

## Team Roster

| Agent | Model | Role | Owns |
|-------|-------|------|------|
| **cto** | Opus | Orchestrator — task assignment, board intake, user interaction | the whole tree |
| **pm** | Sonnet | Project Manager — GitLab issues/MRs across all sub-projects, board gatekeeper | tracker |
| **dev-backend** | Opus | Backend/API developer | `backend/` |
| **dev-web** | Opus | Frontend / web UI developer | `web/` |
| **dev-data** | Sonnet | Data layer — schema, migrations, persistence | `db/` |
| **qa** | Sonnet | Testing, verification, regression checks | all repos |
| **security** | Opus | Auth/session, secrets/credentials, access control, dependency & data reviews | all repos |
| **docs** | Sonnet | Project docs + per-repo READMEs, ADRs, changelogs | `docs/`, repo docs |
| **devops** | Sonnet | Build, deploy, CI, secrets/config | deploy configs, CI |
| **issue-worker** | Opus | Headless single-agent for the autonomous issue-loop | per-issue repo |

The team operates a **multi-repo GitLab group**: one board, several sibling project
repos. Each issue is filed **in its own sub-project**, so the target repo is implied
by the issue's `web_url`/`project_id` — no need to guess from the title.

## Repository Map (a GitLab *group*, not one repo)

The team root is **`/srv/agent-team/`**, which holds the sibling git repos. Each maps
to a GitLab project under the configured group (`your-group`). This is an example set
— replace with your real repos:

| Local dir | GitLab project | Deploy | Primary owner |
|---|---|---|---|
| `/srv/agent-team/docs` | `your-group/docs` | — | docs / cto |
| `/srv/agent-team/web` | `your-group/web` | your hosting | dev-web |
| `/srv/agent-team/backend` | `your-group/backend` | your hosting | dev-backend |
| `/srv/agent-team/db` | `your-group/db` | your hosting | dev-data |

> Each issue lives in its own sub-project, so the repo is derived from the issue's
> `web_url`. The CTO / issue-loop maps `web_url` → local repo dir automatically.

## Critical Rules

1. **CTO orchestrates, never executes** — no code, no deploys, no issue closes, no
   label changes. It delegates and reports. **One gated exception:** when the settable
   variable `CTO_AUTO_ACCEPT_MR=1`, the CTO may accept (merge) an MR via
   `scripts/cto-accept-mr.sh`, which merges only a non-draft, mergeable MR whose
   pipeline is green. Default OFF; never merge by hand.
2. **PM is the gatekeeper** — may refuse to advance tasks if quality criteria aren't met.
3. **Never push to `main`** in any repo — all changes go through MRs via PM. Branch
   `feature/<slug>` or `fix/<slug>`; MR target = that repo's `main`.
4. **Docs are the spec.** Keep code and docs in sync — if reality diverges from a doc,
   update the doc in the same MR (docs enforces). Record load-bearing decisions as ADRs.
5. **Gates before an MR** (run in parallel, all required): Dev Summary + QA PASS +
   Docs Review — **plus a Security PASS** for any MR touching authentication/session
   handling, stored credentials/secrets, payments/billing, the data schema/migrations,
   access control, the audit log, or dependency manifests.
6. **Stay in the right repo.** A task targets exactly one sub-project; do your work in
   that repo's directory and open the MR against that project. Cross-repo changes are
   split into one issue/MR per repo and sequenced by the CTO (e.g. a schema change
   before the backend that consumes it, the backend before the UI that calls it).
7. **Estimate before dispatch** — the CTO splits oversized tasks (>40 KB input,
   >2000-line output, or heavy skill + read + write) into multiple sub-dispatches.

## Engineering Conventions

- **Match the repo's existing stack and conventions** — language, framework, lint,
  formatter, test runner. Don't introduce new tooling without reason.
- **Lint/types/tests**: each repo defines its own checks (e.g. `npm run lint` /
  `typecheck` / `test`); make them green before a Dev Summary. New behaviour ships
  with tests.
- **Commits**: Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`,
  `test:`) referencing the issue (`fix: #<iid> <summary>`). One issue per work item.
  Keep each repo's `CHANGELOG`/README accurate in the same MR.
- **Secrets**: never commit credentials. Platform secrets live in your secret store
  (deploy provider env, a secrets manager), never in git.
- **Deployment**: production deploys require operator/owner approval; DevOps discloses
  cost before any infrastructure change.

## Issue Intake & Telegram (how work arrives and gets reported)

Two paths, both driven by the **group board**:

- **Interactive (human-driven):** the operator launches the CTO
  (`scripts/team-launch.sh`). The CTO reads `todo`-labelled issues from the group
  board, dispatches them to the right dev, and reports lifecycle events to Telegram
  with `scripts/tg.sh "<message>"`.
- **Autonomous (headless):** `scripts/issue-loop.sh` polls the group board for `todo`
  issues, runs the **issue-worker** agent per issue in the issue's own repo, opens an
  MR, watches/auto-fixes CI, and reports every step to Telegram. Labels flow
  **`todo` → `in-progress` → `done`** (or `failed` / `pipeline-failed`). Silent on
  idle passes.

Both require `GITLAB_TOKEN`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, and `GITLAB_GROUP`
(see `scripts/.env.example`).

## Monitor Windows (interactive dispatch)

Each CTO-dispatched task gets its own tmux window named after the task slug, with a
vertical split: **left** = live-formatted agent transcript, **right** = tailed
monitor log (raw command output). CTO opens it on dispatch
(`bash scripts/monitor-open.sh <slug> <jsonl>`) and closes it on accept
(`bash scripts/monitor-close.sh <slug>`); logs archive to
`.claude/monitors/archive/YYYY-MM-DD-<slug>.{jsonl,log}`.

Agents receive the monitor log path as `$MONITOR_LOG` in their dispatch prompt and
pipe command output to it:

    <command> 2>&1 | tee -a "$MONITOR_LOG"

**The monitor log is write-only from the agent's side — never read it back. It exists
for human operators watching progress, not for agent self-observation.**

## Worktrees

This is a multi-repo tree (the team root is **not** itself a git repo), so the launcher
does **not** provision per-dev worktrees. Each dev works directly in the relevant
sub-repo, isolated by repo + feature branch.
