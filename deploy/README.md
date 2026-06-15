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
