/* Claude Dev Team control panel — vanilla SPA, no build step, no CDN. */
'use strict';

const State = { csrf: '', user: '', view: 'overview', status: null, timer: null };

/* ── tiny DOM helpers ───────────────────────────────────────────────────── */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function el(tag, attrs = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (k === 'value') node.value = v;
    else if (v === true) node.setAttribute(k, '');
    else node.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

const fmtBytes = (n) => n < 1024 ? `${n} B`
  : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`;

function ago(ts) {
  if (!ts) return '—';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
const agoIso = (iso) => iso ? ago(Math.floor(new Date(iso).getTime() / 1000)) : '—';

function toast(message, kind = 'ok', ms = 4200) {
  const node = el('div', { class: `toast ${kind}` }, message);
  $('#toasts').append(node);
  setTimeout(() => { node.style.opacity = '0'; setTimeout(() => node.remove(), 250); }, ms);
}

function confirmDialog(title, body, okLabel = 'Confirm') {
  return new Promise((resolve) => {
    const modal = $('#modal');
    $('#modal-title').textContent = title;
    $('#modal-body').textContent = body;
    $('#modal-ok').textContent = okLabel;
    modal.hidden = false;
    const done = (val) => {
      modal.hidden = true;
      $('#modal-ok').onclick = null; $('#modal-cancel').onclick = null;
      resolve(val);
    };
    $('#modal-ok').onclick = () => done(true);
    $('#modal-cancel').onclick = () => done(false);
  });
}

async function busy(button, fn) {
  if (button) button.classList.add('busy');
  try { return await fn(); }
  finally { if (button) button.classList.remove('busy'); }
}

/* ── API ────────────────────────────────────────────────────────────────── */
async function api(path, { method = 'GET', body } = {}) {
  const opts = { method, headers: {}, credentials: 'same-origin' };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  if (method !== 'GET') opts.headers['X-CSRF-Token'] = State.csrf;
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch { /* empty body */ }
  if (res.status === 401) { showLogin(); throw new Error('Session expired — sign in again.'); }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

const post = (path, body) => api(path, { method: 'POST', body });

/* Run an action, toast the outcome, refresh the view. */
async function action(button, fn, okMessage) {
  try {
    const out = await busy(button, fn);
    toast(okMessage || out.message || 'Done.', 'ok');
    return out;
  } catch (err) {
    toast(err.message, 'bad', 7000);
    throw err;
  }
}

/* ── auth ───────────────────────────────────────────────────────────────── */
function showLogin(message) {
  clearInterval(State.timer);
  $('#app').hidden = true;
  $('#login').hidden = false;
  if (message) { $('#login-error').textContent = message; $('#login-error').hidden = false; }
  $('#login-user').focus();
}

function showApp() {
  $('#login').hidden = true;
  $('#app').hidden = false;
  render();
  refreshStatus();
  clearInterval(State.timer);
  State.timer = setInterval(refreshStatus, 10000);
}

$('#login-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const err = $('#login-error');
  err.hidden = true;
  try {
    const out = await busy($('#login-btn'), () => post('/api/login', {
      user: $('#login-user').value, password: $('#login-pass').value,
    }));
    State.csrf = out.csrf; State.user = out.user;
    $('#login-pass').value = '';
    showApp();
  } catch (e) {
    err.textContent = e.message; err.hidden = false;
  }
});

$('#logout').addEventListener('click', async () => {
  await post('/api/logout', {}).catch(() => {});
  State.csrf = '';
  showLogin('Signed out.');
});

/* ── status bar ─────────────────────────────────────────────────────────── */
async function refreshStatus() {
  try {
    State.status = await api('/api/status');
  } catch { return; }
  const s = State.status;
  const loop = s.loop.mode;
  const cls = loop === 'running' ? 'ok' : loop === 'paused' ? 'warn'
    : loop === 'stopped' ? 'bad' : '';
  $('#side-status').replaceChildren(
    el('span', { class: `dot ${cls}` }),
    el('span', {}, `loop ${loop} · bot ${s.bot.running ? 'up' : 'down'}`));
  $('#brand-sub').textContent = `${State.user || 'signed in'} · ${s.agents} roles`;
  $('#refresh-note').textContent = `updated ${new Date().toLocaleTimeString()}`;
  if (State.view === 'overview') VIEWS.overview.refresh?.();
}

/* ── navigation ─────────────────────────────────────────────────────────── */
$$('#nav button').forEach((btn) => btn.addEventListener('click', () => {
  State.view = btn.dataset.view;
  $$('#nav button').forEach((b) => b.classList.toggle('active', b === btn));
  render();
}));
$('#refresh').addEventListener('click', () => { refreshStatus(); render(); });

function render() {
  const view = VIEWS[State.view];
  $('#view-title').textContent = view.title;
  const root = $('#view');
  root.replaceChildren(el('div', { class: 'empty' }, 'Loading…'));
  Promise.resolve(view.render(root)).catch((err) => {
    root.replaceChildren(card('Error', el('div', { class: 'body' },
      el('p', { class: 'muted' }, err.message))));
  });
}

/* ── layout helpers ─────────────────────────────────────────────────────── */
function card(title, ...body) {
  const head = el('header', {}, el('h3', {}, title));
  const node = el('div', { class: 'card' }, head);
  const actions = [];
  for (const part of body.flat()) {
    if (part === null || part === undefined || part === false) continue;
    if (part.dataset && part.dataset.slot === 'header') actions.push(part);
    else node.append(part);
  }
  if (actions.length) head.append(el('div', { class: 'row gap' }, actions));
  return node;
}
const headerSlot = (...kids) => el('div', { class: 'row gap', 'data-slot': 'header' }, kids);
const body = (...kids) => el('div', { class: 'body' }, kids);

function stat(k, v, s, cls) {
  return el('div', { class: 'stat' },
    el('div', { class: 'k' }, k),
    el('div', { class: 'v' }, typeof v === 'string' ? v : v),
    s ? el('div', { class: `s ${cls || ''}` }, s) : null);
}

function table(columns, rows, renderRow) {
  if (!rows.length) return el('div', { class: 'empty' }, 'Nothing here yet.');
  return el('div', { class: 'table-wrap' },
    el('table', {},
      el('thead', {}, el('tr', {}, columns.map((c) => el('th', {}, c)))),
      el('tbody', {}, rows.map((r, i) => el('tr', {}, renderRow(r, i).map(
        (cell) => cell instanceof HTMLTableCellElement ? cell : el('td', {}, cell)))))));
}

function field(label, input, hint) {
  return el('label', { class: 'field' }, label,
    hint ? el('span', { class: 'hint' }, hint) : null, input);
}

/* ═══════════════════════════════════════════════════════════════════════════
   Views
   ═══════════════════════════════════════════════════════════════════════════ */
const VIEWS = {};

/* ── Overview ───────────────────────────────────────────────────────────── */
VIEWS.overview = {
  title: 'Overview',
  async render(root) {
    const [s, repos] = await Promise.all([
      State.status ? Promise.resolve(State.status) : api('/api/status'),
      api('/api/repos').catch(() => ({ repos: [] })),
    ]);
    State.status = s;

    const loopBadge = { running: 'ok', paused: 'warn', stopped: 'bad', idle: '' }[s.loop.mode];
    const cards = el('div', { class: 'grid c4' },
      stat('Issue loop', el('span', { class: `badge ${loopBadge}` }, s.loop.mode),
        s.loop.pids.length ? `pid ${s.loop.pids.join(', ')}` : 'no worker process'),
      stat('Telegram bot',
        el('span', { class: `badge ${s.bot.running ? 'ok' : 'bad'}` },
          s.bot.running ? 'running' : 'stopped'),
        s.bot.configured ? `chat ${s.bot.chatId || '—'}` : 'not configured'),
      stat('tmux session',
        el('span', { class: `badge ${s.tmux.running ? 'ok' : ''}` },
          s.tmux.running ? 'up' : 'down'),
        s.tmux.running ? `${s.tmux.windows.length} window(s)` : s.tmux.session),
      stat('CTO merge power',
        el('span', { class: `badge ${s.autoAcceptMr ? 'accent' : ''}` },
          s.autoAcceptMr ? 'enabled' : 'disabled'),
        'CTO_AUTO_ACCEPT_MR'));

    const warn = s.missingConfig.length
      ? card('Setup needed', body(
        el('p', { class: 'muted' },
          `These variables are not set yet: ${s.missingConfig.join(', ')}. `
          + 'The board, the bot and the loop stay inert until they are.'),
        el('div', {}, el('button', {
          class: 'btn primary small',
          onclick: () => { State.view = 'settings'; $$('#nav button').forEach(
            (b) => b.classList.toggle('active', b.dataset.view === 'settings')); render(); },
        }, 'Open settings'))))
      : null;

    const pause = Object.keys(s.loop.pause || {}).length
      ? card('Loop paused — waiting for a human', body(
        table(['Field', 'Value'], Object.entries(s.loop.pause),
          ([k, v]) => [el('code', {}, k), v]),
        el('div', { class: 'row gap' },
          el('button', {
            class: 'btn small', onclick: (e) => action(e.target,
              () => post('/api/control/loop', { action: 'clear-pause' }))
              .then(() => { refreshStatus(); render(); }),
          }, 'Clear pause marker'))))
      : null;

    const quick = card('Quick actions', body(
      el('div', { class: 'row gap wrap' },
        el('button', {
          class: 'btn small', onclick: (e) => action(e.target,
            () => post('/api/control/team', { action: 'launch' }))
            .then((o) => { showOutput(o.output); refreshStatus(); }),
        }, 'Launch tmux team'),
        el('button', {
          class: 'btn small', onclick: (e) => action(e.target,
            () => post('/api/control/team', { action: 'stop' }))
            .then((o) => { showOutput(o.output); refreshStatus(); }),
        }, 'Stop tmux team'),
        el('button', {
          class: 'btn small', onclick: (e) => action(e.target,
            () => post('/api/control/bot', { action: s.bot.running ? 'restart' : 'start' }))
            .then(() => refreshStatus()),
        }, s.bot.running ? 'Restart Telegram bot' : 'Start Telegram bot'),
        el('button', {
          class: 'btn small', onclick: (e) => action(e.target,
            () => post('/api/control/loop', { action: 'run' })).then(() => refreshStatus()),
        }, 'Run one loop pass'),
        el('button', {
          class: 'btn small', onclick: (e) => action(e.target, () => api('/api/gitlab/test'))
            .then((o) => toast(`GitLab OK — ${o.group.path}, ${o.projects.length} project(s)`)),
        }, 'Test GitLab'),
        el('button', {
          class: 'btn small', onclick: (e) => action(e.target,
            () => post('/api/telegram/test', {}))
            .then((o) => toast(`Telegram OK — @${o.bot.username}`)),
        }, 'Test Telegram')),
      el('pre', { class: 'out', id: 'quick-out', hidden: true })));

    const tools = card('Host tools', body(
      el('div', { class: 'row gap wrap' }, s.tools.map((t) =>
        el('span', { class: `badge ${t.ok ? 'ok' : 'bad'}`, title: t.path || 'not found' },
          t.name)))));

    const repoCard = card(`Repos in ${s.envFile.replace(/scripts\/\.env$/, '')} team root`, body(
      table(['Repo', 'Branch', 'Dirty files', 'Last commit'], repos.repos,
        (r) => [el('strong', {}, r.name), el('code', {}, r.branch),
          r.dirty ? el('span', { class: 'badge warn' }, r.dirty) : el('span', { class: 'muted' }, '0'),
          el('span', { class: 'muted small mono' }, r.lastCommit || '—')])));

    root.replaceChildren(cards, warn, pause, quick,
      el('div', { class: 'grid c2' }, tools, repoCard));
  },
  refresh() { if (State.view === 'overview') this.render($('#view')); },
};

function showOutput(text) {
  const pre = $('#quick-out');
  if (!pre) return;
  pre.textContent = text || '(no output)';
  pre.hidden = false;
}

/* ── Tokens & settings ──────────────────────────────────────────────────── */
VIEWS.settings = {
  title: 'Tokens & settings',
  async render(root) {
    const data = await api('/api/env');
    const groups = new Map();
    for (const row of data.rows) {
      if (!groups.has(row.group)) groups.set(row.group, []);
      groups.get(row.group).push(row);
    }
    const pending = new Map();
    const cards = [];

    for (const [group, rows] of groups) {
      const inputs = rows.map((row) => {
        let input;
        if (row.kind === 'bool') {
          input = el('select', {
            onchange: (e) => pending.set(row.key, e.target.value),
          }, ['1', '0'].map((v) => el('option', {
            value: v, selected: String(row.value) === v || (v === '0' && !row.value),
          }, v === '1' ? 'enabled (1)' : 'disabled (0)')));
        } else {
          input = el('input', {
            type: row.secret ? 'password' : 'text',
            placeholder: row.secret && row.set ? row.value : (row.set ? '' : 'not set'),
            value: row.secret ? '' : (row.value || ''),
            autocomplete: 'off', spellcheck: 'false',
            oninput: (e) => pending.set(row.key, e.target.value),
          });
        }
        const hint = row.help + (row.secret && row.set
          ? '  ·  currently set; leave blank to keep it' : '');
        return el('div', {},
          field(el('span', { class: 'row gap' },
            el('strong', { class: 'mono small' }, row.key),
            row.set ? el('span', { class: 'badge ok' }, 'set')
              : el('span', { class: 'badge' }, 'empty')), input, hint));
      });

      cards.push(card(group, body(
        el('div', { class: 'grid c2' }, inputs),
        el('div', { class: 'row gap end' },
          el('button', {
            class: 'btn primary small',
            onclick: async (e) => {
              const updates = {};
              for (const row of rows) {
                if (!pending.has(row.key)) continue;
                const val = pending.get(row.key);
                if (row.secret && val === '') continue;  // blank = keep existing
                updates[row.key] = val;
              }
              if (!Object.keys(updates).length) return toast('Nothing changed.', 'warn');
              await action(e.target, () => post('/api/env', { updates }),
                `Saved ${Object.keys(updates).join(', ')} to scripts/.env`);
              pending.clear();
              render();
              refreshStatus();
            },
          }, `Save ${group}`)))));
    }

    const tests = card('Connection tests', body(
      el('p', { class: 'muted small' },
        'Checks the saved credentials against the live APIs.'),
      el('div', { class: 'row gap wrap' },
        el('button', {
          class: 'btn small', onclick: (e) => action(e.target, () => api('/api/gitlab/test'))
            .then((o) => {
              toast(`GitLab OK — ${o.group.path}`);
              $('#settings-out').textContent = o.projects
                .map((p) => `${String(p.id).padEnd(10)} ${p.name}\n    ${p.url}`).join('\n');
              $('#settings-out').hidden = false;
            }),
        }, 'Test GitLab token'),
        el('button', {
          class: 'btn small', onclick: (e) => action(e.target, () => post('/api/telegram/test', {}))
            .then((o) => toast(`Telegram OK — @${o.bot.username} (id ${o.bot.id})`)),
        }, 'Test Telegram token')),
      el('pre', { class: 'out', id: 'settings-out', hidden: true })));

    const pw = (() => {
      const cur = el('input', { type: 'password', autocomplete: 'current-password' });
      const next = el('input', { type: 'password', autocomplete: 'new-password' });
      const again = el('input', { type: 'password', autocomplete: 'new-password' });
      return card('Web UI password', body(
        el('p', { class: 'muted small' },
          'Stored as a PBKDF2 hash in scripts/.env (WEBUI_PASSWORD_HASH). '
          + 'Changing it does not sign you out.'),
        el('div', { class: 'grid c3' },
          field('Current password', cur),
          field('New password', next, 'at least 8 characters'),
          field('Repeat new password', again)),
        el('div', { class: 'row end' },
          el('button', {
            class: 'btn primary small',
            onclick: async (e) => {
              if (next.value !== again.value) return toast('The new passwords differ.', 'bad');
              await action(e.target, () => post('/api/password',
                { current: cur.value, new: next.value }), 'Password changed.');
              cur.value = next.value = again.value = '';
            },
          }, 'Change password'))));
    })();

    root.replaceChildren(
      el('p', { class: 'muted small' },
        `Values are written to ${data.file} (chmod 600). Secrets are never sent back to the browser.`),
      ...cards, tests, pw);
  },
};

/* ── Agent roles ────────────────────────────────────────────────────────── */
const MODELS = ['opus', 'sonnet', 'haiku', 'inherit'];

VIEWS.agents = {
  title: 'Agent roles',
  selected: null,
  async render(root) {
    const data = await api('/api/agents');
    if (!this.selected || !data.agents.some((a) => a.name === this.selected)) {
      this.selected = data.agents[0]?.name || null;
    }

    const list = el('div', { class: 'list' }, data.agents.map((a) =>
      el('button', {
        class: a.name === this.selected ? 'active' : '',
        onclick: () => { this.selected = a.name; render(); },
      }, el('span', { class: 'row gap' }, a.name,
        a.model ? el('span', { class: 'badge small' }, a.model) : null),
      el('small', {}, `${a.toolCount} tools · ${fmtBytes(a.bytes)}`))));

    const left = card('Roles',
      headerSlot(el('button', {
        class: 'btn small', onclick: () => this.createRole(),
      }, '+ New')),
      el('div', { class: 'body' }, list,
        el('p', { class: 'muted small' }, data.dir)));

    const right = this.selected
      ? await this.editor(this.selected)
      : card('Editor', body(el('div', { class: 'empty' }, 'No agent roles found.')));

    root.replaceChildren(el('div', { class: 'split' }, left, right));
  },

  async createRole() {
    const name = prompt('New role name (kebab-case, e.g. dev-mobile):', '');
    if (!name) return;
    if (!/^[a-z0-9][a-z0-9-]{0,39}$/.test(name)) return toast('Use kebab-case letters, digits and dashes.', 'bad');
    try {
      await post(`/api/agents/${name}`, {
        model: 'sonnet',
        description: `${name} — describe this role`,
        tools: ['Read', 'Glob', 'Grep', 'Bash(git -C *)'],
        body: `# ${name}\n\nDescribe the role's responsibilities, its repo, and its rules.\n`,
      });
      toast(`Created ${name}.md`);
      this.selected = name;
      render();
    } catch (e) { toast(e.message, 'bad'); }
  },

  async editor(name) {
    const a = await api(`/api/agents/${name}`);
    let tools = [...a.tools];
    let rawMode = false;

    const desc = el('input', { value: a.description });
    const model = el('select', {}, MODELS.map((m) =>
      el('option', { value: m, selected: m === a.model }, m)));
    if (a.model && !MODELS.includes(a.model)) {
      model.prepend(el('option', { value: a.model, selected: true }, a.model));
    }
    const bodyArea = el('textarea', { spellcheck: 'false' }, a.body);
    bodyArea.style.minHeight = '340px';
    const rawArea = el('textarea', { spellcheck: 'false', hidden: true }, a.raw);
    rawArea.style.minHeight = '460px';

    const tagBox = el('div', { class: 'tag-input' });
    const tagInput = el('input', {
      placeholder: 'add a tool, e.g. Bash(npm test *) — Enter to add',
      onkeydown: (ev) => {
        if (ev.key !== 'Enter') return;
        ev.preventDefault();
        const val = tagInput.value.trim();
        if (val && !tools.includes(val)) { tools.push(val); drawTags(); }
        tagInput.value = '';
      },
    });
    function drawTags() {
      tagBox.replaceChildren(
        ...tools.map((t, i) => el('span', { class: 'tag' }, t,
          el('button', { title: 'remove', onclick: () => { tools.splice(i, 1); drawTags(); } }, '×'))),
        tagInput);
      tagInput.focus();
    }
    drawTags();

    const structured = el('div', { class: 'body' },
      el('div', { class: 'grid c2' },
        field('Description', desc, 'Shown in the agent picker and in dispatch decisions.'),
        field('Model', model, 'opus for judgement-heavy roles, sonnet for routine work.')),
      field(`Allowed tools (${a.tools.length})`, tagBox,
        'Exactly the allowedTools entries written to the frontmatter.'),
      field('Instructions (markdown body)', bodyArea,
        'The system prompt this role runs with.'));

    const rawWrap = el('div', { class: 'body', hidden: true },
      el('p', { class: 'muted small' },
        'Raw file — frontmatter plus body. Saving in raw mode writes exactly what you see.'),
      rawArea);

    const save = el('button', {
      class: 'btn primary small',
      onclick: (e) => action(e.target, () => post(`/api/agents/${name}`,
        rawMode ? { raw: rawArea.value }
          : { model: model.value, description: desc.value, tools, body: bodyArea.value }),
        `Saved ${name}.md`).then(() => render()),
    }, 'Save role');

    const toggle = el('button', {
      class: 'btn ghost small',
      onclick: () => {
        rawMode = !rawMode;
        structured.hidden = rawMode;
        rawWrap.hidden = !rawMode;
        toggle.textContent = rawMode ? 'Form editor' : 'Raw file';
      },
    }, 'Raw file');

    const del = el('button', {
      class: 'btn danger small',
      onclick: async (e) => {
        const ok = await confirmDialog('Delete role',
          `Delete ${name}.md? A timestamped copy is kept in .claude/agents/.backups/.`,
          'Delete');
        if (!ok) return;
        await action(e.target, () => post(`/api/agents/${name}/delete`, {}), `Deleted ${name}.md`);
        this.selected = null;
        render();
      },
    }, 'Delete');

    return card(`${name}.md`, headerSlot(toggle, del, save), structured, rawWrap);
  },
};

/* ── Telegram ───────────────────────────────────────────────────────────── */
VIEWS.telegram = {
  title: 'Telegram bot',
  async render(root) {
    const s = State.status || await api('/api/status');
    const bot = s.bot;

    const controls = card('Bot process',
      headerSlot(el('span', { class: `badge ${bot.running ? 'ok' : 'bad'}` },
        bot.running ? `running · pid ${bot.pids.join(', ')}` : 'stopped')),
      body(
        el('p', { class: 'muted small' },
          'scripts/tg-issue-bot.py — long-poll bot for /issue, /status and loop control. '
          + 'Started detached; its output goes to the log below.'),
        el('div', { class: 'row gap wrap' },
          el('button', {
            class: 'btn primary small', disabled: bot.running,
            onclick: (e) => action(e.target, () => post('/api/control/bot', { action: 'start' }))
              .then(() => { refreshStatus(); render(); }),
          }, 'Start'),
          el('button', {
            class: 'btn small', disabled: !bot.running,
            onclick: (e) => action(e.target, () => post('/api/control/bot', { action: 'stop' }))
              .then(() => { refreshStatus(); render(); }),
          }, 'Stop'),
          el('button', {
            class: 'btn small',
            onclick: (e) => action(e.target, () => post('/api/control/bot', { action: 'restart' }))
              .then(() => { refreshStatus(); render(); }),
          }, 'Restart'),
          el('button', {
            class: 'btn ghost small',
            onclick: (e) => action(e.target, () => post('/api/telegram/test', {}))
              .then((o) => toast(`Connected as @${o.bot.username} (id ${o.bot.id})`)),
          }, 'Check credentials'))));

    const msg = el('textarea', {
      placeholder: '✅ <b>Hello from the control panel</b>',
    }, '');
    msg.style.minHeight = '90px';
    const send = card('Send a message to the team chat', body(
      el('p', { class: 'muted small' },
        `Posts to chat ${bot.chatId || '(not set)'} with HTML parse mode — the same channel the agents report on.`),
      msg,
      el('div', { class: 'row end' },
        el('button', {
          class: 'btn primary small',
          onclick: (e) => {
            if (!msg.value.trim()) return toast('Write something first.', 'warn');
            action(e.target, () => post('/api/telegram/test', { text: msg.value }), 'Message sent.')
              .then(() => { msg.value = ''; });
          },
        }, 'Send'))));

    const logPre = el('pre', { class: 'out tall' }, 'loading…');
    const loadLog = async () => {
      try {
        const out = await api(`/api/log?path=${encodeURIComponent(bot.log)}&lines=300`);
        logPre.textContent = out.content || '(empty)';
        logPre.scrollTop = logPre.scrollHeight;
      } catch (e) { logPre.textContent = e.message; }
    };
    const logCard = card('Bot log',
      headerSlot(el('button', { class: 'btn ghost small', onclick: loadLog }, 'Reload')),
      body(el('p', { class: 'muted small mono' }, bot.log), logPre));
    loadLog();

    root.replaceChildren(el('div', { class: 'grid c2' }, controls, send), logCard);
  },
};

/* ── Board & MRs ────────────────────────────────────────────────────────── */
const WORKFLOW_LABELS = ['todo', 'roadmap', 'in-progress', 'done', 'failed', 'pipeline-failed'];
const labelClass = (l) => ({
  todo: 'accent', roadmap: 'info', 'in-progress': 'warn', done: 'ok',
  failed: 'bad', 'pipeline-failed': 'bad',
}[l] || '');

VIEWS.board = {
  title: 'Board & merge requests',
  filter: '',
  async render(root) {
    let issues = [];
    let mrs = [];
    let error = null;
    try {
      [issues, mrs] = await Promise.all([
        api(`/api/board${this.filter ? `?label=${encodeURIComponent(this.filter)}` : ''}`)
          .then((d) => d.issues),
        api('/api/mrs').then((d) => d.mrs).catch(() => []),
      ]);
    } catch (e) { error = e.message; }

    if (error) {
      root.replaceChildren(card('Board unavailable', body(
        el('p', { class: 'muted' }, error),
        el('p', { class: 'muted small' },
          'Set GITLAB_GROUP and GITLAB_TOKEN on the Tokens & settings tab.'))));
      return;
    }

    const filters = el('div', { class: 'row gap wrap' },
      ['', ...WORKFLOW_LABELS].map((l) => el('button', {
        class: `btn small ${this.filter === l ? 'primary' : 'ghost'}`,
        onclick: () => { this.filter = l; render(); },
      }, l || 'all open')));

    const issueCard = card(`Open issues (${issues.length})`, headerSlot(filters), body(
      table(['Issue', 'Repo', 'Labels', 'Milestone', 'Updated', ''], issues, (i) => [
        el('div', {},
          el('a', { href: i.url, target: '_blank', rel: 'noreferrer' }, `#${i.iid}`),
          ' ', el('span', {}, i.title)),
        el('code', { class: 'small' }, i.repo),
        el('div', { class: 'row gap wrap' }, i.labels.map((l) =>
          el('span', { class: `badge ${labelClass(l)}` }, l))),
        i.milestone || '—',
        el('span', { class: 'muted small' }, agoIso(i.updated)),
        el('div', { class: 'row gap end' },
          !i.labels.includes('todo') ? el('button', {
            class: 'btn small',
            onclick: (e) => action(e.target, () => post('/api/board/label',
              { projectId: i.projectId, iid: i.iid, add: ['todo'] }),
              `#${i.iid} queued as todo`).then(() => render()),
          }, 'Promote → todo') : null,
          i.labels.includes('todo') ? el('button', {
            class: 'btn ghost small',
            onclick: (e) => action(e.target, () => post('/api/board/label',
              { projectId: i.projectId, iid: i.iid, remove: ['todo'] }),
              `#${i.iid} removed from the queue`).then(() => render()),
          }, 'Unqueue') : null),
      ])));

    const mrCard = card(`Open merge requests (${mrs.length})`, body(
      table(['MR', 'Branch', 'Pipeline', 'Status', 'Updated', ''], mrs, (m) => [
        el('div', {},
          el('a', { href: m.url, target: '_blank', rel: 'noreferrer' }, `!${m.iid}`),
          ' ', m.title, m.draft ? el('span', { class: 'badge warn' }, 'draft') : null),
        el('code', { class: 'small' }, m.sourceBranch),
        m.pipeline
          ? el('a', { href: m.pipelineUrl || '#', target: '_blank', rel: 'noreferrer',
            class: `badge ${m.pipeline === 'success' ? 'ok'
              : m.pipeline === 'failed' ? 'bad' : 'warn'}` }, m.pipeline)
          : el('span', { class: 'badge' }, 'none'),
        m.conflicts ? el('span', { class: 'badge bad' }, 'conflicts')
          : el('span', { class: 'muted small' }, m.mergeStatus || '—'),
        el('span', { class: 'muted small' }, agoIso(m.updated)),
        el('div', { class: 'row gap end' },
          el('button', {
            class: 'btn small',
            onclick: (e) => busy(e.target, () => post('/api/mr/merge',
              { projectId: m.projectId, iid: m.iid }))
              .then((o) => {
                toast(o.message, o.ok ? 'ok' : 'warn', 8000);
                $('#mr-out').textContent = o.output; $('#mr-out').hidden = false;
                if (o.ok) render();
              })
              .catch((err) => toast(err.message, 'bad', 8000)),
          }, 'Accept (gated)'),
          el('button', {
            class: 'btn danger small',
            onclick: async (e) => {
              if (!await confirmDialog('Close MR', `Close !${m.iid} without merging?`, 'Close MR')) return;
              await action(e.target, () => post('/api/mr/close',
                { projectId: m.projectId, iid: m.iid }), `Closed !${m.iid}`);
              render();
            },
          }, 'Close')),
      ]),
      el('pre', { class: 'out', id: 'mr-out', hidden: true }),
      el('p', { class: 'muted small' },
        'Accept runs scripts/cto-accept-mr.sh — it refuses unless CTO_AUTO_ACCEPT_MR=1 '
        + 'and the MR is open, non-draft, conflict-free and green.')));

    root.replaceChildren(newIssueCard(), issueCard, mrCard);
  },
};

function newIssueCard() {
  const title = el('input', { placeholder: 'Issue title' });
  const desc = el('textarea', { placeholder: 'Description (markdown)' });
  desc.style.minHeight = '110px';
  const project = el('select', {}, el('option', { value: '' }, 'loading projects…'));
  const labels = el('input', { placeholder: 'todo,P1:Schema', value: 'todo' });

  api('/api/gitlab/test').then((o) => {
    project.replaceChildren(...o.projects.map((p) =>
      el('option', { value: p.id }, `${p.name} (${p.id})`)));
  }).catch(() => {
    project.replaceChildren(el('option', { value: '' }, 'GitLab not configured'));
  });

  return card('Create an issue', body(
    el('div', { class: 'grid c3' },
      field('Project', project),
      field('Title', title),
      field('Labels', labels, 'comma separated; `todo` dispatches it immediately')),
    field('Description', desc),
    el('div', { class: 'row end' },
      el('button', {
        class: 'btn primary small',
        onclick: async (e) => {
          if (!project.value || !title.value.trim()) return toast('Pick a project and a title.', 'warn');
          const out = await action(e.target, () => post('/api/board/create', {
            projectId: project.value, title: title.value, description: desc.value,
            labels: labels.value.split(',').map((s) => s.trim()).filter(Boolean),
          }));
          toast(`Created issue #${out.iid}`, 'ok');
          title.value = ''; desc.value = '';
          render();
        },
      }, 'Create issue'))));
}

/* ── Issue loop ─────────────────────────────────────────────────────────── */
VIEWS.loop = {
  title: 'Issue loop',
  async render(root) {
    const s = await api('/api/status');
    State.status = s;
    const loop = s.loop;

    const state = Object.keys(loop.state || {}).length
      ? table(['Field', 'Value'], Object.entries(loop.state), ([k, v]) => [el('code', {}, k), v])
      : el('p', { class: 'muted small' }, 'No issue is currently checked out by the loop.');

    const controls = card('Loop control',
      headerSlot(el('span', {
        class: `badge ${{ running: 'ok', paused: 'warn', stopped: 'bad', idle: '' }[loop.mode]}`,
      }, loop.mode)),
      body(
        el('p', { class: 'muted small' },
          'The loop pulls `todo` issues group-wide, runs the issue-worker per issue, opens '
          + 'the MR, watches CI, and relabels. The stop flag is a file the loop checks on '
          + 'every tick, so cron/systemd ticks stay inert while it is set.'),
        el('div', { class: 'row gap wrap' },
          el('button', {
            class: 'btn primary small', disabled: loop.mode === 'running',
            onclick: (e) => action(e.target, () => post('/api/control/loop', { action: 'run' }))
              .then(() => render()),
          }, 'Run a pass now'),
          el('button', {
            class: 'btn small', disabled: !loop.pids.length,
            onclick: async (e) => {
              if (!await confirmDialog('Stop the loop',
                'Send SIGTERM to the running loop? The current agent is interrupted.',
                'Stop it')) return;
              await action(e.target, () => post('/api/control/loop', { action: 'kill' }));
              render();
            },
          }, 'Kill running pass'),
          loop.stopped
            ? el('button', {
              class: 'btn small',
              onclick: (e) => action(e.target, () => post('/api/control/loop', { action: 'enable' }))
                .then(() => render()),
            }, 'Clear stop flag')
            : el('button', {
              class: 'btn danger small',
              onclick: (e) => action(e.target, () => post('/api/control/loop', { action: 'disable' }))
                .then(() => render()),
            }, 'Set stop flag'),
          Object.keys(loop.pause || {}).length ? el('button', {
            class: 'btn small',
            onclick: (e) => action(e.target, () => post('/api/control/loop', { action: 'clear-pause' }))
              .then(() => render()),
          }, 'Clear pause marker') : null,
          el('button', {
            class: 'btn ghost small',
            onclick: (e) => action(e.target, () => post('/api/control/loop', { action: 'dry-run' }))
              .then((o) => { $('#loop-out').textContent = o.output || '(no output)'; $('#loop-out').hidden = false; }),
          }, 'Dry-run selection')),
        !loop.stopFileWritable
          ? el('p', { class: 'muted small' },
            `⚠ ${loop.stopFile} is not writable by this process — run the UI as the same user as the loop.`)
          : null,
        el('pre', { class: 'out', id: 'loop-out', hidden: true })));

    const stateCard = card('Current work', body(state,
      Object.keys(loop.pause || {}).length
        ? el('div', {}, el('p', { class: 'muted small' }, 'Pause marker:'),
          table(['Field', 'Value'], Object.entries(loop.pause), ([k, v]) => [el('code', {}, k), v]))
        : null));

    const logPre = el('pre', { class: 'out tall' }, 'loading…');
    const loadLog = async () => {
      const files = await api('/api/logs');
      const pick = files.files.find((f) => /issue-loop|agent-team/.test(f.name));
      if (!pick) { logPre.textContent = '(no loop log yet)'; return; }
      const out = await api(`/api/log?path=${encodeURIComponent(pick.path)}&lines=400`);
      logPre.textContent = `# ${pick.path}\n\n${out.content}`;
      logPre.scrollTop = logPre.scrollHeight;
    };
    loadLog().catch((e) => { logPre.textContent = e.message; });

    root.replaceChildren(el('div', { class: 'grid c2' }, controls, stateCard),
      card('Latest loop log',
        headerSlot(el('button', { class: 'btn ghost small', onclick: () => loadLog() }, 'Reload')),
        body(logPre)));
  },
};

/* ── Config files ───────────────────────────────────────────────────────── */
VIEWS.config = {
  title: 'Config files',
  selected: null,
  async render(root) {
    const data = await api('/api/files');
    if (!this.selected) this.selected = data.files[0]?.path;

    const list = el('div', { class: 'list' }, data.files.map((f) =>
      el('button', {
        class: f.path === this.selected ? 'active' : '',
        onclick: () => { this.selected = f.path; render(); },
      }, f.path.replace('.claude/', ''),
      el('small', {}, f.exists ? `${fmtBytes(f.bytes)} · ${ago(f.modified)}` : 'not present'))));

    const left = card('Files', el('div', { class: 'body' }, list,
      el('p', { class: 'muted small' },
        'The team contract, the CTO reminder and the Claude Code permission set.')));

    const current = await api(`/api/file?path=${encodeURIComponent(this.selected)}`);
    const area = el('textarea', { spellcheck: 'false' }, current.content);
    area.style.minHeight = '58vh';

    const right = card(this.selected,
      headerSlot(el('button', {
        class: 'btn primary small',
        onclick: (e) => action(e.target, () => post('/api/file',
          { path: this.selected, content: area.value }), `Saved ${this.selected}`),
      }, 'Save')),
      body(
        el('p', { class: 'muted small' },
          'A timestamped backup is written to .claude/agents/.backups/ before each save. '
          + 'JSON files are validated before they are written.'),
        area));

    root.replaceChildren(el('div', { class: 'split' }, left, right));
  },
};

/* ── Logs & monitors ────────────────────────────────────────────────────── */
VIEWS.logs = {
  title: 'Logs & monitors',
  selected: null,
  follow: false,
  async render(root) {
    const [logs, monitors] = await Promise.all([api('/api/logs'), api('/api/monitors')]);
    if (!this.selected && logs.files.length) this.selected = logs.files[0].path;

    const list = el('div', { class: 'list' }, logs.files.map((f) =>
      el('button', {
        class: f.path === this.selected ? 'active' : '',
        onclick: () => { this.selected = f.path; render(); },
      }, f.name, el('small', {}, `${fmtBytes(f.bytes)} · ${ago(f.modified)}`))));

    const left = card(`Log files (${logs.files.length})`,
      el('div', { class: 'body' }, list.children.length ? list
        : el('div', { class: 'empty' }, 'No logs yet.'),
        el('p', { class: 'muted small mono' }, logs.roots.join('\n'))));

    const pre = el('pre', { class: 'out tall' }, 'select a log');
    const lineSel = el('select', {}, [200, 500, 1000, 2000].map((n) =>
      el('option', { value: n, selected: n === 500 }, `${n} lines`)));
    const load = async () => {
      if (!this.selected) return;
      try {
        const out = await api(
          `/api/log?path=${encodeURIComponent(this.selected)}&lines=${lineSel.value}`);
        pre.textContent = out.content || '(empty)';
        pre.scrollTop = pre.scrollHeight;
      } catch (e) { pre.textContent = e.message; }
    };
    lineSel.onchange = load;

    const followBtn = el('button', {
      class: `btn small ${this.follow ? 'primary' : 'ghost'}`,
      onclick: () => {
        this.follow = !this.follow;
        followBtn.className = `btn small ${this.follow ? 'primary' : 'ghost'}`;
        followBtn.textContent = this.follow ? 'Following' : 'Follow';
      },
    }, this.follow ? 'Following' : 'Follow');

    clearInterval(this._tail);
    this._tail = setInterval(() => {
      if (this.follow && State.view === 'logs') load();
    }, 4000);

    const right = card(this.selected ? this.selected.split('/').pop() : 'Viewer',
      headerSlot(lineSel, followBtn,
        el('button', { class: 'btn ghost small', onclick: load }, 'Reload')),
      body(el('p', { class: 'muted small mono' }, this.selected || '—'), pre));
    load();

    const mon = card('Monitor windows', body(
      el('div', { class: 'row gap wrap' },
        monitors.tmux.running
          ? monitors.tmux.windows.map((w) => el('span', { class: 'badge info' }, w))
          : el('span', { class: 'badge bad' }, `tmux session "${monitors.tmux.session}" not running`)),
      el('h4', { class: 'muted small' }, `Live task logs (${monitors.live.length})`),
      table(['Slug', 'Size', 'Updated'], monitors.live, (m) => [
        el('button', {
          class: 'btn ghost small',
          onclick: () => { this.selected = m.path; render(); },
        }, m.slug), fmtBytes(m.bytes), ago(m.modified)]),
      el('h4', { class: 'muted small' }, `Archived (${monitors.archived.length})`),
      table(['File', 'Size', 'Archived'], monitors.archived.slice(0, 25), (m) => [
        el('button', {
          class: 'btn ghost small',
          onclick: () => { this.selected = m.path; render(); },
        }, m.name), fmtBytes(m.bytes), ago(m.modified)])));

    root.replaceChildren(el('div', { class: 'split' }, left, right), mon);
  },
};

/* ── boot ───────────────────────────────────────────────────────────────── */
(async function boot() {
  try {
    const s = await api('/api/session');
    if (s.authed) { State.csrf = s.csrf; State.user = s.user; showApp(); }
    else {
      showLogin(s.configured ? '' :
        'No password is configured yet — run `python3 webui/server.py set-password`.');
    }
  } catch {
    showLogin('Cannot reach the control panel API.');
  }
})();
