# `.claude/` — Agent Team Configuration

This directory configures the multi-agent dev team. See the top-level `README.md` for
the full overview and setup.

## Layout
- `TEAM.md` — the shared contract (repo map, critical rules, gates, monitor protocol).
- `agents/*.md` — the 10 role definitions (cto, pm, dev-backend, dev-web, dev-data, qa,
  security, docs, devops, issue-worker). Each has frontmatter (`name`, `model`,
  `allowedTools`) + prose.
- `reminders/cto.md` — re-injected into the CTO at every session start (board intake,
  ranking, gates).
- `hooks/session-start-cto.sh` — the SessionStart hook that injects the reminder +
  live `CTO_AUTO_ACCEPT_MR` state.
- `monitors/archive/` — where closed per-task monitor logs are archived.
- `settings.json` — permissions (push only to `feature/*` / `fix/*`, never `main`) and
  the SessionStart hook registration.
- `settings.local.json.example` — machine-local permission overrides; copy to
  `settings.local.json` and adapt paths.

## Customizing for your project
1. Edit `TEAM.md`: repo names, GitLab group, team root, stack conventions.
2. Edit each `agents/*.md` to match your repos' responsibilities and tooling.
3. Edit `reminders/cto.md`: the project description and milestone scheme.
4. Set `GITLAB_GROUP`, `GITLAB_TOKEN`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` in
   `scripts/.env` (copy from `scripts/.env.example`).
