// Tests for what the lookout panel says about a square.
//
// Run with:  node --test tests/lookout.test.mjs
//
// The backend decides what is a hit. What this half has to get right is that
// a reader can tell the three outcomes apart: a hit, a square that was looked
// at and was not one, and a square nobody could answer for. A panel that
// shows only its hits cannot be told from one that failed silently, and
// "cloud" drawn like "no" is the one mistake that makes a sweep untrustworthy.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { isBlank, isHit, progressLine, saidOf, whyNot }
  from '../frontend/js/lookout.js';

const ASKED = [
  { id: 'is_there_a_building', ask: 'is there a building', hit: 'yes' },
  { id: 'is_there_a_colour_circle', ask: 'is there a colour circle', hit: 'yes' },
  { id: 'refinery', ask: 'is it a refinery or a place with oil', hit: 'no' },
  { id: 'circle_and_building', ask: 'is the circle over the building', hit: 'yes' },
];

const HIT = {
  id: '0-0', lat: 50.62, lon: 30.28, hit: true, missed: [], unsure: [],
  answers: {
    is_there_a_building: { said: 'yes', p: 0.96 },
    is_there_a_colour_circle: { said: 'yes', p: 0.93 },
    refinery: { said: 'no', p: 0.03 },
    circle_and_building: { said: 'yes', p: 0.88 },
  },
};

// ── Telling the three apart ────────────────────────────────────

test('a hit is a hit', () => {
  assert.equal(isHit(HIT), true);
  assert.equal(isBlank(HIT), false);
});

test('a square that was answered for and missed is neither', () => {
  const miss = { ...HIT, hit: false, missed: ['refinery'] };
  assert.equal(isHit(miss), false);
  assert.equal(isBlank(miss), false);
});

test('a square nobody could answer for is its own thing', () => {
  // Not a miss. "We looked and it is not there" and "we could not look" are
  // different results, and a sweep that draws them alike cannot be trusted.
  const blank = { id: '0-1', lat: 50, lon: 30, hit: false,
                  trouble: 'Codiv answered 503' };
  assert.equal(isBlank(blank), true);
  assert.equal(isHit(blank), false);
});

test('nothing at all is neither', () => {
  assert.equal(isHit(null), false);
  assert.equal(isBlank(undefined), false);
});

// ── What a square says ─────────────────────────────────────────

test('every question and its answer is shown', () => {
  const said = saidOf(HIT, ASKED);
  for (const q of ASKED) assert.ok(said.includes(q.ask), q.ask);
  assert.match(said, /is there a building: yes 96%/);
});

test('the number is shown beside the answer', () => {
  // How close it was is the first thing anybody asks of a square, and an
  // answer with no figure beside it cannot be argued with.
  assert.match(saidOf(HIT, ASKED), /no 3%/);
});

test('a question with no answer is shown as having none', () => {
  const partial = { ...HIT, answers: { refinery: { said: 'no', p: 0.1 } } };
  assert.match(saidOf(partial, ASKED), /is there a building: —/);
});

test('an unsure answer is shown as unsure rather than guessed', () => {
  const unsure = { ...HIT, hit: false, unsure: ['is_there_a_colour_circle'],
    answers: { ...HIT.answers,
      is_there_a_colour_circle: { said: 'unsure', p: 0.5 } } };
  assert.match(saidOf(unsure, ASKED), /is there a colour circle: unsure 50%/);
});

test('a square that could not be answered for says why instead', () => {
  const blank = { trouble: 'Codiv is rate limiting' };
  assert.equal(saidOf(blank, ASKED), 'Codiv is rate limiting');
});

// ── Why a square is not a hit ──────────────────────────────────

test('a hit says so', () => {
  assert.equal(whyNot(HIT, ASKED), 'every answer matched');
});

test('a miss names the question, in words rather than by id', () => {
  const miss = { ...HIT, hit: false, missed: ['refinery'], unsure: [] };
  assert.match(whyNot(miss, ASKED), /is it a refinery or a place with oil/);
  assert.doesNotMatch(whyNot(miss, ASKED), /refinery:/);
});

test('and says which way it went', () => {
  const miss = { ...HIT, hit: false, missed: ['refinery'], unsure: [] };
  const vague = { ...HIT, hit: false, missed: [], unsure: ['refinery'] };
  assert.match(whyNot(miss, ASKED), /answered the other way/);
  assert.match(whyNot(vague, ASKED), /not sure enough/);
});

test('both reasons are given when there are both', () => {
  const mixed = { ...HIT, hit: false, missed: ['refinery'],
                  unsure: ['circle_and_building'] };
  const said = whyNot(mixed, ASKED);
  assert.match(said, /answered the other way/);
  assert.match(said, /not sure enough/);
});

test('a question this panel has never heard of still gets named', () => {
  const odd = { ...HIT, hit: false, missed: ['something_new'], unsure: [] };
  assert.match(whyNot(odd, ASKED), /something_new/);
});

test('a square with nothing to say says that rather than nothing', () => {
  const empty = { hit: false, missed: [], unsure: [] };
  assert.equal(whyNot(empty, ASKED), 'nothing to go on');
});

// ── The line above the list ────────────────────────────────────

test('it counts what was looked at and what was found', () => {
  assert.equal(progressLine({ of: 49, done: 12, hits: [1, 2], looked: [] }),
               '12 of 49 · 2 hits');
});

test('one hit is not "1 hits"', () => {
  assert.equal(progressLine({ of: 49, done: 49, hits: [1], looked: [] }),
               '49 of 49 · 1 hit');
});

test('and squares nobody could answer for are counted out loud', () => {
  // The sentence that makes a sweep readable: "forty-nine squares, two hits,
  // nine under cloud" is a result. "Two hits" on its own is not.
  const said = progressLine({ of: 49, done: 49, hits: [1],
    looked: [{ trouble: 'cloud' }, { trouble: 'cloud' }, {}] });
  assert.match(said, /2 could not be answered/);
});

test('a clean sweep does not mention them', () => {
  const said = progressLine({ of: 4, done: 4, hits: [], looked: [{}, {}] });
  assert.doesNotMatch(said, /could not be answered/);
});

test('with no key it says that rather than "0 of 0"', () => {
  assert.match(progressLine({ of: 0, done: 0, hits: [], looked: [],
                              have_key: false }), /Codiv API key/);
});

test('with a key and no sweep yet it says it is ready', () => {
  assert.equal(progressLine({ of: 0, done: 0, hits: [], looked: [],
                              have_key: true }), 'Ready');
});

test('and before anything has arrived it says so', () => {
  assert.equal(progressLine(null), 'Loading…');
});

// ── The wiring ─────────────────────────────────────────────────

const SOURCE = readFileSync(
  new URL('../frontend/js/lookout.js', import.meta.url), 'utf8');
const API = readFileSync(
  new URL('../frontend/js/api.js', import.meta.url), 'utf8');

test('the key goes in a POST body, never in a URL', () => {
  // A query string ends up in the server log and in the browser's history.
  assert.match(API, /lookoutKey: \(key\) => request\('\/api\/lookout\/key', \{ body: \{ key \} \}\)/);
});

test('the key box is a password field and is emptied after use', () => {
  assert.match(SOURCE, /type: 'password'/);
  assert.match(SOURCE, /if \(box\) box\.value = '';/);
});

test('the panel says where the answers come from', () => {
  assert.match(SOURCE, /OpenJev at Codiv/);
});

test('and that the key is not written down', () => {
  assert.match(SOURCE, /written nowhere/);
});

test('all three outcomes are drawn differently, in outline and in fill', () => {
  // Per line, not "somewhere in the block". A three-way choice left on the
  // fill and dropped from the outline reads as two outcomes with a slightly
  // different wash, which is the one mistake that makes a sweep untrustworthy
  // -- and a block-wide match passes straight through it.
  const at = SOURCE.indexOf('for (const square of state.looked');
  const block = SOURCE.slice(at, at + 1600);
  const line = (name) => block.split('\n').find(
    (row) => row.trim().startsWith(`${name}:`)) ?? '';
  for (const name of ['color', 'fillColor']) {
    assert.match(line(name), /hit \?/, `${name} does not single out a hit`);
    assert.match(line(name), /blank \?/, `${name} does not single out a blank`);
  }
  // And a hit is not merely a different colour: it is heavier and more solid.
  assert.match(line('weight'), /hit \?/);
  assert.match(line('fillOpacity'), /hit \?/);
});
