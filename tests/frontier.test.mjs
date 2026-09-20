// Tests for lighting the stretch of a border with something flying near it.
//
// Run with:  node --test tests/frontier.test.mjs
//
// This is a warning about somebody else's airspace, which makes it the one
// thing on this map with a cost to being wrong in either direction. Lighting
// a border because a warning was declared two hundred kilometres away teaches
// people to ignore it; failing to light one with a Shahed four kilometres off
// it is the whole feature not working.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { apart, flying, hotSaid, hotSegments, toSegment, watched, NEAR_KM }
  from '../frontend/js/frontier.js';

// A north-south line at longitude 24, which is about where Poland's border
// with Ukraine runs. Points are [lon, lat], as GeoJSON has them.
const LINE = [[24, 50], [24, 50.5], [24, 51], [24, 51.5], [24, 52]];

const POLAND = { level: 'country', in: 'pl', name: 'Polska',
                 shape: { type: 'Polygon', coordinates: [LINE] } };
const BELARUS = { ...POLAND, in: 'by', name: 'Беларусь' };
const OBLAST = { level: 'region', in: 'pl', name: 'Lubelskie',
                 shape: { type: 'Polygon', coordinates: [LINE] } };

const drone = (lat, lon, over = {}) =>
  ({ kind: 'drone', lat, lon, placed: true, ...over });

// ── Distance ───────────────────────────────────────────────────

test('a degree of latitude is about 111 km', () => {
  assert.ok(Math.abs(apart(50, 24, 51, 24) - 111.2) < 1);
});

test('a degree of longitude is shorter this far north', () => {
  // Cos(50°) ≈ 0.64. Getting this wrong is what makes an east-west distance
  // half again too large, which at a 25 km threshold is the whole answer.
  assert.ok(Math.abs(apart(50, 24, 50, 25) - 71.5) < 1);
});

test('distance to a segment is measured to the segment', () => {
  // The reason this is not measured to the nearest drawn point: these rings
  // are thinned before they are sent, so a point halfway along a segment can
  // be far from both of its ends and right on the line.
  const middle = toSegment(50.5, 24, 50, 24, 51, 24);
  assert.ok(middle < 0.001, `${middle}`);
  assert.ok(apart(50.5, 24, 50, 24) > 50);
});

test('and past the end of a segment, to its end', () => {
  // Not to the infinite line it lies on, which would report a point a hundred
  // kilometres beyond the border as being on it.
  const beyond = toSegment(52, 24, 50, 24, 51, 24);
  assert.ok(Math.abs(beyond - 111.2) < 1.5, `${beyond}`);
});

test('a segment of no length is a point rather than a divide by zero', () => {
  const got = toSegment(50.1, 24, 50, 24, 50, 24);
  assert.ok(Number.isFinite(got) && got > 10 && got < 12, `${got}`);
});

// ── What is measured ───────────────────────────────────────────

test('drones and missiles are measured', () => {
  for (const kind of ['drone', 'jet_drone', 'fpv', 'missile']) {
    assert.equal(flying({ ...drone(50, 24), kind }), true, kind);
  }
});

test('an air alert is not', () => {
  // It has no position: it covers a province, and its coordinates are that
  // province's arithmetic centre.
  assert.equal(flying({ ...drone(50, 24), kind: 'alert' }), false);
});

test('nor is anything placed only to a region', () => {
  assert.equal(flying({ ...drone(50, 24), area_only: true }), false);
  assert.equal(flying({ ...drone(50, 24), region_scope: 'located' }), false);
  assert.equal(flying({ ...drone(50, 24), from_warning: true }), false);
});

test('nor anything the backend could not place at all', () => {
  assert.equal(flying({ ...drone(50, 24), placed: false }), false);
  assert.equal(flying({ kind: 'drone', placed: true }), false);
  assert.equal(flying(null), false);
});

// ── Whose border ───────────────────────────────────────────────

test('Poland, Romania and Moldova are watched', () => {
  for (const code of ['pl', 'ro', 'md']) {
    assert.equal(watched({ level: 'country', in: code }), true, code);
  }
});

test('Belarus and Russia are not', () => {
  // They are the other side of this war. A drone near the Belarusian border
  // is not the same fact and must not be drawn as though it were.
  for (const code of ['by', 'ru', 'ua']) {
    assert.equal(watched({ level: 'country', in: code }), false, code);
  }
});

test('and a province of Poland is not its border', () => {
  assert.equal(watched(OBLAST), false);
});

// ── The lit stretch ────────────────────────────────────────────

test('nothing near anything lights nothing', () => {
  assert.deepEqual(hotSegments([POLAND], [drone(50, 30)]), []);
  assert.deepEqual(hotSegments([POLAND], []), []);
  assert.deepEqual(hotSegments([], [drone(50, 24)]), []);
});

test('a drone just inside the range lights the border', () => {
  // 0.2° of longitude at 50°N is about 14 km.
  const got = hotSegments([POLAND], [drone(50.5, 24.2)]);
  assert.equal(got.length, 1);
  assert.equal(got[0].in, 'pl');
  assert.ok(got[0].km < NEAR_KM, `${got[0].km}`);
});

test('and one just outside it does not', () => {
  // 0.5° at 50°N is about 36 km.
  assert.deepEqual(hotSegments([POLAND], [drone(50.5, 24.5)]), []);
});

test('the range is the one that was asked for', () => {
  assert.equal(NEAR_KM, 25);
  // Either side of it, on the nose: 0.3° east at 50.5°N is ~21 km, 0.4° ~28.
  assert.equal(hotSegments([POLAND], [drone(50.5, 24.3)]).length, 1);
  assert.equal(hotSegments([POLAND], [drone(50.5, 24.4)]).length, 0);
});

test('a lit stretch is drawable — at least two points', () => {
  for (const run of hotSegments([POLAND], [drone(50.5, 24.1)])) {
    assert.ok(run.points.length >= 2, `${run.points.length}`);
  }
});

test('it carries how close the nearest thing actually was', () => {
  const got = hotSegments([POLAND], [drone(50.5, 24.1)]);
  // 0.1° of longitude at 50.5°N is about 7 km.
  assert.ok(Math.abs(got[0].km - 7.1) < 0.6, `${got[0].km}`);
});

test('a drone between two drawn points is still on the border', () => {
  // The reason distance is measured to the segment. These rings are thinned
  // before they are sent: this one has points 55 km apart, so a drone halfway
  // along it is 28 km from BOTH of them while sitting on the line. Measured
  // to the nearest drawn point it would read as outside 25 km and light
  // nothing, which is the border warning silently not working.
  const mid = hotSegments([POLAND], [drone(50.25, 24.02)]);
  assert.equal(mid.length, 1, 'a drone on the line must light it');
  assert.ok(mid[0].km < 2, `${mid[0].km}`);
  // And it really is far from every drawn point, so the test is about the
  // segment rather than about a vertex that happened to be close.
  for (const [lon, lat] of LINE) {
    assert.ok(apart(50.25, 24.02, lat, lon) > NEAR_KM,
      `${lat},${lon} is only ${apart(50.25, 24.02, lat, lon)} km away`);
  }
});

test('the distance reported is the nearest point of the stretch', () => {
  // Not the furthest, and not an average. A stretch runs as far as 25 km, so
  // a run whose near end is 3 km away and whose far end is 24 reports 3 --
  // the number a reader acts on is how close the thing actually got.
  const fine = [];
  for (let lat = 50; lat <= 51; lat += 0.02) fine.push([24, lat]);
  const border = { ...POLAND, shape: { type: 'Polygon', coordinates: [fine] } };
  const got = hotSegments([border], [drone(50.5, 24.04)]);
  assert.equal(got.length, 1);
  // 0.04° of longitude at 50.5°N is about 2.8 km, and the run reaches out to
  // nearly 25 km at its ends -- so min and max are far apart here.
  assert.ok(got[0].km < 4, `reported ${got[0].km}`);
  assert.ok(got[0].points.length > 10, `${got[0].points.length} points`);
});

test('a drone flying along the border lights the length of it', () => {
  // Not one dot per drone: the run is what makes this read as "this stretch"
  // rather than "this point".
  const along = [drone(50.1, 24.05), drone(50.9, 24.05), drone(51.6, 24.05)];
  const got = hotSegments([POLAND], along);
  const points = got.reduce((n, run) => n + run.points.length, 0);
  assert.equal(got.length, 1);
  assert.equal(points, LINE.length);
});

test('two drones far apart light two separate stretches', () => {
  const long = [];
  for (let lat = 45; lat <= 55; lat += 0.25) long.push([24, lat]);
  const border = { ...POLAND, shape: { type: 'Polygon', coordinates: [long] } };
  const got = hotSegments([border], [drone(46, 24.05), drone(54, 24.05)]);
  assert.equal(got.length, 2);
});

test('only the watched country is lit, even with a drone on both', () => {
  const got = hotSegments([POLAND, BELARUS], [drone(50.5, 24.05)]);
  assert.deepEqual(got.map((run) => run.in), ['pl']);
});

test('a multipolygon border is walked too', () => {
  // Romania's coast and Moldova's exclave both arrive as several rings.
  const many = { level: 'country', in: 'ro', name: 'România',
                 shape: { type: 'MultiPolygon', coordinates: [[LINE], [LINE]] } };
  assert.equal(hotSegments([many], [drone(50.5, 24.05)]).length, 2);
});

test('a shape that is nonsense lights nothing rather than throwing', () => {
  for (const shape of [null, {}, { type: 'Point', coordinates: [24, 50] },
                       { type: 'Polygon', coordinates: 'no' },
                       { type: 'Polygon', coordinates: [[[24]]] }]) {
    assert.doesNotThrow(
      () => hotSegments([{ level: 'country', in: 'pl', shape }],
                        [drone(50.5, 24.05)]), JSON.stringify(shape));
  }
});

// ── What it says ───────────────────────────────────────────────

test('nothing near anything says nothing', () => {
  assert.equal(hotSaid([]), '');
  assert.equal(hotSaid(null), '');
});

test('it names the country and the distance', () => {
  const said = hotSaid([{ in: 'pl', km: 4.2 }], { pl: 'Poland' });
  assert.match(said, /Poland 4 km/);
});

test('the closest of several is reported per country, nearest first', () => {
  const said = hotSaid([{ in: 'pl', km: 18 }, { in: 'ro', km: 6 },
                        { in: 'pl', km: 4 }],
                       { pl: 'Poland', ro: 'Romania' });
  assert.match(said, /Poland 4 km.*Romania 6 km/);
  assert.equal((said.match(/Poland/g) ?? []).length, 1);
});

// ── Both places it is drawn ────────────────────────────────────

const SHOT = readFileSync(
  new URL('../frontend/js/trackershot.js', import.meta.url), 'utf8');
const MAP = readFileSync(
  new URL('../frontend/js/tracker.js', import.meta.url), 'utf8');

test('the picture and the map light it from the same arithmetic', () => {
  // Asked for on both. Two implementations would drift, and a picture that
  // disagrees with the screen it was taken from is worse than no picture.
  assert.match(SHOT, /from '\.\/frontier\.js'/);
  assert.match(MAP, /from '\.\/frontier\.js'/);
  assert.match(SHOT, /hotSegments\(outlines, marks/);
  assert.match(MAP, /hotSegments\(outlines, events/);
});

test('the picture draws it over the border rather than instead of it', () => {
  // The CALL order, not the order the functions happen to be written in.
  // Drawn under the border, the glow is a smudge with a thin line down the
  // middle of it; drawn over, it is that border lit.
  const line = SHOT.indexOf('  drawFrontiers(ctx, frame, frontiers);\n  drawHotFrontier');
  assert.ok(line > 0, 'the lit stretch must be stroked after the border');
  const at = SHOT.indexOf('function drawHotFrontier');
  assert.match(SHOT.slice(at, at + 1400), /FRONTIER_GLOW/);
});

test('and it draws nothing at all when nothing is near', () => {
  // A picture where every border is lit all the time is a picture where the
  // lighting means nothing.
  const at = SHOT.indexOf('function drawHotFrontier');
  assert.match(SHOT.slice(at, at + 400), /if \(!runs\.length\) return;/);
});
