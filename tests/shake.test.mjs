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
  assert.match(code, /color: SHAKE_COLOUR,/);
  const stations = code.match(/color: '(#[0-9a-f]{6})', fillColor: '#0d1015'/i);
  assert.ok(stations, 'the station pin colour moved');
  const shakeColour = code.match(/const SHAKE_COLOUR = '(#[0-9a-f]{6})'/i);
  assert.ok(shakeColour, 'the Shake pin colour moved');
  assert.notEqual(shakeColour[1].toLowerCase(), stations[1].toLowerCase());
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
  assert.match(api, /'\/api\/shake'/);
});

test('they are not asked for by rectangle', () => {
  const line = api.match(/shakes: \([^)]*\)/);
  assert.ok(line, 'the shakes accessor moved');
  assert.equal(line[0], 'shakes: ()');
});

test('a second look does not ask again', () => {
  assert.match(code, /if \(shakes\) \{ drawShakes\(shakes\); return; \}/);
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
  assert.match(code, /placed === 'station'/);
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
