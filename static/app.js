// TelegramQ app: pick your group, take places in line. No build step, no dependencies.
// Times are shown in Minsk time, the college's clock.

import {
  $, h, api, auth, now, syncClock, setUtcOffset, fmtTime, fmtHM, fmtDay, fmtDateTime, fmtDayInline,
  avatar, toast, showError, withBusy, sentStats, tickCountdowns, loadDialogs, chatPicker,
} from './common.js';

// --- state -----------------------------------------------------------------

const state = {
  user: null,
  dialogs: [],
  groups: [],
  group: null,
  days: [],
  timetable: null,
  entries: [],
  signature: '',
  scheduleSignature: '',
  tab: 'upcoming',
  loginId: null,
  joining: null, // lesson whose join panel is open
};

const UPCOMING = new Set(['pending', 'sending']);
const LAST_CHAT_KEY = 'telegramq.lastChat';

// --- login -----------------------------------------------------------------

auth.onUnauthorized = (message) => { if (state.user) onLoggedOut(message); };

function setLoginStep(step) {
  $('#phone-form').hidden = step !== 'phone';
  $('#code-form').hidden = step !== 'code';
  $('#password-form').hidden = step !== 'password';
  const focus = { phone: '#phone-input', code: '#code-input', password: '#password-input' }[step];
  setTimeout(() => $(focus).focus(), 0);
}

function showLogin(message) {
  $('#login-view').hidden = false;
  $('#app-view').hidden = true;
  $('#account').hidden = true;
  $('#code-input').value = '';
  $('#password-input').value = '';
  setLoginStep('phone');
  showError($('#login-error'), message ? new Error(message) : null);
}

function onLoggedOut(message) {
  state.user = null;
  state.entries = [];
  state.signature = '';
  state.scheduleSignature = '';
  state.dialogs = [];
  state.days = [];
  clearTimeout(pollTimer);
  closeJoin();
  showLogin(message);
}

async function loginStep(form, path, body) {
  const errEl = $('#login-error');
  showError(errEl, null);
  try {
    const result = await withBusy(form.querySelector('button[type=submit]'), () => api(path, { method: 'POST', body }));
    return result;
  } catch (err) {
    showError(errEl, err);
    if (err.status === 410) setLoginStep('phone');
    return null;
  }
}

function finishLogin(result) {
  if (result?.step !== 'done') return false;
  state.user = result.user;
  showApp();
  return true;
}

$('#phone-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const r = await loginStep(e.target, '/api/auth/phone', { phone: $('#phone-input').value });
  if (!r) return;
  state.loginId = r.login_id;
  $('#code-sent-msg').textContent = r.sent_via ? `We sent a login code ${r.sent_via}.` : 'We sent you a login code.';
  if (r.code_length) $('#code-input').placeholder = '0'.repeat(r.code_length);
  setLoginStep('code');
});

$('#code-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const r = await loginStep(e.target, '/api/auth/code', { login_id: state.loginId, code: $('#code-input').value });
  if (!r || finishLogin(r)) return;
  if (r.step === 'password') {
    $('#password-hint').textContent = r.hint ? `(hint: ${r.hint})` : '';
    setLoginStep('password');
  }
});

$('#password-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const r = await loginStep(e.target, '/api/auth/password', { login_id: state.loginId, password: $('#password-input').value });
  $('#password-input').value = '';
  finishLogin(r);
});

document.querySelectorAll('[data-restart]').forEach((b) => b.addEventListener('click', () => {
  showError($('#login-error'), null);
  setLoginStep('phone');
}));

// --- account menu ------------------------------------------------------------
// --- account menu ------------------------------------------------------------

const menu = $('#account-menu');
const menuBtn = $('#account-btn');

function toggleMenu(open = menu.hidden) {
  menu.hidden = !open;
  menuBtn.setAttribute('aria-expanded', String(open));
}

menuBtn.addEventListener('click', (e) => { e.stopPropagation(); toggleMenu(); });
document.addEventListener('click', (e) => { if (!menu.contains(e.target)) toggleMenu(false); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') toggleMenu(false); });

$('#logout-btn').addEventListener('click', async () => {
  toggleMenu(false);
  await api('/api/auth/logout', { method: 'POST', body: { disconnect_telegram: false } }).catch(() => {});
  onLoggedOut('Signed out. You keep your places in line.');
});

$('#disconnect-btn').addEventListener('click', async () => {
  toggleMenu(false);
  const ok = confirm('Disconnect your Telegram account?\n\nThis ends TelegramQ\'s Telegram session and deletes your '
    + 'places in line and their history from this server. You leave every line you are in.');
  if (!ok) return;
  try {
    await api('/api/auth/logout', { method: 'POST', body: { disconnect_telegram: true } });
    onLoggedOut('Your Telegram account was disconnected.');
  } catch (err) {
    toast(err.message);
  }
});

// --- app shell ---------------------------------------------------------------

function showApp() {
  const u = state.user;
  const name = [u.first_name, u.last_name].filter(Boolean).join(' ') || u.username || 'You';
  $('#login-view').hidden = true;
  $('#app-view').hidden = false;
  $('#account').hidden = false;
  $('#account-avatar').replaceWith(Object.assign(avatar(name), { id: 'account-avatar' }));
  $('#account-name').textContent = name;
  $('#account-info').textContent = [u.username && `@${u.username}`, u.phone && `+${u.phone}`, `ID ${u.id}`]
    .filter(Boolean).join(' · ');
  $('#admin-link').hidden = !u.is_admin;
  loadDialogs();
  loadGroups().then(refreshSchedule);
  refreshEntries().then(schedulePoll);
}

// --- timetable ---------------------------------------------------------------

const groupSelect = $('#group-select');

async function loadGroups() {
  try {
    const r = await api('/api/groups');
    state.groups = r.groups;
    state.group = r.selected;
    state.timetable = r.timetable;
    renderGroups();
    renderTimetableStatus();
  } catch (err) {
    if (err.status !== 401) toast(`Could not load the groups: ${err.message}`);
  }
}

function renderGroups() {
  const groups = [...state.groups];
  if (state.group && !groups.includes(state.group)) groups.unshift(state.group);
  groupSelect.replaceChildren(
    h('option', { value: '' }, groups.length ? 'Choose your group…' : 'No groups yet'),
    ...groups.map((g) => h('option', { value: g }, g)));
  groupSelect.value = state.group || '';
}

groupSelect.addEventListener('change', async () => {
  const group = groupSelect.value || null;
  try {
    await api('/api/me/group', { method: 'PUT', body: { group } });
    state.group = group;
    closeJoin();
    state.scheduleSignature = '';
    await refreshSchedule();
  } catch (err) {
    toast(err.message);
    groupSelect.value = state.group || '';
  }
});

function renderTimetableStatus() {
  const t = state.timetable;
  const el = $('#timetable-status');
  $('#refresh-btn').hidden = !t?.enabled;
  if (!t) return;
  if (!t.enabled) { el.textContent = 'Reading the timetable is turned off (SCHEDULE_URL is empty).'; return; }
  const checked = t.refreshing ? 'Checking for new days…'
    : t.updated_at ? `Timetable checked ${fmtDayInline(t.updated_at)} at ${fmtHM(t.updated_at)}`
      : 'The timetable has not been loaded yet.';
  el.replaceChildren(checked, t.error ? h('span', { class: 'err' }, ` (${t.error})`) : '', ' · ');
}

$('#refresh-btn').addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  btn.textContent = 'Checking…';
  try {
    await withBusy(btn, async () => {
      state.timetable = await api('/api/schedule/refresh', { method: 'POST' });
      await loadGroups();
      state.scheduleSignature = '';
      await refreshSchedule();
    });
  } catch (err) {
    toast(err.message);
  }
  btn.textContent = 'Check for new days';
});

let lastScheduleLoad = 0;

async function refreshSchedule() {
  if (!state.user) return;
  lastScheduleLoad = Date.now();
  try {
    const r = await api(`/api/schedule${state.group ? `?group=${encodeURIComponent(state.group)}` : ''}`);
    setUtcOffset(r.utc_offset_minutes);
    state.timetable = r.timetable;
    renderTimetableStatus();
    if (!state.groups.length && r.timetable.updated_at) loadGroups(); // the first download just finished
    const sig = JSON.stringify([r.group, r.days]);
    if (sig === state.scheduleSignature) return;
    state.scheduleSignature = sig;
    state.days = r.days;
    renderDays();
  } catch (err) {
    if (err.status !== 401) console.warn('schedule refresh failed', err);
  }
}

let renderedAt = 0;

function renderDays() {
  renderedAt = now();
  const container = $('#days');
  const empty = $('#days-empty');
  parkJoinPanel();
  container.replaceChildren(...state.days.map(renderDay));
  empty.hidden = state.days.length > 0;
  if (!state.group) {
    empty.textContent = state.groups.length ? 'Choose your group to see its classes.' : 'The timetable has not been loaded yet.';
  } else {
    empty.textContent = `No classes for ${state.group} from today on. New days appear here as soon as the college publishes them.`;
  }
  if (state.joining) {
    const lesson = state.days.flatMap((d) => d.lessons).find((l) => l.id === state.joining.id);
    if (lesson && lesson.send_at > now()) openJoin(lesson, false);
    else closeJoin();
  }
}

function renderDay(day) {
  return h('section', { class: 'day' },
    h('h3', {}, fmtDay(day.lessons[0].start, true)),
    h('ul', { class: 'lessons' }, day.lessons.map(renderLesson)));
}

function placeLabel(p) {
  const chat = p.topic_name ? `${p.peer_name} › ${p.topic_name}` : p.peer_name;
  return h('span', { class: `place ${p.status}`, title: `Your place in line in ${chat}` }, `#${p.place} → ${chat}`);
}

function renderLesson(l) {
  const open = l.send_at > now();
  const meta = [`${fmtHM(l.start)}–${fmtHM(l.end)}`, l.teachers, l.rooms].filter(Boolean).join(' · ');
  const line = open
    ? [`Line opens ${fmtDayInline(l.send_at)} at ${fmtHM(l.send_at)} `, h('span', { class: 'countdown', dataset: { at: l.send_at } })]
    : [`Line opened at ${fmtHM(l.send_at)}`];
  return h('li', { class: `lesson${l.end < now() ? ' past' : ''}`, dataset: { lesson: l.id } },
    h('span', { class: 'pair', title: `Pair ${l.pair}` }, l.pair),
    h('div', { class: 'lesson-main' },
      h('div', { class: 'lesson-title' }, l.subject, l.subgroup ? h('span', { class: 'muted' }, ` · subgroup ${l.subgroup}`) : null),
      h('div', { class: 'lesson-meta' }, meta),
      l.details ? h('div', { class: 'lesson-meta' }, l.details) : null,
      h('div', { class: 'lesson-line' }, h('span', {}, ...line), ...l.places.map(placeLabel))),
    open ? h('button', { class: 'take', type: 'button', onclick: () => openJoin(l) },
      l.places.length ? 'Another chat' : 'Take a place') : null);
}

// --- taking a place ------------------------------------------------------------

const joinPanel = $('#join-panel');
const joinChat = chatPicker({ onChange: () => updateJoinPreview() });
$('#join-chat').replaceWith(joinChat.el);

function parkJoinPanel() {
  joinPanel.hidden = true;
  $('#app-view').append(joinPanel);
}

function openJoin(lesson, focus = true) {
  const row = document.querySelector(`[data-lesson="${lesson.id}"]`);
  if (!row) return;
  const switching = state.joining?.id !== lesson.id;
  state.joining = lesson;
  row.append(joinPanel);
  joinPanel.hidden = false;
  if (switching) showError($('#join-error'), null);
  if (joinChat.isEmpty) {
    const last = loadLastChat();
    if (last) joinChat.select(last.dialog, last.topicId, last.topicName);
  }
  updateJoinPreview();
  if (focus) (joinChat.chosen ? $('#join-submit') : joinChat).focus();
}

function closeJoin() {
  state.joining = null;
  parkJoinPanel();
}

$('#join-cancel').addEventListener('click', closeJoin);

function updateJoinPreview() {
  const l = state.joining;
  if (!l) return;
  const chat = joinChat.label() ?? 'the chat you pick';
  $('#join-preview').replaceChildren(
    `${fmtDay(l.send_at)} at `, h('strong', {}, fmtTime(l.send_at)),
    ' your account posts ', h('code', {}, `{your place} ${l.subject}`), ` in ${chat}. `,
    h('span', { class: 'muted' }, 'You get the next free place, and move up if someone ahead of you leaves.'));
}

function loadLastChat() {
  try { return JSON.parse(localStorage.getItem(LAST_CHAT_KEY)); } catch { return null; }
}

function saveLastChat(entry) {
  const dialog = joinChat.chosen ?? { peer: entry.peer_ref, name: entry.peer_name, kind: entry.peer_type, forum: entry.topic_id != null };
  localStorage.setItem(LAST_CHAT_KEY, JSON.stringify({ dialog, topicId: entry.topic_id, topicName: entry.topic_name }));
}

joinPanel.addEventListener('submit', async (e) => {
  e.preventDefault();
  const lesson = state.joining;
  if (!lesson) return;
  const errEl = $('#join-error');
  showError(errEl, null);
  const body = { lesson_id: lesson.id, ...joinChat.value() };
  try {
    const entry = await withBusy($('#join-submit'), () => api('/api/lines', { method: 'POST', body }));
    saveLastChat(entry);
    toast(`You're #${entry.place}: “${entry.text}” goes out ${fmtDayInline(entry.send_at)} at ${fmtHM(entry.send_at)}.`);
    closeJoin();
    setTab('upcoming');
    state.scheduleSignature = '';
    await Promise.all([refreshEntries(), refreshSchedule()]);
    schedulePoll();
  } catch (err) {
    showError(errEl, err);
  }
});

// --- my places -------------------------------------------------------------------

let pollTimer;

async function refreshEntries() {
  if (!state.user) return;
  try {
    const entries = await api('/api/lines');
    const sig = JSON.stringify(entries);
    if (sig !== state.signature) {
      const first = state.signature === '';
      state.signature = sig;
      state.entries = entries;
      renderQueue();
      if (!first) refreshSchedule(); // places shown in the timetable may have moved
    }
  } catch (err) {
    if (err.status !== 401) console.warn('refresh failed', err);
  }
}

function schedulePoll() {
  clearTimeout(pollTimer);
  if (!state.user) return;
  const soon = state.entries.some((m) => UPCOMING.has(m.status) && m.send_at - now() < 30000);
  pollTimer = setTimeout(async () => {
    await refreshEntries();
    if (Date.now() - lastScheduleLoad > 60000) await refreshSchedule();
    schedulePoll();
  }, soon ? 1000 : 5000);
}

function setTab(tab) {
  state.tab = tab;
  document.querySelectorAll('.tabs [role=tab]').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === tab)));
  renderQueue();
}

document.querySelectorAll('.tabs [role=tab]').forEach((b) => b.addEventListener('click', () => setTab(b.dataset.tab)));

function renderQueue() {
  const upcoming = state.entries.filter((m) => UPCOMING.has(m.status)).sort((a, b) => a.send_at - b.send_at);
  const history = state.entries.filter((m) => !UPCOMING.has(m.status)).sort((a, b) => b.send_at - a.send_at);
  $('#count-upcoming').textContent = upcoming.length;
  $('#count-history').textContent = history.length;

  const list = state.tab === 'upcoming' ? upcoming : history;
  $('#queue-list').replaceChildren(...list.map(renderItem));
  const empty = $('#queue-empty');
  empty.hidden = list.length > 0;
  empty.textContent = state.tab === 'upcoming'
    ? 'You are not in any line. Pick a class in the timetable and take a place.'
    : 'Nothing here yet.';
  tick();
}

const LOCK_MS = 30000; // places are fixed this long before a line opens (lines.LOCK_SECONDS)

function metaLine(m) {
  if (m.status === 'sent') return sentStats(m);
  if (m.error) return h('div', { class: 'item-meta' }, h('span', { class: 'err' }, m.error));
  if (m.status === 'pending') {
    return h('div', { class: 'item-meta' }, m.send_at - now() > LOCK_MS
      ? 'You move up if someone ahead of you leaves.'
      : 'Places are fixed now.');
  }
  return null;
}

function renderItem(m) {
  const upcoming = UPCOMING.has(m.status);
  const chat = m.topic_name ? `${m.peer_name} › ${m.topic_name}` : m.peer_name;
  const lesson = m.lesson_start != null ? `${fmtDay(m.lesson_start)} · pair ${m.pair} at ${fmtHM(m.lesson_start)}` : null;
  const actions = h('div', { class: 'item-actions' },
    m.status === 'pending' ? h('button', { onclick: () => leaveLine(m) }, 'Leave') : null,
    !upcoming ? h('button', { onclick: () => removeEntry(m) }, 'Remove') : null,
    h('span', { class: `badge ${m.status}` }, m.status === 'pending' ? 'in line' : m.status));

  return h('li', { class: 'item' },
    avatar(m.peer_name, m.peer_type),
    h('div', { class: 'item-head' },
      h('span', { class: 'item-name' }, m.text),
      h('span', { class: 'muted chat' }, `in ${chat}`)),
    actions,
    h('div', { class: 'item-sub' }, [m.group_name, lesson].filter(Boolean).join(' · ')),
    h('div', { class: 'item-sub' },
      h('span', { class: 'when', title: new Date(m.send_at).toISOString() }, `${upcoming ? 'Posts' : 'Due'} ${fmtDateTime(m.send_at)}`),
      upcoming ? h('span', { class: 'countdown', dataset: { at: m.send_at } }) : null),
    metaLine(m));
}

async function leaveLine(m) {
  try {
    await api(`/api/lines/${m.id}/cancel`, { method: 'POST' });
    toast(`You left the line for ${m.subject}.`);
  } catch (err) {
    toast(err.message);
  }
  await refreshEntries();
}

async function removeEntry(m) {
  try {
    await api(`/api/lines/${m.id}`, { method: 'DELETE' });
  } catch (err) {
    toast(err.message);
  }
  await refreshEntries();
}

// --- ticking -----------------------------------------------------------------

function tick() {
  const t = now();
  $('#clock-time').textContent = fmtTime(t);
  tickCountdowns(t);
  // A line just opened: drop its "Take a place" button.
  if (state.days.some((d) => d.lessons.some((l) => l.send_at > renderedAt && l.send_at <= t))) renderDays();
}

// --- boot --------------------------------------------------------------------

async function init() {
  setInterval(tick, 41);
  syncClock().catch(() => {});
  setInterval(() => syncClock().catch(() => {}), 10 * 60 * 1000);
  try {
    state.user = await api('/api/me');
    showApp();
  } catch {
    showLogin();
  }
}

init();
