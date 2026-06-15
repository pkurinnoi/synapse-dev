# Publishing this as an open-source repo

A step-by-step guide to turn this directory into a public repository. Commands assume
you are inside the kit directory (`cd /srv/agent-team` or wherever you copied it — here
it is `synapse-dev/`).

---

## 0. Pre-flight — confirm it's safe to publish

Never publish until these all pass.

```bash
# No real secrets anywhere (only .env.example should mention tokens):
grep -rInIE 'glpat-[A-Za-z0-9_-]+|[0-9]{8,10}:[A-Za-z0-9_-]{30,}|-----BEGIN' . ; echo "exit=$?"

# The real secrets file is NOT here (only the template is):
ls scripts/.env 2>/dev/null && echo "!! REMOVE scripts/.env BEFORE PUBLISHING" || echo "ok: no scripts/.env"

# No build artifacts / logs:
find . -name '__pycache__' -o -name '*.pyc' -o -name '*.log' | grep -v .gitkeep || echo "ok: clean"

# .gitignore exists and ignores .env + settings.local.json:
cat .gitignore
```

A `grep` exit of `1` (no matches) on the first command is what you want.

---

## 1. Set a clean commit identity (important)

The machine's global git identity may leak an internal name. Set a **local** identity
for this repo so your commit history is clean:

```bash
git init -b main
git config user.name  "Your Name"
git config user.email "you@example.com"
```

(Use the same name/email as your GitHub/GitLab account so commits link to your profile.)

---

## 2. Make the first commit

```bash
git add -A
git status            # review — confirm scripts/.env is NOT listed
git commit -m "Initial commit: Claude Dev Team — autonomous multi-agent dev team kit"
```

---

## 3. Create the remote repository

### Option A — GitHub via the `gh` CLI (fastest)

```bash
gh auth login                      # one-time, interactive
gh repo create claude-dev-team \
  --public \
  --source . \
  --description "Autonomous multi-role Claude Code engineering team driven by a GitLab board, reported to Telegram." \
  --push
```

`--source . --push` adds the remote and pushes `main` in one step. Done — skip to §5.

### Option B — GitHub via the web UI

1. Go to <https://github.com/new>.
2. Name it (e.g. `claude-dev-team`), set **Public**, and **do not** add a README,
   .gitignore, or license (you already have them).
3. Create, then follow §4 with the URL it shows.

### Option C — GitLab

```bash
glab auth login
glab repo create claude-dev-team --public --description "..."
```
…or create it in the GitLab web UI, then §4.

---

## 4. Add the remote and push (Options B/C)

```bash
git remote add origin git@github.com:<you>/claude-dev-team.git   # or the https URL
git push -u origin main
```

---

## 5. Post-publish polish (recommended)

- **About / topics:** add a description and topics like `claude-code`, `ai-agents`,
  `gitlab`, `automation`, `telegram-bot`, `devops`.
- **Branch protection:** Settings → Branches → protect `main` (require PRs/MRs, no
  direct pushes) — it mirrors what this kit enforces for its own agents.
- **Community files (optional):** add `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, and
  issue/PR templates under `.github/` (GitHub) or `.gitlab/` (GitLab).
- **Releases:** tag a version when ready — `git tag v0.1.0 && git push --tags`.
- **CI (optional):** add a workflow that runs `bash -n scripts/*.sh`,
  `python3 -m py_compile scripts/tg-issue-bot.py`, and a `jq` lint of the JSON files.

---

## 6. Keep secrets out, permanently

- `scripts/.env` and `.claude/settings.local.json` are already in `.gitignore`. Keep
  real tokens **only** in your local `scripts/.env` (mode 600).
- If a secret is ever committed by accident, rotate it immediately (GitLab PAT,
  Telegram bot token) — rewriting history is not enough once it's pushed.
- Consider enabling **secret scanning / push protection** on the host (GitHub: Settings
  → Code security).
