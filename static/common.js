// Shared by the app (app.js) and the admin panel (admin.js): helpers, API, clock,
// Minsk-time formatting and the chat picker. No build step, no dependencies.

export const $ = (sel) => document.querySelector(sel);

export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) {
    if (c != null && c !== false) el.append(c instanceof Node ? c : String(c));
  }
  return el;
}

// --- API -----------------------------------------------------------------

export const auth = { onUnauthorized: null }; // set by the page

export class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

export async function api(path, { method = 'GET', body } = {}) {
  const headers = { 'X-TelegramQ': '1' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const res = await fetch(path, {
    method, headers, cache: 'no-store', credentials: 'same-origin',
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    let msg = data?.detail ?? res.statusText;
    if (Array.isArray(msg)) msg = msg.map((d) => d.msg).join('; ');
    if (res.status === 401) auth.onUnauthorized?.(msg);
    throw new ApiError(msg, res.status);
  }
  return data;
}

// --- clock (server time is the reference) ----------------------------------

export const clock = { offset: 0, rtt: null };
export const now = () => Date.now() + clock.offset;

export async function syncClock() {
  let best = null;
  for (let i = 0; i < 5; i++) {
    const t0 = performance.now();
    const w0 = Date.now();
    const { now: server } = await api('/api/time');
    const rtt = performance.now() - t0;
    if (!best || rtt < best.rtt) best = { rtt, offset: server - (w0 + rtt / 2) };
  }
  clock.offset = best.offset;
  clock.rtt = best.rtt;
  const el = $('#clock-offset');
  const off = Math.round(best.offset);
  el.textContent = Math.abs(off) <= Math.max(5, best.rtt / 2)
    ? 'browser clock in sync'
    : `browser clock ${off > 0 ? 'behind' : 'ahead'} by ${Math.abs(off)} ms`;
  el.classList.toggle('warn', Math.abs(off) > 250);
  el.title = 'Countdowns use the server clock, which is what sends the messages.';
}

// --- formatting (Minsk time) -------------------------------------------------

let utcOffset = 180 * 60000; // Europe/Minsk is UTC+3 all year; the server confirms it
export const setUtcOffset = (minutes) => { utcOffset = minutes * 60000; };
export const wall = (ms) => new Date(ms + utcOffset); // read with getUTC*()
export const dayIndex = (ms) => Math.floor((ms + utcOffset) / 86400000);
export const pad = (n, w = 2) => String(n).padStart(w, '0');

export function fmtTime(ms) {
  const d = wall(ms);
  return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}.${pad(d.getUTCMilliseconds(), 3)}`;
}

export function fmtHM(ms) {
  const d = wall(ms);
  return `${d.getUTCHours()}:${pad(d.getUTCMinutes())}`;
}

export function fmtDay(ms, long = false) {
  const diff = dayIndex(ms) - dayIndex(now());
  const date = wall(ms).toLocaleDateString('en-GB', {
    timeZone: 'UTC', weekday: long ? 'long' : 'short', day: 'numeric', month: long ? 'long' : 'short',
  });
  const rel = { 0: 'Today', 1: 'Tomorrow', [-1]: 'Yesterday' }[diff];
  if (!rel) return date;
  return long ? `${rel} · ${date}` : rel;
}

export const fmtDateTime = (ms) => `${fmtDay(ms)} ${fmtTime(ms)}`;

// Mid-sentence: "today", "tomorrow", or "on Mon 9 Nov".
export function fmtDayInline(ms) {
  const d = fmtDay(ms);
  return ['Today', 'Tomorrow', 'Yesterday'].includes(d) ? d.toLowerCase() : `on ${d}`;
}

export function fmtCountdown(ms) {
  if (ms <= 0) return 'now';
  const s = ms / 1000;
  const d = Math.floor(s / 86400);
  const hr = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  if (d) return `in ${d}d ${hr}h ${pad(m)}m`;
  if (hr) return `in ${hr}h ${pad(m)}m ${pad(sec)}s`;
  if (m) return `in ${m}m ${pad(sec)}s`;
  return `in ${s.toFixed(1)}s`;
}

export function fmtMs(v, signed = true) {
  const a = Math.abs(v);
  const num = a < 10 ? a.toFixed(2) : a.toFixed(1);
  return `${signed ? (v < 0 ? '−' : '+') : ''}${num} ms`;
}

const AVATAR_COLORS = ['#e8746a', '#f0a35e', '#9b87e0', '#6cbf68', '#4fb8c4', '#5a9fe0', '#e46fa5'];

export function avatar(name, kind) {
  let hash = 0;
  for (const ch of name) hash = (hash * 31 + ch.codePointAt(0)) | 0;
  const initials = kind === 'self'
    ? '★'
    : name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => [...w][0]).join('').toUpperCase() || '?';
  return h('span', { class: 'avatar', style: `background:${AVATAR_COLORS[Math.abs(hash) % AVATAR_COLORS.length]}` }, initials);
}

let toastTimer;
export function toast(text) {
  const el = $('#toast');
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3500);
}

export function showError(el, err) {
  el.textContent = err ? (err.message || String(err)) : '';
  el.hidden = !err;
}

export async function withBusy(button, fn) {
  button.disabled = true;
  try { return await fn(); } finally { button.disabled = false; }
}

// Minsk wall time <-> <input type="datetime-local" step="0.001"> value.
export function toMinskInput(ms) {
  const d = wall(ms);
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}T`
    + `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}.${pad(d.getUTCMilliseconds(), 3)}`;
}

export function fromMinskInput(value) {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?$/.exec(value || '');
  if (!m) return null;
  const [, y, mo, d, hh, mi, ss = '0', frac = '0'] = m;
  return Date.UTC(+y, +mo - 1, +d, +hh, +mi, +ss, +frac.padEnd(3, '0')) - utcOffset;
}

// Timing of a sent message, as one line.
export function sentStats(m) {
  const parts = [`fired ${fmtTime(m.fired_at)}`];
  if (m.timer_error_ms != null) parts.push(`timer ${fmtMs(m.timer_error_ms)}`);
  if (m.rtt_ms != null) parts.push(`RTT ${fmtMs(m.rtt_ms, false)}`);
  if (m.lead_ms) parts.push(`sent ${fmtMs(m.lead_ms, false)} early`);
  if (m.arrival_error_ms != null) parts.push(`est. arrival ${fmtMs(m.arrival_error_ms)}`);
  if (m.tg_date != null) parts.push(`Telegram dated it ${fmtTime(m.tg_date).slice(0, 8)}`);
  return h('div', {
    class: 'item-meta',
    title: 'Timer error: how far from the planned firing moment the request left the server.\n'
      + 'Est. arrival: when the request is estimated to have reached Telegram (fired + RTT/2), relative to the target.\n'
      + 'Telegram date: the time Telegram stamped on the message (whole seconds).',
  }, parts.join(' · '));
}

export function tickCountdowns(t = now()) {
  for (const el of document.querySelectorAll('.countdown')) {
    el.textContent = fmtCountdown(Number(el.dataset.at) - t);
  }
}

// --- chat picker -------------------------------------------------------------
// Search your chats, or type @username / t.me link / phone. Forums get a topic select.

const KIND_LABEL = { self: 'saved', user: 'user', bot: 'bot', group: 'group', channel: 'channel' };
const GENERAL_TOPIC = 1;
const dialogs = { list: [], loaded: false, listeners: new Set() };
let pickerCount = 0;

export async function loadDialogs() {
  try {
    dialogs.list = await api('/api/dialogs');
    dialogs.loaded = true;
    dialogs.listeners.forEach((fn) => fn());
  } catch (err) {
    if (err.status !== 401) toast(`Could not load chats: ${err.message}`);
  }
}

export function chatPicker({ label = 'Post in', onChange = () => {} } = {}) {
  const id = `chat-picker-${++pickerCount}`;
  const input = h('input', {
    id, placeholder: 'Search chats, or type @username / t.me link', role: 'combobox', autocomplete: 'off',
    'aria-expanded': 'false', 'aria-controls': `${id}-list`, 'aria-autocomplete': 'list', required: true,
  });
  const list = h('ul', { id: `${id}-list`, class: 'combo-list', role: 'listbox', hidden: true });
  const topicSelect = h('select', { id: `${id}-topic` });
  const topicRow = h('div', { hidden: true }, h('label', { for: topicSelect.id }, 'Topic'), topicSelect);
  const el = h('div', { class: 'chat-picker' }, h('label', { for: id }, label), h('div', { class: 'combo' }, input, list), topicRow);

  let chosen = null; // {peer, name, kind, forum}
  let matches = [];
  let active = -1;
  let topicsRequest = 0;

  function setOpen(open) {
    list.hidden = !open;
    input.setAttribute('aria-expanded', String(open));
  }

  function render() {
    const q = input.value.trim().toLowerCase().replace(/^@/, '');
    matches = dialogs.list
      .filter((d) => !q || d.name.toLowerCase().includes(q) || (d.username || '').toLowerCase().includes(q))
      .slice(0, 50);
    active = matches.length && q ? 0 : -1;
    const items = matches.map((d, i) => h('li', {
      role: 'option',
      'aria-selected': String(i === active),
      onmousedown: (e) => { e.preventDefault(); select(d); },
    }, avatar(d.name, d.kind), h('span', { class: 'name' }, d.name, d.username ? h('span', { class: 'muted' }, `  @${d.username}`) : null),
    h('span', { class: 'kind' }, d.forum ? 'topics' : (KIND_LABEL[d.kind] || d.kind))));
    if (!items.length) {
      items.push(h('li', { class: 'hint' }, dialogs.loaded
        ? (q ? `No chat matches. “${input.value.trim()}” will be looked up as a username, link or phone.` : 'No chats.')
        : 'Loading chats…'));
    }
    list.replaceChildren(...items);
    setOpen(true);
  }

  function moveActive(delta) {
    if (!matches.length) return;
    active = (active + delta + matches.length) % matches.length;
    [...list.children].forEach((li, i) => {
      li.setAttribute('aria-selected', String(i === active));
      if (i === active) li.scrollIntoView({ block: 'nearest' });
    });
  }

  function hideTopics() {
    topicsRequest++;
    topicRow.hidden = true;
    topicSelect.replaceChildren();
  }

  async function loadTopics(peer, wantedId = null, wantedName = null) {
    const req = ++topicsRequest;
    topicRow.hidden = false;
    topicSelect.disabled = true;
    topicSelect.replaceChildren(h('option', {}, 'Loading topics…'));
    try {
      const topics = await api(`/api/topics?peer=${encodeURIComponent(peer)}`);
      if (req !== topicsRequest) return;
      if (wantedId != null && !topics.some((t) => t.id === wantedId)) {
        topics.unshift({ id: wantedId, title: wantedName || `Topic ${wantedId}`, closed: false });
      }
      if (!topics.length) { hideTopics(); onChange(); return; }
      topicSelect.replaceChildren(...topics.map((t) => h('option', { value: t.id }, t.closed ? `${t.title} (closed)` : t.title)));
      const pick = topics.find((t) => t.id === wantedId) ?? topics.find((t) => t.id === GENERAL_TOPIC) ?? topics[0];
      topicSelect.value = String(pick.id);
      topicSelect.disabled = false;
      onChange();
    } catch (err) {
      if (req !== topicsRequest) return;
      hideTopics();
      if (err.status !== 401) toast(`Could not load topics: ${err.message}`);
    }
  }

  function select(d, topicId = null, topicName = null) {
    chosen = d;
    input.value = d.name;
    setOpen(false);
    if (d.forum) loadTopics(d.peer, topicId, topicName);
    else hideTopics();
    onChange();
  }

  const topicId = () => (topicRow.hidden || topicSelect.disabled ? null : Number(topicSelect.value));

  input.addEventListener('input', () => { chosen = null; hideTopics(); render(); onChange(); });
  input.addEventListener('focus', () => { if (!chosen) render(); });
  input.addEventListener('blur', () => setOpen(false));
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); if (list.hidden) render(); else moveActive(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); moveActive(-1); }
    else if (e.key === 'Escape') setOpen(false);
    else if (e.key === 'Enter' && !list.hidden && active >= 0) {
      e.preventDefault();
      select(matches[active]);
    }
  });
  topicSelect.addEventListener('change', () => onChange());
  dialogs.listeners.add(() => { if (document.activeElement === input) render(); });

  return {
    el,
    select,
    focus: () => input.focus(),
    get chosen() { return chosen; },
    get isEmpty() { return !chosen && !input.value.trim(); },
    /** What to send to the API: {peer, topic_id}. */
    value: () => ({ peer: chosen?.peer ?? input.value.trim(), topic_id: topicId() }),
    /** "Chat › Topic", or null when nothing is chosen yet. */
    label() {
      if (!chosen) return null;
      const topic = topicId() == null ? null : topicSelect.selectedOptions[0]?.textContent;
      return topic ? `${chosen.name} › ${topic}` : chosen.name;
    },
  };
}
