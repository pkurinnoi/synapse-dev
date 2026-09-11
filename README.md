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
│   ├── webui.sh                    # start / detach / set-password for the web panel
│   └── .env.example                # config template (copy to .env)
├── webui/
│   ├── server.py                   # the control panel (Python stdlib only, no deps)
│   └── static/                     # the SPA: index.html, app.js, style.css
└── deploy/
    ├── agent-issues.service / .timer  # systemd unit for the autonomous loop
    ├── agent-webui.service            # systemd unit for the web panel
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

### Web control panel

```bash
./scripts/webui.sh set-password     # once — writes WEBUI_USER + a PBKDF2 hash to scripts/.env
./scripts/webui.sh                  # serves http://<host>:9000
```

Port 9000 taken? Choose your own — `--port` for one launch, the `port` subcommand to
make it stick:

```bash
./scripts/webui.sh --port 9001      # this launch only; scripts/.env is untouched
./scripts/webui.sh port 9001        # saves WEBUI_PORT=9001 to scripts/.env
./scripts/webui.sh port             # show the configured port and bind address
./scripts/webui.sh --background     # detach; --stop to shut it down again
```

`--host` works the same way. Both forms validate the port and tell you which process
holds it if it is already in use.

A dependency-free admin UI (Python stdlib + vanilla JS — no npm, no CDN, nothing to
install) for driving the whole harness from a browser. It sits behind the
login/password stored in `scripts/.env`; sessions are signed HTTP-only cookies, every
write is CSRF-checked, and failed logins are rate-limited per IP.

| Tab | What you can do |
|---|---|
| **Overview** | Live state of the loop, the Telegram bot, the tmux session and the merge gate; host tool check; per-repo branch / dirty-file / last-commit; one-click launch or stop of the team. |
| **Tokens & settings** | Edit every `scripts/.env` variable through a labelled form — GitLab group + PAT, Telegram token + chat id, `CTO_AUTO_ACCEPT_MR`, team root, and the panel's own knobs. Secrets are **write-only**: the browser only ever sees `••••••••abcd`. Comments and key order survive the edit and the file stays `chmod 600`. Includes live token tests and a password changer. |
| **Agent roles** | Browse, edit, create and delete `.claude/agents/*.md` — description, model, the `allowedTools` list (as removable chips) and the markdown instructions, or the raw file. Every save backs the previous version up to `.claude/agents/.backups/`. |
| **Telegram bot** | Start / stop / restart `tg-issue-bot.py`, verify the token against `getMe`, send a message to the team chat, and tail the bot log. |
| **Board & MRs** | The group board with label filters; promote `roadmap` → `todo` (or unqueue) in one click; create an issue in any sub-project; open MRs with pipeline status and **Accept** — which runs the same gated `cto-accept-mr.sh`, so it still refuses a draft, a conflict, a red pipeline, or a disabled `CTO_AUTO_ACCEPT_MR`. |
| **Issue loop** | Run a pass, kill a running pass, set or clear the stop flag, clear a pause marker, dry-run the ranked selection, and read the current issue state plus the loop log. |
| **Config files** | Edit `.claude/TEAM.md`, `.claude/reminders/cto.md` and `.claude/settings.json` (JSON is validated before it is written). |
| **Logs & monitors** | List and tail every log the team writes — live monitor transcripts, the archive, `/var/log/agent-team` — with an auto-follow mode. |

Tune it with the `WEBUI_*` keys in `scripts/.env` (`WEBUI_HOST`, `WEBUI_PORT`,
`WEBUI_SESSION_TTL`, `WEBUI_TLS`). The server refuses to start until a password is set.

> **The panel is root over the harness** — it rewrites tokens, agent prompts and
> permissions. Keep it on `127.0.0.1` behind an SSH tunnel or a TLS reverse proxy
> (`WEBUI_TLS=1`) rather than open to the internet. A ready-made nginx vhost is in
> [`deploy/nginx/agent-webui.conf`](./deploy/nginx/agent-webui.conf); see
> [`deploy/README.md`](./deploy/README.md#exposing-it-safely).

### Safety model
- The **CTO cannot author code or merge by hand.** Merges happen only through the gated
  `cto-accept-mr.sh`, which refuses unless `CTO_AUTO_ACCEPT_MR=1` **and** the MR is
  open, non-draft, conflict-free, mergeable, and its pipeline is green. Default **OFF**.
- `settings.json` permits pushes to **`feature/*` / `fix/*` only — never `main`**.
- **QA and Security are read-only** (no Write/Edit) — they report, devs fix.
- MRs touching sensitive paths (auth/session, credentials/secrets, payments,
  schema/migrations, access-control, dependencies) are **never auto-merged** — they
  require a manual Security PASS.
- The **web UI bypasses none of this** — it drives the same scripts, so the merge gate,
  the branch restrictions and the read-only roles hold exactly as they do in a terminal.

---

## Setup

**Requirements:** `claude` (Claude Code CLI), `tmux`, `jq`, `git`, `flock`, `curl`, and
`python3` (for the Telegram bot and the web panel — stdlib only, nothing to
pip-install) on `PATH`. A GitLab group with a project board, and a
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
6. **(Optional) web panel:** `./scripts/webui.sh set-password`, then `./scripts/webui.sh`
   — the UI comes up on port **9000** and can do steps 2–4 for you from the browser.

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
| `WEBUI_USER` / `WEBUI_PASSWORD_HASH` | `scripts/.env` | — | web panel login (required before it starts) |
| `WEBUI_HOST` / `WEBUI_PORT` | `scripts/.env` | `0.0.0.0` / `9000` | where the web panel listens |
| `WEBUI_SESSION_TTL` | `scripts/.env` | `43200` | web panel login lifetime, in seconds |
| `WEBUI_TLS` | `scripts/.env` | `0` | `1` behind HTTPS — marks the session cookie `Secure` |

---

## License

[MIT](./LICENSE).
