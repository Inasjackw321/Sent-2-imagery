// Tests for telling one country's marks from another's.
//
// Run with:  node --test tests/country.test.mjs
//
// Why this exists at all: nothing in a report says which country a place is
// in. The source's own region names the CHANNEL's beat, not the mark's. And
// no bounding box can do it either -- Belgorod sits at 50.6N 36.6E and
// Kharkiv at 50.0N 36.2E, a hundred kilometres apart and each inside any box
// drawn round the other's country. Only a real border separates them, and
// NEPTUN publish Ukraine's.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { ringHolds, learnUkraine, inUkraine } from '../frontend/js/tracker.js';

// A square "country" from 49N to 51N and 35E to 37E, written the way GeoJSON
// writes one: [lon, lat].
const SQUARE = [[35, 49], [37, 49], [37, 51], [35, 51], [35, 49]];
const at = (lat, lng) => ({ lat, lng });

test('a point inside a ring is inside it', () => {
  assert.equal(ringHolds(SQUARE, 36, 50), true);
});

test('and a point outside is not', () => {
  for (const [x, y] of [[34, 50], [38, 50], [36, 48], [36, 52]]) {
    assert.equal(ringHolds(SQUARE, x, y), false, `${x},${y}`);
  }
});

test('longitude and latitude are not swapped', () => {
  // The mistake that would put every mark in the wrong country while still
  // running: GeoJSON rings are [lon, lat] and a LatLng is the other way.
  assert.equal(ringHolds(SQUARE, 36, 50), true, 'lon 36 lat 50 should be in');
  assert.equal(ringHolds(SQUARE, 50, 36), false, 'the pair was swapped');
});

test('a concave country still holds its own points', () => {
  // A bite out of the east side: a point in the bite is not in the country,
  // and a point either side of it is.
  const notched = [[35, 49], [37, 49], [37, 49.8], [35.6, 49.8],
                   [35.6, 50.2], [37, 50.2], [37, 51], [35, 51], [35, 49]];
  assert.equal(ringHolds(notched, 36.5, 50.0), false, 'the bite counts as land');
  assert.equal(ringHolds(notched, 36.5, 49.5), true);
  assert.equal(ringHolds(notched, 36.5, 50.6), true);
});

test('the border is learned only from the country it belongs to', () => {
  const rings = learnUkraine([
    { name: 'a Ukrainian oblast', in: 'ua',
      shape: { type: 'Polygon', coordinates: [SQUARE] } },
    { name: 'somewhere else', in: 'elsewhere',
      shape: { type: 'Polygon', coordinates: [
        [[40, 49], [42, 49], [42, 51], [40, 51], [40, 49]]] } },
  ]);
  assert.equal(rings.length, 1, 'a foreign province was taken for Ukraine');
  assert.equal(inUkraine(at(50, 36)), true);
  assert.equal(inUkraine(at(50, 41)), false, 'the other country counts as Ukraine');
});

test('a country made of several pieces keeps all of them', () => {
  const rings = learnUkraine([
    { name: 'mainland', in: 'ua',
      shape: { type: 'MultiPolygon', coordinates: [
        [SQUARE], [[[30, 45], [31, 45], [31, 46], [30, 46], [30, 45]]]] } },
  ]);
  assert.equal(rings.length, 2);
  assert.equal(inUkraine(at(45.5, 30.5)), true, 'the second piece was dropped');
});

test('an enclave inside the country is still the country', () => {
  // A hole in a province is a city with its own boundary, and it is not a
  // piece of somewhere else.
  learnUkraine([{ name: 'oblast with a city in it', in: 'ua',
    shape: { type: 'Polygon', coordinates: [
      SQUARE, [[35.8, 49.8], [36.2, 49.8], [36.2, 50.2], [35.8, 50.2], [35.8, 49.8]]] } }]);
  assert.equal(inUkraine(at(50, 36)), true);
});

test('until the border is known, it says so rather than guessing', () => {
  // null, not false. "I do not know" and "it is not in Ukraine" are
  // different answers, and treating the first as the second would file every
  // Ukrainian mark under Russia for as long as the fetch took.
  learnUkraine([]);
  assert.equal(inUkraine(at(50, 36)), null);
  learnUkraine(undefined);
  assert.equal(inUkraine(at(50, 36)), null);
});

test('a shape it cannot read is skipped rather than thrown on', () => {
  const rings = learnUkraine([
    { name: 'no shape', in: 'ua', shape: null },
    { name: 'wrong type', in: 'ua', shape: { type: 'Point', coordinates: [36, 50] } },
    { name: 'a sliver', in: 'ua', shape: { type: 'Polygon', coordinates: [[[1, 1], [2, 2]]] } },
    { name: 'real', in: 'ua', shape: { type: 'Polygon', coordinates: [SQUARE] } },
  ]);
  assert.equal(rings.length, 1);
});

test('a country with one mark in it is not a street map', () => {
  // Fitting to a single point is infinite zoom: the map flew to a scale bar
  // reading ten metres -- a rooftop, with no border, no coast and no town in
  // view to say which country you asked for.
  const source = readFileSync(
    fileURLToPath(new URL('../frontend/js/tracker.js', import.meta.url)), 'utf8');
  assert.match(source, /const LEAST_VIEW = [\d.]+;/, 'no floor on the view');
  const floor = Number(/const LEAST_VIEW = ([\d.]+);/.exec(source)[1]);
  assert.ok(floor >= 0.2, `a floor of ${floor} degrees is no floor`);
  const block = source.slice(source.indexOf('function areaOf(which)'));
  assert.match(block.slice(0, block.indexOf('\n}')), /atLeast\(/,
    'the fitted bounds skip the floor');
  assert.match(source, /flyToBounds\(got\.bounds, \{ duration: 0\.7, maxZoom: \d+ \}\)/,
    'the flight has no zoom cap');
});

// Ukraine's own national outline now arrives in the same list as its
// provinces, tagged the same way. These are about that not disturbing the
// one question this file exists to answer.

test('the national outline counts as Ukraine too', () => {
  // It is tagged in: 'ua' like everything else Ukrainian, so it reaches
  // learnUkraine. That is right, and worth pinning: it fills the gaps
  // between provinces rather than leaving a point in one of them stateless.
  learnUkraine([{ name: 'Україна', in: 'ua', level: 'country',
                  shape: { type: 'Polygon', coordinates: [SQUARE] } }]);
  assert.equal(inUkraine(at(50, 36)), true);
  assert.equal(inUkraine(at(50, 38)), false);
});

test('and a neighbour\'s national outline does not', () => {
  // The whole point of the flag. Poland's border is in the same list and
  // must not make a Polish point read as Ukrainian.
  const POLAND = [[19, 49], [23, 49], [23, 53], [19, 53], [19, 49]];
  learnUkraine([
    { name: 'Україна', in: 'ua', level: 'country',
      shape: { type: 'Polygon', coordinates: [SQUARE] } },
    { name: 'Polska', in: 'pl', level: 'country',
      shape: { type: 'Polygon', coordinates: [POLAND] } },
  ]);
  assert.equal(inUkraine(at(51, 21)), false);
  assert.equal(inUkraine(at(50, 36)), true);
});
