// The SR4RS and S2DR3 buttons: when each is offered, and what a request
// carries when one of them drew the picture.

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  NETWORKS, networkOf, networkRefusal, plainRequest, priceSaid,
} from '../frontend/js/sen2sr.js';

const config = {
  composites: {
    true_color: { bands: ['red', 'green', 'blue'] },
    swir: { bands: ['swir22', 'nir08', 'red'] },
  },
  indices: { ndvi: { bands: ['nir', 'red'] } },
};

const one = (extra = {}) => ({
  satellite: 'sentinel-2', mode: 'composite', preset: 'true_color',
  scene: { id: 'S2', date: '2024-06-01' }, ...extra,
});

test('all three are offered for one Sentinel-2 date in true colour', () => {
  for (const key of Object.keys(NETWORKS)) assert.equal(networkRefusal(key, one(), config), '');
});

test('SR4RS has the same band limits as SEN2SR, in its own name', () => {
  assert.match(networkRefusal('sr4rs', one({ preset: 'swir' }), config), /^SR4RS .*near-infrared/);
});

test('S2DR3 makes its own true colour, so any picture of one date will do', () => {
  assert.equal(networkRefusal('s2dr3', one({ preset: 'swir' }), config), '');
  assert.equal(networkRefusal('s2dr3', one({ mode: 'index', index: 'ndvi' }), config), '');
});

test('but still only one Sentinel-2 date, and not a difference', () => {
  assert.match(networkRefusal('s2dr3', one({ satellite: 'landsat' }), config), /Sentinel-2 only/);
  assert.match(networkRefusal('s2dr3', one({ scenes: [{}, {}] }), config), /one date/);
  assert.match(networkRefusal('s2dr3', one({ mode: 'change' }), config), /not a difference/);
  assert.match(networkRefusal('s2dr3', null, config), /Show a Sentinel-2 date/);
});

test('each button starts from the plain picture, whichever drew the last one', () => {
  const drawn = one({ sr4rs: true });
  assert.equal(networkOf(drawn), 'sr4rs');
  assert.equal(networkOf(plainRequest(drawn)), '');
  assert.deepEqual(plainRequest(one({ sen2sr: true, s2dr3: true })), one());
  assert.equal(networkOf(one()), '');
});

test('the price is said in credits, and a product already owned is free', () => {
  assert.match(priceSaid({ owned: false, credits: 4 }), /about 4 credits/);
  assert.match(priceSaid({ owned: false, credits: 1 }), /about 1 credit from/);
  assert.match(priceSaid({ owned: true, credits: 0 }), /costs nothing/);
});
