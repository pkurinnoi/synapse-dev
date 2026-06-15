#!/usr/bin/env python3
"""
scripts/tg-issue-bot.py — Interactive Telegram bot for the autonomous agent dev team.

Implements a /issue command that walks a user through creating a GitLab issue
conversationally via inline keyboard buttons.  Long-poll daemon; no webhook or
public URL required.

USAGE
  # Standalone (primary run path):
  python3 scripts/tg-issue-bot.py

  # Via tmux (launched automatically by team-launch.sh as the "issuebot" window):
  # team-launch.sh handles this — see the "issuebot" block in that script.

ENVIRONMENT (from scripts/.env or exported):
  GITLAB_TOKEN      GitLab PAT with api scope on the GitLab group
  TELEGRAM_TOKEN    @BotFather bot token
  TELEGRAM_CHAT_ID  Numeric chat id (group chats are negative, e.g. -5152538587)

CONVERSATION FLOW
  /issue
    1. "Which project?" — inline keyboard, one button per live project
    2. Label multi-select — tap labels to toggle, then tap "Done" or "Skip"
    3. "Enter a title" — next free-text message
    4. "Enter a description (or /skip)" — next free-text message
    5. Issue created → confirmation with direct link

  /cancel — abort the flow at any step.

ACCESS CONTROL
  Only updates from TELEGRAM_CHAT_ID are processed; all others are silently dropped.
  The bot's own messages are also ignored.

STATE FILE
  /tmp/agent-team/tg-issue-bot.offset — persists the last processed update
  offset so a restart never replays old updates.

DEPENDENCIES
  Python 3 standard library only (urllib, json, os, sys, time, logging, html,
  pathlib, signal).  No pip installs required.
"""

import datetime
import html
import json
import logging
import os
import pathlib
import re
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from typing import Any

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("tg-issue-bot")

# ---------------------------------------------------------------------------
# Config / env loading
# ---------------------------------------------------------------------------
_SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
_ENV_FILE = _SCRIPT_DIR / ".env"

def _load_dotenv(path: pathlib.Path) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ (no-op if missing)."""
    if not path.exists():
        return
    with path.open() as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


# Load .env if the required vars are not already exported (mirrors tg.sh behaviour).
if not os.environ.get("TELEGRAM_TOKEN"):
    _load_dotenv(_ENV_FILE)

GITLAB_TOKEN: str = os.environ.get("GITLAB_TOKEN", "")
TELEGRAM_TOKEN: str = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.environ.get("TELEGRAM_CHAT_ID", "")

# ---------------------------------------------------------------------------
# State file for offset persistence
# ---------------------------------------------------------------------------
_STATE_DIR = pathlib.Path("/tmp/agent-team")
_OFFSET_FILE = _STATE_DIR / "tg-issue-bot.offset"

GITLAB_API = "https://gitlab.com/api/v4"
GITLAB_GROUP = os.environ.get("GITLAB_GROUP", "your-group")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Runtime files shared with scripts/issue-loop.sh (must mirror its constants).
#   STOP_FILE  — manual stop flag (/stop creates it, /start removes it).
#   STATE_FILE — the issue currently being worked (PID/IID/WORKER/STARTED).
#   PAUSE_FILE — auto-managed hold (TYPE=mr or TYPE=session-limit).
_ISSUE_LOOP_STOP_FILE = pathlib.Path("/var/run/agent-issue-loop.stopped")
_ISSUE_LOOP_STATE_FILE = pathlib.Path("/var/run/agent-issue-loop.state")
_ISSUE_LOOP_PAUSE_FILE = pathlib.Path("/var/run/agent-issue-loop.paused")

# ---------------------------------------------------------------------------
# Per-user conversation state
# ---------------------------------------------------------------------------
# State machine steps:
#   "project"     — waiting for project selection (callback_query)
#   "labels"      — waiting for label toggles or done/skip (callback_query)
#   "title"       — waiting for free-text title (message)
#   "description" — waiting for free-text description or /skip (message)
_user_states: dict[tuple[int, int], dict[str, Any]] = {}
# keyed by (chat_id, user_id) → {
#   "step": str,
#   "project_id": int,
#   "project_path": str,
#   "labels": list[str],          # all available labels for the selected project
#   "selected_labels": list[str], # currently toggled labels
#   "label_msg_id": int,          # message_id of the label keyboard so we can edit it
#   "title": str,
# }

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
_HTTP_TIMEOUT = 20  # seconds for non-poll requests


def _http_get(url: str, headers: dict[str, str] | None = None, timeout: int = _HTTP_TIMEOUT) -> Any:
    """GET JSON from url, follow Link-header pagination up to 500 items."""
    all_items: list[Any] | None = None
    current_url: str | None = url

    while current_url:
        req = urllib.request.Request(current_url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            data = json.loads(body)
            next_page = _parse_next_link(resp.headers.get("Link", ""))
        if isinstance(data, list):
            if all_items is None:
                all_items = []
            all_items.extend(data)
            if next_page and len(all_items) < 500:
                current_url = next_page
                continue
        else:
            return data
        break

    return all_items if all_items is not None else data


def _parse_next_link(link_header: str) -> str | None:
    """Extract rel=next URL from a Link header."""
    if not link_header:
        return None
    for part in link_header.split(","):
        url_part, _, rel_part = part.strip().partition(";")
        url_part = url_part.strip().strip("<>")
        if 'rel="next"' in rel_part:
            return url_part
    return None


def _http_post(url: str, body: dict[str, Any] | None = None,
               headers: dict[str, str] | None = None) -> Any:
    """POST JSON body to url, return parsed JSON response."""
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        return json.loads(resp.read())


def _http_put(url: str, body: dict[str, Any] | None = None,
              headers: dict[str, str] | None = None) -> Any:
    """PUT JSON body to url, return parsed JSON response."""
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        return json.loads(resp.read())


def _gitlab_headers() -> dict[str, str]:
    return {"PRIVATE-TOKEN": GITLAB_TOKEN}


# ---------------------------------------------------------------------------
# Telegram API helpers
# ---------------------------------------------------------------------------

def tg_call(method: str, payload: dict[str, Any]) -> Any:
    """Call a Telegram Bot API method, return result dict."""
    url = f"{TELEGRAM_API}/{method}"
    return _http_post(url, payload)


def send_message(chat_id: int, text: str,
                 reply_markup: dict | None = None) -> dict:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_call("sendMessage", payload)


def edit_reply_markup(chat_id: int, message_id: int,
                      reply_markup: dict) -> None:
    tg_call("editMessageReplyMarkup", {
        "chat_id": chat_id,
        "message_id": message_id,
        "reply_markup": reply_markup,
    })


def answer_callback_query(callback_query_id: str, text: str = "") -> None:
    tg_call("answerCallbackQuery", {
        "callback_query_id": callback_query_id,
        "text": text,
    })


def get_me() -> dict:
    url = f"{TELEGRAM_API}/getMe"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        return json.loads(resp.read())


def set_my_commands() -> Any:
    """Register the slash-command menu shown in Telegram clients (setMyCommands)."""
    commands = [
        {"command": "issue",  "description": "Create a new GitLab issue"},
        {"command": "status", "description": "Show current work status of the agent team"},
        {"command": "merge",  "description": "Merge a held MR (reply to it or pass its URL)"},
        {"command": "stop",   "description": "Stop the issue loop"},
        {"command": "start",  "description": "Start the issue loop"},
        {"command": "cancel", "description": "Cancel the current /issue flow"},
    ]
    return tg_call("setMyCommands", {"commands": commands})


def get_updates(offset: int | None, timeout: int = 30) -> list[dict]:
    params: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
    if offset is not None:
        params["offset"] = offset
    url = f"{TELEGRAM_API}/getUpdates?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout + 10) as resp:
        data = json.loads(resp.read())
    if not data.get("ok"):
        raise RuntimeError(f"getUpdates error: {data}")
    return data.get("result", [])


# ---------------------------------------------------------------------------
# GitLab helpers
# ---------------------------------------------------------------------------

def fetch_projects() -> list[dict]:
    """Return list of projects in the GitLab group (live from API)."""
    url = (
        f"{GITLAB_API}/groups/{GITLAB_GROUP}/projects"
        f"?per_page=100&order_by=path&include_subgroups=true"
    )
    projects = _http_get(url, headers=_gitlab_headers())
    if not isinstance(projects, list):
        return []
    return projects


def fetch_labels(project_id: int) -> list[str]:
    """Return sorted list of label names for a project."""
    url = f"{GITLAB_API}/projects/{project_id}/labels?per_page=100"
    labels = _http_get(url, headers=_gitlab_headers())
    if not isinstance(labels, list):
        return []
    return sorted(lbl["name"] for lbl in labels if isinstance(lbl, dict) and lbl.get("name"))


def create_issue(project_id: int, title: str,
                 description: str, labels: list[str]) -> dict:
    """Create a GitLab issue; return the API response dict."""
    url = f"{GITLAB_API}/projects/{project_id}/issues"
    payload: dict[str, Any] = {"title": title}
    if description:
        payload["description"] = description
    if labels:
        payload["labels"] = ",".join(labels)
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", **_gitlab_headers()},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Offset persistence
# ---------------------------------------------------------------------------

def load_offset() -> int | None:
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        if _OFFSET_FILE.exists():
            val = _OFFSET_FILE.read_text().strip()
            return int(val) if val else None
    except Exception as exc:
        log.warning("Could not read offset file: %s", exc)
    return None


def save_offset(offset: int) -> None:
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        _OFFSET_FILE.write_text(str(offset))
    except Exception as exc:
        log.warning("Could not save offset: %s", exc)


# ---------------------------------------------------------------------------
# Inline keyboard builders
# ---------------------------------------------------------------------------

def _build_project_keyboard(projects: list[dict]) -> dict:
    """One button per project, callback_data = 'proj:<id>'."""
    buttons = []
    for proj in projects:
        pid = proj.get("id")
        name = proj.get("path") or proj.get("name") or str(pid)
        # callback_data max 64 bytes — 'proj:' + numeric id is well within limit
        cb = f"proj:{pid}"
        buttons.append([{"text": name, "callback_data": cb}])
    return {"inline_keyboard": buttons}


def _build_label_keyboard(labels: list[str], selected: list[str]) -> dict:
    """Multi-select label keyboard.  Selected labels get a checkmark prefix."""
    buttons = []
    for lbl in labels:
        checked = lbl in selected
        display = f"✓ {lbl}" if checked else lbl
        # callback_data = 'lbl:<label>' — label names in the group are short;
        # guard against the 64-byte limit by truncating the name part if needed
        cb_prefix = "lbl:"
        max_name = 64 - len(cb_prefix)
        cb = cb_prefix + lbl[:max_name]
        buttons.append([{"text": display, "callback_data": cb}])
    # Control row
    buttons.append([
        {"text": "✅ Done", "callback_data": "lbl_done"},
        {"text": "⏭ Skip (no labels)", "callback_data": "lbl_skip"},
    ])
    return {"inline_keyboard": buttons}


def _build_mr_action_keyboard(pid: int | str, iid: int | str,
                              mr_url: str | None = None) -> dict:
    """Merge / Close buttons for a held-MR pause message.

    callback_data is fully stateless ('mr:<action>:<pid>:<iid>') so the buttons
    keep working across bot restarts and regardless of who taps them.
    """
    rows: list[list[dict]] = [[
        {"text": "✅ Merge MR", "callback_data": f"mr:merge:{pid}:{iid}"},
        {"text": "❌ Close MR", "callback_data": f"mr:close:{pid}:{iid}"},
    ]]
    if mr_url:
        rows.append([{"text": f"🔗 Open MR !{iid}", "url": mr_url}])
    return {"inline_keyboard": rows}


def _build_mr_confirm_keyboard(action: str, pid: int | str, iid: int | str) -> dict:
    """One-tap confirm row shown after the first Merge/Close tap."""
    if action == "merge":
        yes = {"text": f"✅ Yes, merge !{iid}", "callback_data": f"mr:merge!:{pid}:{iid}"}
    else:
        yes = {"text": f"🚫 Yes, close !{iid}", "callback_data": f"mr:close!:{pid}:{iid}"}
    no = {"text": "↩️ Cancel", "callback_data": f"mr:cancel:{pid}:{iid}"}
    return {"inline_keyboard": [[yes, no]]}


# ---------------------------------------------------------------------------
# HTML escaping helper
# ---------------------------------------------------------------------------

def esc(text: str) -> str:
    """Escape text for safe inclusion in HTML parse_mode messages."""
    return html.escape(str(text), quote=False)


# ---------------------------------------------------------------------------
# Conversation state helpers
# ---------------------------------------------------------------------------

def _state_key(chat_id: int, user_id: int) -> tuple[int, int]:
    return (chat_id, user_id)


def _get_state(chat_id: int, user_id: int) -> dict | None:
    return _user_states.get(_state_key(chat_id, user_id))


def _set_state(chat_id: int, user_id: int, state: dict) -> None:
    _user_states[_state_key(chat_id, user_id)] = state


def _clear_state(chat_id: int, user_id: int) -> None:
    _user_states.pop(_state_key(chat_id, user_id), None)


# ---------------------------------------------------------------------------
# Update handlers
# ---------------------------------------------------------------------------

def handle_message(msg: dict) -> None:
    chat_id: int = msg["chat"]["id"]
    user_id: int = msg["from"]["id"]
    is_bot: bool = msg["from"].get("is_bot", False)
    text: str = msg.get("text") or ""

    # Drop messages from the bot itself
    if is_bot:
        return

    # Access control
    if str(chat_id) != str(TELEGRAM_CHAT_ID):
        log.debug("Ignoring message from unauthorized chat %s", chat_id)
        return

    # /cancel at any time
    if text.strip().lower().startswith("/cancel"):
        state = _get_state(chat_id, user_id)
        if state:
            _clear_state(chat_id, user_id)
            send_message(chat_id, "Issue creation cancelled.")
        else:
            send_message(chat_id, "No active /issue flow to cancel.")
        return

    # /issue command — start a new flow
    if text.strip().lower().startswith("/issue"):
        # Clear any existing flow for this user first
        _clear_state(chat_id, user_id)
        _cmd_start_issue(chat_id, user_id)
        return

    # /merge command — trigger gated MR merge via cto-accept-mr.sh
    if re.match(r"/merge(?:@\w+)?(?:\s|$)", text.strip(), re.IGNORECASE):
        _cmd_merge(chat_id, msg)
        return

    # /stop command — stop the issue loop (set the manual stop flag)
    if re.match(r"/stop(?:@\w+)?(?:\s|$)", text.strip(), re.IGNORECASE):
        _cmd_stop_loop(chat_id)
        return

    # /start command — start the issue loop (clear the flag + kick a pass)
    if re.match(r"/start(?:@\w+)?(?:\s|$)", text.strip(), re.IGNORECASE):
        _cmd_start_loop(chat_id)
        return

    # /status command — report current work state of the agent team
    if re.match(r"/status(?:@\w+)?(?:\s|$)", text.strip(), re.IGNORECASE):
        _cmd_status(chat_id)
        return

    # Free-text handling for active flow
    state = _get_state(chat_id, user_id)
    if state is None:
        return  # No active flow; silently ignore

    step = state.get("step")

    if step == "title":
        title = text.strip()
        if not title:
            send_message(chat_id, "Title cannot be empty.  Please enter a title for the issue.")
            return
        state["title"] = title
        state["step"] = "description"
        _set_state(chat_id, user_id, state)
        send_message(
            chat_id,
            "Enter a description for the issue, or send <code>/skip</code> to leave it empty.",
            reply_markup={"force_reply": True, "input_field_placeholder": "Issue description (or /skip)", "selective": True},
        )

    elif step == "description":
        if text.strip().lower() == "/skip":
            description = ""
        else:
            description = text.strip()
        _cmd_create_issue(chat_id, user_id, state, description)

    else:
        # A message arrived but we are in a callback-driven step — politely inform.
        send_message(
            chat_id,
            "Please use the buttons above, or send /cancel to abort.",
        )


def handle_callback_query(cq: dict) -> None:
    cq_id: str = cq["id"]
    chat_id: int = cq["message"]["chat"]["id"]
    user_id: int = cq["from"]["id"]
    data: str = cq.get("data") or ""

    # Access control
    if str(chat_id) != str(TELEGRAM_CHAT_ID):
        answer_callback_query(cq_id)
        return

    # ── MR action buttons (held-MR pause messages) ─────────────────────────────
    # Stateless: everything is encoded in callback_data, so these work even after
    # a bot restart and are independent of any /issue conversation state.
    if data.startswith("mr:"):
        _handle_mr_callback(cq_id, chat_id, cq.get("message", {}), data)
        return

    state = _get_state(chat_id, user_id)

    # ── Project selection ──────────────────────────────────────────────────────
    if data.startswith("proj:"):
        if state is None or state.get("step") != "project":
            answer_callback_query(cq_id, "That button is no longer active.  Use /issue to start over.")
            return
        project_id_str = data[len("proj:"):]
        try:
            project_id = int(project_id_str)
        except ValueError:
            answer_callback_query(cq_id, "Invalid project data.")
            return

        # Find the matching project name
        projects: list[dict] = state.get("_projects", [])
        project_path = next(
            (p.get("path") or p.get("name") for p in projects if p.get("id") == project_id),
            f"project:{project_id}",
        )
        answer_callback_query(cq_id, f"Selected: {project_path}")

        # Fetch labels for this project
        try:
            labels = fetch_labels(project_id)
        except Exception as exc:
            log.error("Failed to fetch labels for project %s: %s", project_id, exc)
            send_message(chat_id, f"Failed to fetch labels: {esc(str(exc))}\nUse /cancel to abort.")
            return

        state["project_id"] = project_id
        state["project_path"] = project_path
        state["labels"] = labels
        state["selected_labels"] = []

        if not labels:
            # No labels — skip straight to title step
            state["step"] = "title"
            state.pop("_projects", None)
            _set_state(chat_id, user_id, state)
            send_message(
                chat_id,
                f"Project <b>{esc(project_path)}</b> has no labels.  "
                "Enter a <b>title</b> for the issue:",
                reply_markup={"force_reply": True, "input_field_placeholder": "Issue title", "selective": True},
            )
            return

        state["step"] = "labels"
        state.pop("_projects", None)
        _set_state(chat_id, user_id, state)

        keyboard = _build_label_keyboard(labels, [])
        resp = send_message(
            chat_id,
            f"Project: <b>{esc(project_path)}</b>\n"
            "Select labels (tap to toggle, then tap <b>Done</b>):",
            reply_markup=keyboard,
        )
        # Store the message id so we can edit the markup in-place on toggles
        state["label_msg_id"] = resp.get("result", {}).get("message_id")
        _set_state(chat_id, user_id, state)
        return

    # ── Label toggles ──────────────────────────────────────────────────────────
    if data.startswith("lbl:"):
        if state is None or state.get("step") != "labels":
            answer_callback_query(cq_id, "That button is no longer active.")
            return
        label_raw = data[len("lbl:"):]
        # Match against the full label names (handles names truncated in callback_data)
        available = state.get("labels", [])
        matched = next((l for l in available if l.startswith(label_raw) or label_raw.startswith(l)), None)
        # Prefer exact match
        exact = label_raw if label_raw in available else None
        label_name = exact or matched
        if label_name is None:
            answer_callback_query(cq_id, "Unknown label.")
            return
        selected = state.get("selected_labels", [])
        if label_name in selected:
            selected.remove(label_name)
            answer_callback_query(cq_id, f"Deselected: {label_name}")
        else:
            selected.append(label_name)
            answer_callback_query(cq_id, f"Selected: {label_name}")
        state["selected_labels"] = selected
        _set_state(chat_id, user_id, state)

        # Edit the keyboard in-place to show updated checkmarks
        new_keyboard = _build_label_keyboard(available, selected)
        msg_id = state.get("label_msg_id")
        if msg_id:
            try:
                edit_reply_markup(chat_id, msg_id, new_keyboard)
            except Exception as exc:
                log.warning("Could not edit label keyboard: %s", exc)
        return

    # ── Labels done / skip ─────────────────────────────────────────────────────
    if data in ("lbl_done", "lbl_skip"):
        if state is None or state.get("step") != "labels":
            answer_callback_query(cq_id, "That button is no longer active.")
            return
        answer_callback_query(cq_id)
        if data == "lbl_skip":
            state["selected_labels"] = []
        state["step"] = "title"
        _set_state(chat_id, user_id, state)
        selected = state.get("selected_labels", [])
        label_summary = (
            ", ".join(esc(l) for l in selected) if selected else "<i>none</i>"
        )
        send_message(
            chat_id,
            f"Labels: {label_summary}\n\nEnter the <b>issue title</b>:",
            reply_markup={"force_reply": True, "input_field_placeholder": "Issue title", "selective": True},
        )
        return

    # Unrecognised callback data — answer to clear the spinner
    answer_callback_query(cq_id)


# ---------------------------------------------------------------------------
# Command actions
# ---------------------------------------------------------------------------

def _cmd_start_issue(chat_id: int, user_id: int) -> None:
    """Start the /issue conversation: fetch projects and show the selection keyboard."""
    send_message(chat_id, "Fetching projects from GitLab...")
    try:
        projects = fetch_projects()
    except Exception as exc:
        log.error("Failed to fetch projects: %s", exc)
        send_message(chat_id, f"Failed to fetch projects: {esc(str(exc))}")
        return

    if not projects:
        send_message(chat_id, f"No projects found in the <code>{GITLAB_GROUP}</code> group.")
        return

    # Store projects temporarily in state so the callback handler can look up names
    _set_state(chat_id, user_id, {
        "step": "project",
        "_projects": projects,
    })

    keyboard = _build_project_keyboard(projects)
    send_message(chat_id, "Which <b>project</b> should the issue go into?", reply_markup=keyboard)


def _cmd_create_issue(chat_id: int, user_id: int, state: dict, description: str) -> None:
    """Final step: POST the issue to GitLab and report the result."""
    project_id: int = state["project_id"]
    project_path: str = state.get("project_path", f"project:{project_id}")
    title: str = state["title"]
    selected_labels: list[str] = state.get("selected_labels", [])

    _clear_state(chat_id, user_id)

    # Construct the payload (for logging / dry-run visibility)
    payload_preview = {
        "project_id": project_id,
        "title": title,
        "description": description,
        "labels": ",".join(selected_labels),
    }
    log.info("Creating issue — payload: %s", json.dumps(payload_preview))

    send_message(chat_id, "Creating issue in GitLab...")
    try:
        result = create_issue(project_id, title, description, selected_labels)
    except Exception as exc:
        log.error("Failed to create issue: %s", exc)
        send_message(
            chat_id,
            f"Failed to create issue: {esc(str(exc))}\n"
            "Your flow has been reset.  Use /issue to try again.",
        )
        return

    iid = result.get("iid") or result.get("id")
    web_url = result.get("web_url", "")
    send_message(
        chat_id,
        f"✅ Issue <b>#{iid}</b> created in <b>{esc(project_path)}</b>:\n"
        f"{esc(title)}\n"
        f"{web_url}",
    )


# ---------------------------------------------------------------------------
# /merge helpers
# ---------------------------------------------------------------------------

# Matches: https://gitlab.com/<path>/-/merge_requests/<iid>
_MR_URL_RE = re.compile(
    r"https?://gitlab\.com/(?P<path>[^\s]+?)/-/merge_requests/(?P<iid>\d+)",
    re.IGNORECASE,
)


def _find_mr_url(text: str) -> tuple[str, int] | None:
    """Return (project_path, iid) for the first GitLab MR URL in text, or None."""
    m = _MR_URL_RE.search(text or "")
    if m:
        return m.group("path"), int(m.group("iid"))
    return None


def _resolve_project_id(project_path: str) -> int:
    """Look up the numeric GitLab project id by URL-encoded path."""
    encoded = urllib.parse.quote(project_path, safe="")
    url = f"{GITLAB_API}/projects/{encoded}"
    data = _http_get(url, headers=_gitlab_headers())
    if not isinstance(data, dict) or "id" not in data:
        raise RuntimeError(f"Unexpected response from GitLab projects API: {data!r}")
    return int(data["id"])


def _fetch_mr(pid: int, iid: int) -> dict:
    """Fetch a single MR by project numeric id and MR iid."""
    url = f"{GITLAB_API}/projects/{pid}/merge_requests/{iid}"
    data = _http_get(url, headers=_gitlab_headers())
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected MR response: {data!r}")
    return data


# ---------------------------------------------------------------------------
# /status — report the live work state of the agent team
# ---------------------------------------------------------------------------

# Workflow labels reported in the board breakdown (mirror issue-loop.sh columns).
_STATUS_LABELS = ["in-progress", "todo", "roadmap", "pipeline-failed", "failed"]


def _read_kv_file(path: pathlib.Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE file (issue-loop.sh state/pause markers)."""
    out: dict[str, str] = {}
    try:
        for raw in path.read_text().splitlines():
            key, sep, val = raw.partition("=")
            if sep:
                out[key.strip()] = val.strip()
    except OSError:
        pass
    return out


def _pgrep_cmdline(needle: str) -> list[int]:
    """Return PIDs whose /proc/<pid>/cmdline contains needle (best-effort)."""
    pids: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return pids
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if needle in cmd:
            pids.append(int(entry))
    return pids


def _ago(iso_str: str) -> str:
    """Human 'Xm ago' for an ISO-8601 timestamp; '' if it can't be parsed."""
    if not iso_str:
        return ""
    try:
        started = datetime.datetime.fromisoformat(iso_str)
        if started.tzinfo is None:
            started = started.replace(tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        secs = int((now - started).total_seconds())
    except (ValueError, TypeError):
        return ""
    if secs < 0:
        secs = 0
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    return f"{secs // 3600}h {(secs % 3600) // 60}m ago"


def _repo_from_url(web_url: str) -> str:
    """Extract the repo slug from a GitLab issue web_url, '' if not matched."""
    m = re.search(r"gitlab\.com/[^/]+/([^/]+)/-/(?:issues|work_items)", web_url or "")
    return m.group(1) if m else ""


def _fetch_open_group_issues() -> list[dict]:
    """All open issues across the GitLab group (label-counting source)."""
    url = f"{GITLAB_API}/groups/{GITLAB_GROUP}/issues?state=opened&per_page=100"
    data = _http_get(url, headers=_gitlab_headers())
    return data if isinstance(data, list) else []


def _cmd_status(chat_id: int) -> None:
    """Handle /status — report loop run-state, the active issue, and the board."""
    lines: list[str] = ["📊 <b>Work status</b>"]

    # ── 1. Loop run-state (stop flag > pause marker > running) ────────────────
    stopped = _ISSUE_LOOP_STOP_FILE.exists()
    pause = _read_kv_file(_ISSUE_LOOP_PAUSE_FILE) if _ISSUE_LOOP_PAUSE_FILE.exists() else {}
    loop_pids = _pgrep_cmdline("issue-loop.sh")
    worker_pids = _pgrep_cmdline("agent issue-worker")

    if stopped:
        lines.append("Loop: ⏹️ <b>Stopped</b> (manual /stop) — send /start to resume.")
    elif pause.get("TYPE") == "session-limit":
        reset = pause.get("RESET_STR", "?")
        lines.append(f"Loop: ⏸️ <b>Paused</b> — Claude session limit, resumes at {esc(reset)}.")
    elif pause.get("TYPE") == "mr" or (pause and "MR_IID" in pause):
        mr_iid = pause.get("MR_IID", "?")
        mr_url = pause.get("MR_URL", "")
        suffix = f"\n🔗 {esc(mr_url)}" if mr_url else ""
        lines.append(f"Loop: ⏸️ <b>Paused</b> — waiting for human to resolve MR !{esc(mr_iid)}."
                     f" Reply /merge to it.{suffix}")
    elif loop_pids:
        lines.append("Loop: ▶️ <b>Running</b> — a pass is active now.")
    else:
        lines.append("Loop: ▶️ <b>Running</b> — idle between cron ticks (every ~1 min).")

    # ── 2. Active issue (from the loop state marker) ──────────────────────────
    issues: list[dict] = []
    api_error = ""
    try:
        issues = _fetch_open_group_issues()
    except Exception as exc:
        api_error = str(exc)
        log.warning("status: could not fetch group issues: %s", exc)

    state = _read_kv_file(_ISSUE_LOOP_STATE_FILE) if _ISSUE_LOOP_STATE_FILE.exists() else {}
    lines.append("")
    if state.get("IID"):
        s_pid = state.get("PID", "")
        s_iid = state.get("IID", "")
        started = _ago(state.get("STARTED", ""))
        match = next((i for i in issues
                      if str(i.get("project_id")) == s_pid and str(i.get("iid")) == s_iid), None)
        title = match.get("title", "") if match else ""
        repo = _repo_from_url(match.get("web_url", "")) if match else ""
        worker = "🟢 coding" if worker_pids else "⚪ between steps (pipeline/MR)"
        head = f"🔧 <b>Working now:</b> #{esc(s_iid)}"
        if repo:
            head += f" · {esc(repo)}"
        lines.append(head)
        if title:
            lines.append(f"   {esc(title)}")
        meta = []
        if started:
            meta.append(f"started {started}")
        meta.append(f"agent: {worker}")
        lines.append(f"   {' · '.join(meta)}")
    else:
        lines.append("🔧 <b>Working now:</b> nothing in flight (no active issue marker).")

    # ── 3. Board breakdown + in-progress list ─────────────────────────────────
    lines.append("")
    if api_error:
        lines.append(f"⚠️ Board unavailable (GitLab API error): {esc(api_error)}")
    else:
        counts = {lbl: 0 for lbl in _STATUS_LABELS}
        in_progress: list[dict] = []
        for issue in issues:
            labels = issue.get("labels", []) or []
            for lbl in _STATUS_LABELS:
                if lbl in labels:
                    counts[lbl] += 1
            if "in-progress" in labels:
                in_progress.append(issue)
        lines.append("<b>Board (open):</b>")
        lines.append("   " + " · ".join(f"{lbl} {counts[lbl]}" for lbl in _STATUS_LABELS))

        if in_progress:
            lines.append("")
            lines.append("<b>In progress:</b>")
            for issue in in_progress[:10]:
                iid = issue.get("iid", "?")
                title = issue.get("title", "")
                repo = _repo_from_url(issue.get("web_url", ""))
                tag = f" · {esc(repo)}" if repo else ""
                lines.append(f"   • #{esc(iid)}{tag} — {esc(title)}")

    send_message(chat_id, "\n".join(lines))


# ---------------------------------------------------------------------------
# /stop and /start — control the cron-driven issue loop via the shared stop flag
# ---------------------------------------------------------------------------

def _cmd_stop_loop(chat_id: int) -> None:
    """Handle /stop — set the manual stop flag so issue-loop.sh stops picking up work."""
    already = _ISSUE_LOOP_STOP_FILE.exists()
    try:
        _ISSUE_LOOP_STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
        _ISSUE_LOOP_STOP_FILE.write_text(
            f"stopped via Telegram /stop at {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n"
        )
    except Exception as exc:
        log.error("stop: could not write stop flag %s: %s", _ISSUE_LOOP_STOP_FILE, exc)
        send_message(chat_id, f"⚠️ Could not stop the loop: {esc(str(exc))}")
        return

    log.info("Issue loop STOP flag set via /stop (already=%s)", already)
    if already:
        send_message(chat_id,
                     "⏹️ Issue loop was already stopped — it stays stopped until <code>/start</code>.")
    else:
        send_message(chat_id,
                     "⏹️ <b>Issue loop stopped.</b>\n"
                     "No new issues will be picked up. Any issue already in flight finishes first.\n"
                     "Send <code>/start</code> to resume.")


def _cmd_start_loop(chat_id: int) -> None:
    """Handle /start — clear the manual stop flag and kick off an issue-loop pass."""
    was_stopped = _ISSUE_LOOP_STOP_FILE.exists()
    try:
        _ISSUE_LOOP_STOP_FILE.unlink(missing_ok=True)
    except Exception as exc:
        log.error("start: could not remove stop flag %s: %s", _ISSUE_LOOP_STOP_FILE, exc)
        send_message(chat_id, f"⚠️ Could not start the loop: {esc(str(exc))}")
        return

    # Kick off a pass now rather than waiting for the next cron tick. The loop is
    # flock-guarded, so if one is already running this invocation just exits cleanly.
    script_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(script_dir, "issue-loop.sh")
    try:
        subprocess.Popen(
            ["bash", script_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        kicked = " A pass was kicked off now"
    except Exception as exc:
        log.warning("start: could not launch issue-loop.sh immediately: %s", exc)
        kicked = " It will start on the next cron tick (within ~1 min)"

    log.info("Issue loop START flag cleared via /start (was_stopped=%s)", was_stopped)
    if was_stopped:
        send_message(chat_id, f"▶️ <b>Issue loop started.</b>{kicked}.")
    else:
        send_message(chat_id, f"▶️ Issue loop was already running.{kicked}.")


def _cmd_merge(chat_id: int, msg: dict) -> None:
    """Handle /merge [<url>] — shell out to cto-accept-mr.sh and report the result."""
    text: str = msg.get("text") or ""

    # Strip the command token (handle /merge@botname mention suffix)
    cmd_match = re.match(r"/merge(?:@\w+)?(?:\s+(?P<rest>.+))?$", text.strip(), re.IGNORECASE)
    arg_rest = (cmd_match.group("rest") or "").strip() if cmd_match else ""

    # ── 1. Resolve the MR URL (arg → reply → usage) ─────────────────────────
    mr_ref: tuple[str, int] | None = None
    multiple_note: str = ""

    if arg_rest:
        mr_ref = _find_mr_url(arg_rest)
        if mr_ref is None:
            # Treat the whole rest as a bare URL attempt; give friendly error
            send_message(chat_id, "Could not parse a GitLab MR URL from that argument.\n"
                         "Usage: reply /merge to a held-MR message, or /merge &lt;MR url&gt;.")
            return
    else:
        replied = msg.get("reply_to_message")
        if replied:
            search_text = (replied.get("text") or "") + " " + (replied.get("caption") or "")
            # Check for multiple matches
            all_matches = list(_MR_URL_RE.finditer(search_text))
            if all_matches:
                first = all_matches[0]
                mr_ref = (first.group("path"), int(first.group("iid")))
                if len(all_matches) > 1:
                    multiple_note = f" (found {len(all_matches)} MR URLs — using the first)"
            else:
                send_message(chat_id,
                             "No GitLab MR URL found in the replied-to message.\n"
                             "Reply /merge to a message containing an MR link, "
                             "or use /merge &lt;MR url&gt;.")
                return
        else:
            send_message(chat_id,
                         "Reply /merge to a message containing an MR link, "
                         "or use /merge &lt;MR url&gt;.")
            return

    project_path, iid = mr_ref

    # ── 2. Resolve numeric project id ────────────────────────────────────────
    try:
        numeric_pid = _resolve_project_id(project_path)
    except Exception as exc:
        log.error("merge: could not resolve project id for %r: %s", project_path, exc)
        send_message(chat_id, f"Could not resolve project <code>{esc(project_path)}</code>: {esc(str(exc))}")
        return

    # ── 3+. Run the gated merge (shared with the button-driven path) ───────────
    _run_merge(chat_id, numeric_pid, iid, project_path, multiple_note)


def _run_merge(chat_id: int, numeric_pid: int, iid: int,
               project_label: str, multiple_note: str = "") -> None:
    """Run the gated merge script for a resolved MR and report the result.

    Shared by the /merge text command and the inline 'Merge MR' button.
    """
    # ── Optimistic progress reply ──────────────────────────────────────────────
    send_message(chat_id, f"⏳ Merging MR !{iid} in {esc(project_label)}{esc(multiple_note)} …")

    # ── Shell out to the gated merge script ────────────────────────────────────
    script_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(script_dir, "cto-accept-mr.sh")
    log.info("merge: running %s %s %s", script_path, numeric_pid, iid)

    try:
        result = subprocess.run(
            ["bash", script_path, str(numeric_pid), str(iid)],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        log.error("merge: cto-accept-mr.sh timed out for MR !%s", iid)
        send_message(chat_id, f"❌ MR !{iid} — merge script timed out after 120 s.")
        return
    except Exception as exc:
        log.error("merge: failed to run cto-accept-mr.sh: %s", exc)
        send_message(chat_id, f"❌ MR !{iid} — could not run merge script: {esc(str(exc))}")
        return

    exit_code = result.returncode
    # Combine stderr + stdout for a reason line; prefer stderr (script writes there)
    raw_output = (result.stderr or result.stdout or "").strip()
    # Grab the first non-empty output line as the reason
    reason_line = next((ln.strip() for ln in raw_output.splitlines() if ln.strip()), raw_output)

    log.info("merge: cto-accept-mr.sh exit=%s, output=%r", exit_code, raw_output[:400])

    # ── 5. Report result ──────────────────────────────────────────────────────
    if exit_code == 0:
        # Verify merge state via re-fetch
        try:
            mr_data = _fetch_mr(numeric_pid, iid)
            mr_state = mr_data.get("state", "")
            mr_title = mr_data.get("title", "")
            web_url = mr_data.get("web_url", "")
            if mr_state == "merged":
                send_message(
                    chat_id,
                    f"✅ MR !{iid} merged into main\n"
                    f"{esc(mr_title)}\n"
                    f"{web_url}",
                )
            else:
                send_message(
                    chat_id,
                    f"⚠️ Script exited 0 but MR !{iid} state is <code>{esc(mr_state)}</code> — please verify.",
                )
        except Exception as exc:
            log.warning("merge: could not re-fetch MR after success: %s", exc)
            send_message(chat_id, f"✅ MR !{iid} — merge script reported success (could not re-verify state).")

    elif exit_code == 2:
        # CTO_AUTO_ACCEPT_MR is OFF
        send_message(chat_id, f"⛔ Merge power is OFF (CTO_AUTO_ACCEPT_MR). MR !{iid} not merged.")

    else:
        # exit 1 — a gate failed; relay the reason from the script
        send_message(
            chat_id,
            f"❌ MR !{iid} not merged — {esc(reason_line)}",
        )


def _run_close_mr(chat_id: int, numeric_pid: int, iid: int) -> None:
    """Close a merge request via the GitLab API and report the result."""
    send_message(chat_id, f"⏳ Closing MR !{iid} …")
    url = f"{GITLAB_API}/projects/{numeric_pid}/merge_requests/{iid}"
    try:
        data = _http_put(url, {"state_event": "close"}, headers=_gitlab_headers())
    except Exception as exc:
        log.error("close: failed to close MR !%s in project %s: %s", iid, numeric_pid, exc)
        send_message(chat_id, f"❌ MR !{iid} — could not close: {esc(str(exc))}")
        return

    state = data.get("state", "") if isinstance(data, dict) else ""
    title = data.get("title", "") if isinstance(data, dict) else ""
    web_url = data.get("web_url", "") if isinstance(data, dict) else ""
    if state == "closed":
        send_message(chat_id, f"🚫 MR !{iid} closed\n{esc(title)}\n{web_url}")
    else:
        send_message(
            chat_id,
            f"⚠️ Close requested but MR !{iid} state is <code>{esc(state)}</code> — please verify.",
        )


def _handle_mr_callback(cq_id: str, chat_id: int, message: dict, data: str) -> None:
    """Handle the held-MR action buttons: merge / close with a confirm step.

    callback_data shapes (all 'mr:<action>:<pid>:<iid>'):
      mr:merge   / mr:close   — first tap → swap to a confirm row
      mr:merge!  / mr:close!   — confirmed → run the action
      mr:cancel                — restore the original Merge/Close row
    """
    parts = data.split(":")
    if len(parts) != 4:
        answer_callback_query(cq_id, "Malformed action.")
        return
    _, action, pid_s, iid_s = parts
    try:
        pid = int(pid_s)
        iid = int(iid_s)
    except ValueError:
        answer_callback_query(cq_id, "Malformed action data.")
        return

    msg_id = message.get("message_id")

    # First tap on Merge/Close → ask for confirmation in-place.
    if action in ("merge", "close"):
        answer_callback_query(cq_id)
        if msg_id:
            try:
                edit_reply_markup(chat_id, msg_id, _build_mr_confirm_keyboard(action, pid, iid))
            except Exception as exc:
                log.warning("Could not show MR confirm keyboard: %s", exc)
        return

    # Cancel → restore the original action row.
    if action == "cancel":
        answer_callback_query(cq_id, "Cancelled.")
        if msg_id:
            try:
                edit_reply_markup(chat_id, msg_id, _build_mr_action_keyboard(pid, iid))
            except Exception as exc:
                log.warning("Could not restore MR action keyboard: %s", exc)
        return

    # Confirmed actions → drop the keyboard (prevents double-taps) and run.
    if action in ("merge!", "close!"):
        answer_callback_query(cq_id, "Merging…" if action == "merge!" else "Closing…")
        if msg_id:
            try:
                edit_reply_markup(chat_id, msg_id, {"inline_keyboard": []})
            except Exception as exc:
                log.warning("Could not clear MR keyboard: %s", exc)
        if action == "merge!":
            _run_merge(chat_id, pid, iid, f"project {pid}")
        else:
            _run_close_mr(chat_id, pid, iid)
        return

    answer_callback_query(cq_id)


# ---------------------------------------------------------------------------
# Main poll loop
# ---------------------------------------------------------------------------

def run() -> None:
    if not TELEGRAM_TOKEN:
        log.error("TELEGRAM_TOKEN is not set.  Export it or put it in scripts/.env")
        sys.exit(1)
    if not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_CHAT_ID is not set.  Export it or put it in scripts/.env")
        sys.exit(1)
    if not GITLAB_TOKEN:
        log.error("GITLAB_TOKEN is not set.  Export it or put it in scripts/.env")
        sys.exit(1)

    # Validate token and log the bot username
    try:
        me = get_me()
        if not me.get("ok"):
            log.error("getMe failed: %s", me)
            sys.exit(1)
        bot_username = me["result"]["username"]
        bot_id = me["result"]["id"]
        log.info("Bot: @%s (id=%s)", bot_username, bot_id)
    except Exception as exc:
        log.error("Failed to call getMe: %s", exc)
        sys.exit(1)

    # Register the slash-command menu (best-effort — non-fatal if it fails).
    try:
        set_my_commands()
        log.info("Registered bot command menu (issue, status, merge, stop, start, cancel).")
    except Exception as exc:
        log.warning("Could not register bot commands: %s", exc)

    offset = load_offset()
    log.info("Starting long-poll loop (offset=%s, chat_id=%s)", offset, TELEGRAM_CHAT_ID)

    # Graceful shutdown on SIGTERM / SIGINT
    _running = [True]

    def _shutdown(sig, _frame):
        log.info("Signal %s received — shutting down.", sig)
        _running[0] = False

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while _running[0]:
        try:
            updates = get_updates(offset, timeout=30)
        except KeyboardInterrupt:
            log.info("Interrupted — exiting.")
            break
        except Exception as exc:
            log.error("getUpdates error: %s — backing off 5s", exc)
            time.sleep(5)
            continue

        for update in updates:
            update_id: int = update["update_id"]
            # Advance offset immediately so a restart doesn't replay this update
            new_offset = update_id + 1
            if offset is None or new_offset > offset:
                offset = new_offset
                save_offset(offset)

            try:
                _dispatch(update, bot_id)
            except Exception as exc:
                log.error("Unhandled error processing update %s: %s", update_id, exc)
                # Continue processing remaining updates regardless

    log.info("Bot stopped.")


def _dispatch(update: dict, bot_id: int) -> None:
    """Route a single update to the appropriate handler."""
    if "message" in update:
        msg = update["message"]
        # Ignore messages from the bot itself
        if msg.get("from", {}).get("id") == bot_id:
            return
        handle_message(msg)

    elif "callback_query" in update:
        cq = update["callback_query"]
        # Always answer callback queries to clear the spinner
        handle_callback_query(cq)


# ---------------------------------------------------------------------------
# Smoke-test helpers (called from validation, not the daemon loop)
# ---------------------------------------------------------------------------

def smoke_test() -> None:
    """Print getMe, project list, and first project's labels.  Does NOT start the daemon."""
    print("=== getMe ===")
    me = get_me()
    if me.get("ok"):
        u = me["result"]
        print(f"  Bot: @{u['username']} (id={u['id']})")
    else:
        print(f"  ERROR: {me}")
        sys.exit(1)

    print(f"\n=== Projects (/groups/{GITLAB_GROUP}/projects) ===")
    projects = fetch_projects()
    for p in projects:
        print(f"  [{p['id']}] {p.get('path_with_namespace') or p.get('path')}")

    if projects:
        first = projects[0]
        pid = first["id"]
        pname = first.get("path") or first.get("name")
        print(f"\n=== Labels for project '{pname}' (id={pid}) ===")
        labels = fetch_labels(pid)
        if labels:
            for lbl in labels:
                print(f"  - {lbl}")
        else:
            print("  (no labels)")

    print("\n=== Dry-run issue payload (NOT posted) ===")
    if projects:
        first = projects[0]
        pid = first["id"]
        pname = first.get("path") or first.get("name")
        labels = fetch_labels(pid)
        sample_payload = {
            "project_id": pid,
            "project_path": pname,
            "title": "Sample issue title",
            "description": "Sample description",
            "labels": ",".join(labels[:2]) if labels else "",
        }
        print("  Would POST:", json.dumps(sample_payload, indent=4))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--smoke-test":
        smoke_test()
    else:
        run()
