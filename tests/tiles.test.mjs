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

import {
  BASEMAPS, DEFAULT_BASEMAP, KEYLESS_HOSTS, NAMES, PROBE,
  hostsUsed, probeUrl, refusal, saidNo,
} from '../frontend/js/tiles.js';

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

// ── The keyless rule ───────────────────────────────────────────
//
// These are the tests that exist because comments did not work.
//
// A basemap that needs an API key does not fail. It draws, with "API KEY
// REQUIRED" stamped diagonally across every tile, and the app reports nothing
// wrong because as far as any code can tell nothing is. That happened with
// CARTO; the comment recording it sat four lines above the basemap list; the
// list was later edited back onto CARTO without the comment being read.
//
// So the rule is enforced here instead. If you are reading this because a test
// failed: the host you added is not known to serve anonymous clients without a
// key. Confirm that it does, then add it to KEYLESS_HOSTS with the reason.
// Do not add it to make the test pass.

test('every basemap comes from a host known to need no key', () => {
  for (const host of hostsUsed(BASEMAPS)) {
    const known = Object.keys(KEYLESS_HOSTS).some(
      (allowed) => host === allowed || host.endsWith(`.${allowed}`));
    assert.ok(known,
      `${host} is not in KEYLESS_HOSTS. A host that requires a key does not `
      + 'fail, it watermarks — read the note above this test before adding it.');
  }
});

test('no basemap URL carries a key, or a place to put one', () => {
  for (const spec of BASEMAPS) {
    const url = probeUrl(spec).toLowerCase();
    for (const smell of ['apikey', 'api_key', 'access_token', 'accesstoken',
                         'key=', 'token=', 'appid=', '{key}', '{token}']) {
      assert.ok(!url.includes(smell), `${spec.key}: ${url} contains ${smell}`);
    }
  }
});

test('every basemap is served over https', () => {
  // The page is https, so an http tile is a tile that never arrives.
  for (const spec of BASEMAPS) assert.ok(spec.url.startsWith('https://'), spec.key);
});

test('the basemaps are not all on one host', () => {
  // The fallback skips to a different provider rather than the next line,
  // which only means anything if there is a different provider to skip to.
  assert.ok(hostsUsed(BASEMAPS).length > 1, 'one host means no fallback');
});

test('every basemap carries an attribution', () => {
  // These are other people's tiles and every one of these providers asks for
  // credit as the price of serving them.
  for (const spec of BASEMAPS) {
    assert.ok(spec.options.attribution?.length > 5, spec.key);
  }
});

test('the basemaps have distinct keys and labels', () => {
  assert.equal(new Set(BASEMAPS.map((s) => s.key)).size, BASEMAPS.length);
  assert.equal(new Set(BASEMAPS.map((s) => s.label)).size, BASEMAPS.length);
});

test('the default basemap is one that exists', () => {
  assert.ok(BASEMAPS.some((s) => s.key === DEFAULT_BASEMAP), DEFAULT_BASEMAP);
});

test('the default is not the last one, so it has somewhere to fall to', () => {
  assert.notEqual(BASEMAPS.at(-1).key, DEFAULT_BASEMAP);
});

// ── Place names ────────────────────────────────────────────────
//
// The other basemap fault that does not look like a fault. Esri's World Street
// Map drew Ukraine perfectly and captioned its capital "Kiev" -- roads,
// borders and rivers all correct, only the names years out of date. Nothing
// automatic catches that: the tile is valid, the service is healthy, the
// label is simply wrong. So each basemap declares where its names come from
// and the default is held to it here.

test('every basemap says where its place names come from', () => {
  for (const spec of BASEMAPS) {
    assert.ok(Object.values(NAMES).includes(spec.names),
              `${spec.key}: names is ${spec.names}`);
  }
});

test('the default basemap does not use a vendor\'s own place names', () => {
  const spec = BASEMAPS.find((s) => s.key === DEFAULT_BASEMAP);
  assert.notEqual(spec.names, NAMES.VENDOR,
    `${spec.key} is the default and uses vendor cartography — this is exactly `
    + 'how "Kiev" ended up on screen. The default must render OpenStreetMap '
    + 'names or carry none at all.');
});

test('at least one basemap has current place names on it', () => {
  // A map with no names anywhere is not a map you can find anything on.
  assert.ok(BASEMAPS.some((s) => s.names === NAMES.OSM));
});
