# Running the autonomous loop continuously

The headless `scripts/issue-loop.sh` is designed to run on a timer. Pick **one** of the
following — don't run cron and the systemd timer at the same time.

## Option A — systemd timer (recommended)

```bash
sudo mkdir -p /var/log/agent-team
sudo cp deploy/agent-issues.service /etc/systemd/system/
sudo cp deploy/agent-issues.timer   /etc/systemd/system/
# Edit the unit to match your paths (WorkingDirectory, EnvironmentFile, PATH).
sudo systemctl daemon-reload
sudo systemctl enable --now agent-issues.timer
systemctl list-timers agent-issues.timer
journalctl -u agent-issues.service -f
```

## Option B — cron

```bash
mkdir -p /var/log/agent-team
crontab -e   # paste a line from deploy/crontab.example
```

Both rely on the `flock` guard inside `issue-loop.sh`, so overlapping ticks are safe:
a long-running issue holds the lock and later ticks no-op until it finishes.

---

# Running the web control panel

`webui/server.py` (see [the Web UI section of the README](../README.md#web-control-panel))
is a stdlib-only HTTP server — no virtualenv, no packages to install.

```bash
sudo mkdir -p /var/log/agent-team
./scripts/webui.sh set-password            # writes WEBUI_USER + a PBKDF2 hash to scripts/.env
sudo cp deploy/agent-webui.service /etc/systemd/system/
# Edit User/WorkingDirectory/ExecStart in the unit to match your install.
sudo systemctl daemon-reload
sudo systemctl enable --now agent-webui
journalctl -u agent-webui -f
```

Run it as the **same user as the agent team** — it edits `scripts/.env`, the agent
role files, and the loop's control files in `/var/run/`.

## Exposing it safely

The panel holds the keys to the whole harness: it can rewrite tokens, edit agent
prompts, and start processes. Choose one:

- **SSH tunnel (safest).** Set `WEBUI_HOST=127.0.0.1`, then from your laptop:
  `ssh -N -L 9000:127.0.0.1:9000 you@your-host` and open <http://localhost:9000>.
- **TLS reverse proxy.** Keep `WEBUI_HOST=127.0.0.1`, terminate HTTPS in
  nginx/Caddy, and set `WEBUI_TLS=1` so the session cookie is marked `Secure`.
  The panel also honours `X-Forwarded-Proto: https`.

  ```nginx
  location / {
      proxy_pass http://127.0.0.1:9000;
      proxy_set_header Host $host;
      proxy_set_header X-Forwarded-Proto $scheme;
  }
  ```

- **Plain `0.0.0.0:9000`** is the default and works out of the box, but the login
  then travels in cleartext. Only do that on a trusted/private network.
