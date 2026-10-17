// Tests for the internet connectivity panel.
//
// Run with:  node --test tests/connectivity.test.mjs
//
// The backend decides what each probe is saying. This half decides whether
// the reader can see it: whether the probes that just went dark stand out
// from the thousands that did not, and whether the headline number is the
// right fraction of the right thing.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { areaSaid, countLine, edges, isGraphWindow, STATES, whereSaid }
  from '../frontend/js/connectivity.js';

const SOURCE = readFileSync(
  new URL('../frontend/js/connectivity.js', import.meta.url), 'utf8');
const CSS = readFileSync(new URL('../frontend/css/app.css', import.meta.url), 'utf8');
const HTML = readFileSync(new URL('../frontend/index.html', import.meta.url), 'utf8');
const MAP = readFileSync(new URL('../frontend/js/map.js', import.meta.url), 'utf8');

// ── The headline ───────────────────────────────────────────────

const answer = (summary, extra = {}) => ({
  count: 1, hours: 6, summary: { up: 0, down: 0, back: 0, gone: 0, share_down: 0, ...summary },
  ...extra,
});

test('the headline says how many went offline, and out of how many', () => {
  const said = countLine(answer({ up: 90, down: 6, back: 4, share_down: 0.06 }));
  assert.match(said, /<b>94<\/b> of 100 online/);
  assert.match(said, /<b class="net-bad">6<\/b> went offline in the last 6 h \(6\.0%\)/);
  assert.match(said, /4 came back/);
});

test('ones already offline before the window are not in the denominator', () => {
  // Five already gone since last week could not go offline today. Counting
  // them would make "2 of 17" out of what is really "2 of 12".
  const said = countLine(answer({ up: 10, down: 2, gone: 5, share_down: 2 / 12 }));
  assert.match(said, /of 12 online/);
  assert.doesNotMatch(said, /of 17/);
});

test('a quiet window is said to be quiet, not left blank', () => {
  const said = countLine(answer({ up: 40 }));
  assert.match(said, /none went offline in the last 6 h/);
  assert.doesNotMatch(said, /net-bad/);
});

test('no probes at all is said in words', () => {
  assert.equal(countLine({ count: 0, hours: 6, summary: {} }),
               'No RIPE Atlas probes in view');
});

test('a capped answer says so, and what to do about it', () => {
  assert.match(countLine(answer({ up: 1 }, { capped: true })), /zoom in for the rest/);
  assert.doesNotMatch(countLine(answer({ up: 1 })), /zoom in/);
});

// ── What stands out ────────────────────────────────────────────

test('going offline is the biggest, brightest thing on the map', () => {
  // A continental view is thousands of steady probes. The three that just
  // went dark have to be the first thing seen, not the last.
  assert.ok(STATES.down.radius > STATES.back.radius);
  assert.ok(STATES.back.radius > STATES.gone.radius);
  assert.ok(STATES.gone.radius > STATES.up.radius);
  assert.ok(STATES.down.fill > STATES.up.fill);
});

test('and is drawn last, on top of everything else', () => {
  assert.match(SOURCE, /const LAYERING = \['up', 'gone', 'back', 'down'\];/);
});

test('every state has a colour of its own', () => {
  const colours = Object.values(STATES).map((s) => s.colour.toLowerCase());
  assert.equal(new Set(colours).size, colours.length);
});

test('the key shows every state, in the order of how much it matters', () => {
  assert.match(SOURCE, /\['down', 'back', 'gone', 'up'\]\.map\(\(key\) => el\('div', \{ class: 'net-key-row' \}/);
});

// ── Honest about what a probe is ───────────────────────────────

test('the panel says one probe is one site and many is an outage', () => {
  assert.match(SOURCE, /One going dark is one site; many in one area at once is an/);
  assert.match(SOURCE, /outage/);
});

test('and that the positions are blurred', () => {
  // RIPE blurs them on purpose, to protect the people hosting them. A dot
  // read as an address is a dot read wrong.
  assert.match(SOURCE, /Positions are blurred by RIPE/);
  assert.match(SOURCE, /blurred by RIPE`\)/);
});

test('an owner\'s description is shown as text, not markup', () => {
  // el() makes text nodes. A template literal into innerHTML would let a
  // stranger's description write into the page.
  assert.match(SOURCE, /probe\.description \? el\('div', \{\}, probe\.description\) : null/);
  assert.doesNotMatch(SOURCE, /innerHTML[^;]*description/);
});

test('the link to the probe is escaped and opens safely', () => {
  // On the graph window's own out-link now, which the window opens with
  // noopener -- one place that does it right rather than one per panel.
  assert.match(SOURCE, /link: `https:\/\/atlas\.ripe\.net\/probes\/\$\{encodeURIComponent\(probe\.id\)\}\/`/);
  const windows = readFileSync(new URL('../frontend/js/windows.js', import.meta.url), 'utf8');
  assert.match(windows, /class: 'win-out', href: link, target: '_blank', rel: 'noopener noreferrer'/);
});

// ── Asking ─────────────────────────────────────────────────────

test('it asks again when the map moves somewhere new', () => {
  assert.match(SOURCE, /map\.on\('moveend', debounce\(\(\) => \{ if \(enabled\) refresh\(\); \}/);
  assert.match(SOURCE, /if \(!force && covered\?\.contains\(view\)\) return;/);
});

test('and every few minutes while it is on, and not while it is off', () => {
  assert.match(SOURCE, /const REFRESH_MS = 3 \* 60 \* 1000;/);
  const at = SOURCE.indexOf('function toggle()');
  const block = SOURCE.slice(at, SOURCE.indexOf('\n}\n', at));
  assert.match(block, /timer = setInterval\(/);
  assert.match(block, /clearInterval\(timer\);/);
  // Nor while the tab is hidden.
  assert.match(block, /if \(!document\.hidden\) refresh\(\{ force: true \}\);/);
});

test('it is drawn on a canvas, so thousands of dots do not freeze a pan', () => {
  assert.match(SOURCE, /canvas = L\.canvas\(/);
  assert.match(SOURCE, /renderer: canvas,/);
});

// ── It is a tool like the others ───────────────────────────────

test('it is called what it was asked to be called', () => {
  assert.match(SOURCE, /'Internet connectivity'\)/);
});

test('it has a place in the dock, and is started with the map', () => {
  assert.match(HTML, /<div class="net-dock" id="connectivityDock"><\/div>/);
  assert.match(MAP, /import \{ initConnectivity \} from '\.\/connectivity\.js';/);
  assert.match(MAP, /initConnectivity\(map, \{ pickArea \}\);/);
});

test('and is styled, not left as bare buttons', () => {
  for (const rule of ['.net-toggle {', '.net-body {', '.net-window {',
                      '.net-country {', '.net-key {', '.net-note {']) {
    assert.ok(CSS.includes(rule), rule);
  }
});

test('a probe is clicked through a target wider than the dot', () => {
  // A steady probe is drawn three pixels across. Leaflet cancels a click if
  // the map moved at all, so missing one by a pixel pans the map instead.
  assert.match(SOURCE, /const HIT_RADIUS = 10;/);
  assert.match(SOURCE, /radius: HIT_RADIUS,\s*stroke: false, fill: true, fillOpacity: 0, interactive: true,/);
  // And the dot itself does not compete with its own target for the click.
  assert.match(SOURCE, /opacity: state === 'up' \? 0\.5 : 0\.95,\s*interactive: false,/);
  assert.ok(Object.values(STATES).every((s) => s.radius < 10));
});

// ── Each probe's graph, like a seismograph's trace ─────────────

const block = (start) => {
  const at = SOURCE.indexOf(start);
  assert.ok(at >= 0, `${start} should exist`);
  return SOURCE.slice(at, SOURCE.indexOf('\n}\n', at));
};

test('clicking a probe opens its graph, the way clicking a station does', () => {
  assert.match(SOURCE, /\.on\('click', \(\) => openGraph\(probe\)\)/);
  assert.doesNotMatch(SOURCE, /bindPopup/);
});

test('and clicking it again closes it', () => {
  assert.match(block('function openGraph('), /if \(isOpen\(id\) && !redraw\) \{\s*closeWindow\(id\);\s*return;/);
});

test('it redraws itself every minute while it is open', () => {
  assert.match(SOURCE, /const GRAPH_REFRESH_MS = 60000;/);
  const open = block('function openGraph(');
  assert.match(open, /setInterval\(\(\) => \{[\s\S]*?fetchGraph\([\s\S]*?\{ quiet: true \}\)/);
  // One timer per window, never two.
  assert.ok(/stopTicking\(id\);\s*\n\s*ticking\.set\(id, setInterval/.test(open));
});

test('and stops when it is closed, or when the layer is switched off', () => {
  assert.match(block('function openGraph('), /onClose: \(\) => \{\s*graphs\.delete\(id\);\s*stopTicking\(id\);/);
  assert.match(block('function toggle()'), /closeAll\(isGraphWindow\);/);
  assert.match(block('function openGraph('), /if \(!isOpen\(id\)\) \{ stopTicking\(id\); return; \}/);
});

test('its windows are told apart from every other kind', () => {
  assert.equal(isGraphWindow('net:123'), true);
  assert.equal(isGraphWindow('trace:AM.R1F39'), false);
  assert.equal(isGraphWindow('cam:tulcea'), false);
});

test('a failed refresh keeps the graph that is there, and says so', () => {
  const fetchBlock = block('async function fetchGraph(');
  assert.match(fetchBlock, /if \(quiet && plot\.querySelector\('img'\)\)/);
  assert.match(fetchBlock, /not updating — \$\{err\.message\}/);
});

test('a slow answer cannot overwrite a newer one', () => {
  const fetchBlock = block('async function fetchGraph(');
  assert.match(fetchBlock, /if \(tokens\.get\(id\) !== token\) \{ URL\.revokeObjectURL\(blobUrl\); return; \}/);
});

test('each refresh hands its old picture back', () => {
  assert.match(block('async function fetchGraph('),
    /if \(previous && previous !== blobUrl\) URL\.revokeObjectURL\(previous\);/);
});

test('changing the span redraws the graphs already open, of both kinds', () => {
  const span = block('function setHistory(');
  assert.match(span, /for \(const \[id, what\] of \[\.\.\.graphs\.entries\(\)\]\)/);
  assert.match(span, /if \(what\.area\) openArea\(what\.area, \{ redraw: true, id \}\);/);
  assert.match(span, /else openGraph\(what, \{ redraw: true \}\);/);
});

test('the title bar says what the probe is doing and since when', () => {
  const said = whereSaid({ state: 'down', since: new Date(Date.now() - 2 * 3.6e6).toISOString(),
                           country: 'UA', asn: 15895 });
  assert.match(said, /^Went offline .* ago · UA · AS15895$/);
  assert.equal(whereSaid({ state: 'up', country: null, asn: null }), 'Online');
});

test('the note under it says how to read the lines', () => {
  const note = block('function graphNote(');
  assert.match(note, /online \(green\) and offline \(red\)/);
  assert.match(note, /round trip to k\.root-servers\.net/);
  assert.match(note, /lost /);
});

test('it looks like the seismograph windows', () => {
  // The same plot, waiting and live-stamp classes, so the two kinds of graph
  // read as one kind of thing.
  const open = block('function openGraph(');
  assert.match(open, /el\('div', \{ class: 'trace-plot' \}\)/);
  assert.match(open, /el\('div', \{ class: 'trace-wait' \}/);
  assert.match(open, /el\('div', \{ class: 'trace-live', 'data-live': '' \}/);
  assert.ok(CSS.includes('.net-span {'));
});

// ── A whole area's graph ───────────────────────────────────────

test('there is a button to graph an area, in the map\'s own tool family', () => {
  // data-draw makes it light while armed, and Escape or a click elsewhere puts
  // it away like any other tool -- rather than a second, private box tool.
  assert.match(SOURCE, /class: 'net-area', id: 'netArea', type: 'button', dataset: \{ draw: 'pick' \}/);
  assert.match(SOURCE, /'▭ Graph an area'/);
});

test('it uses the box tool the map hands it, rather than importing the map', () => {
  // connectivity.js is loaded by map.js; importing back would be a cycle, and
  // would drag Leaflet into the panel's Node tests.
  assert.doesNotMatch(SOURCE, /from '\.\/map\.js'/);
  assert.match(SOURCE, /pickArea = tools\.pickArea \?\? null;/);
  assert.match(block('function armArea('), /pickArea\(\(bounds\) => openArea\(bounds\)\)/);
});

test('graphing an area switches the layer on if it was off', () => {
  assert.match(block('function armArea('), /if \(!enabled\) toggle\(\);/);
});

test('the box stays on the map while its graph is open, and goes with it', () => {
  const open = block('function openArea(');
  assert.match(open, /outlines\.set\(winId, L\.rectangle\(bounds, AREA_STYLE\)\.addTo\(map\)\)/);
  assert.match(open, /outlines\.get\(winId\)\?\.remove\(\);/);
});

test('area windows are graph windows, so switching the layer off closes them', () => {
  assert.equal(isGraphWindow('net:area:3'), true);
});

test('an area graph refreshes, but less often than one probe', () => {
  assert.match(SOURCE, /const AREA_REFRESH_MS = 5 \* 60 \* 1000;/);
  assert.match(block('function openArea('), /\}, AREA_REFRESH_MS\)\);/);
});

test('the edges sent are clamped to the globe', () => {
  const box = { getWest: () => -200, getSouth: () => -95, getEast: () => 190, getNorth: () => 91 };
  assert.deepEqual(edges(box), { west: -180, south: -90, east: 180, north: 90 });
});

test('the title bar reads the figures the backend sent with the picture', () => {
  const headers = new Map([['X-Name', encodeURIComponent('Житомирська область')],
                           ['X-Now', '68'], ['X-Low', '62'], ['X-Probes', '47']]);
  assert.equal(areaSaid(headers),
    'Житомирська область · now 68% · lowest 62% · 47 probes');
});

test('and leaves out what it was not told', () => {
  assert.equal(areaSaid(new Map([['X-Probes', '3']])), '3 probes');
  // Zero is a figure, not a missing one.
  assert.match(areaSaid(new Map([['X-Now', '0']])), /now 0%/);
});

test('it can be saved, as the very pixels on screen, with the mark', () => {
  const saveBlock = block('async function saveArea(');
  // The bytes that arrived, kept -- not fetched back from the image's blob:
  // address, which the page's security policy does not let fetch read. That
  // was the first version, and it failed with "Failed to fetch".
  assert.match(saveBlock, /const blob = pictures\.get\(plot\);/);
  assert.doesNotMatch(saveBlock, /fetch\(/);
  assert.match(block('async function fetchArea('), /pictures\.set\(plot, blob\)/);
  assert.match(saveBlock, /_kaldockhi\.png`/);
  assert.match(saveBlock, /savePicture\(/);
});

test('the save button waits until there is a picture to save', () => {
  assert.match(block('function openArea('), /class: 'net-save', type: 'button', disabled: true,/);
  assert.match(block('async function fetchArea('), /save\.disabled = false;/);
});
