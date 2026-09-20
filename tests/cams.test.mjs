// Tests for the clock under a camera.
//
// Run with:  node --test tests/cams.test.mjs
//
// The thing being fixed: every camera stamped its frame with the VIEWER's
// clock. A road in Vladivostok at local midnight, shown to somebody in
// London, read "16:20" -- and whether it is dark in the picture because it
// is night there is the first question anybody asks of a webcam.
//
// Half of these are about the data rather than the code, and deliberately: a
// zone is one word per camera, and one wrong word puts a camera's clock
// hours out while everything still runs. So every zone is checked for being
// real, and checked against the longitude it sits at.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { CAMS, clockAt } from '../frontend/js/cams.js';

const NOON = new Date(Date.UTC(2026, 8, 16, 12, 0, 0));
const MIDWINTER = new Date(Date.UTC(2026, 0, 16, 12, 0, 0));

test('every camera carries a timezone', () => {
  const without = CAMS.filter((cam) => !cam.tz).map((cam) => cam.id);
  assert.deepEqual(without, [], `cameras with no zone: ${without}`);
});

test('every zone is one the runtime actually knows', () => {
  // An unknown name throws rather than guessing, which is right and is a
  // terrible thing to discover in a panel.
  for (const cam of CAMS) {
    assert.doesNotThrow(
      () => new Intl.DateTimeFormat([], { timeZone: cam.tz }).format(NOON),
      `${cam.id}: ${cam.tz} is not a timezone`);
  }
});

// How far a zone may sit from the sun over its own longitude.
//
// Three hours. Real zones are generous -- Spain runs on Berlin's clock, and
// western China on Beijing's -- but nothing legitimate is four hours out, so
// this catches the mistake that actually happens: the right-looking zone
// from the wrong continent.
const SLACK_HOURS = 3;

function offsetHours(tz, when) {
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: tz, timeZoneName: 'longOffset',
  }).formatToParts(when);
  const said = parts.find((p) => p.type === 'timeZoneName').value;
  const hit = /GMT([+-])(\d{2}):(\d{2})/.exec(said);
  if (!hit) return 0;
  return (hit[1] === '-' ? -1 : 1) * (Number(hit[2]) + Number(hit[3]) / 60);
}

test('every zone is plausible for where its camera is', () => {
  for (const cam of CAMS) {
    if (!Number.isFinite(cam.lon)) continue;
    const solar = cam.lon / 15;
    for (const when of [NOON, MIDWINTER]) {
      const off = offsetHours(cam.tz, when);
      assert.ok(Math.abs(off - solar) <= SLACK_HOURS,
        `${cam.id} (${cam.place}) at lon ${cam.lon} is on ${cam.tz}, `
        + `which is ${off} — ${Math.abs(off - solar).toFixed(1)}h from the sun`);
    }
  }
});

test('the clock reads in the camera\'s zone, not this machine\'s', () => {
  assert.equal(clockAt({ tz: 'Europe/Moscow' }, NOON).time, '15:00:00');
  assert.equal(clockAt({ tz: 'Asia/Taipei' }, NOON).time, '20:00:00');
  assert.equal(clockAt({ tz: 'America/Panama' }, NOON).time, '07:00:00');
});

test('including the zones that are not whole hours', () => {
  assert.equal(clockAt({ tz: 'Asia/Kolkata' }, NOON).time, '17:30:00');
});

test('and it follows daylight saving rather than a fixed offset', () => {
  // The reason a zone name is stored rather than an offset. Kyiv is UTC+3 in
  // September and UTC+2 in January; a stored offset would be wrong for half
  // the year, every year.
  assert.equal(clockAt({ tz: 'Europe/Kyiv' }, NOON).time, '15:00:00');
  assert.equal(clockAt({ tz: 'Europe/Kyiv' }, MIDWINTER).time, '14:00:00');
});

test('it says whose time it is', () => {
  const said = clockAt({ tz: 'Europe/Moscow' }, NOON);
  assert.ok(said.where && said.where !== 'your time');
  assert.equal(said.local, true);
});

test('a camera with no zone falls back and admits it', () => {
  // A time labelled as somewhere it is not is worse than an honest local one.
  for (const cam of [{}, { tz: '' }, { tz: 'Nowhere/Real' }, null]) {
    const said = clockAt(cam, NOON);
    assert.equal(said.where, 'your time', JSON.stringify(cam));
    assert.equal(said.local, false);
  }
});

test('the stamp is drawn from the camera clock, not the viewer clock', () => {
  // Read off the source: there is no DOM here. Both kinds of camera have a
  // stamp and both used to call toLocaleTimeString on the viewer's clock.
  const source = readSource();
  assert.ok(!/stamp\.textContent = new Date\(\)\.toLocaleTimeString/.test(source),
    'a still camera still stamps the viewer clock');
  assert.ok(!/stamp\.textContent = .*when\.toLocaleTimeString/.test(source),
    'a dated camera still stamps the viewer clock');
  // Calls, not the definition, which shares the same opening.
  assert.equal((source.match(/= clockAt\(cam/g) ?? []).length, 2,
    'both stamps should go through clockAt');
});

function readSource() {
  return readFileSync(
    fileURLToPath(new URL('../frontend/js/cams.js', import.meta.url)), 'utf8');
}

test('every camera has an id of its own', () => {
  // The id keys the pin, the wall tile and the refresh timer. Two cameras
  // sharing one means the second quietly replaces the first everywhere.
  const seen = CAMS.map((cam) => cam.id);
  assert.equal(new Set(seen).size, seen.length,
    `duplicated: ${seen.filter((id, i) => seen.indexOf(id) !== i)}`);
});

test('every camera has somewhere to be drawn', () => {
  for (const cam of CAMS) {
    assert.ok(Number.isFinite(cam.lat) && Number.isFinite(cam.lon), cam.id);
    assert.ok(Math.abs(cam.lat) <= 90 && Math.abs(cam.lon) <= 180, cam.id);
  }
});

// Every shape a camera comes in. An unlisted one is a typo that shows as a
// pin with nothing behind it, which looks exactly like a camera that is down.
const KINDS = [undefined, 'still', 'hls', 'dated', 'embed'];

test('every camera is a kind the app can draw', () => {
  for (const cam of CAMS) {
    assert.ok(KINDS.includes(cam.kind), `${cam.id}: ${cam.kind}`);
  }
});

test('every camera has something to show, over https', () => {
  for (const cam of CAMS) {
    // A dated camera builds its address from the clock, so it has a template
    // where the others have a src -- but it still has to be https, because
    // the page is and a mixed-content image is silently blocked.
    const from = cam.kind === 'dated' ? cam.template : cam.src;
    assert.match(from ?? '', /^https:\/\//, cam.id);
  }
});
