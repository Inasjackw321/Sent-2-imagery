// When the SEN2SR button is offered, and what it says when it is not.
//
// Asked of the request that made the picture on screen: the button enhances
// what you are looking at, so a menu changed since then must not decide it.

import test from 'node:test';
import assert from 'node:assert/strict';

import { SEN2SR_BANDS, sen2srRefusal } from '../frontend/js/sen2sr.js';

const config = {
  composites: {
    true_color: { bands: ['red', 'green', 'blue'] },
    false_color: { bands: ['nir', 'red', 'green'] },
    swir: { bands: ['swir22', 'nir08', 'red'] },
  },
  indices: {
    ndvi: { bands: ['nir', 'red'] },
    ndmi: { bands: ['nir', 'swir16'] },
  },
};

const one = (extra = {}) => ({
  satellite: 'sentinel-2', mode: 'composite', preset: 'true_color',
  scene: { id: 'S2' }, ...extra,
});

test('the four bands are the network\'s own, in its order', () => {
  assert.deepEqual(SEN2SR_BANDS, ['red', 'green', 'blue', 'nir']);
});

test('one Sentinel-2 date in true colour, false colour or NDVI is offered', () => {
  assert.equal(sen2srRefusal(one(), config), '');
  assert.equal(sen2srRefusal(one({ preset: 'false_color' }), config), '');
  assert.equal(sen2srRefusal(one({ mode: 'index', index: 'ndvi' }), config), '');
});

test('nothing on screen yet', () => {
  assert.match(sen2srRefusal(null, config), /Show a Sentinel-2 date/);
});

test('another satellite', () => {
  assert.match(sen2srRefusal(one({ satellite: 'landsat' }), config), /Sentinel-2 only/);
  assert.match(sen2srRefusal(one({ satellite: 'sentinel-1' }), config), /Sentinel-2 only/);
});

test('a merge of several dates', () => {
  assert.match(sen2srRefusal(one({ scenes: [{}, {}] }), config), /one date/);
  // One date sent as a list of one is still one date.
  assert.equal(sen2srRefusal(one({ scenes: [{}] }), config), '');
});

test('a picture needing a band the network does not make', () => {
  assert.match(sen2srRefusal(one({ preset: 'swir' }), config), /near-infrared only/);
  assert.match(sen2srRefusal(one({ mode: 'index', index: 'ndmi' }), config),
    /near-infrared only/);
});

test('a difference between two passes', () => {
  assert.match(sen2srRefusal(one({ mode: 'change' }), config), /not a difference/);
});
