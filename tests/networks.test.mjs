// The SEN2SR and SR4RS buttons: when each is offered, and what a request
// carries when one of them drew the picture.

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  NETWORKS, networkOf, networkRefusal, plainRequest,
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

test('both are offered for one Sentinel-2 date in true colour', () => {
  for (const key of Object.keys(NETWORKS)) assert.equal(networkRefusal(key, one(), config), '');
});

test('SR4RS has the same band limits as SEN2SR, in its own name', () => {
  assert.match(networkRefusal('sr4rs', one({ preset: 'swir' }), config), /^SR4RS .*near-infrared/);
});

test('each button starts from the plain picture, whichever drew the last one', () => {
  const drawn = one({ sr4rs: true });
  assert.equal(networkOf(drawn), 'sr4rs');
  assert.equal(networkOf(plainRequest(drawn)), '');
  assert.deepEqual(plainRequest(one({ sen2sr: true, sr4rs: true })), one());
  assert.equal(networkOf(one()), '');
});
