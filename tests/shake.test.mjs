// Tests for the Raspberry Shake layer in the seismic panel.
//
// Run with:  node --test tests/shake.test.mjs
//
// The behaviour worth pinning down is the separation. Four hobby
// seismographs sitting in the same list as the federated research stations,
// in the same colour, is an invitation to read one trace against the other
// as though they measured the same thing -- and they do not: a Shake is a
// geophone on a floor and a door closing registers on it. So: its own
// checkbox, its own colour, its own layer, and turning one layer off must
// not take the other's windows down with it.
//
// The source is read with its comments stripped before anything is matched
// in it, because a test that finds the phrase it is looking for inside its
// own explanatory comment passes whatever the code does.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { isShakeWindow } from '../frontend/js/seismic.js';

const here = fileURLToPath(new URL('.', import.meta.url));
const raw = readFileSync(`${here}../frontend/js/seismic.js`, 'utf8');
const css = readFileSync(`${here}../frontend/css/app.css`, 'utf8');
const api = readFileSync(`${here}../frontend/js/api.js`, 'utf8');

/** The file with every comment taken out, so a match is a match in the code. */
const code = raw
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .split('\n')
  .map((line) => line.replace(/(^|\s)\/\/.*$/, '$1'))
  .join('\n');

// ── Telling a Shake's window from a station's ──────────────────

test('a Shake trace window is recognised as one', () => {
  assert.equal(isShakeWindow('trace:AM.RD834'), true);
});

test('a federated station trace window is not', () => {
  assert.equal(isShakeWindow('trace:IU.KIEV'), false);
});

test('a camera window is not a Shake window', () => {
  assert.equal(isShakeWindow('cam:tulcea'), false);
});

test('a station whose code happens to start with AM is not a Shake', () => {
  assert.equal(isShakeWindow('trace:GE.AMBA'), false);
});

test('a network that merely begins with AM is not the AM network', () => {
  assert.equal(isShakeWindow('trace:AMX.0001'), false);
});

// ── They are a layer of their own ──────────────────────────────

test('the Shakes have their own checkbox', () => {
  assert.match(code, /id: 'seisShakes'/);
});

test('the checkbox is called what it was asked to be called', () => {
  assert.match(code, /'Raspberry Shake seismographs'/);
});

test('the Shakes have their own layer group', () => {
  assert.match(code, /shakeLayer = L\.layerGroup\(\)/);
});

test('the Shakes have their own canvas, so a redraw leaves the rest alone', () => {
  assert.match(code, /shakeCanvas = L\.canvas\(/);
});

test('their pins are drawn in the Shake colour, not the stations’', () => {
  // The pin itself, not just the constant: a named colour that nothing uses
  // is a comment, and the map would still show two identical blue dots.
  assert.match(code, /color: air \? MIC_COLOUR : SHAKE_COLOUR,/);
  const stations = code.match(/color: '(#[0-9a-f]{6})', fillColor: '#0d1015'/i);
  assert.ok(stations, 'the station pin colour moved');
  const shakeColour = code.match(/const SHAKE_COLOUR = '(#[0-9a-f]{6})'/i);
  assert.ok(shakeColour, 'the Shake pin colour moved');
  assert.notEqual(shakeColour[1].toLowerCase(), stations[1].toLowerCase());
});

test('and a Boom is a third colour, not a second Shake', () => {
  // Same network and same panel, different instrument. Drawn identically,
  // the only way to find out that a trace is air pressure rather than ground
  // motion is to open it.
  const mic = code.match(/const MIC_COLOUR = '(#[0-9a-f]{6})'/i);
  const shakeColour = code.match(/const SHAKE_COLOUR = '(#[0-9a-f]{6})'/i);
  const stations = code.match(/color: '(#[0-9a-f]{6})', fillColor: '#0d1015'/i);
  assert.ok(mic, 'the Boom pin colour moved');
  for (const other of [shakeColour[1], stations[1]]) {
    assert.notEqual(mic[1].toLowerCase(), other.toLowerCase());
  }
  // And it is the colour the key shows beside the word, rather than a
  // fourth one written out again next to it.
  assert.match(code, /color:\$\{MIC_COLOUR\}/);
});

test('the legend shows the Shake colour rather than hard-coding it again', () => {
  assert.match(code, /color:\$\{SHAKE_COLOUR\}/);
});

// ── One layer's windows are not the other's ────────────────────

test('turning the stations off spares the Shake windows', () => {
  assert.match(code, /closeAll\(\(id\) => id\.startsWith\(WIN\) && !isShakeWindow\(id\)\)/);
});

test('turning the Shakes off closes only theirs', () => {
  assert.match(code, /closeAll\(isShakeWindow\)/);
});

// ── Asking for them ────────────────────────────────────────────

test('they are fetched from their own endpoint', () => {
  assert.match(api, /\/api\/shake/);
});

test('and asked for by rectangle, so what is in view is found', () => {
  // The named stations are a decision; what else of this network is under
  // the current view is a question, and it can only be answered by asking
  // about the rectangle. This is what puts an instrument on a coast nobody
  // thought to name.
  const line = api.match(/shakes: \([^)]*\)/);
  assert.ok(line, 'the shakes accessor moved');
  assert.equal(line[0], 'shakes: (box)');
  assert.match(api, /west: box\.west\.toFixed\(4\)/);
});

test('but the rectangle is optional, so a first look still works', () => {
  // Before the map has settled there is no view worth asking about, and an
  // endpoint that demanded one would leave the named list empty until it had.
  assert.match(api, /\/api\/shake\$\{box \?/);
});

test('a view inside one already asked about does not ask again', () => {
  assert.match(code, /if \(!force && shakes && shakesCovered\?\.contains\(view\)\)/);
});

test('and a pan that leaves it does', () => {
  assert.match(code, /if \(showShakes\) loadShakes\(\);/);
});

test('two clicks at once only ask once', () => {
  assert.match(code, /if \(shakesInFlight\) return;/);
});

// ── What a reader is told ──────────────────────────────────────

test('the panel says a door closing registers on one', () => {
  assert.match(code, /door closing/);
});

test('the trace footnote says it is not a research instrument', () => {
  assert.match(code, /not a research instrument/);
});

test('a pin on the town says so rather than passing for a position', () => {
  // Marked when it IS a town, not when it is not a surveyed station: a
  // position published with the instrument is a better claim than the middle
  // of a city and must not be labelled as a worse one.
  assert.match(code, /placed === 'town'/);
  assert.ok(!/placed === 'station'/.test(code),
    'the town marker is deciding on the wrong end of the three positions');
});

test('each one links to Raspberry Shake’s own live viewer', () => {
  assert.match(code, /href: s\.view/);
});

test('the list has somewhere to be drawn', () => {
  assert.match(code, /id: 'seisShakeList'/);
  assert.match(css, /\.seis-list\s*\{/);
});

test('the Shake links are styled, not left as default blue', () => {
  assert.match(css, /\.seis-link\s*\{/);
});

// ── The trace refreshing itself ────────────────────────────────
//
// A trace window shows the last stretch of ground motion, which stops being
// the last stretch the moment it is drawn. Fetched once, it is a photograph:
// it goes stale without ever saying so, and somebody watching for something
// happening now would be watching a picture of ten minutes ago.

test('an open trace refetches itself every minute', () => {
  assert.match(code, /const REFRESH_MS = 60000;/);
  assert.match(code, /setInterval\(\(\) => \{[\s\S]*?fetchTrace\([\s\S]*?\}, REFRESH_MS\)/);
});

test('closing a window stops its timer', () => {
  // Otherwise it keeps fetching into a window nobody can see, for the life of
  // the page, once a minute, per window ever opened.
  assert.match(code, /onClose: \(\) => \{[\s\S]*?stopTicking\(id\);/);
  assert.match(code, /function stopTicking\(id\) \{\s*clearInterval\(ticking\.get\(id\)\);/);
});

test('a window that has gone stops the timer from the other end too', () => {
  assert.match(code, /if \(!isOpen\(id\)\) \{ stopTicking\(id\); return; \}/);
});

test('reopening does not leave the old timer running', () => {
  // plotStation runs again on a redraw, and a second interval for one window
  // would double the asking rate every time the window length was changed.
  const body = code.slice(code.indexOf('function plotStation'));
  assert.ok(/stopTicking\(id\);\s*\n\s*ticking\.set\(id, setInterval/.test(body),
    'the previous timer is not cleared before a new one is set');
});

test('a refresh does not throw the picture away while it waits', () => {
  // Replacing the image element once a minute makes the window blink; so does
  // putting the "Plotting…" notice back. The source is swapped instead.
  assert.match(code, /let image = plot\.querySelector\('img'\);/);
  assert.match(code, /image\.src = blobUrl;/);
});

test('a refresh that fails leaves the trace that is there', () => {
  assert.match(code, /if \(quiet && plot\.querySelector\('img'\)\)/);
});

test('but it says so rather than showing an old picture as the present', () => {
  assert.match(code, /not updating/);
  assert.match(code, /is-stale/);
  assert.match(css, /\.trace-live\.is-stale/);
});

test('every refresh hands back the picture it replaced', () => {
  // The bytes live in the object URL, not the element. One a minute, for as
  // long as a window is open, is a leak with a clock on it.
  assert.match(code, /URL\.revokeObjectURL\(previous\)/);
});

test('the window says when what is on screen was fetched', () => {
  assert.match(code, /data-live/);
  assert.match(code, /updated \$\{clockNow\(\)\}/);
  assert.match(css, /\.trace-live \{/);
});

// ── Booms: the same network, a different instrument ────────────
//
// A Raspberry Boom is a barometer sampling fast enough to hear. What crosses
// a red line on one is a pressure wave in the air -- a blast, a sonic boom,
// thunder -- and the ground may not have moved at all. Drawn and captioned
// as a seismograph, it turns a sonic boom into an earthquake.

test('a Boom is told apart on the map by shape as well as colour', () => {
  assert.match(code, /radius: air \? 2\.6 : 1\.5/);
});

test('and in the list, where a colour alone is not enough', () => {
  assert.match(code, /el\('span', \{ class: 'seis-mic' \}, 'mic'\)/);
  assert.match(css, /\.seis-mic \{/);
});

test('its trace is captioned as air pressure, not ground motion', () => {
  const at = code.indexOf('function footnote(');
  const block = code.slice(at, code.indexOf('\n}\n', at));
  const mic = block.indexOf("station.kind === 'microphone'");
  assert.ok(mic > 0, 'the footnote should answer a microphone first');
  assert.ok(mic < block.indexOf('vertical ground motion'),
            'a Boom must be answered before the seismograph wording');
  assert.match(block, /air pressure at a Raspberry Boom/);
  assert.match(block, /An infrasound microphone, not a seismograph/);
});

test('and its window is titled a Boom rather than a Shake', () => {
  assert.match(code, /'Raspberry Boom' : 'Raspberry Shake'/);
});

// ── What the rectangle found ───────────────────────────────────

test('the panel says how many are in view, not how many are listed', () => {
  // Eight named stations elsewhere in the world plus two here is not "ten in
  // view", and it is the second number somebody watching a coast wants.
  const at = code.indexOf('function foundHere(');
  const block = code.slice(at, code.indexOf('\n}\n', at));
  assert.match(block, /data\.in_view/);
  assert.match(block, /in view/);
  assert.match(block, /nearest shown/);
});

test('an empty rectangle says so rather than saying nothing', () => {
  const at = code.indexOf('function foundHere(');
  const block = code.slice(at, code.indexOf('\n}\n', at));
  assert.match(block, /None of this network in view/);
  // And says where the list above came from, so it is not read as this
  // coast's instruments.
  assert.match(block, /named ones/);
});

test('the microphones in view are counted apart from the named ones', () => {
  const at = code.indexOf('function foundHere(');
  const block = code.slice(at, code.indexOf('\n}\n', at));
  assert.match(block, /!s\.asked_for && s\.kind === 'microphone'/);
});

test('a station labelled with its own position does not say it twice', () => {
  // Its place IS its coordinates when nobody named it, and "in 48.974°N
  // 2.322°E, 48.9735° N, 2.3225° E" reads as two different places.
  const at = code.indexOf('function whereItIs(');
  const block = code.slice(at, code.indexOf('\n}\n', at));
  assert.match(block, /station\.named === false \? `at \$\{at\}`/);
  const foot = code.indexOf('function footnote(');
  const body = code.slice(foot, code.indexOf('\n}\n', foot));
  assert.ok(!/\$\{station\.place\}, \$\{fmt\.coord/.test(body),
            'the footnote is writing the place and the coordinates itself again');
});
