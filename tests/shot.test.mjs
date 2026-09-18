// Tests for how tight the exported picture is drawn.
//
// Run with:  node --test tests/shot.test.mjs
//
// The picture used to be framed by the map view. A map view is chosen for
// looking at with panels down one side, and what is in the air is usually
// clustered in one part of it -- so the thing being sent was a few specks in
// a lot of empty dark. These are about the two ways of overcorrecting: a
// crop so tight there is no landmark left in it, and a crop that quietly
// shows more ground than was asked for.

import test from 'node:test';
import assert from 'node:assert/strict';

import { closeIn, isFrontier, labelSpot, legendKeys, markScale, shortName,
  stampedAt }
  from '../frontend/js/trackershot.js';

const VIEW = { north: 52.5, south: 45.5, west: 22.0, east: 40.0 };
const mark = (lat, lon) => ({ lat, lon });

test('a cluster in one corner crops to the cluster', () => {
  const got = closeIn(VIEW, [mark(50.0, 31.0), mark(50.3, 32.0),
                             mark(49.7, 32.5)]);
  // Comfortably inside the view on every side.
  assert.ok(got.north < VIEW.north && got.south > VIEW.south);
  assert.ok(got.west > VIEW.west && got.east < VIEW.east);
  // And a small fraction of the ground: the view is 18 degrees across.
  assert.ok(got.east - got.west < 3, `${got.east - got.west} degrees wide`);
});

test('the marks all stay inside the crop', () => {
  const marks = [mark(50.0, 31.0), mark(50.3, 32.0), mark(49.7, 32.5)];
  const got = closeIn(VIEW, marks);
  for (const m of marks) {
    assert.ok(m.lat <= got.north && m.lat >= got.south, `${m.lat} off frame`);
    assert.ok(m.lon >= got.west && m.lon <= got.east, `${m.lon} off frame`);
  }
});

test('there is room around them rather than a crop through them', () => {
  const got = closeIn(VIEW, [mark(50.0, 31.0), mark(51.0, 33.0)]);
  assert.ok(got.north > 51.0, 'the top mark is on the edge');
  assert.ok(got.south < 50.0, 'the bottom mark is on the edge');
});

test('one mark on its own does not become a picture of nothing', () => {
  // Without a floor this is a crop of zero degrees at infinite magnification,
  // with no landmark in it to say where it was taken.
  const got = closeIn(VIEW, [mark(50.0, 31.0)]);
  assert.ok(got.north - got.south >= 0.7, `${got.north - got.south} tall`);
  assert.ok(got.east - got.west >= 0.7, `${got.east - got.west} wide`);
});

test('two marks a mile apart get the same floor', () => {
  const got = closeIn(VIEW, [mark(50.000, 31.000), mark(50.008, 31.012)]);
  assert.ok(got.north - got.south >= 0.7);
  assert.ok(got.east - got.west >= 0.7);
});

test('it never shows more ground than was asked for', () => {
  // "The whole country" has to keep meaning the country. A crop that grew
  // past the view would be the picture answering a question nobody asked.
  const spread = [mark(52.4, 22.1), mark(45.6, 39.9)];
  const got = closeIn(VIEW, spread);
  assert.ok(got.north <= VIEW.north && got.south >= VIEW.south);
  assert.ok(got.west >= VIEW.west && got.east <= VIEW.east);
});

test('the floor does not push a crop past the view either', () => {
  // A single mark inside a view smaller than the floor: the crop is the
  // view, not the floor.
  const tiny = { north: 50.1, south: 50.0, west: 31.0, east: 31.1 };
  const got = closeIn(tiny, [mark(50.05, 31.05)]);
  assert.deepEqual(got, tiny);
});

test('no marks means the view, unchanged', () => {
  assert.equal(closeIn(VIEW, []), VIEW);
  assert.equal(closeIn(VIEW, undefined), VIEW);
});

// The wiring and the size, read off the source. Neither is arithmetic that
// can be called from here -- drawShot needs a canvas -- but both are a line
// that can be deleted without a single test above noticing, which is exactly
// what a mutation run showed.

import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const SOURCE = readFileSync(
  fileURLToPath(new URL('../frontend/js/trackershot.js', import.meta.url)),
  'utf8');

test('the picture is actually drawn from the tightened frame', () => {
  // Unless the area was named by the person -- see further down.
  assert.ok(/closeIn\(bounds,/.test(SOURCE),
    'drawShot frames the whole view again');
  assert.ok(/framing\(exactly \? bounds/.test(SOURCE),
    'the frame no longer comes from framing()');
});

test('the frame takes in the warnings, not only the marks', () => {
  // A country whose only activity is a shaded province would otherwise crop
  // to nothing at all -- which is how picking Russia came back with "nothing
  // in that area" on a night of two alerts and no drones.
  const block = SOURCE.slice(SOURCE.indexOf('export async function drawShot'));
  const head = block.slice(0, block.indexOf('drawLand('));
  assert.match(head, /spread\(warnings\)/, 'warnings do not reach the framing');
});

test('a mark is small enough to say where it is', () => {
  // A glyph wide enough to cover the town it is over has stopped being a
  // position. Forty-four was chosen against a picture of a whole country;
  // the frame is much tighter now, so the same glyph covers far more ground.
  const cap = Number(/Math.min\((\d+), Math.max/.exec(SOURCE)?.[1]);
  assert.ok(cap > 0, 'the mark size is gone');
  assert.ok(cap <= 36, `a mark can be ${cap}px`);
});

test('and the same apparent size whatever shape the picture is', () => {
  // A fixed pixel size means a mark shrinks as the crop gets taller, so the
  // single-track picture -- the tallest one this draws -- had the smallest
  // mark in it.
  assert.ok(/frame\.height \* 0\.0\d+/.test(SOURCE),
    'the mark size no longer follows the picture');
  assert.ok(markSize(864) === 28, `${markSize(864)} on a short picture`);
  assert.ok(markSize(1280) > markSize(864),
    'a taller picture does not get a bigger mark');
  assert.ok(markSize(4000) <= 36, 'no ceiling on the mark size');
  assert.ok(markSize(200) >= 24, 'no floor on the mark size');
});

// The same arithmetic the module uses, read off it so the two cannot drift.
function markSize(height) {
  const [, hi, lo, share] =
    /Math\.min\((\d+), Math\.max\((\d+), frame\.height \* ([\d.]+)\)\)/
      .exec(SOURCE);
  return Math.round(Math.min(Number(hi),
    Math.max(Number(lo), height * Number(share))));
}

// ── What the picture says, beyond the marks ────────────────────
//
// The picture used to be marks on a dark rectangle with two lines of text in
// opposite corners. Somebody sent it could not answer any of the three
// questions a picture like this is sent to answer: where is this, when was
// it, and what do the colours mean. These are about the parts that answer
// them -- and about the two ways each one can go wrong, which is saying
// nothing, or saying something false.

test('a province is named without its type word', () => {
  assert.equal(shortName('Полтавська область'), 'ПОЛТАВСЬКА');
  assert.equal(shortName('Белгородская область'), 'БЕЛГОРОДСКАЯ');
  assert.equal(shortName('Krasnodar krai'), 'KRASNODAR');
  assert.equal(shortName('Kharkiv oblast'), 'KHARKIV');
});

// A red line is drawn round a country, and red on this picture means "a
// country ends here". These are about which boundaries earn one.
//
// The rule used to be "a country all of whose provinces are here", and the
// red line was worked out from those provinces. That needed every ring of
// every province wound the same way; Ukraine arrives as oblasts AND raions,
// two nested levels from two files, and where they disagreed the picture
// drew a red line round all hundred and thirty-six raions and none round
// Ukraine. The line is now the country's own boundary, and this is the flag
// that says which one that is.

test('a national boundary is drawn as a frontier', () => {
  assert.equal(isFrontier({ name: 'Polska', in: 'pl', level: 'country' }),
    true);
});

test('a province is not, however big', () => {
  // The case that broke: every raion in Ukraine came out red.
  assert.equal(isFrontier(
    { name: 'Харківська область', in: 'ua', level: 'region' }), false);
  assert.equal(isFrontier(
    { name: 'Краснодарский край', in: 'elsewhere', level: 'region' }), false);
});

test('and a boundary that says nothing about itself is not', () => {
  // Fail closed. A missing flag draws a pale province, which is a boundary
  // drawn modestly; guessing the other way draws a national border where
  // nobody said there was one.
  assert.equal(isFrontier({ name: 'x', in: 'ua' }), false);
  assert.equal(isFrontier({}), false);
  assert.equal(isFrontier(null), false);
  assert.equal(isFrontier(undefined), false);
});

test('the neighbours are named without theirs either', () => {
  // Polish puts the type word at the FRONT. Stripping a suffix left sixteen
  // labels each three quarters the same word, which is the least
  // informative thing a map label can be.
  assert.equal(shortName('województwo lubelskie'), 'LUBELSKIE');
  assert.equal(shortName('województwo warmińsko-mazurskie'),
    'WARMIŃSKO-MAZURSKIE');
  // And Belarusian spells oblast its own way, which was in none of the
  // patterns -- so every Belarusian region carried its type word on the map
  // while its Ukrainian and Russian neighbours did not.
  assert.equal(shortName('Брэсцкая вобласць'), 'БРЭСЦКАЯ');
  assert.equal(shortName('Магілёўская вобласць'), 'МАГІЛЁЎСКАЯ');
  // A city that is its own region has no type word to lose.
  assert.equal(shortName('Мінск'), 'МІНСК');
});

test('a name that is only its type word keeps it', () => {
  // Trimming it away would leave an empty label, which is a province with no
  // name written on it for the sake of a rule about suffixes.
  assert.equal(shortName('Крим'), 'КРИМ');
  assert.ok(shortName('область').length > 0);
});

test('nothing in, nothing out', () => {
  assert.equal(shortName(''), '');
  assert.equal(shortName(null), '');
  assert.equal(shortName(undefined), '');
});

test('the time is spelt out rather than numbered', () => {
  // 09/16 and 16/09 are two different dates and the picture does not know
  // who is reading it.
  const said = stampedAt(new Date(Date.UTC(2026, 8, 16, 11, 48)));
  assert.match(said, /16 Sep 2026/);
  assert.match(said, /11:48 UTC/);
});

test('the time is UTC whatever the clock here says', () => {
  // A picture of moving things stamped in the sender's local time is a
  // picture the receiver reads as hours out -- and it says "UTC" on it while
  // doing so, which is worse than saying nothing.
  //
  // Run in another timezone, because this machine's clock is already UTC:
  // asserting here would compare UTC against UTC and pass whatever the code
  // did. Tokyo is nine hours ahead and over the date line from the moment
  // below, so a local reading gets both the time AND the date wrong.
  const here = fileURLToPath(new URL('../frontend/js/trackershot.js',
    import.meta.url));
  const out = execFileSync(process.execPath, ['--input-type=module', '-e',
    `import { stampedAt } from ${JSON.stringify(here)};`
    + 'process.stdout.write('
    + 'stampedAt(new Date(Date.UTC(2025, 11, 31, 23, 50))));',
  ], { env: { ...process.env, TZ: 'Asia/Tokyo' }, encoding: 'utf8' });
  // In Tokyo that instant is 1 January 2026. Reading it locally gets the
  // time, the day, the month AND the year wrong, and still writes "UTC".
  assert.equal(out, '31 Dec 2025 · 23:50 UTC');
});

test('a kind keeps the colour it was first seen with', () => {
  // Two marks of one kind can arrive with different colours. The key shows
  // one swatch, and it is the one the first of them was drawn with -- so the
  // key matches at least one mark in the picture rather than neither.
  const keys = legendKeys([
    { label: 'Drone', colour: '#ffd400' },
    { label: 'Drone', colour: '#ff3b30' },
  ]);
  assert.deepEqual(keys,
    [{ label: 'Drone', colour: '#ffd400', area: false }]);
});

test('the key lists each kind once, in the order it appears', () => {
  const keys = legendKeys([
    { label: 'Drone', colour: '#ffd400' },
    { label: 'Cruise missile', colour: '#a855f7' },
    { label: 'Drone', colour: '#ffd400' },
  ]);
  assert.deepEqual(keys.map((k) => k.label), ['Drone', 'Cruise missile']);
});

test('the key lists only what is in the picture', () => {
  // A legend naming things that are not there is a legend nobody trusts.
  assert.deepEqual(legendKeys([]), []);
  assert.deepEqual(legendKeys(undefined), []);
  assert.deepEqual(legendKeys([{ lat: 1, lon: 1 }]), []);
});

// A square province, 2 degrees on a side, drawn on a 1000x1000 picture whose
// frame is exactly that province.
const SQUARE = {
  type: 'Polygon',
  coordinates: [[[30, 49], [32, 49], [32, 51], [30, 51], [30, 49]]],
};
const FRAME = {
  width: 1000,
  height: 1000,
  at: (lat, lon) => [((lon - 30) / 2) * 1000, ((51 - lat) / 2) * 1000],
};

test('a province is labelled on itself', () => {
  const spots = labelSpot(SQUARE, FRAME, 120, 30, null);
  assert.ok(spots?.length, 'no spot found on a province filling the frame');
  // Inside the province, which here is the whole picture.
  for (const spot of spots) {
    assert.ok(spot.x > 0 && spot.x < 1000, `${spot.x} off the picture`);
    assert.ok(spot.y > 0 && spot.y < 1000, `${spot.y} off the picture`);
  }
});

test('a province zoomed into past its own centre is still labelled', () => {
  // The bug this exists for. The first version labelled a province at its
  // centroid and gave up when that was off the picture -- so the tighter the
  // crop, the less got named, and at the crop the exporter actually uses not
  // one of seventy-four provinces had its centre on screen.
  const corner = {
    width: 1000,
    height: 1000,
    // A window on the top-left quarter of the province only.
    at: (lat, lon) => [((lon - 30) / 0.5) * 1000, ((51 - lat) / 0.5) * 1000],
  };
  const spots = labelSpot(SQUARE, corner, 120, 30, null);
  assert.ok(spots?.length, 'a province filling the frame went unnamed');
});

// A province with a bite out of the middle of its west side, so that the
// centre of its bounding box is NOT in it. Real ones do this constantly --
// an oblast wrapped round a city, a coastline, a river border.
const NOTCHED = {
  type: 'Polygon',
  coordinates: [[[30, 49], [32, 49], [32, 51], [30, 51], [30, 50.3],
                 [31.2, 50.3], [31.2, 49.7], [30, 49.7], [30, 49]]],
};

test('a name goes on the land, not in the bay', () => {
  // The middle of what you can see is easily a place the province is not.
  // Without checking, the name is written in the notch -- which on a map is
  // the next province along, and a picture that lies about where a drone is.
  const spots = labelSpot(NOTCHED, FRAME, 150, 40, null);
  assert.ok(spots?.length, 'a notched province went unnamed entirely');
  for (const spot of spots) {
    // The notch: west of lon 31.2, between lat 49.7 and 50.3.
    const inNotch = spot.x < 600 && spot.y > 350 && spot.y < 650;
    assert.ok(!inNotch, `name placed in the notch at ${spot.x},${spot.y}`);
  }
});

test('a name is not written across a bay between two arms', () => {
  // A province shaped like a U -- two arms with water or a neighbour between
  // them -- can have both ENDS of its name on land while the middle of the
  // word sits in the gap. Checking only the ends is not enough.
  const HORSESHOE = {
    type: 'Polygon',
    coordinates: [[[30, 49], [32, 49], [32, 51], [31.7, 51], [31.7, 49.6],
                   [30.3, 49.6], [30.3, 51], [30, 51], [30, 49]]],
  };
  const spots = labelSpot(HORSESHOE, FRAME, 800, 40, null);
  assert.ok(spots?.length, 'a horseshoe province went unnamed entirely');
  for (const spot of spots) {
    // The gap: between the arms (x 150..850) and above the base, which
    // starts at lat 49.6 -- y 700 on this frame.
    const inGap = spot.x > 150 && spot.x < 850 && spot.y < 700;
    assert.ok(!inGap, `name written across the bay at ${spot.x},${spot.y}`);
  }
});

test('both ends of the name are on the land too', () => {
  // A point can be inside a province while the word around it runs off into
  // the next one. A tapering province is where this shows.
  const WEDGE = {
    type: 'Polygon',
    coordinates: [[[31, 49], [32, 51], [30, 51], [31, 49]]],
  };
  const spots = labelSpot(WEDGE, FRAME, 300, 40, null) ?? [];
  for (const spot of spots) {
    for (const end of [spot.x - 150, spot.x + 150]) {
      // Inside the wedge: its width at this height, about the middle.
      const halfWide = ((1000 - spot.y) / 1000) * 500;
      assert.ok(Math.abs(end - 500) <= halfWide + 1,
        `the name runs off the province at ${end}`);
    }
  }
});

test('a name is never written on the next province along', () => {
  // The middle of what you can see is easily in somebody else's land. A name
  // there is worse than no name: it is a picture that lies about where a
  // drone is.
  const far = {
    width: 1000,
    height: 1000,
    // A window well to the east of the province, which is not in it at all.
    at: (lat, lon) => [((lon - 34) / 2) * 1000, ((51 - lat) / 2) * 1000],
  };
  assert.equal(labelSpot(SQUARE, far, 120, 30, null), null);
});

test('a province too small to hold its name is left unnamed', () => {
  const tiny = {
    width: 1000,
    height: 1000,
    at: (lat, lon) => [500 + (lon - 31) * 5, 500 - (lat - 50) * 5],
  };
  assert.equal(labelSpot(SQUARE, tiny, 120, 30, null), null);
});

test('a name keeps clear of the heading and the footer', () => {
  // Drawn under an opaque band, a name is a name nobody sees.
  const bands = { head: 480, foot: 480 };
  assert.equal(labelSpot(SQUARE, FRAME, 120, 30, bands), null);
  assert.ok(labelSpot(SQUARE, FRAME, 120, 30, { head: 50, foot: 50 })?.length);
});

test('more than one spot is offered, so a name can move off a mark', () => {
  // With one candidate, "keep this name off that mark" can only mean "drop
  // the name" -- and the province with something in the air over it is the
  // one you most want named.
  assert.ok(labelSpot(SQUARE, FRAME, 120, 30, null).length > 1);
});

test('a picture is never a tower', () => {
  // The floor on the crop is in degrees, and a degree of longitude at fifty
  // north is two thirds of a degree of latitude before Mercator stretches
  // the vertical again. One mark on its own came out 1600 by 2000: a
  // portrait of empty ground with a speck in it.
  const view = { north: 52.5, south: 45.5, west: 22.0, east: 40.0 };
  const got = closeIn(view, [{ lat: 50.0, lon: 31.0 }]);
  const tall = got.north - got.south;
  const wide = got.east - got.west;
  // In degrees the box is wider than it is tall, which after the projection
  // stretches the vertical is a landscape picture.
  assert.ok(wide > tall * 1.3, `${wide} by ${tall} degrees`);
});

test('and shaping it never loses what was in it', () => {
  const view = { north: 52.5, south: 45.5, west: 22.0, east: 40.0 };
  const marks = [{ lat: 50.0, lon: 31.0 }, { lat: 50.2, lon: 31.4 }];
  const got = closeIn(view, marks);
  for (const m of marks) {
    assert.ok(m.lat <= got.north && m.lat >= got.south);
    assert.ok(m.lon >= got.west && m.lon <= got.east);
  }
  assert.ok(got.north <= view.north && got.south >= view.south);
  assert.ok(got.west >= view.west && got.east <= view.east);
});

test('a name is never cut off by the edge of the picture', () => {
  // Half a province name running off the side is worse than no name: it
  // reads as a different province.
  const offLeft = {
    width: 1000,
    height: 1000,
    // The frame starts inside the province, so its western half is off
    // picture and what remains sits hard against the left edge.
    at: (lat, lon) => [((lon - 31) / 2) * 1000, ((51 - lat) / 2) * 1000],
  };
  const spots = labelSpot(SQUARE, offLeft, 300, 40, null) ?? [];
  assert.ok(spots.length, 'a province at the edge went unnamed');
  for (const spot of spots) {
    assert.ok(spot.x - 150 >= 0, `name starts at ${spot.x - 150}`);
    assert.ok(spot.x + 150 <= 1000, `name ends at ${spot.x + 150}`);
  }
});

test('a mark is drawn on something that separates it from the ground', () => {
  // The glyphs are bright on a dark ground until one lands on a border or a
  // province name, and then it is bright on bright. Read off the source:
  // there is no canvas here to look at.
  const block = SOURCE.slice(SOURCE.indexOf('async function drawMarks'));
  assert.match(block.slice(0, block.indexOf('\n}')), /ctx\.arc\(x, y, size/,
    'nothing is drawn behind a mark');
});

test('the borders are drawn strongly enough to see', () => {
  // They are not decoration: they are the only thing telling a viewer which
  // country, and which province, a mark is over. At 38% on a dark ground
  // they were visible on the screen this was drawn on and faint everywhere
  // else.
  const alpha = (name) => {
    const line = SOURCE.split('\n').find((l) => l.startsWith(`const ${name} = `));
    assert.ok(line, `${name} is gone`);
    const nums = line.match(/[\d.]+/g);
    return Number(nums[nums.length - 1]);
  };
  assert.ok(alpha('BORDER') >= 0.6, `borders at ${alpha('BORDER')}`);
  assert.ok(alpha('LAND') > 0, 'the land has no fill at all');
});

test('a border is drawn over its own shadow, not under it', () => {
  // The dark line goes down first and wider; the bright one sits on it. The
  // other order paints the shadow over the border and hides it.
  const block = SOURCE.slice(SOURCE.indexOf('function drawLand'));
  const body = block.slice(0, block.indexOf('\n}'));
  assert.ok(body.indexOf('BORDER_SHADOW') < body.indexOf('ctx.strokeStyle = BORDER;'),
    'the shadow is painted over the border');
  const shadowWidth = /lineWidth = ([\d.]+);\n  for \(const path of paths\) ctx\.stroke/
    .exec(body.slice(body.indexOf('BORDER_SHADOW')));
  assert.ok(Number(shadowWidth[1]) > 1.5, 'the shadow is no wider than the line');
});

test('the land is filled before anything is stroked over it', () => {
  const block = SOURCE.slice(SOURCE.indexOf('function drawLand'));
  const body = block.slice(0, block.indexOf('\n}'));
  assert.ok(body.indexOf('ctx.fillStyle = LAND;') < body.indexOf('BORDER_SHADOW'),
    'a province fill is painted over its neighbour\'s border');
});

// ── Warnings in the picture ────────────────────────────────────
//
// They used to be left out entirely -- asked for that way, back when a
// warning was a triangle sitting on a centroid and the picture was about the
// arrows. A warning is a shaded province now, and leaving it out meant a
// country whose only activity is warnings had nothing to draw at all:
// picking Russia on a night of two alerts and no drones produced "nothing in
// that area" and no picture.

const REGION = {
  type: 'Polygon',
  coordinates: [[[36, 50], [38, 50], [38, 52], [36, 52], [36, 50]]],
};

test('a warning is enough to make a picture on its own', () => {
  // The Russia case. The frame has to find the warnings, or it crops to
  // nothing and the export refuses.
  const view = { north: 61, south: 43.5, west: 27, east: 60 };
  const points = [];
  for (const ring of REGION.coordinates) {
    for (const [lon, lat] of ring) points.push({ lat, lon });
  }
  const got = closeIn(view, points);
  assert.ok(got.north > 52 && got.south < 50, 'the warning is not in frame');
  assert.ok(got.east - got.west < 20, 'the crop is still the whole box');
});

test('the key tells a thing in the air from a state of the ground', () => {
  // A yellow dot beside "Drone" and a yellow dot beside "Drone warning" are
  // the same swatch for two quite different claims.
  const keys = legendKeys(
    [{ label: 'Drone', colour: '#ffd400' }],
    [{ label: 'Drone warning', colour: '#ffd400' }]);
  assert.deepEqual(keys.map((k) => k.label), ['Drone', 'Drone warning']);
  assert.equal(keys[0].area, false);
  assert.equal(keys[1].area, true);
});

test('a warning alone still gets a key', () => {
  const keys = legendKeys([], [{ label: 'Missile warning', colour: '#ff3b30' }]);
  assert.deepEqual(keys.map((k) => k.label), ['Missile warning']);
});

test('the warned ground is drawn under the marks, not over them', () => {
  // A wash painted over an arrow is an arrow nobody can see.
  const block = SOURCE.slice(SOURCE.indexOf('export async function drawShot'));
  const body = block.slice(0, block.indexOf('return canvas;'));
  assert.ok(body.indexOf('drawWarnings(') < body.indexOf('await drawMarks('),
    'the warnings are painted over the marks');
  assert.ok(body.indexOf('drawLand(') < body.indexOf('drawWarnings('),
    'the land is painted over the warnings');
});

test('a warned province is filled and edged in its own colour', () => {
  const block = SOURCE.slice(SOURCE.indexOf('function drawWarnings'));
  const body = block.slice(0, block.indexOf('\n}\n'));
  assert.match(body, /ctx\.fillStyle = warning\.colour;/);
  assert.match(body, /ctx\.strokeStyle = warning\.colour;/);
  // Lighter than the map's wash: there are no tiles under this one to fight,
  // and a heavy fill buries the province's name and any mark standing on it.
  const alpha = Number(/globalAlpha = ([\d.]+);/.exec(body)[1]);
  assert.ok(alpha > 0.1 && alpha <= 0.35, `warned ground at ${alpha}`);
});

test('the heading counts warnings as well as tracks', () => {
  const block = SOURCE.slice(SOURCE.indexOf('function drawFurniture'));
  const body = block.slice(0, block.indexOf('\n}'));
  assert.match(body, /warnings\?\.length/);
  assert.match(body, /'warning' : 'warnings'/);
});

// ── The ground you asked for ───────────────────────────────────
//
// "When you press this view, it takes it from that view and ignores whether
// there's a drone there or not. Make it so it just takes it for where you
// highlighted."
//
// The crop was pulling in around the marks whatever the area came from, so
// asking for the view got a picture of one corner of it, and asking for a
// quiet border got "nothing in that area" and no picture at all.

test('an area named by the person is the picture, crop and all', () => {
  const block = SOURCE.slice(SOURCE.indexOf('export async function drawShot'));
  const head = block.slice(0, block.indexOf('drawLand('));
  assert.match(head, /exactly \? bounds/,
    'the chosen area is still cropped to the marks');
});

test('and one the app worked out itself is still closed in on', () => {
  // "Russia" means the part of Russia something is happening in, not three
  // thousand kilometres of empty ground.
  const block = SOURCE.slice(SOURCE.indexOf('export async function drawShot'));
  const head = block.slice(0, block.indexOf('drawLand('));
  assert.match(head, /: closeIn\(bounds,/, 'the country fit is gone');
});

test('the crop is off by default, only when asked for', () => {
  const block = SOURCE.slice(SOURCE.indexOf('export async function drawShot'));
  assert.match(block.slice(0, block.indexOf(')')), /exactly = false/,
    'exactly has no default, so a caller that omits it gets undefined');
});

// A mark may be drawn bigger than its neighbours, and the map is what decides.
// A drone is four rings, a frame and a nose rather than a solid triangle, and
// it is unreadable at the size a triangle is fine at.

test('a mark with its own scale is drawn at it', () => {
  assert.equal(markScale(24, { scale: 32 / 24 }), 32);
  assert.equal(markScale(34, { scale: 32 / 24 }), 45);
});

test('and one without is drawn at the picture\'s own size', () => {
  for (const mark of [{}, null, undefined, { scale: 1 }]) {
    assert.equal(markScale(24, mark), 24, JSON.stringify(mark));
  }
});

test('a nonsense scale is ignored rather than obeyed', () => {
  // It arrives over the wire with everything else. A mark drawn at zero
  // pixels, or backwards, is worse than one drawn at the ordinary size.
  for (const bad of [0, -2, NaN, Infinity, 'big', null]) {
    assert.equal(markScale(24, { scale: bad }), 24, String(bad));
  }
});
