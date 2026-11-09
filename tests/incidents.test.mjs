// Detected fires on the live Fire Temperature picture: which are shown for
// the frame on screen, and how their megawatts are said.

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  HOLD_HOURS, activeAt, fireColour, mwSaid, whenSaid,
} from '../frontend/js/incidents.js';

const fire = (acquired) => ({ lat: 50, lon: 30, frp: 10, acquired, sensor: 'viirs-snpp' });

test('a detection is evidence for the hours after it, not before', () => {
  const f = fire('2026-11-06T10:00:00Z');
  assert.equal(activeAt([f], '2026-11-06T09:00:00Z').length, 0);
  assert.equal(activeAt([f], '2026-11-06T10:00:00Z').length, 1);
  assert.equal(activeAt([f], `2026-11-06T${10 + HOLD_HOURS - 1}:00:00Z`).length, 1);
  assert.equal(activeAt([f], `2026-11-06T${10 + HOLD_HOURS + 1}:00:00Z`).length, 0);
  // A whole-day frame is a range; its start is the moment.
  assert.equal(activeAt([f], '2026-11-06T10:00:00Z/2026-11-07T00:00:00Z').length, 1);
  assert.deepEqual(activeAt([f], 'not a time'), []);
});

test('megawatts are said plainly', () => {
  assert.equal(mwSaid(87), '87 MW');
  assert.equal(mwSaid(12.34), '12.3 MW');
  assert.equal(mwSaid(1530), '1.5 GW');
  assert.equal(mwSaid(0.4), 'under 1 MW');
  assert.equal(mwSaid(null), 'power not measured');
});

test('times are UTC, and bigger fires are drawn hotter', () => {
  assert.equal(whenSaid('2026-11-06T01:12:00Z'), '6 Nov 01:12 UTC');
  assert.equal(fireColour(5), '#e5383b');
  assert.equal(fireColour(150), '#fff3b0');
});
