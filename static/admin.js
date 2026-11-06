// TelegramQ admin panel: schedule messages, see every user's messages, users, timetable, settings.
// Times are shown in Minsk time.

import {
  $, h, api, auth, now, syncClock, fmtTime, fmtHM, fmtDay, fmtDateTime, fmtCountdown, avatar, toast,
  showError, withBusy, sentStats, tickCountdowns, toMinskInput, fromMinskInput, loadDialogs, chatPicker,
} from './common.js';

const state = {
  me: null,
  overview: null,
  tab: 'upcoming',
  kind: '',
  messages: [],
  signature: '',
  users: [],
  timetable: null,
};

const UPCOMING = new Set(['pending', 'sending']);

auth.onUnauthorized = () => { location.href = '/'; };

// --- overview ------------------------------------------------------------------

async function refreshOverview() {
  state.overview = await api('/api/admin/overview');
  renderStats();
}

function tile(label, value, sub) {
  return h('div', { class: 'stat card' },
    h('div', { class: 'stat-label' }, label),
    h('div', { class: 'stat-value' }, value),
    h('div', { class: 'stat-sub muted' }, sub || ' '));
}

function timetableSummary(t) {
  if (!t.enabled) return ['Off', 'SCHEDULE_URL is empty'];
  if (t.refreshing) return ['Checking…', ''];
  if (!t.updated_at) return ['Not loaded', t.error || ''];
  return [`${fmtDay(t.updated_at)} ${fmtHM(t.updated_at)}`, t.error ? `⚠ ${t.error}` : 'last checked'];
}

function renderStats() {
  const o = state.overview;
  const [ttValue, ttSub] = timetableSummary(o.timetable);
  $('#stats').replaceChildren(
    tile('Users', o.users),
    tile('Waiting to send', o.pending, `${o.pending_lines} place${o.pending_lines === 1 ? '' : 's'} in line`),
    tile('Next message', o.next_send_at ? h('span', { class: 'countdown', dataset: { at: o.next_send_at } }) : '—',
      o.next_send_at ? fmtDateTime(o.next_send_at) : 'nothing scheduled'),
    tile('Timetable', ttValue, ttSub));
  $('#count-upcoming').textContent = o.pending;
  document.querySelectorAll('input[name=line-timing]').forEach((r) => { r.checked = r.value === o.line_timing; });
  tickCountdowns();
}

// --- schedule a message ------------------------------------------------------------

const composeChat = chatPicker({ label: 'To' });
$('#compose-chat').replaceWith(composeChat.el);
const whenInput = $('#when-input');
const textInput = $('#text-input');

function setWhen(ms) {
  whenInput.value = toMinskInput(ms);
  updateWhenPreview();
}

function updateWhenPreview() {
  const target = fromMinskInput(whenInput.value);
  const el = $('#when-preview');
  if (target == null) { el.textContent = ' '; return; }
  const delta = target - now();
  el.textContent = delta < -1000 ? '⚠ this time is in the past' : `${fmtCountdown(delta)} · ${fmtDay(target)}`;
}

$('#quick-times').addEventListener('click', (e) => {
  const b = e.target.closest('button');
  if (!b) return;
  if (b.dataset.add) setWhen(Math.ceil((now() + Number(b.dataset.add) * 1000) / 1000) * 1000);
  if (b.dataset.round) {
    const step = Number(b.dataset.round) * 1000; // Minsk is a whole number of hours from UTC
    setWhen(Math.ceil((now() + 1000) / step) * step);
  }
});

whenInput.addEventListener('input', updateWhenPreview);

textInput.addEventListener('input', () => {
  const n = textInput.value.length;
  const el = $('#text-counter');
  el.textContent = `${n} / 4096`;
  el.classList.toggle('over', n > 4096);
});

textInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) $('#compose').requestSubmit();
});

$('#compose').addEventListener('submit', async (e) => {
  e.preventDefault();
  const errEl = $('#compose-error');
  showError(errEl, null);
  const sendAt = fromMinskInput(whenInput.value);
  if (sendAt == null) { showError(errEl, new Error('Pick a date and time.')); return; }
  const body = {
    ...composeChat.value(),
    text: textInput.value,
    send_at: sendAt,
    parse_mode: $('#parse-mode').value,
    silent: $('#opt-silent').checked,
    link_preview: $('#opt-preview').checked,
    compensate_latency: $('#opt-compensate').checked,
  };
  try {
    const msg = await withBusy($('#compose-submit'), () => api('/api/admin/messages', { method: 'POST', body }));
    toast(`Scheduled for ${fmtDateTime(msg.send_at)} → ${msg.topic_name ? `${msg.peer_name} › ${msg.topic_name}` : msg.peer_name}`);
    textInput.value = '';
    textInput.dispatchEvent(new Event('input'));
    setTab('upcoming');
    await Promise.all([refreshMessages(), refreshOverview()]);
  } catch (err) {
    showError(errEl, err);
  }
});

// --- line timing -------------------------------------------------------------------

document.querySelectorAll('input[name=line-timing]').forEach((radio) => radio.addEventListener('change', async () => {
  try {
    await api('/api/admin/settings', { method: 'PUT', body: { line_timing: radio.value } });
    toast(radio.value === 'exact'
      ? 'Line messages are now sent at the line\'s time, never early.'
      : 'Line messages now aim to reach Telegram at the line\'s time.');
  } catch (err) {
    toast(err.message);
  }
  await refreshOverview();
}));

// --- messages ----------------------------------------------------------------------

async function refreshMessages() {
  const msgs = await api(`/api/admin/messages?upcoming=${state.tab === 'upcoming'}`);
  const sig = JSON.stringify([state.tab, msgs]);
  if (sig === state.signature) return;
  state.signature = sig;
  state.messages = msgs;
  renderMessages();
}

function setTab(tab) {
  state.tab = tab;
  state.signature = '';
  document.querySelectorAll('.tabs [role=tab]').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === tab)));
  state.messages = [];
  renderMessages();
  refreshMessages().catch((err) => toast(err.message));
}

document.querySelectorAll('.tabs [role=tab]').forEach((b) => b.addEventListener('click', () => setTab(b.dataset.tab)));
$('#kind-filter').addEventListener('change', (e) => { state.kind = e.target.value; renderMessages(); });

function renderMessages() {
  const list = state.messages.filter((m) => !state.kind || m.kind === state.kind);
  $('#queue-list').replaceChildren(...list.map(renderMessage));
  const empty = $('#queue-empty');
  empty.hidden = list.length > 0;
  empty.textContent = state.tab === 'upcoming' ? 'Nothing waiting to be sent.' : 'Nothing sent yet.';
  tickCountdowns();
}

function pendingFlags(m) {
  const flags = [m.compensate_latency ? 'aims to arrive on time' : 'never early'];
  if (m.kind === 'message') {
    if (m.silent) flags.push('silent');
    if (!m.link_preview) flags.push('no link preview');
    if (m.parse_mode !== 'md') flags.push(m.parse_mode === 'html' ? 'HTML' : 'plain text');
  }
  return h('div', { class: 'item-meta' }, flags.join(' · '));
}

function renderMessage(m) {
  const upcoming = UPCOMING.has(m.status);
  const chat = m.topic_name ? `${m.peer_name} › ${m.topic_name}` : m.peer_name;
  const what = m.kind === 'line'
    ? `Place in line · ${m.group_name} · ${fmtDay(m.lesson_start)}, pair ${m.pair} at ${fmtHM(m.lesson_start)}`
    : 'Admin message';
  const meta = m.status === 'sent' ? sentStats(m)
    : m.error ? h('div', { class: 'item-meta' }, h('span', { class: 'err' }, m.error))
      : upcoming ? pendingFlags(m) : null;
  return h('li', { class: 'item' },
    avatar(m.peer_name, m.peer_type),
    h('div', { class: 'item-head' },
      h('span', { class: 'item-user' }, m.user_name),
      h('span', { class: 'muted chat' }, `→ ${chat}`)),
    h('div', { class: 'item-actions' },
      m.status === 'pending' ? h('button', { onclick: () => cancelMessage(m) }, 'Cancel') : null,
      !upcoming ? h('button', { onclick: () => removeMessage(m) }, 'Remove') : null,
      h('span', { class: `badge ${m.status}` }, m.status)),
    h('div', { class: 'item-text' }, m.text),
    h('div', { class: 'item-sub' }, what),
    h('div', { class: 'item-sub' },
      h('span', { class: 'when', title: new Date(m.send_at).toISOString() }, `${upcoming ? 'Sends' : 'Was due'} ${fmtDateTime(m.send_at)}`),
      upcoming ? h('span', { class: 'countdown', dataset: { at: m.send_at } }) : null),
    meta);
}

async function cancelMessage(m) {
  if (m.user_id !== state.me.id
      && !confirm(`Cancel ${m.user_name}'s message “${m.text}”?${m.kind === 'line' ? '\n\nEveryone behind them in this line moves up.' : ''}`)) return;
  try {
    await api(`/api/admin/messages/${m.id}/cancel`, { method: 'POST' });
    toast('Cancelled.');
  } catch (err) {
    toast(err.message);
  }
  await Promise.all([refreshMessages(), refreshOverview(), refreshUsers()]);
}

async function removeMessage(m) {
  try {
    await api(`/api/admin/messages/${m.id}`, { method: 'DELETE' });
  } catch (err) {
    toast(err.message);
  }
  await refreshMessages();
}

// --- users -------------------------------------------------------------------------

async function refreshUsers() {
  state.users = await api('/api/admin/users');
  $('#users tbody').replaceChildren(...state.users.map(renderUser));
}

function renderUser(u) {
  const contact = [u.username && `@${u.username}`, u.phone && `+${u.phone}`, `ID ${u.id}`].filter(Boolean).join(' · ');
  return h('tr', {},
    h('td', {}, h('div', { class: 'user-cell' }, avatar(u.name),
      h('div', {},
        h('div', {}, u.name, u.is_admin ? h('span', { class: 'tag' }, 'admin') : null, u.is_you ? h('span', { class: 'muted' }, ' (you)') : null),
        h('div', { class: 'muted small' }, contact)))),
    h('td', {}, u.group_name || h('span', { class: 'muted' }, '—')),
    h('td', { class: 'num' }, u.pending),
    h('td', { class: 'num' }, u.sent),
    h('td', { class: 'nowrap' }, fmtDay(u.created_at)),
    h('td', { class: 'actions' }, u.is_you ? null : h('button', { class: 'ghost small danger', onclick: () => removeUser(u) }, 'Remove')));
}

async function removeUser(u) {
  const ok = confirm(`Remove ${u.name}?\n\nThis gives up their places in line (everyone behind them moves up), `
    + 'ends TelegramQ\'s session on their Telegram account and deletes their data from this server. '
    + 'They can log in again later.');
  if (!ok) return;
  try {
    await api(`/api/admin/users/${u.id}`, { method: 'DELETE' });
    toast(`${u.name} was removed.`);
  } catch (err) {
    toast(err.message);
  }
  await Promise.all([refreshUsers(), refreshOverview(), refreshMessages()]);
}

// --- timetable -----------------------------------------------------------------------

async function refreshTimetable(data) {
  state.timetable = data ?? await api('/api/admin/timetable');
  renderTimetable();
}

const fmtIsoDay = (day) => fmtDay(Date.parse(`${day}T12:00:00Z`));

function renderTimetable() {
  const t = state.timetable;
  const status = $('#tt-status');
  $('#tt-refresh').hidden = !t.enabled;
  if (!t.enabled) {
    status.textContent = 'Reading the timetable is turned off (SCHEDULE_URL is empty).';
  } else {
    status.replaceChildren(
      'From ', h('a', { href: t.source, target: '_blank', rel: 'noopener' }, t.source),
      `, checked every ${t.interval_minutes} min`,
      t.updated_at ? `, last at ${fmtHM(t.updated_at)} ${fmtDay(t.updated_at).toLowerCase()}` : '',
      '.',
      t.error ? h('span', { class: 'err' }, ` ${t.error}`) : '');
  }
  $('#tt-days tbody').replaceChildren(...t.days.map((d) => h('tr', {},
    h('td', { class: 'nowrap' }, h('a', { href: d.url, target: '_blank', rel: 'noopener', title: 'Open the PDF' }, fmtIsoDay(d.day))),
    h('td', { class: 'num' }, d.classes),
    h('td', { class: 'num' }, d.groups),
    h('td', { class: 'nowrap muted' }, `${fmtDay(d.fetched_at)} ${fmtHM(d.fetched_at)}`),
    h('td', { class: 'actions' }, h('button', {
      class: 'ghost small', title: 'Download and read this PDF again', onclick: (e) => reloadDay(d, e.currentTarget),
    }, 'Re-read')))));
  if (!t.days.length) {
    $('#tt-days tbody').replaceChildren(h('tr', {}, h('td', { colspan: 5, class: 'muted' }, 'No days stored yet.')));
  }
}

$('#tt-refresh').addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  btn.textContent = 'Checking…';
  try {
    await withBusy(btn, async () => refreshTimetable(await api('/api/admin/timetable/refresh', { method: 'POST' })));
    toast('Timetable checked.');
  } catch (err) {
    toast(err.message);
  }
  btn.textContent = 'Check now';
  refreshOverview();
});

async function reloadDay(d, btn) {
  try {
    const r = await withBusy(btn, () => api(`/api/admin/timetable/days/${d.day}/reload`, { method: 'POST' }));
    toast(`${fmtIsoDay(d.day)}: ${r.classes} classes read.`);
    await refreshTimetable(r);
  } catch (err) {
    toast(err.message);
  }
}

// --- ticking & polling ------------------------------------------------------------------

function tick() {
  const t = now();
  $('#clock-time').textContent = fmtTime(t);
  tickCountdowns(t);
  if (!$('#admin').hidden) updateWhenPreview();
}

let polls = 0;

async function poll() {
  try {
    const soon = state.messages.some((m) => UPCOMING.has(m.status) && m.send_at - now() < 30000);
    if (soon || polls % 2 === 0) await Promise.all([refreshMessages(), refreshOverview()]);
    if (polls % 15 === 0) await Promise.all([refreshUsers(), refreshTimetable()]);
  } catch (err) {
    if (err.status !== 401) console.warn('admin refresh failed', err);
  }
  polls++;
  setTimeout(poll, 1000);
}

// --- boot ----------------------------------------------------------------------------

async function init() {
  setInterval(tick, 41);
  syncClock().catch(() => {});
  setInterval(() => syncClock().catch(() => {}), 10 * 60 * 1000);
  let me;
  try {
    me = await api('/api/me');
  } catch {
    return; // not logged in: auth.onUnauthorized sends you to the login page
  }
  if (!me.is_admin) {
    const name = [me.first_name, me.last_name].filter(Boolean).join(' ') || me.username || 'you';
    $('#denied-text').textContent = `You're logged in as ${name} (Telegram ID ${me.id}). To make this account an admin, `
      + 'add its Telegram ID or phone number to ADMINS in .env and restart the server.';
    $('#denied').hidden = false;
    return;
  }
  state.me = me;
  $('#admin').hidden = false;
  loadDialogs();
  setWhen(Math.ceil((now() + 60000) / 60000) * 60000);
  await Promise.all([refreshOverview(), refreshMessages()]).catch((err) => toast(err.message));
  poll();
}

init();
