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
  subsolar, elevation, isDaylight, sunTimes, terminator, nightRing, nightRings,
  antisolar, darkRadius, poleInside, turns, tile, withinMap, capRing, TWILIGHT,
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

// ── The shape of the night ─────────────────────────────────────
//
// These are the tests that were missing, and their absence is why the layer
// shipped drawing vertical stripes down half the world.
//
// What was checked before: that every point on a twilight curve has the sun
// at that altitude. True, and not enough -- because the old code, when a
// meridian had no such point, answered with the pole, and a vertex at the
// pole passes a point-by-point check while being thousands of miles from the
// curve it claims to belong to. Strung together into a polygon, those
// vertices are spikes, and a stack of them is the striping.
//
// What is checked now: the SHAPE. A place is either shaded or it is not, and
// whether it should be is a fact about the sun that this file can work out
// independently. Nothing about the answer depends on how the polygon is
// built, which is the point.

/** Is a point inside a ring? Ray casting, on the ring's own longitudes. */
function inside(ring, lat, lon) {
  let crossings = 0;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i, i += 1) {
    const [ay, ax] = ring[i];
    const [by, bx] = ring[j];
    if ((ax > lon) !== (bx > lon)
        && ay + ((lon - ax) / (bx - ax)) * (by - ay) > lat) crossings += 1;
  }
  return crossings % 2 === 1;
}

/** Shaded by any of the rings, on any turn of longitude. */
const shaded = (rings, lat, lon) => rings.some(
  (ring) => [-720, -360, 0, 360, 720].some((turn) => inside(ring, lat, lon + turn)));

// A spread of dates and times: both solstices, both equinoxes, and a few
// ordinary days. The equinoxes are the ones that mattered -- at a solstice
// the old code was right.
const DATES = [
  '2026-03-20T12:00:00Z', '2026-06-21T12:00:00Z',
  '2026-09-22T06:30:00Z', '2026-09-22T18:00:00Z',
  '2026-12-21T12:00:00Z', '2026-02-05T03:00:00Z',
  '2026-04-30T21:00:00Z', '2026-08-14T09:00:00Z',
].map((iso) => new Date(iso));

const BANDS = [0, -4.5, -9, -13.5, -18];

test('every point on a twilight curve has the sun at that altitude', () => {
  for (const day of DATES) {
    for (const altitude of BANDS) {
      for (const [lat, lon] of terminator(day, altitude, 3)) {
        close(elevation(lat, lon, day), altitude, 0.01,
          `${day.toISOString()} band ${altitude} at ${lat},${lon}`);
      }
    }
  }
});

test('no point on a curve is parked at a pole to stand in for a missing one', () => {
  // The old failure, stated as what it was: a vertex at 90 degrees where the
  // band does not exist on that meridian. A curve that is a circle round the
  // antisolar point has no such case, so none should appear.
  for (const day of DATES) {
    for (const altitude of BANDS) {
      for (const [lat] of terminator(day, altitude, 3)) {
        assert.ok(Math.abs(lat) < 89.999,
          `${day.toISOString()} band ${altitude} has a vertex at ${lat}`);
      }
    }
  }
});

test('the curve never jumps from one side of a band to the other', () => {
  // Two latitudes solve the equation on most meridians, and taking whichever
  // came back first made the curve flap between them from sample to sample.
  // Consecutive points on a circle are a step apart, never a continent.
  for (const day of DATES) {
    for (const altitude of BANDS) {
      const ring = terminator(day, altitude, 1);
      for (let i = 1; i < ring.length; i += 1) {
        const jump = Math.abs(ring[i][0] - ring[i - 1][0]);
        assert.ok(jump < 5,
          `${day.toISOString()} band ${altitude}: ${jump.toFixed(1)}° between `
          + `neighbouring points (${ring[i - 1]} then ${ring[i]})`);
      }
    }
  }
});

test('what is shaded is what is actually dark', () => {
  // The test the striping would not have survived. A grid of places, each
  // one either inside the drawn shape or not, against what the sun is doing
  // there -- worked out from the elevation, which knows nothing about how the
  // polygon was built.
  let checked = 0;
  for (const day of DATES) {
    for (const altitude of BANDS) {
      const rings = nightRings(day, altitude, 2, 1);
      for (let lat = -80; lat <= 80; lat += 10) {
        for (let lon = -175; lon <= 175; lon += 15) {
          const sun = elevation(lat, lon, day);
          // Points sitting on the line are a tie: which side of a polygon
          // edge they land on is arithmetic noise, not a mistake.
          if (Math.abs(sun - altitude) < 0.6) continue;
          checked += 1;
          assert.equal(shaded(rings, lat, lon), sun < altitude,
            `${day.toISOString()} band ${altitude}: ${lat},${lon} has the sun `
            + `at ${sun.toFixed(1)}° and is drawn `
            + `${shaded(rings, lat, lon) ? 'dark' : 'light'}`);
        }
      }
    }
  }
  assert.ok(checked > 10000, `only ${checked} places checked`);
});

test('a curve that goes round the world is told from one that comes back', () => {
  // At a solstice the terminator winds: it passes through every longitude
  // once and has to be closed along a pole. At an equinox it runs up over the
  // pole and back down the other side, ending where it started, and closing
  // it along a pole as well would lay a band of shade across the daylight.
  assert.equal(Math.abs(turns(terminator(JUNE, TWILIGHT.astronomical, 2))), 1,
    'the June astronomical band winds around the world');
  assert.equal(turns(terminator(MARCH, TWILIGHT.astronomical, 2)), 0,
    'the March astronomical band closes on itself');
});

test('the night is bounded by the circle around the antisolar point', () => {
  // Which is what night is: the far side of the earth from the sun. The
  // radius follows from the altitude, because the sun's height at a place is
  // ninety degrees minus that place's distance from the point beneath it.
  for (const day of DATES) {
    const { lat, lon } = antisolar(day);
    close(elevation(lat, lon, day), -90, 0.01, 'sun below the antisolar point');
    for (const altitude of BANDS) {
      const ring = capRing(lat, lon, darkRadius(altitude), 5);
      for (const [a, b] of ring) {
        close(elevation(a, b, day), altitude, 0.01, `cap edge at ${a},${b}`);
      }
    }
  }
});

test('no ring is a sliver', () => {
  // A polygon of no height still paints. Clamping a curve that runs off the
  // top of the projection into the band used to leave a run of it lying flat
  // along the bottom of the map, and that drew as a hairline ruled across the
  // daylight at eighty-four degrees south. Measured against the box the ring
  // sits in, because "area above zero" passes for a hairline attached to
  // something real -- every honest band here fills at least a third of its
  // own box, and a hairline fills almost none of it.
  const area = (ring) => Math.abs(ring.reduce(
    (sum, [ay, ax], i) => {
      const [by, bx] = ring[(i + 1) % ring.length];
      return sum + (ax * by - bx * ay);
    }, 0) / 2);
  for (const day of DATES) {
    for (const altitude of BANDS) {
      for (const ring of nightRings(day, altitude, 2, 1)) {
        const lats = ring.map(([lat]) => lat);
        const lons = ring.map(([, lon]) => lon);
        const box = (Math.max(...lats) - Math.min(...lats))
          * (Math.max(...lons) - Math.min(...lons));
        const filled = area(ring) / box;
        assert.ok(filled > 0.3,
          `${day.toISOString()} band ${altitude}: a ring fills `
          + `${(filled * 100).toFixed(1)}% of its own box -- that is a `
          + `hairline, not a shape`);
      }
    }
  }
});

test('nothing is drawn where the projection cannot place it', () => {
  for (const day of DATES) {
    for (const altitude of BANDS) {
      for (const ring of nightRings(day, altitude, 3, 2)) {
        for (const [lat] of ring) {
          assert.ok(Math.abs(lat) <= MERCATOR_LIMIT,
            `a ring reaches ${lat}, past what Web Mercator can hold`);
        }
      }
    }
  }
});

test('the shading covers the longitudes it was asked for', () => {
  // Leaflet pans sideways for ever, so a shape tiled around the meridian runs
  // out partway across a view of the Pacific -- which is a band of daylight
  // down the middle of the night.
  const span = { west: 520, east: 900 };            // panned right round twice
  for (const day of DATES) {
    const rings = nightRings(day, 0, 3, span);
    const covered = rings.flat().map(([, lon]) => lon);
    assert.ok(Math.min(...covered) <= span.west,
      `shading starts at ${Math.min(...covered)}, east of ${span.west}`);
    assert.ok(Math.max(...covered) >= span.east,
      `shading stops at ${Math.max(...covered)}, west of ${span.east}`);
  }
});

test('a place is shaded the same however far round the world it is written', () => {
  const day = new Date('2026-09-22T06:30:00Z');
  const rings = nightRings(day, 0, 2, { west: -900, east: 900 });
  for (const lon of [-160, -40, 20, 140]) {
    const here = shaded(rings, 10, lon);
    assert.equal(shaded(rings, 10, lon + 360), here, `${lon} vs ${lon + 360}`);
    assert.equal(shaded(rings, 10, lon - 360), here, `${lon} vs ${lon - 360}`);
  }
});

test('tiling covers a span without piling up copies nobody can see', () => {
  const ring = [[0, -10], [10, 0], [0, 10], [-10, 0]];
  const copies = tile(ring, -180, 180);
  assert.ok(copies.length >= 1 && copies.length <= 3, `${copies.length} copies`);
  const wide = tile(ring, -1000, 1000);
  assert.ok(wide.length >= 5, `${wide.length} copies for five turns`);
  // And it never runs away, whatever it is handed.
  assert.ok(tile(ring, -1e9, 1e9).length <= 41);
  assert.deepEqual(tile([], -180, 180), []);
});

test('a curve is cut where it leaves the drawable band, not pinned to it', () => {
  const curve = [[0, 0], [60, 10], [88, 20], [89, 30], [60, 40], [0, 50]];
  const pieces = withinMap(curve);
  assert.equal(pieces.length, 2, 'the near-pole stretch splits it in two');
  assert.deepEqual(pieces[0], [[0, 0], [60, 10]]);
  assert.deepEqual(pieces[1], [[60, 40], [0, 50]]);
  // A single point left over is not a line and is dropped.
  assert.deepEqual(withinMap([[89, 0], [0, 10], [89, 20]]), []);
});

test('the single-ring helper still answers with the first of them', () => {
  const day = new Date('2026-06-21T12:00:00Z');
  assert.deepEqual(nightRing(day, 0, 10, 1), nightRings(day, 0, 10, 1)[0]);
});

test('a circle is drawn as one unbroken line, not cut at the date line', () => {
  // Longitudes come back running continuously, past 180 where the curve goes
  // past it. Wrapped into range instead, each crossing is a jump the width of
  // the world -- an edge straight across the map, and a fill to match.
  //
  // Near a pole a step of bearing really does sweep a lot of longitude: at an
  // equinox the terminator passes within a twentieth of a degree of the pole,
  // where two neighbouring samples can be ninety degrees apart in longitude
  // and a few hundred metres apart on the ground. So the rule is checked
  // where it means something -- away from the poles -- and the wrap itself,
  // which is a jump of a whole turn, is refused everywhere.
  for (const day of DATES) {
    for (const altitude of BANDS) {
      const ring = terminator(day, altitude, 2);
      for (let i = 1; i < ring.length; i += 1) {
        const jump = Math.abs(ring[i][1] - ring[i - 1][1]);
        assert.ok(Math.abs(jump - 360) > 20,
          `${day.toISOString()} band ${altitude}: longitude jumps a whole turn `
          + `(${jump.toFixed(0)}°) -- the curve was wrapped instead of run on`);
        if (Math.abs(ring[i][0]) < 80 && Math.abs(ring[i - 1][0]) < 80) {
          assert.ok(jump < 30,
            `${day.toISOString()} band ${altitude}: ${jump.toFixed(0)}° of `
            + `longitude between neighbouring points at ${ring[i][0].toFixed(0)}°`);
        }
      }
    }
  }
});

test('no ring lies flat along the edge it is closed on', () => {
  // The other half of "no sliver", and the one that catches it when the
  // hairline is attached to something real: a ring closed along the bottom of
  // the projection may touch that edge twice -- the two points that close it
  // -- and no more. A run of them lying along it is a stretch of the shape
  // with no height, and a fill with no height still paints a line.
  const edge = MERCATOR_LIMIT - 0.6;
  for (const day of DATES) {
    for (const altitude of BANDS) {
      for (const ring of nightRings(day, altitude, 2, 2)) {
        // A ring closed along an edge ends with the two points that close
        // it: same latitude, and a whole span apart in longitude. A loop that
        // happens to end on a clamped point is not that, and its flat top is
        // honest -- there is shape underneath it.
        const [lastLat, lastLon] = ring[ring.length - 1];
        const [prevLat, prevLon] = ring[ring.length - 2];
        const closing = lastLat;
        if (Math.abs(Math.abs(closing) - edge) > 1e-6
            || Math.abs(lastLat - prevLat) > 1e-9
            || Math.abs(lastLon - prevLon) < 90) continue;
        let run = 0;
        let worst = 0;
        for (const [lat] of ring.slice(0, -2)) {
          run = Math.abs(lat - closing) < 1e-6 ? run + 1 : 0;
          worst = Math.max(worst, run);
        }
        assert.ok(worst <= 1,
          `${day.toISOString()} band ${altitude}: ${worst} points in a row lie `
          + `on the closing edge at ${closing.toFixed(2)} -- that stretch of `
          + `the shape has no height and draws as a hairline`);
      }
    }
  }
});

test('a curve counted as going round the world really passes every longitude', () => {
  // `turns` decides which of two shapes gets drawn, so it is worth checking
  // against something that does not use it. A curve that goes round the world
  // passes through every longitude; one that runs up over a pole and back
  // covers only the span the dark cap reaches, and walks that span twice.
  //
  // Counted along the segments rather than at the sample points, because at
  // an equinox the terminator is very nearly the two meridians through the
  // poles: it crosses the rest of the world's longitudes in a couple of
  // steps taken right at the pole, and counting where the samples landed
  // would say it never went there.
  const wrap = (lon) => ((((lon + 180) % 360) + 360) % 360) - 180;
  for (const day of DATES) {
    for (const altitude of BANDS) {
      const ring = terminator(day, altitude, 1);
      const seen = new Set();
      for (let i = 1; i < ring.length; i += 1) {
        const from = Math.min(ring[i - 1][1], ring[i][1]);
        const to = Math.max(ring[i - 1][1], ring[i][1]);
        for (let lon = from; lon <= to; lon += 5) seen.add(Math.floor(wrap(lon) / 10));
        seen.add(Math.floor(wrap(to) / 10));
      }
      const went = turns(ring) !== 0;
      const said = `${day.toISOString()} band ${altitude}`;
      if (went) {
        assert.ok(seen.size >= 34,
          `${said}: counted as going round the world, but covers only `
          + `${seen.size} of 36 slices of longitude`);
      } else {
        assert.ok(seen.size < 34,
          `${said}: counted as closing on itself, but covers ${seen.size} of `
          + `36 slices of longitude`);
      }
    }
  }
});

test('shading is right after panning right round the world', () => {
  // Leaflet pans sideways for ever: longitude 700 is a perfectly ordinary
  // place to be looking, and it is the same ground as -20. The shapes are
  // tiled to cover what the map asked for, and a tiling that stops one copy
  // short leaves a band of daylight down the middle of the night -- at the
  // edge of the view, where it is easy to take for the terminator.
  const span = { west: 520, east: 900 };
  const wrap = (lon) => ((((lon + 180) % 360) + 360) % 360) - 180;
  for (const day of DATES) {
    for (const altitude of BANDS) {
      const rings = nightRings(day, altitude, 2, span);
      for (let lon = span.west + 5; lon <= span.east - 5; lon += 11) {
        for (let lat = -75; lat <= 75; lat += 15) {
          const sun = elevation(lat, wrap(lon), day);
          if (Math.abs(sun - altitude) < 0.6) continue;
          const drawn = rings.some((ring) => inside(ring, lat, lon));
          assert.equal(drawn, sun < altitude,
            `${day.toISOString()} band ${altitude}: ${lat},${lon} `
            + `(that is ${wrap(lon)}) has the sun at ${sun.toFixed(1)}° and is `
            + `drawn ${drawn ? 'dark' : 'light'}`);
        }
      }
    }
  }
});
