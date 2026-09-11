#!/usr/bin/env python3
"""
webui/server.py — Claude Dev Team control panel.

A dependency-free (Python stdlib only) web UI to manage the whole agent harness:
tokens/secrets in scripts/.env, the Telegram bot, agent role definitions, the
autonomous issue loop, the tmux team session, the GitLab board, and the logs.

Everything is behind a login/password taken from scripts/.env:

    WEBUI_USER=admin
    WEBUI_PASSWORD=...            # or, preferred:
    WEBUI_PASSWORD_HASH=pbkdf2_sha256$...

Generate a hash with:

    python3 webui/server.py hash-password

Run:

    ./scripts/webui.sh            # or: python3 webui/server.py

Listens on WEBUI_HOST:WEBUI_PORT (default 0.0.0.0:9000). The UI can edit agent
files and start processes — put it behind TLS (reverse proxy) or an SSH tunnel
before exposing it to a network you do not control.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import pathlib
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────
WEBUI_DIR = pathlib.Path(__file__).resolve().parent
ROOT = WEBUI_DIR.parent
STATIC_DIR = WEBUI_DIR / "static"
SCRIPTS_DIR = ROOT / "scripts"
ENV_FILE = SCRIPTS_DIR / ".env"
ENV_EXAMPLE = SCRIPTS_DIR / ".env.example"
CLAUDE_DIR = ROOT / ".claude"
AGENTS_DIR = CLAUDE_DIR / "agents"
AGENT_BACKUP_DIR = AGENTS_DIR / ".backups"
ARCHIVE_DIR = CLAUDE_DIR / "monitors" / "archive"

RUN_DIR = pathlib.Path(os.environ.get("AGENT_TEAM_LOG_DIR", "/tmp/agent-team"))
WEBUI_LOG_DIR = pathlib.Path(os.environ.get("WEBUI_LOG_DIR", str(RUN_DIR)))

# issue-loop.sh control files (override if you changed them in the script)
LOOP_STOP_FILE = pathlib.Path(
    os.environ.get("AGENT_LOOP_STOP_FILE", "/var/run/agent-issue-loop.stopped"))
LOOP_STATE_FILE = pathlib.Path(
    os.environ.get("AGENT_LOOP_STATE_FILE", "/var/run/agent-issue-loop.state"))
LOOP_PAUSE_FILE = pathlib.Path(
    os.environ.get("AGENT_LOOP_PAUSE_FILE", "/var/run/agent-issue-loop.paused"))
LOOP_LOG_DIR = pathlib.Path(os.environ.get("AGENT_LOOP_LOG_DIR", "/var/log/agent-team"))

GITLAB_API = os.environ.get("GITLAB_API", "https://gitlab.com/api/v4")

# Files the "Config" tab is allowed to read/write, relative to ROOT.
EDITABLE_FILES = [
    ".claude/TEAM.md",
    ".claude/reminders/cto.md",
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".claude/hooks/session-start-cto.sh",
]

# Directories the "Logs" tab may read from.
LOG_ROOTS = [RUN_DIR, WEBUI_LOG_DIR, LOOP_LOG_DIR, ARCHIVE_DIR]

SECRET_HINT = re.compile(r"(TOKEN|PASSWORD|SECRET|_KEY|APIKEY|API_KEY|PAT)$|^WEBUI_SECRET$")

# Known .env keys — drives the nicely-labelled Settings form.
KNOWN_ENV: list[dict] = [
    {"key": "GITLAB_GROUP", "group": "GitLab", "label": "Group path",
     "help": "GitLab group the board lives in, e.g. your-group."},
    {"key": "GITLAB_TOKEN", "group": "GitLab", "label": "API token",
     "help": "Personal access token with the api scope on the group.", "secret": True},
    {"key": "TELEGRAM_TOKEN", "group": "Telegram", "label": "Bot token",
     "help": "From @BotFather. The bot must be a member of the chat.", "secret": True},
    {"key": "TELEGRAM_CHAT_ID", "group": "Telegram", "label": "Chat id",
     "help": "Numeric chat id — negative for group chats."},
    {"key": "CTO_AUTO_ACCEPT_MR", "group": "Pipeline", "label": "CTO merge power",
     "help": "1 lets the CTO merge green, non-sensitive MRs. Default 0 (off).",
     "kind": "bool"},
    {"key": "TEAM_BASE", "group": "Pipeline", "label": "Team root",
     "help": "Directory holding the sibling project repos (default /srv/agent-team)."},
    {"key": "WEBUI_USER", "group": "Web UI", "label": "Username",
     "help": "Login for this control panel."},
    {"key": "WEBUI_PASSWORD_HASH", "group": "Web UI", "label": "Password hash",
     "help": "pbkdf2_sha256$... — set it from the Password card below.", "secret": True},
    {"key": "WEBUI_PASSWORD", "group": "Web UI", "label": "Password (plaintext)",
     "help": "Fallback if no hash is set. Prefer the hash.", "secret": True},
    {"key": "WEBUI_HOST", "group": "Web UI", "label": "Bind host",
     "help": "Default 0.0.0.0. Restart the UI after changing."},
    {"key": "WEBUI_PORT", "group": "Web UI", "label": "Port",
     "help": "Default 9000. Restart the UI after changing."},
    {"key": "WEBUI_SESSION_TTL", "group": "Web UI", "label": "Session TTL (s)",
     "help": "How long a login stays valid. Default 43200 (12h)."},
    {"key": "WEBUI_SECRET", "group": "Web UI", "label": "Cookie signing secret",
     "help": "Auto-generated on first run. Rotating it logs everyone out.",
     "secret": True},
]
KNOWN_KEYS = {k["key"] for k in KNOWN_ENV}


# ─────────────────────────────────────────────────────────────────────────────
# .env handling — read/write while preserving comments and order
# ─────────────────────────────────────────────────────────────────────────────
_ENV_LOCK = threading.Lock()
_KV_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _unquote(raw: str) -> str:
    """Read one shell-ish value: unquote it, and drop any trailing ` # comment`."""
    raw = raw.strip()
    if raw[:1] not in ("'", '"'):
        return re.split(r"(?:^|\s+)#", raw, maxsplit=1)[0].strip()
    quote, out, i, n = raw[0], [], 1, len(raw)
    while i < n:
        ch = raw[i]
        if ch == "\\" and quote == '"' and i + 1 < n:
            out.append(raw[i + 1])
            i += 2
            continue
        if ch == quote:
            if quote == "'" and raw[i + 1:i + 4] == "\\''":   # bash '\'' concatenation
                out.append("'")
                i += 4
                continue
            break
        out.append(ch)
        i += 1
    return "".join(out)


def _quote(value: str) -> str:
    if value == "" or re.fullmatch(r"[A-Za-z0-9_./:@+,=-]*", value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def env_read() -> dict[str, str]:
    out: dict[str, str] = {}
    if not ENV_FILE.exists():
        return out
    for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _KV_RE.match(line)
        if m:
            out[m.group(1)] = _unquote(m.group(2))
    return out


def env_write(updates: dict[str, str], deletes: list[str] | None = None) -> None:
    """Apply updates/deletes to scripts/.env, keeping comments and key order."""
    deletes = deletes or []
    with _ENV_LOCK:
        lines: list[str] = []
        if ENV_FILE.exists():
            lines = ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        elif ENV_EXAMPLE.exists():
            lines = ENV_EXAMPLE.read_text(encoding="utf-8", errors="replace").splitlines()

        seen: set[str] = set()
        out: list[str] = []
        for line in lines:
            m = _KV_RE.match(line) if line.strip() and not line.lstrip().startswith("#") else None
            if not m:
                out.append(line)
                continue
            key = m.group(1)
            if key in deletes:
                continue
            if key in updates:
                trailing = ""
                tail = re.split(r"\s+#", m.group(2), maxsplit=1)
                if len(tail) == 2 and m.group(2).lstrip()[:1] not in ("'", '"'):
                    trailing = "   # " + tail[1].strip()
                out.append(f"{key}={_quote(updates[key])}{trailing}")
                seen.add(key)
                continue
            out.append(line)

        new_keys = [k for k in updates if k not in seen and k not in deletes]
        if new_keys:
            if out and out[-1].strip():
                out.append("")
            out.append("# Added by the web UI")
            for k in new_keys:
                out.append(f"{k}={_quote(updates[k])}")

        ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = ENV_FILE.with_suffix(".env.tmp")
        tmp.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(ENV_FILE)


def cfg(key: str, default: str = "") -> str:
    """Config lookup: process environment first, then scripts/.env."""
    val = os.environ.get(key)
    if val:
        return val
    return env_read().get(key, default)


def is_secret(key: str) -> bool:
    for spec in KNOWN_ENV:
        if spec["key"] == key:
            return bool(spec.get("secret"))
    return bool(SECRET_HINT.search(key))


def mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 6:
        return "•" * len(value)
    return "•" * 8 + value[-4:]


# ─────────────────────────────────────────────────────────────────────────────
# Password hashing + sessions
# ─────────────────────────────────────────────────────────────────────────────
PBKDF2_ROUNDS = 210_000


def hash_password(password: str, *, salt: bytes | None = None,
                  rounds: int = PBKDF2_ROUNDS) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, rounds)
    return "pbkdf2_sha256${}${}${}".format(
        rounds, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, rounds, salt_b64, hash_b64 = encoded.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 base64.b64decode(salt_b64), int(rounds))
        return hmac.compare_digest(dk, base64.b64decode(hash_b64))
    except Exception:
        return False


def signing_secret() -> bytes:
    """Cookie signing key; generated and persisted into scripts/.env on first run."""
    val = cfg("WEBUI_SECRET")
    if not val:
        val = secrets.token_urlsafe(48)
        try:
            env_write({"WEBUI_SECRET": val})
        except OSError:
            pass  # read-only .env — sessions then die on restart, which is safe
    os.environ["WEBUI_SECRET"] = val  # cache: cfg() checks the environment first
    return val.encode()


def _sign(payload: bytes) -> str:
    sig = hmac.new(signing_secret(), payload, hashlib.sha256).digest()
    return (base64.urlsafe_b64encode(payload).decode().rstrip("=") + "." +
            base64.urlsafe_b64encode(sig).decode().rstrip("="))


def _b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def make_session(user: str) -> str:
    ttl = int(cfg("WEBUI_SESSION_TTL", "43200") or 43200)
    payload = json.dumps({"u": user, "exp": int(time.time()) + ttl,
                          "n": secrets.token_urlsafe(8)}, separators=(",", ":")).encode()
    return _sign(payload)


def read_session(token: str) -> dict | None:
    try:
        body, sig = token.split(".", 1)
        payload = _b64d(body)
        expected = hmac.new(signing_secret(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(_b64d(sig), expected):
            return None
        data = json.loads(payload)
        if int(data.get("exp", 0)) < time.time():
            return None
        return data
    except Exception:
        return None


def csrf_for(token: str) -> str:
    return hmac.new(signing_secret(), b"csrf:" + token.encode(),
                    hashlib.sha256).hexdigest()[:32]


class LoginThrottle:
    """Per-IP exponential lockout on failed logins."""

    def __init__(self, limit: int = 6, window: int = 900) -> None:
        self.limit, self.window = limit, window
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked_for(self, ip: str) -> int:
        now = time.time()
        with self._lock:
            hits = [t for t in self._fails.get(ip, []) if now - t < self.window]
            self._fails[ip] = hits
            if len(hits) < self.limit:
                return 0
            return int(self.window - (now - hits[self.limit - 1])) + 1

    def fail(self, ip: str) -> None:
        with self._lock:
            self._fails.setdefault(ip, []).append(time.time())

    def reset(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)


THROTTLE = LoginThrottle()


# ─────────────────────────────────────────────────────────────────────────────
# Shell / process helpers
# ─────────────────────────────────────────────────────────────────────────────
def script_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(env_read())
    env.setdefault("AGENT_TEAM_LOG_DIR", str(RUN_DIR))
    return env


def run(argv: list[str], timeout: int = 120) -> dict:
    try:
        p = subprocess.run(argv, cwd=str(ROOT), env=script_env(), timeout=timeout,
                           capture_output=True, text=True)
        return {"ok": p.returncode == 0, "code": p.returncode,
                "stdout": p.stdout[-20000:], "stderr": p.stderr[-20000:]}
    except FileNotFoundError as exc:
        return {"ok": False, "code": 127, "stdout": "", "stderr": str(exc)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": 124, "stdout": "",
                "stderr": f"timed out after {timeout}s"}


def pid_file(name: str) -> pathlib.Path:
    return RUN_DIR / f"webui-{name}.pid"


def spawn(name: str, argv: list[str]) -> dict:
    """Start a detached background process, logging to RUN_DIR/<name>.log."""
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    log = RUN_DIR / f"{name}.log"
    with open(log, "ab") as fh:
        fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} webui start: "
                 f"{' '.join(argv)} ===\n".encode())
        proc = subprocess.Popen(argv, cwd=str(ROOT), env=script_env(),
                                stdout=fh, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    pid_file(name).write_text(str(proc.pid))
    return {"ok": True, "pid": proc.pid, "log": str(log)}


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True


def find_pids(needle: str) -> list[int]:
    """Find running processes whose cmdline contains `needle` (via /proc)."""
    pids: list[int] = []
    proc = pathlib.Path("/proc")
    if not proc.is_dir():
        out = run(["pgrep", "-f", needle], timeout=10)
        return [int(x) for x in out["stdout"].split() if x.isdigit()]
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", "replace")
        except OSError:
            continue
        if needle in cmdline and str(os.getpid()) != entry.name:
            pids.append(int(entry.name))
    return sorted(pids)


def stop_pids(pids: list[int], sig: int = signal.SIGTERM) -> list[int]:
    killed = []
    for pid in pids:
        try:
            os.kill(pid, sig)
            killed.append(pid)
        except OSError:
            pass
    return killed


def read_kv(path: pathlib.Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def tail_file(path: pathlib.Path, lines: int = 200) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block, data = 8192, b""
            while size > 0 and data.count(b"\n") <= lines:
                step = min(block, size)
                size -= step
                fh.seek(size)
                data = fh.read(step) + data
        return b"\n".join(data.splitlines()[-lines:]).decode("utf-8", "replace")
    except OSError as exc:
        return f"[unreadable: {exc}]"


# ─────────────────────────────────────────────────────────────────────────────
# GitLab + Telegram clients (stdlib urllib)
# ─────────────────────────────────────────────────────────────────────────────
class ApiError(Exception):
    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


def http_json(url: str, *, method: str = "GET", headers: dict | None = None,
              body: dict | None = None, timeout: int = 25):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Accept": "application/json", **(headers or {})}
    if data:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise ApiError(f"HTTP {exc.code}: {detail}", status=502) from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise ApiError(f"network error: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ApiError(f"bad JSON from {url}: {exc}") from exc


def gitlab(path: str, *, method: str = "GET", body: dict | None = None,
           params: dict | None = None):
    token = cfg("GITLAB_TOKEN")
    if not token:
        raise ApiError("GITLAB_TOKEN is not set — add it on the Settings tab.", 400)
    url = GITLAB_API.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return http_json(url, method=method, body=body,
                     headers={"PRIVATE-TOKEN": token})


def gitlab_group() -> str:
    group = cfg("GITLAB_GROUP")
    if not group or group == "your-group":
        raise ApiError("GITLAB_GROUP is not configured — set it on the Settings tab.", 400)
    return urllib.parse.quote(group, safe="")


def telegram(method: str, payload: dict | None = None, *, http_method: str = "POST"):
    token = cfg("TELEGRAM_TOKEN")
    if not token:
        raise ApiError("TELEGRAM_TOKEN is not set — add it on the Settings tab.", 400)
    url = f"https://api.telegram.org/bot{token}/{method}"
    return http_json(url, method=http_method, body=payload)


# ─────────────────────────────────────────────────────────────────────────────
# Agent role files
# ─────────────────────────────────────────────────────────────────────────────
AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


def split_frontmatter(text: str) -> tuple[str, str]:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            nl = text.find("\n", end + 1)
            return text[text.find("\n") + 1:end], text[nl + 1:] if nl != -1 else ""
    return "", text


def parse_frontmatter(raw: str) -> dict:
    """Minimal YAML subset: scalars and `- item` lists, one level deep."""
    data: dict = {}
    key = None
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if re.match(r"^\s*-\s+", line) and key:
            data.setdefault(key, [])
            if isinstance(data[key], list):
                data[key].append(re.sub(r"^\s*-\s+", "", line).strip().strip("'\""))
            continue
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if m:
            key = m.group(1)
            val = m.group(2).strip()
            if val in ("", "|", ">"):
                data[key] = []
            elif val.startswith("[") and val.endswith("]"):
                data[key] = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
            else:
                data[key] = val.strip("'\"")
    return data


def agent_files() -> list[pathlib.Path]:
    if not AGENTS_DIR.is_dir():
        return []
    return sorted(p for p in AGENTS_DIR.glob("*.md") if p.is_file())


def agent_summary(path: pathlib.Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    fm = parse_frontmatter(split_frontmatter(text)[0])
    tools = fm.get("allowedTools") or fm.get("tools") or []
    if isinstance(tools, str):
        tools = [tools]
    return {
        "file": path.name,
        "name": fm.get("name") or path.stem,
        "model": fm.get("model") or "",
        "description": fm.get("description") or "",
        "tools": tools,
        "toolCount": len(tools),
        "bytes": path.stat().st_size,
        "modified": int(path.stat().st_mtime),
    }


def agent_path(name: str) -> pathlib.Path:
    if not AGENT_NAME_RE.match(name):
        raise ApiError("invalid agent name (kebab-case, max 40 chars)", 400)
    return AGENTS_DIR / f"{name}.md"


def backup_file(path: pathlib.Path) -> None:
    if not path.exists():
        return
    AGENT_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(path, AGENT_BACKUP_DIR / f"{path.stem}-{stamp}{path.suffix}")


def build_agent_markdown(fields: dict, body: str) -> str:
    lines = ["---", f"name: {fields['name']}"]
    if fields.get("model"):
        lines.append(f"model: {fields['model']}")
    desc = (fields.get("description") or "").replace('"', "'")
    lines.append(f'description: "{desc}"')
    tools = [t.strip() for t in fields.get("tools", []) if t.strip()]
    if tools:
        lines.append("allowedTools:")
        lines.extend(f"  - {t}" for t in tools)
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.lstrip("\n").rstrip() + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# Status aggregation
# ─────────────────────────────────────────────────────────────────────────────
BOT_NEEDLE = "tg-issue-bot.py"
LOOP_NEEDLE = "issue-loop.sh"


def tmux_session() -> dict:
    session = cfg("AGENT_TEAM_SESSION", "agent-team") or "agent-team"
    if not shutil.which("tmux"):
        return {"session": session, "running": False, "windows": [],
                "error": "tmux not installed"}
    alive = run(["tmux", "has-session", "-t", session], timeout=10)["ok"]
    windows: list[str] = []
    if alive:
        out = run(["tmux", "list-windows", "-t", session, "-F",
                   "#{window_index}:#{window_name}"], timeout=10)
        windows = [w for w in out["stdout"].splitlines() if w.strip()]
    return {"session": session, "running": alive, "windows": windows}


def loop_status() -> dict:
    state = read_kv(LOOP_STATE_FILE) if LOOP_STATE_FILE.exists() else {}
    pause = read_kv(LOOP_PAUSE_FILE) if LOOP_PAUSE_FILE.exists() else {}
    pids = find_pids(LOOP_NEEDLE)
    stopped = LOOP_STOP_FILE.exists()
    if stopped:
        mode = "stopped"
    elif pause:
        mode = "paused"
    elif pids:
        mode = "running"
    else:
        mode = "idle"
    return {"mode": mode, "pids": pids, "stopped": stopped,
            "state": state, "pause": pause,
            "stopFile": str(LOOP_STOP_FILE),
            "stopFileWritable": os.access(LOOP_STOP_FILE.parent, os.W_OK)}


def bot_status() -> dict:
    pids = find_pids(BOT_NEEDLE)
    recorded = pid_file("tg-issue-bot").read_text().strip() \
        if pid_file("tg-issue-bot").exists() else ""
    info: dict = {"running": bool(pids), "pids": pids, "recordedPid": recorded,
                  "log": str(RUN_DIR / "tg-issue-bot.log")}
    env = env_read()
    info["configured"] = bool(cfg("TELEGRAM_TOKEN") and cfg("TELEGRAM_CHAT_ID"))
    info["chatId"] = env.get("TELEGRAM_CHAT_ID", os.environ.get("TELEGRAM_CHAT_ID", ""))
    return info


def repos_status() -> list[dict]:
    base = pathlib.Path(cfg("TEAM_BASE", "/srv/agent-team") or "/srv/agent-team")
    out: list[dict] = []
    if not base.is_dir():
        return out
    for entry in sorted(base.iterdir()):
        if not (entry / ".git").exists():
            continue
        branch = run(["git", "-C", str(entry), "rev-parse", "--abbrev-ref", "HEAD"],
                     timeout=15)
        dirty = run(["git", "-C", str(entry), "status", "--porcelain"], timeout=20)
        last = run(["git", "-C", str(entry), "log", "-1", "--pretty=%h %s"], timeout=15)
        out.append({
            "name": entry.name,
            "path": str(entry),
            "branch": branch["stdout"].strip() or "?",
            "dirty": len([l for l in dirty["stdout"].splitlines() if l.strip()]),
            "lastCommit": last["stdout"].strip(),
        })
    return out


def tool_status() -> list[dict]:
    tools = ["claude", "tmux", "jq", "git", "curl", "flock", "python3"]
    return [{"name": t, "path": shutil.which(t) or "", "ok": bool(shutil.which(t))}
            for t in tools]


def overall_status() -> dict:
    env = env_read()
    missing = [k for k in ("GITLAB_GROUP", "GITLAB_TOKEN", "TELEGRAM_TOKEN",
                           "TELEGRAM_CHAT_ID")
               if not (env.get(k) or os.environ.get(k))]
    return {
        "root": str(ROOT),
        "envFile": str(ENV_FILE),
        "envExists": ENV_FILE.exists(),
        "missingConfig": missing,
        "autoAcceptMr": (cfg("CTO_AUTO_ACCEPT_MR", "0") or "0").lower()
                        in ("1", "true", "yes", "on"),
        "loop": loop_status(),
        "bot": bot_status(),
        "tmux": tmux_session(),
        "agents": len(agent_files()),
        "tools": tool_status(),
        "time": int(time.time()),
    }


# ─────────────────────────────────────────────────────────────────────────────
# API handlers
# ─────────────────────────────────────────────────────────────────────────────
def api_env_get() -> dict:
    env = env_read()
    rows: list[dict] = []
    for spec in KNOWN_ENV:
        key = spec["key"]
        value = env.get(key, "")
        rows.append({**spec, "known": True, "set": bool(value),
                     "value": mask(value) if is_secret(key) else value,
                     "secret": is_secret(key)})
    for key, value in env.items():
        if key in KNOWN_KEYS:
            continue
        rows.append({"key": key, "group": "Other", "label": key, "help": "",
                     "known": False, "set": bool(value), "secret": is_secret(key),
                     "value": mask(value) if is_secret(key) else value})
    return {"rows": rows, "file": str(ENV_FILE), "exists": ENV_FILE.exists()}


def api_env_set(body: dict) -> dict:
    updates = {str(k): ("" if v is None else str(v))
               for k, v in (body.get("updates") or {}).items()}
    deletes = [str(k) for k in (body.get("deletes") or [])]
    for key in list(updates) + deletes:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ApiError(f"invalid variable name: {key}", 400)
    if "WEBUI_PASSWORD" in updates and updates["WEBUI_PASSWORD"]:
        # never store a plaintext password when we can hash it
        updates["WEBUI_PASSWORD_HASH"] = hash_password(updates.pop("WEBUI_PASSWORD"))
        deletes.append("WEBUI_PASSWORD")
    if not updates and not deletes:
        raise ApiError("nothing to change", 400)
    env_write(updates, deletes)
    return {"saved": sorted(updates), "removed": sorted(set(deletes))}


def api_password(body: dict, user: str) -> dict:
    current = str(body.get("current") or "")
    new = str(body.get("new") or "")
    if len(new) < 8:
        raise ApiError("the new password must be at least 8 characters", 400)
    if not check_credentials(user, current):
        raise ApiError("current password is wrong", 403)
    env_write({"WEBUI_PASSWORD_HASH": hash_password(new)}, ["WEBUI_PASSWORD"])
    return {"changed": True}


def api_agents_list() -> dict:
    return {"agents": [agent_summary(p) for p in agent_files()],
            "dir": str(AGENTS_DIR)}


def api_agent_get(name: str) -> dict:
    path = agent_path(name)
    if not path.exists():
        raise ApiError(f"no such agent: {name}", 404)
    text = path.read_text(encoding="utf-8", errors="replace")
    fm_raw, body = split_frontmatter(text)
    fm = parse_frontmatter(fm_raw)
    tools = fm.get("allowedTools") or fm.get("tools") or []
    if isinstance(tools, str):
        tools = [tools]
    return {"file": path.name, "name": fm.get("name") or path.stem,
            "model": fm.get("model") or "", "description": fm.get("description") or "",
            "tools": tools, "body": body, "raw": text, "path": str(path)}


def api_agent_save(name: str, body: dict) -> dict:
    path = agent_path(name)
    if body.get("raw") is not None:
        text = str(body["raw"])
        if not text.startswith("---"):
            raise ApiError("an agent file must start with a --- frontmatter block", 400)
    else:
        fields = {
            "name": name,
            "model": str(body.get("model") or "").strip(),
            "description": str(body.get("description") or "").strip(),
            "tools": body.get("tools") or [],
        }
        if isinstance(fields["tools"], str):
            fields["tools"] = [t for t in fields["tools"].splitlines() if t.strip()]
        text = build_agent_markdown(fields, str(body.get("body") or ""))
    existed = path.exists()
    backup_file(path)
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return {"saved": path.name, "created": not existed, "bytes": len(text)}


def api_agent_delete(name: str) -> dict:
    path = agent_path(name)
    if not path.exists():
        raise ApiError(f"no such agent: {name}", 404)
    backup_file(path)
    path.unlink()
    return {"deleted": path.name, "backup": str(AGENT_BACKUP_DIR)}


def resolve_editable(rel: str) -> pathlib.Path:
    if rel not in EDITABLE_FILES:
        raise ApiError("this file is not editable from the UI", 403)
    return ROOT / rel


def api_files_list() -> dict:
    out = []
    for rel in EDITABLE_FILES:
        path = ROOT / rel
        out.append({"path": rel, "exists": path.exists(),
                    "bytes": path.stat().st_size if path.exists() else 0,
                    "modified": int(path.stat().st_mtime) if path.exists() else 0})
    return {"files": out}


def api_file_get(rel: str) -> dict:
    path = resolve_editable(rel)
    return {"path": rel, "exists": path.exists(),
            "content": path.read_text(encoding="utf-8", errors="replace")
            if path.exists() else ""}


def api_file_save(rel: str, body: dict) -> dict:
    path = resolve_editable(rel)
    content = str(body.get("content") or "")
    if rel.endswith(".json") and content.strip():
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            raise ApiError(f"invalid JSON: {exc}", 400) from exc
    backup_file(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"saved": rel, "bytes": len(content)}


# ─────────────────────────────────────────────────────────────────────────────
# Control: loop, bot, tmux team, Telegram
# ─────────────────────────────────────────────────────────────────────────────
def api_control_loop(body: dict) -> dict:
    action = str(body.get("action") or "")
    if action == "enable":
        try:
            LOOP_STOP_FILE.unlink(missing_ok=True)
        except OSError as exc:
            raise ApiError(f"cannot remove {LOOP_STOP_FILE}: {exc}", 500) from exc
        return {"mode": "enabled", "message": "Stop flag cleared — the loop may run."}
    if action == "disable":
        try:
            LOOP_STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
            LOOP_STOP_FILE.write_text(f"stopped by web ui {time.strftime('%F %T')}\n")
        except OSError as exc:
            raise ApiError(f"cannot write {LOOP_STOP_FILE}: {exc}", 500) from exc
        return {"mode": "disabled", "message": "Stop flag set — the loop will not start."}
    if action == "clear-pause":
        try:
            LOOP_PAUSE_FILE.unlink(missing_ok=True)
        except OSError as exc:
            raise ApiError(f"cannot remove {LOOP_PAUSE_FILE}: {exc}", 500) from exc
        return {"message": "Pause marker cleared."}
    if action == "run":
        if find_pids(LOOP_NEEDLE):
            raise ApiError("the issue loop is already running", 409)
        res = spawn("issue-loop", ["bash", str(SCRIPTS_DIR / "issue-loop.sh")])
        return {"message": f"Issue loop started (pid {res['pid']}).", **res}
    if action == "kill":
        pids = find_pids(LOOP_NEEDLE)
        if not pids:
            raise ApiError("the issue loop is not running", 409)
        return {"message": f"Signalled {stop_pids(pids)}.", "pids": pids}
    if action == "dry-run":
        out = run(["bash", str(SCRIPTS_DIR / "issue-loop.sh"), "select-dry"], timeout=180)
        return {"message": "Dry run finished.", "output":
                (out["stdout"] + out["stderr"]).strip(), "code": out["code"]}
    raise ApiError(f"unknown loop action: {action}", 400)


def api_control_bot(body: dict) -> dict:
    action = str(body.get("action") or "")
    if action in ("stop", "restart"):
        pids = find_pids(BOT_NEEDLE)
        stop_pids(pids)
        pid_file("tg-issue-bot").unlink(missing_ok=True)
        if action == "stop":
            if not pids:
                raise ApiError("the Telegram bot is not running", 409)
            return {"message": f"Stopped the Telegram bot (pid {pids})."}
        time.sleep(1.0)
    if action in ("start", "restart"):
        if action == "start" and find_pids(BOT_NEEDLE):
            raise ApiError("the Telegram bot is already running", 409)
        if not (cfg("TELEGRAM_TOKEN") and cfg("TELEGRAM_CHAT_ID")):
            raise ApiError("set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID first", 400)
        res = spawn("tg-issue-bot",
                    [sys.executable, str(SCRIPTS_DIR / "tg-issue-bot.py")])
        return {"message": f"Telegram bot started (pid {res['pid']}).", **res}
    raise ApiError(f"unknown bot action: {action}", 400)


def api_control_team(body: dict) -> dict:
    action = str(body.get("action") or "")
    if action == "launch":
        out = run(["bash", str(SCRIPTS_DIR / "team-launch.sh"), "--replace"], timeout=120)
        return {"message": "team-launch.sh finished.",
                "output": (out["stdout"] + out["stderr"]).strip(), "code": out["code"]}
    if action == "stop":
        out = run(["bash", str(SCRIPTS_DIR / "team-stop.sh")], timeout=120)
        return {"message": "team-stop.sh finished.",
                "output": (out["stdout"] + out["stderr"]).strip(), "code": out["code"]}
    raise ApiError(f"unknown team action: {action}", 400)


def api_telegram_test(body: dict) -> dict:
    me = telegram("getMe", http_method="GET")
    text = str(body.get("text") or "").strip()
    sent = None
    if text:
        chat = cfg("TELEGRAM_CHAT_ID")
        if not chat:
            raise ApiError("TELEGRAM_CHAT_ID is not set", 400)
        sent = telegram("sendMessage", {"chat_id": chat, "text": text,
                                        "parse_mode": "HTML"})
    bot = (me or {}).get("result", {})
    return {"bot": {"username": bot.get("username"), "id": bot.get("id"),
                    "name": bot.get("first_name")},
            "sent": bool(sent)}


def api_gitlab_test() -> dict:
    group = gitlab_group()
    info = gitlab(f"/groups/{group}")
    projects = gitlab(f"/groups/{group}/projects",
                      params={"per_page": 100, "archived": "false",
                              "include_subgroups": "true"})
    return {"group": {"name": info.get("name"), "path": info.get("full_path"),
                      "url": info.get("web_url")},
            "projects": [{"id": p["id"], "name": p["path"], "url": p["web_url"]}
                         for p in projects]}


# ─────────────────────────────────────────────────────────────────────────────
# Board
# ─────────────────────────────────────────────────────────────────────────────
def api_board(query: dict) -> dict:
    group = gitlab_group()
    params = {"state": query.get("state", ["opened"])[0], "per_page": "100",
              "scope": "all", "order_by": "updated_at", "sort": "desc"}
    label = (query.get("label") or [""])[0]
    if label:
        params["labels"] = label
    issues = gitlab(f"/groups/{group}/issues", params=params)
    rows = []
    for i in issues:
        rows.append({
            "id": i["id"], "iid": i["iid"], "projectId": i["project_id"],
            "title": i["title"], "labels": i.get("labels", []),
            "state": i["state"], "url": i["web_url"],
            "milestone": (i.get("milestone") or {}).get("title", ""),
            "updated": i.get("updated_at", ""),
            "repo": re.sub(r"^.*?/([^/]+)/-/issues/\d+$", r"\1", i["web_url"]),
            "assignee": (i.get("assignee") or {}).get("username", ""),
        })
    return {"issues": rows}


def api_board_label(body: dict) -> dict:
    pid = body.get("projectId")
    iid = body.get("iid")
    if not pid or not iid:
        raise ApiError("projectId and iid are required", 400)
    add = [str(x) for x in (body.get("add") or [])]
    remove = [str(x) for x in (body.get("remove") or [])]
    payload: dict = {}
    if add:
        payload["add_labels"] = ",".join(add)
    if remove:
        payload["remove_labels"] = ",".join(remove)
    if body.get("milestoneId"):
        payload["milestone_id"] = int(body["milestoneId"])
    if not payload:
        raise ApiError("nothing to change", 400)
    updated = gitlab(f"/projects/{pid}/issues/{iid}", method="PUT", body=payload)
    return {"labels": updated.get("labels", []), "iid": updated.get("iid")}


def api_board_create(body: dict) -> dict:
    pid = body.get("projectId")
    title = str(body.get("title") or "").strip()
    if not pid or not title:
        raise ApiError("projectId and title are required", 400)
    payload = {"title": title, "description": str(body.get("description") or "")}
    labels = [str(x) for x in (body.get("labels") or [])]
    if labels:
        payload["labels"] = ",".join(labels)
    issue = gitlab(f"/projects/{pid}/issues", method="POST", body=payload)
    return {"iid": issue.get("iid"), "url": issue.get("web_url")}


def api_mrs() -> dict:
    group = gitlab_group()
    mrs = gitlab(f"/groups/{group}/merge_requests",
                 params={"state": "opened", "per_page": "100",
                         "order_by": "updated_at", "sort": "desc"})
    rows = []
    for m in mrs:
        pipeline = m.get("head_pipeline") or {}
        rows.append({
            "projectId": m["project_id"], "iid": m["iid"], "title": m["title"],
            "url": m["web_url"], "draft": m.get("draft", m.get("work_in_progress", False)),
            "sourceBranch": m.get("source_branch", ""),
            "conflicts": m.get("has_conflicts", False),
            "mergeStatus": m.get("detailed_merge_status") or m.get("merge_status", ""),
            "pipeline": pipeline.get("status", ""),
            "pipelineUrl": pipeline.get("web_url", ""),
            "updated": m.get("updated_at", ""),
        })
    return {"mrs": rows}


def api_mr_merge(body: dict) -> dict:
    pid, iid = body.get("projectId"), body.get("iid")
    if not pid or not iid:
        raise ApiError("projectId and iid are required", 400)
    out = run(["bash", str(SCRIPTS_DIR / "cto-accept-mr.sh"), str(pid), str(iid)],
              timeout=180)
    text = (out["stdout"] + out["stderr"]).strip()
    return {"ok": out["ok"], "code": out["code"], "output": text,
            "message": "Merged." if out["ok"] else "Refused — see the gate output."}


def api_mr_close(body: dict) -> dict:
    pid, iid = body.get("projectId"), body.get("iid")
    if not pid or not iid:
        raise ApiError("projectId and iid are required", 400)
    mr = gitlab(f"/projects/{pid}/merge_requests/{iid}", method="PUT",
                body={"state_event": "close"})
    return {"state": mr.get("state")}


# ─────────────────────────────────────────────────────────────────────────────
# Logs
# ─────────────────────────────────────────────────────────────────────────────
def api_logs_list() -> dict:
    files = []
    for root in dict.fromkeys(LOG_ROOTS):
        if not root.is_dir():
            continue
        for path in sorted(root.iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue
            if path.suffix not in (".log", ".jsonl", ".txt") and "log" not in path.name:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            files.append({"path": str(path), "name": path.name,
                          "dir": str(root), "bytes": stat.st_size,
                          "modified": int(stat.st_mtime)})
    files.sort(key=lambda f: f["modified"], reverse=True)
    return {"files": files[:200], "roots": [str(r) for r in dict.fromkeys(LOG_ROOTS)]}


def api_log_tail(query: dict) -> dict:
    raw = (query.get("path") or [""])[0]
    if not raw:
        raise ApiError("path is required", 400)
    path = pathlib.Path(raw).resolve()
    if not any(str(path).startswith(str(r.resolve()) + os.sep)
               for r in LOG_ROOTS if r.exists()):
        raise ApiError("that file is outside the readable log directories", 403)
    if not path.is_file():
        raise ApiError("no such log file", 404)
    lines = max(10, min(2000, int((query.get("lines") or ["300"])[0])))
    return {"path": str(path), "lines": lines, "content": tail_file(path, lines),
            "bytes": path.stat().st_size}


def api_monitors() -> dict:
    live = []
    if RUN_DIR.is_dir():
        for path in sorted(RUN_DIR.glob("*.log")):
            live.append({"slug": path.stem, "path": str(path),
                         "bytes": path.stat().st_size,
                         "modified": int(path.stat().st_mtime)})
    archived = []
    if ARCHIVE_DIR.is_dir():
        for path in sorted(ARCHIVE_DIR.iterdir(), reverse=True):
            if path.is_file() and not path.name.startswith("."):
                archived.append({"name": path.name, "path": str(path),
                                 "bytes": path.stat().st_size,
                                 "modified": int(path.stat().st_mtime)})
    return {"live": live, "archived": archived[:100],
            "tmux": tmux_session()}


# ─────────────────────────────────────────────────────────────────────────────
# Credentials
# ─────────────────────────────────────────────────────────────────────────────
def check_credentials(user: str, password: str) -> bool:
    expected_user = cfg("WEBUI_USER", "admin") or "admin"
    hashed = cfg("WEBUI_PASSWORD_HASH")
    plain = cfg("WEBUI_PASSWORD")
    if not hashed and not plain:
        return False
    user_ok = hmac.compare_digest(user.strip(), expected_user)
    if hashed:
        pass_ok = verify_password(password, hashed)
    else:
        pass_ok = hmac.compare_digest(password, plain)
    return user_ok and pass_ok


def auth_configured() -> bool:
    return bool(cfg("WEBUI_PASSWORD_HASH") or cfg("WEBUI_PASSWORD"))


# ─────────────────────────────────────────────────────────────────────────────
# HTTP layer
# ─────────────────────────────────────────────────────────────────────────────
COOKIE = "agentteam_session"
MAX_BODY = 4 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "ClaudeDevTeamUI"
    protocol_version = "HTTP/1.1"

    # ── plumbing ────────────────────────────────────────────────────────────
    def log_message(self, fmt: str, *args) -> None:  # quieter access log
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes, ctype: str,
              extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, data, status: int = 200, extra: dict | None = None) -> None:
        self._send(status, json.dumps(data).encode(), "application/json", extra)

    def error_json(self, message: str, status: int = 400) -> None:
        self.json({"error": message}, status)

    def read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ApiError("request body too large", 413)
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError(f"invalid JSON body: {exc}", 400) from exc
        if not isinstance(data, dict):
            raise ApiError("the request body must be a JSON object", 400)
        return data

    # ── session ─────────────────────────────────────────────────────────────
    def _cookie_token(self) -> str:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return value
        return ""

    def _secure_cookie(self) -> bool:
        if (cfg("WEBUI_TLS", "0") or "0").lower() in ("1", "true", "yes", "on"):
            return True
        return (self.headers.get("X-Forwarded-Proto") or "").lower() == "https"

    def _set_cookie(self, token: str, ttl: int) -> dict:
        bits = [f"{COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Lax",
                f"Max-Age={ttl}"]
        if self._secure_cookie():
            bits.append("Secure")
        return {"Set-Cookie": "; ".join(bits)}

    def current_session(self) -> tuple[str, dict] | tuple[None, None]:
        token = self._cookie_token()
        data = read_session(token) if token else None
        return (token, data) if data else (None, None)

    # ── routing ─────────────────────────────────────────────────────────────
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                self.handle_api(method, path, query)
            elif method == "GET":
                self.serve_static(path)
            else:
                self.error_json("not found", 404)
        except ApiError as exc:
            self.error_json(str(exc), exc.status)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 — never leak a traceback to the client
            sys.stderr.write(f"[webui] unhandled error on {path}: {exc!r}\n")
            self.error_json(f"internal error: {exc}", 500)

    # ── static files ────────────────────────────────────────────────────────
    def serve_static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            target = STATIC_DIR / "index.html"
            if not target.is_file():
                self._send(404, b"not found", "text/plain")
                return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith(("javascript", "json")):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)

    # ── api ─────────────────────────────────────────────────────────────────
    def handle_api(self, method: str, path: str, query: dict) -> None:
        # public endpoints
        if path == "/api/session" and method == "GET":
            token, data = self.current_session()
            self.json({"authed": bool(data),
                       "user": (data or {}).get("user") or (data or {}).get("u", ""),
                       "csrf": csrf_for(token) if token else "",
                       "configured": auth_configured(),
                       "expires": (data or {}).get("exp", 0)})
            return

        if path == "/api/login" and method == "POST":
            self.handle_login()
            return

        if path == "/api/logout" and method == "POST":
            self.json({"ok": True}, extra={
                "Set-Cookie": f"{COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"})
            return

        # everything below requires a valid session
        token, session = self.current_session()
        if not session:
            self.error_json("not authenticated", 401)
            return
        user = session.get("u", "")

        if method != "GET":
            sent = self.headers.get("X-CSRF-Token") or ""
            if not hmac.compare_digest(sent, csrf_for(token)):
                self.error_json("bad or missing CSRF token — reload the page", 403)
                return

        body = self.read_body() if method == "POST" else {}
        self.json(self.route(method, path, query, body, user))

    def handle_login(self) -> None:
        ip = self.client_address[0]
        wait = THROTTLE.blocked_for(ip)
        if wait:
            self.error_json(f"too many failed attempts — try again in {wait}s", 429)
            return
        body = self.read_body()
        user = str(body.get("user") or "")
        password = str(body.get("password") or "")
        if not auth_configured():
            self.error_json(
                "no web UI password is configured — set WEBUI_PASSWORD_HASH in "
                "scripts/.env (python3 webui/server.py hash-password)", 503)
            return
        if not check_credentials(user, password):
            THROTTLE.fail(ip)
            time.sleep(0.6)
            self.error_json("invalid username or password", 401)
            return
        THROTTLE.reset(ip)
        ttl = int(cfg("WEBUI_SESSION_TTL", "43200") or 43200)
        token = make_session(user)
        self.json({"ok": True, "user": user, "csrf": csrf_for(token)},
                  extra=self._set_cookie(token, ttl))

    # ── the route table ─────────────────────────────────────────────────────
    def route(self, method: str, path: str, query: dict, body: dict, user: str):
        if method == "GET":
            table = {
                "/api/status": overall_status,
                "/api/env": api_env_get,
                "/api/agents": api_agents_list,
                "/api/files": api_files_list,
                "/api/mrs": api_mrs,
                "/api/logs": api_logs_list,
                "/api/monitors": api_monitors,
                "/api/repos": lambda: {"repos": repos_status()},
                "/api/gitlab/test": api_gitlab_test,
            }
            if path in table:
                return table[path]()
            if path == "/api/board":
                return api_board(query)
            if path == "/api/log":
                return api_log_tail(query)
            if path == "/api/file":
                return api_file_get((query.get("path") or [""])[0])
            m = re.fullmatch(r"/api/agents/([a-z0-9-]+)", path)
            if m:
                return api_agent_get(m.group(1))
            raise ApiError("not found", 404)

        table_post = {
            "/api/env": lambda: api_env_set(body),
            "/api/password": lambda: api_password(body, user),
            "/api/file": lambda: api_file_save(str(body.get("path") or ""), body),
            "/api/control/loop": lambda: api_control_loop(body),
            "/api/control/bot": lambda: api_control_bot(body),
            "/api/control/team": lambda: api_control_team(body),
            "/api/telegram/test": lambda: api_telegram_test(body),
            "/api/board/label": lambda: api_board_label(body),
            "/api/board/create": lambda: api_board_create(body),
            "/api/mr/merge": lambda: api_mr_merge(body),
            "/api/mr/close": lambda: api_mr_close(body),
        }
        if path in table_post:
            return table_post[path]()
        m = re.fullmatch(r"/api/agents/([a-z0-9-]+)", path)
        if m:
            return api_agent_save(m.group(1), body)
        m = re.fullmatch(r"/api/agents/([a-z0-9-]+)/delete", path)
        if m:
            return api_agent_delete(m.group(1))
        raise ApiError("not found", 404)


# ─────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ─────────────────────────────────────────────────────────────────────────────
def cli_hash_password(argv: list[str]) -> int:
    import getpass
    password = argv[0] if argv else getpass.getpass("New web UI password: ")
    if not argv:
        again = getpass.getpass("Repeat: ")
        if password != again:
            print("passwords do not match", file=sys.stderr)
            return 1
    if len(password) < 8:
        print("use at least 8 characters", file=sys.stderr)
        return 1
    print(f"WEBUI_PASSWORD_HASH={hash_password(password)}")
    print("\nAdd that line to scripts/.env (chmod 600), then restart the web UI.",
          file=sys.stderr)
    return 0


def cli_set_password(argv: list[str]) -> int:
    import getpass
    user = argv[0] if argv else input("Username [admin]: ").strip() or "admin"
    password = getpass.getpass("New web UI password: ")
    if password != getpass.getpass("Repeat: "):
        print("passwords do not match", file=sys.stderr)
        return 1
    if len(password) < 8:
        print("use at least 8 characters", file=sys.stderr)
        return 1
    env_write({"WEBUI_USER": user, "WEBUI_PASSWORD_HASH": hash_password(password)},
              ["WEBUI_PASSWORD"])
    print(f"Saved WEBUI_USER={user} and a password hash to {ENV_FILE}")
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] in ("hash-password", "hash"):
        return cli_hash_password(argv[1:])
    if argv and argv[0] in ("set-password", "passwd"):
        return cli_set_password(argv[1:])
    if argv and argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    host = cfg("WEBUI_HOST", "0.0.0.0") or "0.0.0.0"
    port = int(cfg("WEBUI_PORT", "9000") or 9000)

    if not auth_configured():
        print(
            "\n  Refusing to start: no web UI password is configured.\n\n"
            "  Set one with:\n\n"
            f"      python3 {WEBUI_DIR.name}/{pathlib.Path(__file__).name}"
            " set-password\n\n"
            "  or add WEBUI_USER / WEBUI_PASSWORD_HASH to scripts/.env by hand.\n",
            file=sys.stderr)
        return 2

    signing_secret()  # generate + persist the cookie key before serving
    RUN_DIR.mkdir(parents=True, exist_ok=True)

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    user = cfg("WEBUI_USER", "admin")
    print(f"Claude Dev Team control panel → http://{host}:{port}  (user: {user})")
    if host not in ("127.0.0.1", "localhost", "::1") and not \
            (cfg("WEBUI_TLS", "0") or "0").lower() in ("1", "true", "yes", "on"):
        print("  ⚠  Listening on a public interface without TLS. Put it behind an "
              "HTTPS reverse proxy or an SSH tunnel.", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
