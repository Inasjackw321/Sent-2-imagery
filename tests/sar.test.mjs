// Tests for what the panel says about a Sentinel-1 pass.
//
// Run with:  node --test tests/sar.test.mjs
//
// The backend decides what a pass can draw. This half decides whether the
// person choosing between passes can see which is which -- and whether the
// picker offers a picture the chosen pass cannot make, which is the failure
// that used to come back as "Scene S1A_... has no vv asset".

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { canMake, changePlan, changeSaid, sarLabel, sarPolarisations, sarSaid }
  from '../frontend/js/sarview.js';

const LAND = { satellite: 'sentinel-1', polarisations: ['VV', 'VH'],
               mode: 'IW', orbit: 36, orbit_state: 'descending' };
const ICE = { satellite: 'sentinel-1', polarisations: ['HH', 'HV'],
              mode: 'EW', orbit: 14, orbit_state: 'ascending' };
// With its orbit fields filled in, because Sentinel-2 publishes those too.
// An empty optical scene would come out blank whether or not the label
// checked the satellite at all, and prove nothing.
const OPTICAL = { satellite: 'sentinel-2', date: '2026-09-01', cloud: 4,
                  orbit: 108, orbit_state: 'descending' };

// ── The line under a pass in the date list ─────────────────────

test('a land pass reads as one', () => {
  assert.equal(sarLabel(LAND), 'Descending · track 36 · IW · VV+VH');
});

test('and an ice pass as the other', () => {
  assert.equal(sarLabel(ICE), 'Ascending · track 14 · EW · HH+HV');
});

test('an optical scene gets none of it', () => {
  // It keeps its tile, which is the useful thing about an optical scene.
  assert.equal(sarLabel(OPTICAL), '');
  assert.equal(sarLabel(null), '');
});

test('a pass missing a field says the rest rather than nothing', () => {
  assert.equal(sarLabel({ ...LAND, orbit: null }),
               'Descending · IW · VV+VH');
  assert.equal(sarLabel({ satellite: 'sentinel-1', polarisations: ['VV'] }),
               'VV');
});

test('track zero is a track, not a missing one', () => {
  // Relative orbit numbers start at 1 in practice, but a falsy check here
  // would drop it silently if one ever arrived.
  assert.match(sarLabel({ ...LAND, orbit: 0 }), /track 0/);
});

test('the direction is the first thing said', () => {
  // It is what decides whether this pass is comparable with the one above
  // it, which is the question somebody scrolling a date list is asking.
  assert.ok(sarLabel(LAND).startsWith('Descending'));
});

// ── Which polarisations a pass carries ─────────────────────────

test('the pair the catalogue named', () => {
  assert.deepEqual(sarPolarisations(LAND), ['VV', 'VH']);
});

test('upper-cased, whatever the catalogue wrote', () => {
  assert.deepEqual(sarPolarisations({ polarisations: ['vv', 'vh'] }),
                   ['VV', 'VH']);
});

test('nothing known is nothing claimed', () => {
  assert.deepEqual(sarPolarisations({}), []);
  assert.deepEqual(sarPolarisations(null), []);
  assert.deepEqual(sarPolarisations({ polarisations: [] }), []);
});

// ── Which pictures the picker may offer ────────────────────────

const RADAR_COLOR = { bands: ['vv', 'vh', 'vvvh'] };
const RADAR_GREY = { bands: ['vv', 'vv', 'vv'] };
const RADAR_COLOR_HH = { bands: ['hh', 'hv', 'hhhv'] };
const TRUE_COLOR = { bands: ['red', 'green', 'blue'] };

test('a land pass can make the VV pictures', () => {
  const have = new Set(['VV', 'VH']);
  assert.equal(canMake(RADAR_COLOR, have), true);
  assert.equal(canMake(RADAR_GREY, have), true);
});

test('and not the HH ones', () => {
  assert.equal(canMake(RADAR_COLOR_HH, new Set(['VV', 'VH'])), false);
});

test('an ice pass is the other way round', () => {
  const have = new Set(['HH', 'HV']);
  assert.equal(canMake(RADAR_COLOR_HH, have), true);
  assert.equal(canMake(RADAR_COLOR, have), false);
});

test('a VV-only pass keeps the plain picture and loses the false colour', () => {
  // Refusing the whole pass would be refusing the ground. Backscatter on its
  // own is a picture.
  const have = new Set(['VV']);
  assert.equal(canMake(RADAR_GREY, have), true);
  assert.equal(canMake(RADAR_COLOR, have), false);
});

test('a derived channel needs both of its parts', () => {
  // radar colour's third channel is VV minus VH, so a VV-only pass cannot
  // make it even though it has the band for the first two channels.
  assert.equal(canMake({ bands: ['vvvh'] }, new Set(['VV'])), false);
  assert.equal(canMake({ bands: ['vvvh'] }, new Set(['VV', 'VH'])), true);
});

test('an optical composite is not measured against polarisations at all', () => {
  assert.equal(canMake(TRUE_COLOR, new Set(['VV'])), true);
});

test('a composite with no bands is not refused', () => {
  assert.equal(canMake({}, new Set(['VV'])), true);
});

// ── What is said under the picture ─────────────────────────────

test('the pass is described rather than coded', () => {
  // "EW" is not an explanation. Somebody looking at a softer picture needs
  // to know it is a 40 m mode, not a bad render.
  const said = sarSaid({ sar: { pass_name: 'Ascending', track: 14, mode: 'EW',
                                mode_name: 'Extra Wide', pair: 'HH+HV',
                                product: 'GRD' } });
  assert.match(said, /Ascending/);
  assert.match(said, /track 14/);
  assert.match(said, /EW — Extra Wide/);
  assert.match(said, /HH\+HV/);
});

test('an optical render says nothing here', () => {
  assert.equal(sarSaid({ sar: {} }), '');
  assert.equal(sarSaid({}), '');
  assert.equal(sarSaid(null), '');
});

// ── The wiring ─────────────────────────────────────────────────

const PANEL = readFileSync(
  new URL('../frontend/js/imagery.js', import.meta.url), 'utf8');

test('the picker lists only what the chosen pass can draw', () => {
  const at = PANEL.indexOf('function buildVisualisationOptions');
  const block = PANEL.slice(at, PANEL.indexOf('\n}\n', at));
  assert.match(block, /sarPolarisations\(chosenScene\(\)\)/);
  assert.match(block, /if \(pol\.size && !canMake\(spec, pol\)\) continue;/);
});

test('and nothing is hidden when the pass is not known either way', () => {
  // pol.size guards it. Without that, a scene list whose properties have not
  // arrived would show an empty picker, which looks exactly like a broken app.
  const at = PANEL.indexOf('function buildVisualisationOptions');
  assert.match(PANEL.slice(at, PANEL.indexOf('\n}\n', at)), /pol\.size &&/);
});

test('the fallback lands on a picture that is on the list', () => {
  // Falling through to the satellite's default would put an HH pass back on
  // a VV picture, which is the failure this whole change is about.
  const at = PANEL.indexOf('function buildVisualisationOptions');
  // To the end of the function rather than a fixed number of
  // characters from its start: a line added inside it used to push the
  // line being looked for out of the window, and the test then failed
  // for a reason that had nothing to do with what it checks.
  const block = PANEL.slice(at, PANEL.indexOf('\n}\n', at));
  assert.match(block, /compGroup\.firstElementChild\?\.value/);
});

test('averaging passes of different geometry is warned about', () => {
  assert.match(PANEL, /function reportSar/);
  assert.match(PANEL, /if \(meta\.sar_merge\) toast\(meta\.sar_merge, 'warn'\)/);
  assert.match(PANEL, /reportSar\(data\.meta\);/);
});

test('a caveat is not dressed up as an error', () => {
  // The app did what was asked. It is a statement about what the picture
  // means, and calling it an error teaches people to dismiss errors.
  assert.doesNotMatch(PANEL, /toast\(meta\.sar_merge, 'err'\)/);
  const ui = readFileSync(new URL('../frontend/js/ui.js', import.meta.url), 'utf8');
  assert.match(ui, /kind === 'err' \|\| kind === 'warn' \? 7000/);
  const css = readFileSync(new URL('../frontend/css/app.css', import.meta.url), 'utf8');
  assert.match(css, /\.toast\.warn \{/);
});

test('the picture list is rebuilt from one place, not per call site', () => {
  // Twice I wired this at the call sites that change the pass, and twice I
  // missed one: first the search path, which sets the date itself, and that
  // is how a picker came to offer a picture that failed on every render --
  // seven identical red toasts and no imagery.
  //
  // So it lives in sync(), which every state change already goes through.
  // Pinned there, and the call sites are pinned to call sync() rather than
  // to call the rebuild, which is a contract with four fewer ways to be
  // wrong.
  const at = PANEL.indexOf('function sync() {');
  assert.ok(at > 0, 'sync() should exist');
  assert.match(PANEL.slice(at, PANEL.indexOf('\n}\n', at)),
               /buildVisualisationOptions\(\);/);
});

// ── Comparing two passes ───────────────────────────────────────

test('a change is offered for radar, and only where two dates can be ticked', () => {
  const at = PANEL.indexOf('function buildVisualisationOptions');
  const block = PANEL.slice(at, PANEL.indexOf('\n}\n', at));
  assert.match(block, /sat\.kind === 'radar' && mode === 'merge'/);
  assert.match(block, /value: 'change:pair'/);
});

test('the button says it is comparing, not averaging', () => {
  // A change render subtracts one pass from the other. A button reading
  // "Average 2 radar passes" over it is describing a different picture.
  const at = PANEL.indexOf('function sync() {');
  const block = PANEL.slice(at, PANEL.indexOf('\n}\n', at));
  assert.match(block, /'Compare these two passes'/);
  assert.match(block, /Tick exactly two passes/);
});

test('and it is pressable only on exactly two', () => {
  // Not "at least two": three passes have no single before and after.
  const at = PANEL.indexOf('function sync() {');
  assert.match(PANEL.slice(at, PANEL.indexOf('\n}\n', at)),
               /comparing\s*\?\s*dates\.length === 2/);
});

test('the line above the button says how far apart the two passes are', () => {
  const said = changePlan([{ date: '2026-09-23' }, { date: '2026-09-11' }],
                          '1024 px');
  assert.match(said, /12 days apart/);
  assert.match(said, /decibels/);
  // Nothing is averaged in a change, so none of the merge wording is true
  // of it.
  assert.doesNotMatch(said, /speckle|merge|average/i);
  // Whichever way round the list is scrolled. Ticking upwards used to read
  // "-12 days apart", which is not a distance.
  assert.equal(changePlan([{ date: '2026-09-11' }, { date: '2026-09-23' }],
                          '1024 px'), said);
});

test('and counts what is ticked while it is the wrong number', () => {
  assert.match(changePlan([{ date: '2026-09-23' }], '1024 px'),
               /Tick exactly two passes — <b>1 ticked<\/b>/);
  assert.match(changePlan([], '1024 px'), /<b>0 ticked<\/b>/);
  assert.match(changePlan([{ date: 'a' }, { date: 'b' }, { date: 'c' }],
                          '1024 px'), /<b>3 ticked<\/b>/);
});

test('a date it cannot read costs the gap, not the line', () => {
  const said = changePlan([{ date: '2026-09-23' }, {}], '1024 px');
  assert.doesNotMatch(said, /NaN|undefined|null/);
  assert.match(said, /decibels/);
});

test('what came back is reported as a comparison', () => {
  // The headline of a change picture is how much of it moved: a pale wash
  // is a quiet fortnight or a broken render, and they look identical.
  const said = changeSaid({ change: { older: '2026-09-11', newer: '2026-09-23',
                                      days: 12, band: 'vv', moved_pct: 15.86,
                                      moved_above_db: 3.0 } });
  assert.match(said, /^VV change/);
  assert.match(said, /over 12 days/);
  assert.match(said, /2026-09-11 → 2026-09-23/);
  // With the threshold said, so the percentage can be read rather than
  // guessed at.
  assert.match(said, /15\.86% of it moved by over 3 dB/);
});

test('and the dates are shown the way the rest of the app shows them', () => {
  const said = changeSaid({ change: { older: '2026-09-11', newer: '2026-09-23',
                                      days: 12, band: 'vv' } },
                          (d) => `[${d}]`);
  assert.match(said, /\[2026-09-11\] → \[2026-09-23\]/);
});

test('an ordinary render is not described as a change', () => {
  assert.equal(changeSaid({ scenes: [1, 2], source: { kind: 'radar' } }), '');
  assert.equal(changeSaid({}), '');
  assert.equal(changeSaid(null), '');
});

test('a change is not reported as an average of its two passes', () => {
  // describeResult has an earlier branch for several radar passes, and a
  // change arrives with exactly that shape.
  const at = PANEL.indexOf('function describeResult(');
  const block = PANEL.slice(at, PANEL.indexOf('\n}\n', at));
  assert.ok(block.indexOf('meta.change') > 0
            && block.indexOf('meta.change') < block.indexOf('radar passes averaged'),
            'a change must be answered before the averaging line');
});

test('and every path that changes the chosen pass goes through it', () => {
  for (const fn of ['function pick(date, on) {', 'function tickBest(n) {',
                    'async function runSearch() {']) {
    const at = PANEL.indexOf(fn);
    assert.ok(at > 0, `${fn} should exist`);
    const body = PANEL.slice(at, PANEL.indexOf('\n}\n', at));
    assert.match(body, /\bsync\(\);/, fn);
  }
});
