// Telegram alerts, plotted where they say they happened.
//
// Signing in, choosing channels, and then a pin per event: an explosion, a
// drone, a missile, an aircraft taking off. The reading is done by a small
// local model, and where there is no model a word list does a cruder job.
//
// Two things this interface has to keep saying, because a pin on a map looks
// far more certain than the sentence behind it:
//
//   * which reader produced each alert, model or word list
//   * that a pin is the centre of a named town, not the place something landed
//
// A message with no place in it is listed but never plotted. There is no
// honest way to invent a position for it.

import { api } from './api.js';
import { $, el, toast } from './ui.js';
import { POPUP } from './fires.js';

let map = null;
let layer = null;
let canvas = null;
let enabled = false;
let timer = null;
let running = false;
let kinds = {};
let chosen = new Set();

// The backend polls Telegram once a minute; this asks the backend for what it
// has slightly more often, so a new alert never waits two minutes to appear.
const REFRESH_SECONDS = 25;

// Each kind draws its own glyph. Drawn as paths on a 24-box so they read at
// pin size, which emoji do not.
const GLYPHS = {
  explosion: 'M12 3l2.2 5.1L20 6.6l-3 4.6 4 3.2-5.4.6L16 20l-4-3-4 3 .4-5-5.4-.6 4-3.2-3-4.6 5.8 1.5z',
  missile: 'M12 2c2.2 2.4 3.4 5.4 3.4 8.6v4.2h-6.8v-4.2C8.6 7.4 9.8 4.4 12 2zM8.6 16.8H15.4L17 21h-3l-2-2-2 2H7z',
  drone: 'M5 5h4v1.6H7.4v2H5zm14 0v3.6h-2.4v-2H15V5zM5 19v-3.6h2.4v2H9V19zm14 0h-4v-1.6h1.6v-2H19zM9.6 9.6h4.8v4.8H9.6z',
  aircraft: 'M12 2l1.6 7.2 7.4 3.4v1.8l-7.4-1.6-.6 4.4 2.6 1.8V21L12 19.6 8.4 21v-1.4l2.6-1.8-.6-4.4L3 15v-1.8l7.4-3.4z',
  artillery: 'M3 16h5.6l9-6.6 1.8 2.4-8 5.8H21V19H3zm2.4 1.6h2.4V21H5.4z',
  air_defence: 'M12 2l8 3.4v5.2c0 4.6-3.2 8.6-8 11.4-4.8-2.8-8-6.8-8-11.4V5.4zm0 4.4l-4 2.2v3c0 2.6 1.6 4.8 4 6.4 2.4-1.6 4-3.8 4-6.4v-3z',
  alert: 'M12 2l10 18H2zm-1 6h2v6h-2zm0 8h2v2h-2z',
  other: 'M12 3a9 9 0 100 18 9 9 0 000-18zm-1 4h2v6h-2zm0 8h2v2h-2z',
};

export function initAlerts(leafletMap) {
  map = leafletMap;
  // Above everything else on the map: these are the things you opened the app
  // to see, and they should not end up under a ship or a seismograph.
  map.createPane('alerts').style.zIndex = 490;
  canvas = L.canvas({ pane: 'alerts', padding: 0.4 });
  layer = L.layerGroup();
  buildDock();
  refreshStatus();
}

// ── The panel ──────────────────────────────────────────────────

function buildDock() {
  const dock = $('#alertDock');
  if (!dock) return;
  dock.innerHTML = '';
  dock.append(
    el('button', { class: 'alert-toggle', id: 'alertToggle', onclick: toggle },
      el('span', { class: 'alert-mark' }, '◈'), 'Telegram alerts'),
    el('div', { class: 'alert-body', id: 'alertBody', hidden: true },
      el('div', { class: 'alert-state', id: 'alertState' }, 'Checking…'),
      el('div', { id: 'alertPanel' }),
      el('div', { class: 'alert-note' },
        'Signed in on this machine only. Nothing is written to disk, so a '
        + 'restart signs you out. A pin is the centre of a named place, not '
        + 'where something landed.')),
  );
}

function toggle() {
  enabled = !enabled;
  $('#alertToggle').classList.toggle('is-on', enabled);
  $('#alertBody').hidden = !enabled;
  if (enabled) {
    layer.addTo(map);
    refreshStatus();
    refresh();
    timer ??= setInterval(refresh, REFRESH_SECONDS * 1000);
  } else {
    layer.remove();
    layer.clearLayers();
    clearInterval(timer);
    timer = null;
  }
}

async function refreshStatus() {
  try {
    const state = await api.alertStatus();
    kinds = state.kinds ?? {};
    paint(state);
  } catch (err) {
    $('#alertState').textContent = `Could not reach the backend: ${err.message}`;
  }
}

/** The panel shows whichever step you are actually at. */
function paint(state) {
  const tg = state.telegram ?? {};
  const model = state.model ?? {};
  const panel = $('#alertPanel');
  const line = $('#alertState');
  if (!panel || !line) return;

  // The model is optional, and the difference it makes is worth stating: one
  // of these reads the message, the other matches words in it.
  line.innerHTML = model.ok
    ? `<span class="ok">Model ready</span> · ${model.detail}`
    : `<span class="warn">No model</span> · ${model.detail ?? 'falling back to word matching'}`;

  if (!tg.available) {
    panel.replaceChildren(el('div', { class: 'alert-need' },
      el('b', {}, 'Telegram support is not installed.'),
      el('code', {}, 'pip install telethon'),
      el('span', {}, 'Then restart the app.')));
    return;
  }

  if (!tg.signed_in) {
    panel.replaceChildren(signInForm(tg));
    return;
  }
  panel.replaceChildren(channelPicker(tg));
}

function signInForm(tg) {
  return el('div', { class: 'alert-form' },
    el('p', { class: 'alert-hint' },
      'api_id and api_hash come from my.telegram.org — they identify the app, '
      + 'not you. They go to Telegram and nowhere else.'),
    el('input', { id: 'tgApiId', type: 'text', inputmode: 'numeric', placeholder: 'api_id' }),
    el('input', { id: 'tgApiHash', type: 'password', placeholder: 'api_hash' }),
    el('input', { id: 'tgPhone', type: 'tel', placeholder: '+44…', value: tg.phone ?? '' }),
    el('button', { class: 'alert-go', id: 'tgSend', onclick: sendCode }, 'Send me a code'),
    el('div', { id: 'tgCodeRow', hidden: true },
      el('input', { id: 'tgCode', type: 'text', inputmode: 'numeric', placeholder: 'Code from Telegram' }),
      el('input', { id: 'tgPassword', type: 'password', placeholder: 'Two-step password, if you have one' }),
      el('button', { class: 'alert-go', id: 'tgSignIn', onclick: signIn }, 'Sign in')));
}

function channelPicker(tg) {
  return el('div', { class: 'alert-form' },
    el('div', { class: 'alert-row' },
      el('button', { class: 'alert-go', onclick: loadChannels }, 'Load my channels'),
      el('button', { class: 'alert-ghost', onclick: signOut }, 'Sign out')),
    el('div', { class: 'alert-channels', id: 'tgChannels' },
      el('span', { class: 'dim' }, tg.channels?.length
        ? `${tg.channels.length} watched` : 'No channels chosen yet.')),
    el('div', { class: 'alert-row' },
      el('button', {
        class: `alert-go${tg.polling ? ' is-on' : ''}`, id: 'tgRun', onclick: toggleRunning,
      }, tg.polling ? 'Stop watching' : 'Watch every minute'),
      el('button', { class: 'alert-ghost', onclick: pollNow }, 'Check now')),
    el('div', { class: 'alert-count', id: 'alertCount' }, ''),
    tg.last_error ? el('div', { class: 'alert-err' }, tg.last_error) : null);
}

// ── Signing in ─────────────────────────────────────────────────

async function sendCode() {
  const id = $('#tgApiId').value.trim();
  const hash = $('#tgApiHash').value.trim();
  const phone = $('#tgPhone').value.trim();
  if (!id || !hash || !phone) { toast('All three fields are needed.', 'err'); return; }
  $('#tgSend').disabled = true;
  try {
    await api.alertLogin({ api_id: Number(id), api_hash: hash, phone });
    $('#tgCodeRow').hidden = false;
    toast('Telegram has sent you a code.');
  } catch (err) {
    toast(`Telegram: ${err.message}`, 'err');
  } finally {
    $('#tgSend').disabled = false;
  }
}

async function signIn() {
  const code = $('#tgCode').value.trim();
  const password = $('#tgPassword').value;
  if (!code) { toast('Enter the code Telegram sent.', 'err'); return; }
  try {
    const out = await api.alertCode({ code, password });
    if (out.needs === 'password') {
      toast('This account has two-step verification. Enter the password too.', 'err');
      return;
    }
    toast('Signed in to Telegram.');
    refreshStatus();
  } catch (err) {
    toast(`Sign-in failed: ${err.message}`, 'err');
  }
}

async function signOut() {
  await api.alertLogout();
  running = false;
  refreshStatus();
}

// ── Channels ───────────────────────────────────────────────────

async function loadChannels() {
  const box = $('#tgChannels');
  box.replaceChildren(el('span', { class: 'dim' }, 'Asking Telegram…'));
  try {
    const { channels } = await api.alertChannels();
    box.replaceChildren(...channels.map((c) => el('label', { class: 'alert-chan' },
      el('input', {
        type: 'checkbox', value: c.id, checked: chosen.has(c.id),
        onchange: (e) => {
          if (e.target.checked) chosen.add(c.id); else chosen.delete(c.id);
          sendWatch();
        },
      }),
      el('span', {}, c.title))));
    if (!channels.length) box.replaceChildren(el('span', { class: 'dim' }, 'No channels found.'));
  } catch (err) {
    box.replaceChildren(el('span', { class: 'alert-err' }, err.message));
  }
}

const sendWatch = () => api.alertWatch({ channels: [...chosen], running })
  .catch((err) => toast(err.message, 'err'));

async function toggleRunning() {
  if (!chosen.size) { toast('Choose at least one channel first.', 'err'); return; }
  running = !running;
  try {
    await api.alertWatch({ channels: [...chosen], running });
    toast(running ? 'Watching, once a minute.' : 'Stopped watching.');
    refreshStatus();
    if (running) refresh();
  } catch (err) {
    running = false;
    toast(err.message, 'err');
  }
}

async function pollNow() {
  try {
    const out = await api.alertPoll();
    toast(`Read ${out.read} new message${out.read === 1 ? '' : 's'}.`);
    draw(out);
  } catch (err) {
    toast(err.message, 'err');
  }
}

// ── On the map ─────────────────────────────────────────────────

async function refresh() {
  if (!enabled) return;
  try {
    draw(await api.alerts());
  } catch (err) {
    $('#alertState').textContent = `Alerts unavailable: ${err.message}`;
  }
}

function draw(data) {
  layer.clearLayers();
  kinds = data.kinds ?? kinds;
  for (const alert of data.alerts) {
    if (alert.lat == null) continue;
    marker(alert).addTo(layer);
  }
  const count = $('#alertCount');
  if (count) {
    const unplaced = data.count - data.plotted;
    count.innerHTML = data.count
      ? `<b>${data.plotted}</b> on the map`
        + (unplaced ? ` · <span class="dim">${unplaced} with no place named</span>` : '')
      : 'Nothing yet.';
  }
}

function marker(alert) {
  const spec = kinds[alert.kind] ?? { colour: '#93a1b8', label: alert.kind };
  const glyph = GLYPHS[alert.kind] ?? GLYPHS.other;
  const fresh = minutesAgo(alert.at) < 15;

  return L.marker([alert.lat, alert.lon], {
    pane: 'alerts',
    riseOnHover: true,
    title: `${spec.label} — ${alert.place}`,
    icon: L.divIcon({
      className: `alert-pin${fresh ? ' is-fresh' : ''}`,
      html: `<svg viewBox="0 0 24 24" aria-hidden="true">
               <circle cx="12" cy="12" r="11" fill="#0d1015" opacity=".85"/>
               <path d="${glyph}" fill="${spec.colour}"/>
             </svg>`,
      iconSize: [26, 26], iconAnchor: [13, 13],
    }),
  }).bindPopup(() => popup(alert, spec), POPUP);
}

// Where each alert's classification came from. Said on every pin, because a
// pin looks equally certain whichever of these produced it.
const READ_BY = {
  model: 'Read by the local model.',
  words: 'Matched on words, not read — the kind may be wrong.',
  demo: 'Synthetic demo alert. Not a real report.',
};

const minutesAgo = (at) => {
  const t = Date.parse(at);
  return Number.isNaN(t) ? Infinity : (Date.now() - t) / 60000;
};

function popup(alert, spec) {
  const mins = minutesAgo(alert.at);
  const ago = mins < 60 ? `${Math.round(mins)} min ago`
    : mins < 1440 ? `${Math.round(mins / 60)} h ago` : `${Math.round(mins / 1440)} d ago`;

  return el('div', { class: 'alert-popup' },
    el('div', { class: 'alert-popup-head' },
      el('span', { class: 'alert-dot', style: `background:${spec.colour}` }),
      spec.label,
      el('span', { class: 'dim' }, ` · ${ago}`),
      alert.read_by === 'model' && !alert.confident
        ? el('span', { class: 'alert-unsure' }, 'unsure') : null),
    el('div', { class: 'alert-text' }, alert.text),
    el('div', { class: 'dim' }, alert.channel_title || alert.channel),
    // The two caveats, on every single pin, because this is where somebody
    // decides whether to believe it.
    el('div', { class: 'alert-caveat' },
      `Placed on ${alert.matched ?? alert.place} — the centre of the place named, `
      + 'not where anything landed.'),
    el('div', { class: 'alert-caveat' }, READ_BY[alert.read_by] ?? READ_BY.words),
  ).outerHTML;
}
