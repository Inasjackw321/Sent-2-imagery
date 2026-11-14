// The video editor's arithmetic: crops, output sizes, the look, names.

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  ASPECTS, SPEEDS, clampCrop, codecsFor, cropFor, encodersFor, filterFor, lengthSaid, outputSize,
  withExtension,
} from '../frontend/js/videoeditor.js';

test('an aspect crop is the largest of that shape, centred', () => {
  assert.deepEqual(cropFor('free', 1280, 720), { x: 0, y: 0, w: 1280, h: 720 });
  assert.deepEqual(cropFor('1:1', 1280, 720), { x: 280, y: 0, w: 720, h: 720 });
  assert.deepEqual(cropFor('9:16', 1280, 720), { x: 438, y: 0, w: 405, h: 720 });
  assert.deepEqual(cropFor('16:9', 1280, 720), { x: 0, y: 0, w: 1280, h: 720 });
  const tall = cropFor('16:9', 720, 1280);
  assert.equal(tall.w, 720);
  assert.equal(tall.h, 405);
});

test('a dragged crop stays inside the frame and never vanishes', () => {
  assert.deepEqual(clampCrop({ x: -50, y: 900, w: 300, h: 200 }, 1280, 720), { x: 0, y: 520, w: 300, h: 200 });
  assert.deepEqual(clampCrop({ x: 10, y: 10, w: 2, h: 5000 }, 1280, 720), { x: 10, y: 0, w: 32, h: 720 });
});

test('the video is even-sized for H.264 and no bigger than 1920', () => {
  assert.deepEqual(outputSize({ w: 1281, h: 721 }), { w: 1280, h: 720 });
  assert.deepEqual(outputSize({ w: 405, h: 720 }), { w: 404, h: 720 });
  const huge = outputSize({ w: 3840, h: 2160 });
  assert.ok(huge.w <= 1920 && huge.h <= 1920 && huge.w % 2 === 0 && huge.h % 2 === 0);
});

test('the look sliders become a canvas filter, and none when untouched', () => {
  assert.equal(filterFor({}), 'none');
  assert.equal(filterFor({ brightness: 120, contrast: 100, saturation: 80 }),
    'brightness(120%) saturate(80%)');
});

test('lengths, names and codecs', () => {
  assert.equal(lengthSaid(24, 8), '24 frames · 3.0 s');
  assert.equal(lengthSaid(1, 2), '1 frame · 0.5 s');
  assert.equal(withExtension('fire_area', 'video/mp4'), 'fire_area.mp4');
  assert.equal(withExtension('fire_area.webm', 'video/mp4'), 'fire_area.mp4');
  assert.equal(withExtension('x', 'video/webm'), 'x.webm');
  assert.ok(codecsFor(1280, 720).every((c) => c.startsWith('avc1.')));
  assert.ok(SPEEDS.includes(8) && ASPECTS.some(([v]) => v === '9:16'));
});

test('H.264 first, VP9 in MP4 only as the fallback', () => {
  const order = encodersFor(1280, 720).map((e) => e.mux);
  assert.deepEqual(order, ['avc', 'avc', 'avc', 'vp9']);
});
