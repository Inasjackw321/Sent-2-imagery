// Tests for reading a tile service's answer.
//
// Run with:  node --test tests/tiles.test.mjs
//
// The bug these exist for: OpenStreetMap blocked the app for breaching its
// tile usage policy, and blocked it by answering 403 with a picture of a
// warning sign. The browser drew the warning sign. Every tile "loaded", the
// tile-error fallback never fired, and the map filled with a tiled refusal
// while the app reported no problem at all.
//
// So the thing under test is a status code, and the two ways of getting it
// wrong. Concluding nothing when a service says no leaves the map broken and
// silent, which is the bug. Concluding "blocked" whenever the probe cannot see
// an answer is worse: it would move people off a working basemap every time a
// service served images without a CORS header, or the network hiccuped, or an
// extension ate the request.

import test from 'node:test';
import assert from 'node:assert/strict';

import { PROBE, probeUrl, refusal, saidNo } from '../frontend/js/tiles.js';

const answers = (status) => async () => ({ ok: status >= 200 && status < 300, status });
const throws = (why) => async () => { throw new Error(why); };

const CARTO = {
  url: 'https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png',
  options: { subdomains: 'abcd' },
};
const ESRI = {
  url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery'
    + '/MapServer/tile/{z}/{y}/{x}',
  options: {},
};

test('the template is filled in the way Leaflet would fill it', () => {
  assert.equal(probeUrl(CARTO),
    `https://a.basemaps.cartocdn.com/rastertiles/voyager/${PROBE.z}/${PROBE.x}/${PROBE.y}.png`);
});

test('a URL with y before x is not quietly reordered', () => {
  // Esri's path is {z}/{y}/{x}, and a probe that swapped them would ask for a
  // tile that may not exist and read the 404 as a refusal.
  assert.ok(probeUrl(ESRI).endsWith(`/tile/${PROBE.z}/${PROBE.y}/${PROBE.x}`));
});

test('subdomains are accepted in both the shapes Leaflet accepts', () => {
  assert.ok(probeUrl({ url: 'https://{s}.example/{z}/{x}/{y}.png',
                       options: { subdomains: 'xyz' } }).startsWith('https://x.'));
  assert.ok(probeUrl({ url: 'https://{s}.example/{z}/{x}/{y}.png',
                       options: { subdomains: ['q', 'r'] } }).startsWith('https://q.'));
});

test('a {s} with nothing said about it still produces a real hostname', () => {
  // Leaflet's own default is 'abc'. What matters here is only that no probe
  // ever goes out with a literal "{s}" in the host.
  for (const options of [{}, undefined]) {
    const got = probeUrl({ url: 'https://{s}.example/{z}/{x}/{y}.png', options });
    assert.ok(!got.includes('{'), got);
  }
});

test('no placeholder survives into a probe URL', () => {
  const every = { url: 'https://{s}.x/{z}/{x}/{y}{r}.png', options: { subdomains: 'ab' } };
  assert.equal(probeUrl(every), `https://a.x/${PROBE.z}/${PROBE.x}/${PROBE.y}.png`);
});

test('the retina suffix is dropped rather than guessed at', () => {
  // A service may serve the plain tile and not the @2x one, and a 404 for the
  // retina tile is not the service refusing to serve the app.
  assert.ok(!probeUrl({ url: 'https://x/{z}/{x}/{y}{r}.png', options: {} }).includes('@2x'));
});

test('a service that answers OK is not a refusal', async () => {
  assert.equal(await refusal(CARTO, answers(200)), null);
});

test('a service that answers 403 with a picture in it is still a refusal', async () => {
  // The whole point. The body is a valid PNG; only the status says no.
  assert.equal(await refusal(CARTO, answers(403)), 403);
});

test('so is being rate limited, or anything else in the 400s and 500s', async () => {
  for (const status of [400, 401, 402, 404, 418, 429, 500, 502, 503]) {
    assert.equal(await refusal(CARTO, answers(status)), status);
  }
});

test('a redirect that was followed to an OK answer is not a refusal', async () => {
  assert.equal(await refusal(CARTO, async () => ({ ok: true, status: 200 })), null);
});

test('a probe that cannot see the answer concludes nothing', async () => {
  // CORS, no network, an extension eating the request: none of these is
  // evidence about the service, and acting on them would move a user off a
  // basemap that was working perfectly well.
  for (const why of ['Failed to fetch', 'NetworkError', 'blocked by client']) {
    assert.equal(await refusal(CARTO, throws(why)), null);
  }
});

test('the refusal is put in words a person can act on', () => {
  assert.match(saidNo(403), /403/);
  assert.match(saidNo(403), /blocked/i);
  assert.match(saidNo(429), /429/);
  assert.match(saidNo(429), /rate/i);
  // Anything else still names the number rather than saying "something".
  assert.match(saidNo(503), /503/);
});

test('the probe asks for one cheap tile, not a detailed one', () => {
  // It is a question put to somebody else's server, and it should cost them
  // as close to nothing as possible.
  assert.ok(PROBE.z <= 4, `zoom ${PROBE.z}`);
});
