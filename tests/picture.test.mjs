// Tests for what goes on a picture of one country.
//
// Run with:  node --test tests/picture.test.mjs
//
// "Make the alerts in Russia just Russia."
//
// The country buttons passed only the BOUNDS of a country's marks to the
// picture, and everything inside that rectangle went on it. Russia's marks
// run from Bryansk to Krasnodar, so the rectangle round them covers the
// whole of Ukraine -- and asking for Russia produced a picture of Ukraine's
// warnings with a few Russian ones at the edges.
//
// The rectangle cannot be made to exclude the ground between two countries.
// Only a real border can say which marks on it belong to which, which is
// what belongsTo is for.

import test from 'node:test';
import assert from 'node:assert/strict';

import { belongsTo, learnUkraine, picked } from '../frontend/js/tracker.js';

// A square "Ukraine" from 49N to 51N and 35E to 37E. Precise geometry rather
// than anything the demo invents, because the whole question is which side of
// a line a point falls on.
const SQUARE = [[35, 49], [37, 49], [37, 51], [35, 51], [35, 49]];

// A box that spans both countries, the way a fit round Russia's marks does.
const WIDE = {
  contains: (at) => at.lat >= 44 && at.lat <= 56 && at.lng >= 30 && at.lng <= 45,
};
const NARROW = {
  contains: (at) => at.lat >= 49.5 && at.lat <= 50.5
    && at.lng >= 35.5 && at.lng <= 36.5,
};

const mark = (lat, lng) => ({ marker: { getLatLng: () => ({ lat, lng }) } });

const inside = mark(50, 36);      // in the square: Ukraine
const outside = mark(50.5, 38);   // outside it, still in WIDE: not Ukraine

test('a Russian picture takes the marks outside Ukraine', () => {
  learnUkraine([{ in: 'ua', shape: { type: 'Polygon', coordinates: [SQUARE] } }]);
  assert.equal(belongsTo(outside, 'ru', WIDE), true);
});

test('and leaves out the ones inside it', () => {
  // The whole of the complaint: these were on the picture.
  learnUkraine([{ in: 'ua', shape: { type: 'Polygon', coordinates: [SQUARE] } }]);
  assert.equal(belongsTo(inside, 'ru', WIDE), false);
});

test('a Ukrainian picture is the other way round', () => {
  learnUkraine([{ in: 'ua', shape: { type: 'Polygon', coordinates: [SQUARE] } }]);
  assert.equal(belongsTo(inside, 'ua', WIDE), true);
  assert.equal(belongsTo(outside, 'ua', WIDE), false);
});

test('the box still bounds it', () => {
  // Belonging to the country is not enough: a mark outside the frame is not
  // in the picture whichever country it is in.
  learnUkraine([{ in: 'ua', shape: { type: 'Polygon', coordinates: [SQUARE] } }]);
  assert.equal(belongsTo(outside, 'ru', NARROW), false);
});

test('with no border learned yet, nothing is thrown away', () => {
  // inUkraine answers null until the provinces arrive, and null is not
  // false. Refusing every mark would make a picture of an empty country; the
  // box is all there is and it is better than nothing.
  learnUkraine([]);
  assert.equal(belongsTo(inside, 'ru', WIDE), true);
  assert.equal(belongsTo(inside, 'ua', WIDE), true);
});

test('a neighbour\'s outline does not make its ground Ukrainian', () => {
  // The borders that ship with the app are in the same list, tagged by
  // country. Only Ukraine's count towards this question.
  const POLAND = [[19, 49], [23, 49], [23, 53], [19, 53], [19, 49]];
  learnUkraine([
    { in: 'ua', shape: { type: 'Polygon', coordinates: [SQUARE] } },
    { in: 'pl', level: 'country', shape: { type: 'Polygon', coordinates: [POLAND] } },
  ]);
  const polish = mark(51, 21);
  assert.equal(belongsTo(polish, 'ru', { contains: () => true }), true);
  assert.equal(belongsTo(polish, 'ua', { contains: () => true }), false);
});

// And the rule the picture actually applies, which is the one that was wrong:
// it used to test the box alone whatever country had been asked for.

test('a picture of a country applies the country test', () => {
  learnUkraine([{ in: 'ua', shape: { type: 'Polygon', coordinates: [SQUARE] } }]);
  assert.equal(picked(inside, WIDE, 'ru'), false);
  assert.equal(picked(outside, WIDE, 'ru'), true);
});

test('a picture of no country in particular applies the box alone', () => {
  // "This view", "Everything", and a drawn box: there is no country to test
  // against and the frame is the whole of the question.
  learnUkraine([{ in: 'ua', shape: { type: 'Polygon', coordinates: [SQUARE] } }]);
  assert.equal(picked(inside, WIDE, null), true);
  assert.equal(picked(outside, WIDE, null), true);
  assert.equal(picked(outside, NARROW), false);
});
