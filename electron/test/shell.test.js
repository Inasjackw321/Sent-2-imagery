// The Electron shell's decisions, without Electron: which Python, what it is
// run with, where the engine says it is, what the window may show and what
// the page may ask for.

'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');

const shell = require('../shell.js');

test('a Python named in the environment wins, then the project .venv, then the PATH', () => {
  const got = shell.pythonCandidates('/repo', 'linux', { EARTHVIEWER_PYTHON: '/opt/py/bin/python3' });
  assert.deepEqual(got[0], ['/opt/py/bin/python3']);
  assert.deepEqual(got[1], [path.join('/repo', '.venv', 'bin', 'python3')]);
  assert.deepEqual(got.slice(2), [['python3'], ['python']]);
});

test('Windows looks for the launcher-made .venv and the py launcher', () => {
  const got = shell.pythonCandidates('C:\\repo', 'win32', {});
  assert.ok(got[0][0].endsWith(path.join('.venv', 'Scripts', 'python.exe')));
  assert.deepEqual(got[1], ['py', '-3']);
});

test('the engine is app.py in serve-only mode on the chosen port', () => {
  const args = shell.serveArgs('/repo', 51234, ['electron', '.']);
  assert.deepEqual(args, [path.join('/repo', 'app.py'), '--serve', '--port', '51234']);
  assert.ok(shell.serveArgs('/repo', 1, ['electron', '.', '--demo']).includes('--demo'));
});

test('the address is read from the engine\'s one line, and only a local one', () => {
  assert.equal(shell.readServeLine('  Installing…\nEARTHVIEWER_URL=http://localhost:51234\n'),
    'http://localhost:51234');
  assert.equal(shell.readServeLine('EARTHVIEWER_URL=http://127.0.0.1:8000/'), 'http://127.0.0.1:8000');
  assert.equal(shell.readServeLine('nothing yet'), null);
  // A line pointing anywhere else is not followed.
  assert.equal(shell.readServeLine('EARTHVIEWER_URL=http://evil.example:80'), null);
  assert.equal(shell.readServeLine('EARTHVIEWER_URL=https://localhost:1'), null);
});

test('the window shows this app\'s own page and nothing else', () => {
  const origin = 'http://localhost:51234';
  assert.equal(shell.isOwnPage('http://localhost:51234/#map', origin), true);
  assert.equal(shell.isOwnPage('http://localhost:9999/', origin), false);
  assert.equal(shell.isOwnPage('https://t.me/s/radarrussiia', origin), false);
  assert.equal(shell.isOwnPage('not a url', origin), false);
  assert.equal(shell.isOwnPage('http://localhost:51234/', null), false);
});

test('the page may copy to the clipboard and go full screen, and nothing more', () => {
  const origin = 'http://localhost:51234';
  for (const allowed of ['clipboard-sanitized-write', 'clipboard-read', 'fullscreen']) {
    assert.equal(shell.permitted(allowed, `${origin}/`, origin), true, allowed);
  }
  for (const refused of ['media', 'geolocation', 'notifications', 'openExternal']) {
    assert.equal(shell.permitted(refused, `${origin}/`, origin), false, refused);
  }
  assert.equal(shell.permitted('fullscreen', 'https://elsewhere.example/', origin), false);
});

test('the engine prints the line this shell waits for', () => {
  // Two languages, one string: if app.py's ever changes, Electron would wait
  // for a line that never comes.
  const source = fs.readFileSync(path.join(__dirname, '..', '..', 'app.py'), 'utf8');
  assert.ok(source.includes(`SERVE_LINE = "${shell.SERVE_LINE}"`));
});

test('the engine is asked for UTF-8 output', () => {
  // Windows gives a pipe its old code page otherwise: the messages arrive
  // garbled, and before app.py guarded its output, the first arrow crashed it.
  const main = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');
  assert.match(main, /PYTHONIOENCODING: 'utf-8'/);
});

test('the window is locked down', () => {
  const main = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');
  for (const needed of ['contextIsolation: true', 'nodeIntegration: false', 'sandbox: true',
                        'webSecurity: true', 'setWindowOpenHandler', "'will-navigate'",
                        'requestSingleInstanceLock']) {
    assert.ok(main.includes(needed), needed);
  }
});
