// Tests for the one path every picture in this app is saved through.
//
// Run with:  node --test tests/saving.test.mjs
//
// "I press save and nothing happens" is a real thing browsers do, and it is
// the reason this path exists. An <a download> click is a request, not an
// action: an installed app, a locked-down profile or an embedded view can
// drop it on the floor with no error, no file, and no way for the page to
// tell. The picture gets made, the button looks like it worked, and there is
// nothing on disk.
//
// So the tests below are mostly about what happens when a route fails.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const here = fileURLToPath(new URL('.', import.meta.url));
const read = (name) => readFileSync(`${here}../frontend/js/${name}`, 'utf8');

/** A file with its comments stripped, so a match is a match in the code. */
const bare = (text) => text
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .split('\n')
  .map((line) => line.replace(/(^|\s)\/\/.*$/, '$1'))
  .join('\n');

// ── The behaviour, against a stand-in browser ──────────────────

/** Just enough document, navigator and window for the save path to run. */
function browser({ share = null, canShare = false, clicks = null,
                   opens = null, blockDownload = false } = {}) {
  const made = [];
  const said = [];
  const doc = {
    body: { append: () => {} },
    createTextNode: (text) => ({ nodeType: 3, text }),
    createElement: (tag) => {
      const node = {
        tag, style: {}, dataset: {}, classList: { add() {}, remove() {}, toggle() {} },
        setAttribute(k, v) { node[k] = v; },
        addEventListener() {},
        append(...kids) {
          node.said = kids.map((k) => (k?.text ?? k ?? '')).join('');
        },
        remove() {},
        click() {
          if (blockDownload) throw new Error('downloads are disabled here');
          clicks?.push(node.download);
        },
        querySelector: () => null,
        querySelectorAll: () => [],
      };
      made.push(node);
      return node;
    },
    // The toast dock, which every one of these paths speaks through.
    querySelector: () => ({
      append: (node) => said.push(node.said ?? ''),
      remove() {}, classList: { add() {}, remove() {} },
    }),
  };
  // Defined rather than assigned: node's own navigator is a getter, and
  // assigning over it throws.
  const put = (name, value) => Object.defineProperty(
    globalThis, name, { value, configurable: true, writable: true });
  put('document', doc);
  put('navigator', share ? { share, canShare: () => canShare } : {});
  put('window', {
    open: (...args) => { opens?.push(args[0]); return opens ? {} : null; },
  });
  globalThis.URL.createObjectURL = () => 'blob:stand-in';
  globalThis.URL.revokeObjectURL = () => {};
  globalThis.setTimeout = () => 0;            // nothing waits in a test
  globalThis.clearTimeout = () => {};
  return { made, said };
}

const picture = (size = 2048) => ({ size, type: 'image/png' });

async function load() {
  // Imported fresh each time so the stand-ins above are the ones it sees.
  return import(`../frontend/js/ui.js?${Math.random()}`);
}

test('a picture with nothing in it is refused rather than silently saved', async () => {
  browser();
  const { savePicture } = await load();
  assert.equal(await savePicture(null, 'x.png'), 'failed');
  assert.equal(await savePicture({ size: 0, type: 'image/png' }, 'x.png'), 'failed');
});

test('where a device can share a file, it is shared', async () => {
  const shared = [];
  const { said } = browser({ share: async (what) => shared.push(what), canShare: true });
  globalThis.File = class { constructor(parts, name, opts) { this.name = name; Object.assign(this, opts); } };
  const { savePicture } = await load();
  assert.equal(await savePicture(picture(), 'air.png', { title: 'Air' }), 'shared');
  assert.equal(shared.length, 1);
  assert.equal(shared[0].title, 'Air');
  // A share is not a save: on a phone the file is not anywhere until the
  // person picks something from the sheet, so the wording has to say that.
  assert.ok(said.some((t) => /Save Image/.test(t)), said);
});

test('cancelling the share sheet does not then download it anyway', async () => {
  // The person said no. Falling through to a file would be doing the thing
  // they just declined -- and claiming a save on top of it.
  const clicks = [];
  const { said } = browser({
    share: async () => { const e = new Error('cancelled'); e.name = 'AbortError'; throw e; },
    canShare: true, clicks,
  });
  globalThis.File = class { constructor(parts, name, opts) { this.name = name; } };
  const { savePicture } = await load();
  assert.equal(await savePicture(picture(), 'air.png'), 'cancelled');
  assert.deepEqual(clicks, []);
  assert.deepEqual(said.filter((t) => /saved/i.test(t)), [],
    'it claimed to have saved a picture the person cancelled');
});

test('where there is no share sheet, it is downloaded', async () => {
  const clicks = [];
  const { said } = browser({ clicks });
  const { savePicture } = await load();
  assert.equal(await savePicture(picture(), 'imagery.png'), 'downloaded');
  assert.deepEqual(clicks, ['imagery.png']);
  // And says so, with the name and the size: a save nobody confirmed is a
  // save nobody can trust, which is where this started.
  assert.ok(said.some((t) => t.includes('imagery.png')), said);
});

test('a browser that refuses the download is not the end of it', async () => {
  // The case this whole path is for: no error the page can see, no file. The
  // picture goes to a tab of its own so it can be saved by hand.
  const opens = [];
  browser({ blockDownload: true, opens });
  const { savePicture } = await load();
  assert.equal(await savePicture(picture(), 'imagery.png'), 'opened');
  assert.deepEqual(opens, ['blob:stand-in']);
});

test('and if even that is blocked, it says so', async () => {
  browser({ blockDownload: true, opens: null });
  const { savePicture } = await load();
  assert.equal(await savePicture(picture(), 'imagery.png'), 'failed');
});

// ── Every export goes through it ───────────────────────────────

test('nothing saves a picture by hand any more', () => {
  // A bare anchor click is the thing that fails silently. One route, so that
  // when it needs fixing again it is fixed everywhere.
  for (const name of ['imagery.js', 'capture.js', 'markup.js']) {
    const code = bare(read(name));
    assert.ok(!/\.download\s*=/.test(code),
      `${name} sets a download attribute by hand`);
    assert.ok(!/createObjectURL/.test(code),
      `${name} builds its own object URL instead of using the save path`);
  }
});

test('the imagery buttons use it', () => {
  const code = bare(read('imagery.js'));
  assert.ok(/savePicture\(/.test(code));
  // PNG, GeoTIFF and the animation.
  assert.ok((code.match(/savePicture\(/g) ?? []).length >= 3,
    'not every export in the imagery panel goes through it');
});

test('the crop and the markup editor use it too', () => {
  assert.match(bare(read('capture.js')), /savePicture\(/);
  assert.match(bare(read('markup.js')), /savePicture\(/);
});

test('a canvas that cannot make the file falls back to the server', () => {
  // toBlob hands back nothing rather than raising when the picture is too
  // large for the browser, and a null blob used to go straight to the
  // download as if it were a file.
  assert.match(bare(read('imagery.js')), /if \(blob\) \{/);
});

test('the anchor is left in the document long enough to be clicked', () => {
  // Removed on the next line, the click -- which is queued, not performed --
  // can find nothing there when it runs.
  const code = bare(read('ui.js'));
  assert.match(code, /setTimeout\(\(\) => a\.remove\(\)/);
});
