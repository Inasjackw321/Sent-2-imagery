// Tests for the solar arithmetic behind the day/night layer.
//
// Run with:  node --test tests/sun.test.mjs
//
// The frontend has no test framework and this does not add one: it is node's
// own runner and node's own assert, against the module as the browser loads
// it. That is deliberate. This is the one piece of front-end code whose
// mistakes are invisible on screen -- a sign error still draws a smooth curve
// across the map, in the wrong place -- so it is the one piece that needs
// checking against facts rather than against a screenshot.
//
// The facts used here are all ones that hold independently of this code:
// where the sun stands at the solstices and equinoxes, that day and night are
// equal on the equator, that the arctic circle is where the midsummer sun
// stops setting, and that the sun is on the horizon at sunrise. None of them
// were taken from running this module.

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  subsolar, elevation, isDaylight, sunTimes, terminator, nightRing, TWILIGHT,
  MERCATOR_LIMIT,
} from '../frontend/js/sun.js';

const JUNE = new Date('2026-06-21T12:00:00Z');     // near the June solstice
const DECEMBER = new Date('2026-12-21T12:00:00Z'); // near the December one
const MARCH = new Date('2026-03-20T12:00:00Z');    // near the March equinox

const close = (got, want, slack, what) => assert.ok(
  Math.abs(got - want) <= slack,
  `${what}: got ${got}, expected ${want} ± ${slack}`);

test('the sun stands over the tropics at the solstices', () => {
  // The tropics are at the earth's axial tilt, 23.44 degrees, by definition.
  close(subsolar(JUNE).declination, 23.44, 0.2, 'June declination');
  close(subsolar(DECEMBER).declination, -23.44, 0.2, 'December declination');
  // And over the equator at an equinox -- which is what an equinox is.
  close(subsolar(MARCH).declination, 0, 0.5, 'March declination');
});

test('the subsolar point is under the noon meridian and travels west', () => {
  // At 12:00 UTC the sun is roughly over Greenwich, off by the equation of
  // time -- up to about four degrees, never more.
  close(subsolar(new Date('2026-06-21T12:00:00Z')).lon, 0, 4, 'noon longitude');

  // An hour later it has moved about fifteen degrees west, not east. This is
  // the check a sign error fails.
  const now = subsolar(JUNE).lon;
  const later = subsolar(new Date(JUNE.getTime() + 3600000)).lon;
  close(later - now, -15.04, 0.1, 'westward drift per hour');
});

test('the sun is overhead at the subsolar point and nowhere else', () => {
  const sun = subsolar(JUNE);
  close(elevation(sun.lat, sun.lon, JUNE), 90, 0.05, 'elevation under the sun');
  // Ninety degrees away it is on the horizon; on the far side, straight down.
  close(elevation(-sun.lat, sun.lon + 180, JUNE), -90, 0.05, 'antipodal elevation');
});

test('midnight is dark and midday is light, on the right sides', () => {
  const sun = subsolar(JUNE);
  assert.ok(isDaylight(sun.lat, sun.lon, JUNE), 'the subsolar point is in daylight');
  assert.ok(!isDaylight(-sun.lat, sun.lon + 180, JUNE), 'its antipode is not');
});

test('the sun really is on the horizon at the times reported', () => {
  // The strongest check available without a second implementation: feed the
  // answer back into the independent elevation calculation.
  for (const [lat, lon] of [[51.5, -0.12], [-33.9, 151.2], [35.7, 139.7], [0, 0]]) {
    for (const day of [JUNE, DECEMBER, MARCH]) {
      const t = sunTimes(lat, lon, day);
      if (t.polar) continue;
      close(elevation(lat, lon, t.sunrise), -0.833, 0.05,
        `elevation at sunrise ${lat},${lon}`);
      close(elevation(lat, lon, t.sunset), -0.833, 0.05,
        `elevation at sunset ${lat},${lon}`);
      // And noon is the highest the sun gets, by a clear margin.
      const noon = elevation(lat, lon, t.noon);
      assert.ok(noon > elevation(lat, lon, new Date(t.noon.getTime() - 3600000)),
        'noon beats an hour earlier');
      assert.ok(noon > elevation(lat, lon, new Date(t.noon.getTime() + 3600000)),
        'noon beats an hour later');
    }
  }
});

test('day and night are equal on the equator, all year', () => {
  for (const day of [JUNE, DECEMBER, MARCH]) {
    // Twelve hours, plus a few minutes for the sun's own width.
    close(sunTimes(0, 0, day).hours, 12.12, 0.1, 'day length on the equator');
  }
});

test('the sun does not set above the arctic circle in June', () => {
  assert.equal(sunTimes(78, 15, JUNE).polar, 'day', 'Svalbard in June');
  assert.equal(sunTimes(78, 15, DECEMBER).polar, 'night', 'Svalbard in December');
  // And the other pole is the other way round at the same moment.
  assert.equal(sunTimes(-78, 15, JUNE).polar, 'night', 'Antarctica in June');
  assert.equal(sunTimes(-78, 15, DECEMBER).polar, 'day', 'Antarctica in December');
});

test('day length grows with latitude in summer and shrinks in winter', () => {
  const june = [0, 20, 40, 55].map((lat) => sunTimes(lat, 0, JUNE).hours);
  for (let i = 1; i < june.length; i += 1) {
    assert.ok(june[i] > june[i - 1], `June day length rises: ${june}`);
  }
  const december = [0, 20, 40, 55].map((lat) => sunTimes(lat, 0, DECEMBER).hours);
  for (let i = 1; i < december.length; i += 1) {
    assert.ok(december[i] < december[i - 1], `December day length falls: ${december}`);
  }
});

test('the terminator is exactly where the sun is on the horizon', () => {
  for (const day of [JUNE, DECEMBER, MARCH]) {
    const ring = terminator(day, 0, 5);
    assert.equal(ring.length, 73, 'one point every five degrees, inclusive');
    for (const [lat, lon] of ring) {
      close(elevation(lat, lon, day), 0, 0.02, `terminator point ${lat},${lon}`);
    }
  }
});

test('the twilight curves are where that twilight actually begins', () => {
  // Every date, not just a solstice. Near an equinox the sun stays close to
  // the equator, so on the meridian beneath it the pole itself is only a few
  // degrees short of the horizon and deep twilight is never reached at all --
  // the curve runs off the globe. That case only exists away from the
  // solstices, and checking one date hid a bug that drew the astronomical
  // twilight band across the equator, in full daylight.
  for (const day of [JUNE, DECEMBER, MARCH, new Date('2026-09-01T06:00:00Z')]) {
    for (const [name, altitude] of Object.entries(TWILIGHT)) {
      const ring = terminator(day, altitude, 10);
      for (const [lat, lon] of ring) {
        if (Math.abs(lat) >= 89.99) {
          // Run off the globe: the pole is the honest answer, but only if the
          // sun really never gets that low anywhere along this meridian.
          const best = [-90, -60, -30, 0, 30, 60, 90]
            .map((l) => elevation(l, lon, day))
            .reduce((a, b) => Math.min(a, b));
          assert.ok(best > altitude - 1e-6,
            `${name} clamped to a pole at lon ${lon}, but the sun does reach `
            + `${altitude}° there (lowest found ${best.toFixed(2)}°)`);
          continue;
        }
        close(elevation(lat, lon, day), altitude, 0.05, `${name} twilight at ${lon}`);
      }
    }
  }
});

test('the night ring closes around the pole that is actually dark', () => {
  // In June the south is dark, so the shape must close at the south pole. Get
  // this backwards and the layer shades the daylight instead -- which looks
  // completely convincing until you check it against a clock.
  assert.ok(nightRing(JUNE, 0, 30).at(-1)[0] < -80,
    'June night closes towards the south pole');
  assert.ok(nightRing(DECEMBER, 0, 30).at(-1)[0] > 80,
    'December night closes towards the north pole');

  // Not at the pole itself. Web Mercator sends ninety degrees to infinity, so
  // a polygon with a vertex there is handed to the clipper as a coordinate it
  // cannot hold; what comes back is folded, and it draws as vertical seams
  // ruled across the whole shaded half of the map.
  for (const day of [JUNE, DECEMBER, MARCH]) {
    for (const [lat] of nightRing(day, TWILIGHT.astronomical, 10)) {
      assert.ok(Math.abs(lat) <= MERCATOR_LIMIT,
        `ring point at ${lat} is outside what the projection can draw`);
    }
  }

  // And a point just inside the closing edge really is in darkness.
  assert.ok(!isDaylight(-89, 0, JUNE), 'the south pole is dark in June');
  assert.ok(isDaylight(89, 0, JUNE), 'the north pole is not');
});

test('sunrise comes before noon comes before sunset', () => {
  const t = sunTimes(48.85, 2.35, MARCH);
  assert.ok(t.sunrise < t.noon, 'sunrise before noon');
  assert.ok(t.noon < t.sunset, 'noon before sunset');
  close((t.sunset - t.sunrise) / 3600000, t.hours, 0.001, 'hours matches the span');
});
