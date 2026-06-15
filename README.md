# Claude Dev Team

An autonomous, multi-role **Claude Code engineering team** for a multi-repo GitLab
group. Work arrives as labelled issues on the group board, gets implemented by
role-specialized agents through a quality-gated pipeline, and every step is reported to
**Telegram**. Runs **interactively** (a live CTO you talk to) or **fully autonomously**
(a headless loop on cron/systemd).

> This is a generic, reusable template. The repo names (`web`, `backend`, `db`,
> `docs`), the GitLab group (`your-group`), and the team root (`/srv/agent-team`) are
> placeholders — adapt them to your project. No product-specific code or secrets are
> included.

---

## What's in the box

```
.
├── .claude/
│   ├── TEAM.md                     # shared contract: repo map, rules, gates, monitor protocol
│   ├── agents/*.md                 # 10 role definitions (see below)
│   ├── reminders/cto.md            # injected into the CTO at every session start
│   ├── hooks/session-start-cto.sh  # SessionStart hook (reminder + live merge-power state)
│   ├── monitors/archive/           # closed per-task monitor logs land here
│   ├── settings.json               # permissions (push to feature/*|fix/* only) + hook
│   └── settings.local.json.example # machine-local permission overrides
├── scripts/
│   ├── team-launch.sh / team-stop.sh   # tmux session with a live CTO window
│   ├── monitor-open.sh / monitor-close.sh  # per-task 2-pane monitor windows
│   ├── format-transcript.jq        # pretty-prints the agent JSONL transcript
│   ├── cto-accept-mr.sh            # gated, green-pipeline-only MR merge
│   ├── tg.sh                       # send a Telegram message (with optional MR buttons)
│   ├── issue-loop.sh               # headless autonomous board → MR → CI loop
│   ├── tg-issue-bot.py             # Telegram bot: create issues + control the loop
│   └── .env.example                # config template (copy to .env)
└── deploy/
    ├── agent-issues.service / .timer  # systemd unit for the autonomous loop
    └── crontab.example                # cron alternative
```

### The roles (`.claude/agents/`)

| Agent | Model | Responsibility |
|-------|-------|----------------|
| **cto** | Opus | Orchestrator — board intake, dispatch, gates, user/Telegram interaction. Never writes code. |
| **pm** | Sonnet | Backlog + MRs + the quality gate. Can refuse to advance work. |
| **dev-backend** | Opus | Server / API repo. |
| **dev-web** | Opus | Frontend / web UI repo. |
| **dev-data** | Sonnet | Schema, migrations, persistence repo. |
| **qa** | Sonnet | Read-only: runs tests, reports PASS/FAIL/BLOCKED. |
| **security** | Opus | Read-only: auth, secrets, access-control, dependency reviews. |
| **docs** | Sonnet | Project docs, READMEs, ADRs, changelogs. |
| **devops** | Sonnet | Build, deploy, CI, secrets/config. |
| **issue-worker** | Opus | Headless generalist that does one whole issue alone (autonomous mode). |

---

## How it works

### The board is a state machine
Labels drive everything: **`todo` → `in-progress` → `done`** (or `failed` /
`pipeline-failed`). An optional `roadmap` tier holds planned-but-not-ready work that the
CTO promotes to `todo` one at a time. Add the **`todo`** label to an issue in any
sub-project to send it to the agents.

### Two ways to run

**Interactive — a CTO you talk to:**
```bash
./scripts/team-launch.sh        # tmux session with a live CTO window (+ Telegram bot)
```
The CTO ranks `todo` issues, dispatches the owning dev into a **per-task monitor
window** (left pane = live transcript, right pane = raw command output), runs the PM
quality gates, and reports lifecycle events to Telegram. Stop with
`./scripts/team-stop.sh`.

**Autonomous — headless loop:**
```bash
./scripts/issue-loop.sh                       # one pass over the board
./scripts/issue-loop.sh repair-mr <pid> <iid> # rescue one MR's red pipeline
```
Pulls `todo` issues group-wide, runs the **issue-worker** per issue in its repo, opens
the MR, watches and auto-fixes CI (up to 3×), relabels `done`/`failed`, and reports to
Telegram. Silent on idle passes. Put it on a timer (`deploy/`) for continuous
operation — the `flock` guard makes overlapping ticks safe.

### Telegram integration
- **Reporting:** every lifecycle event (issue start 🚀, gate hand-offs, blockers ⚠️,
  done/merge ✅) is pushed to a chat via `scripts/tg.sh`.
- **Control:** `scripts/tg-issue-bot.py` is a long-poll bot (no public URL needed) that
  lets you **create issues conversationally** with `/issue`, check `/status`, and
  `/start` · `/stop` the autonomous loop. Held MRs come with inline **Merge / Close**
  buttons.

### Safety model
- The **CTO cannot author code or merge by hand.** Merges happen only through the gated
  `cto-accept-mr.sh`, which refuses unless `CTO_AUTO_ACCEPT_MR=1` **and** the MR is
  open, non-draft, conflict-free, mergeable, and its pipeline is green. Default **OFF**.
- `settings.json` permits pushes to **`feature/*` / `fix/*` only — never `main`**.
- **QA and Security are read-only** (no Write/Edit) — they report, devs fix.
- MRs touching sensitive paths (auth/session, credentials/secrets, payments,
  schema/migrations, access-control, dependencies) are **never auto-merged** — they
  require a manual Security PASS.

---

## Setup

**Requirements:** `claude` (Claude Code CLI), `tmux`, `jq`, `git`, `flock`, `curl`, and
`python3` (for the Telegram bot) on `PATH`. A GitLab group with a project board, and a
Telegram bot.

1. **Clone / copy** this kit to your team root (e.g. `/srv/agent-team`). Place your
   project repos as siblings inside it.
2. **Configure:**
   ```bash
   cp scripts/.env.example scripts/.env && chmod 600 scripts/.env
   ```
   Fill in:
   - `GITLAB_GROUP` — your GitLab group path (e.g. `your-group`).
   - `GITLAB_TOKEN` — a PAT with `api` scope on the group.
   - `TELEGRAM_TOKEN` — from [@BotFather](https://t.me/BotFather); add the bot to your chat.
   - `TELEGRAM_CHAT_ID` — the numeric chat id (negative for group chats).
   - `CTO_AUTO_ACCEPT_MR` — leave `0` until you trust the pipeline.
3. **Adapt the team to your project** (all generic placeholders):
   - `.claude/TEAM.md` — repo map, conventions.
   - `.claude/agents/*.md` — each role's responsibilities and `allowedTools`.
   - `.claude/reminders/cto.md` — the project description and milestone scheme.
   - `scripts/issue-loop.sh` — the repo-rank mapping (`db`/`backend`/`web`/`docs`) and
     `TEAM_BASE` if you don't use `/srv/agent-team`.
4. **(Optional) local permissions:** `cp .claude/settings.local.json.example
   .claude/settings.local.json` and adjust paths.
5. **Run** interactively (`./scripts/team-launch.sh`) or install the timer (see
   `deploy/README.md`).

---

## Configuration knobs

| Variable | Where | Default | Purpose |
|---|---|---|---|
| `GITLAB_GROUP` | `scripts/.env` | `your-group` | GitLab group the board lives in |
| `GITLAB_TOKEN` | `scripts/.env` | — | GitLab API token (`api` scope) |
| `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` | `scripts/.env` | — | Telegram reporting + bot |
| `CTO_AUTO_ACCEPT_MR` | `scripts/.env` | `0` | Gated CTO merge power |
| `TEAM_BASE` | `issue-loop.sh` | `/srv/agent-team` | Team root holding the repos |
| `AGENT_TEAM_SESSION` | env | `agent-team` | tmux session name |
| `AGENT_TEAM_LOG_DIR` | env | `/tmp/agent-team` | per-task monitor logs |

---

## License

[MIT](./LICENSE).
