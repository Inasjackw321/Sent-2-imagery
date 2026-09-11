// Tests for the cloud mask.
//
// Run with:  node --test tests/clouds.test.mjs
//
// The mask decides, for every pixel of every tile, how much of it is cloud.
// That is a million and a half decisions per pan, on the thread that draws, so
// it is written as a lookup table: the judgement is worked out once for each
// of the 65,536 possible (brightest, dimmest) pairs and every pixel after that
// is an array read.
//
// A table is a cache, and a cache is a place for a picture to go quietly
// wrong. So the first test here is the one that matters: the same colours
// through the table and through the arithmetic it stands in for, all sixteen
// million of them, and no disagreement allowed anywhere. The rest check that
// the judgement itself still says what it is supposed to say about known
// colours -- cloud is bright and colourless, sand is bright and orange -- and
// that changing the sensitivity actually rebuilds the table rather than
// serving the previous one.

import test from 'node:test';
import assert from 'node:assert/strict';

// clouds.js reaches for Leaflet when it loads, to subclass its tile layer.
// None of that is under test here, and a stub is enough to let the module
// evaluate. It has to be in place before the import, hence the dynamic one.
globalThis.L = {
  TileLayer: { extend: (spec) => spec },
  DomUtil: { create: () => ({ getContext: () => null, classList: { add() {} } }) },
  tileLayer: { wms: () => ({}) },
};
const { maskToCloud, cloudiness } = await import('../frontend/js/clouds.js');

/** What the mask used to do, written out plainly: no table, one pixel at a time. */
function byArithmetic(data, sensitivity) {
  const out = new Uint8ClampedArray(data);
  for (let i = 0; i < out.length; i += 4) {
    out[i + 3] = Math.round(
      255 * cloudiness(out[i], out[i + 1], out[i + 2], sensitivity));
  }
  return out;
}

/** One row of 256 pixels sharing red and green, sweeping blue. */
function row(r, g) {
  const data = new Uint8ClampedArray(256 * 4);
  for (let x = 0; x < 256; x += 1) {
    data[x * 4] = r;
    data[x * 4 + 1] = g;
    data[x * 4 + 2] = x;
  }
  return data;
}

test('the table agrees with the arithmetic on every colour there is', () => {
  // Not a sample. All 256^3 of them, through both paths, compared.
  let disagreements = 0;
  for (let r = 0; r < 256; r += 1) {
    for (let g = 0; g < 256; g += 1) {
      const pixels = row(r, g);
      const expected = byArithmetic(pixels, 0.42);
      maskToCloud({ data: pixels }, 0.42);
      for (let x = 0; x < 256; x += 1) {
        if (pixels[x * 4 + 3] !== expected[x * 4 + 3]) disagreements += 1;
      }
    }
  }
  assert.equal(disagreements, 0);
});

test('and on every sensitivity the slider can reach', () => {
  // A table built for one sensitivity and served for another would show as a
  // slider that does nothing, which is exactly the kind of fault a lookup
  // table introduces.
  for (const sensitivity of [0, 0.25, 0.42, 0.5, 0.75, 1]) {
    for (const [r, g] of [[0, 0], [17, 200], [128, 128], [255, 255], [240, 90]]) {
      const pixels = row(r, g);
      const expected = byArithmetic(pixels, sensitivity);
      maskToCloud({ data: pixels }, sensitivity);
      assert.deepEqual(Array.from(pixels), Array.from(expected),
                       `sensitivity ${sensitivity}, r=${r} g=${g}`);
    }
  }
});

test('moving the slider and moving it back gives the first answer again', () => {
  const first = row(200, 200);
  maskToCloud({ data: first }, 0.42);
  maskToCloud({ data: row(200, 200) }, 0.9);
  const again = row(200, 200);
  maskToCloud({ data: again }, 0.42);
  assert.deepEqual(Array.from(again), Array.from(first));
});

test('the colours of the mask are not disturbed, only the alpha', () => {
  const pixels = row(200, 120);
  const before = Array.from(pixels);
  maskToCloud({ data: pixels }, 0.42);
  for (let x = 0; x < 256; x += 1) {
    assert.equal(pixels[x * 4], before[x * 4]);
    assert.equal(pixels[x * 4 + 1], before[x * 4 + 1]);
    assert.equal(pixels[x * 4 + 2], before[x * 4 + 2]);
  }
});

test('bright and colourless is cloud; bright and coloured is not', () => {
  // The two facts the whole mask rests on, checked against colours picked
  // from what they actually are rather than from what this code returns.
  const cloud = cloudiness(250, 250, 252);        // thick cloud: white
  const sand = cloudiness(237, 201, 145);         // desert: bright, orange
  const sea = cloudiness(28, 62, 110);            // ocean: dark, blue
  const forest = cloudiness(40, 78, 36);          // vegetation: dark, green
  assert.ok(cloud > 0.9, `cloud ${cloud}`);
  assert.ok(sand < 0.4, `sand ${sand}`);
  assert.equal(sea, 0);
  assert.equal(forest, 0);
});

test('black is never cloud, and does not divide by zero', () => {
  assert.equal(cloudiness(0, 0, 0), 0);
  assert.ok(Number.isFinite(cloudiness(0, 0, 0)));
});

test('turning the sensitivity up can only find more cloud, never less', () => {
  // It slides the brightness a pixel needs, so it has to be monotonic: a
  // slider that made thin haze appear and solid cloud vanish would be a bug
  // nobody could describe.
  for (const grey of [90, 130, 170, 210, 250]) {
    let last = -1;
    for (let s = 0; s <= 1.0001; s += 0.1) {
      const now = cloudiness(grey, grey, grey, s);
      assert.ok(now >= last - 1e-12, `grey ${grey} at ${s}: ${now} < ${last}`);
      last = now;
    }
  }
});
