// The decisions the Electron shell makes, kept free of Electron itself so
// they can be tested with plain Node: which Python to run, what to run it
// with, where the engine says it is, what the window may show and what the
// page may ask for.

'use strict';

const path = require('node:path');

const SERVE_LINE = 'EARTHVIEWER_URL=';

/**
 * Pythons to try, best first, as [command, ...leading args].
 *
 * One named in EARTHVIEWER_PYTHON wins. Then the project's own .venv, which is
 * what the double-click launchers create. Then whatever is on the PATH.
 */
function pythonCandidates(root, platform, env = {}) {
  const out = [];
  if (env.EARTHVIEWER_PYTHON) out.push([env.EARTHVIEWER_PYTHON]);
  if (platform === 'win32') {
    out.push([path.join(root, '.venv', 'Scripts', 'python.exe')]);
    out.push(['py', '-3'], ['python']);
  } else {
    out.push([path.join(root, '.venv', 'bin', 'python3')]);
    out.push(['python3'], ['python']);
  }
  return out;
}

/** The arguments for the engine: app.py in serve-only mode, on this port. */
function serveArgs(root, port, argv = []) {
  const args = [path.join(root, 'app.py'), '--serve', '--port', String(port)];
  // Passed through from the shell's own command line: `npm start -- --demo`.
  if (argv.includes('--demo')) args.push('--demo');
  return args;
}

/** The engine's address out of what it has printed so far, or null. */
function readServeLine(text) {
  for (const line of String(text).split(/\r?\n/)) {
    if (!line.startsWith(SERVE_LINE)) continue;
    const url = line.slice(SERVE_LINE.length).trim();
    // Only ever this machine. The line comes from a process this shell
    // started, but a window pointed at another host by a stray line would be
    // a browser for somebody else's site with this shell's permissions.
    if (/^http:\/\/(localhost|127\.0\.0\.1):\d+\/?$/.test(url)) return url.replace(/\/$/, '');
  }
  return null;
}

/** Whether a URL is this app's own page, which is all the window may show. */
function isOwnPage(target, origin) {
  if (!origin) return false;
  try {
    return new URL(target).origin === origin;
  } catch {
    return false;
  }
}

// What the page may ask for. Copying a highlighted region of the map puts a
// picture on the clipboard, and the Wall view goes full screen. Everything
// else -- camera, microphone, location, notifications -- is refused: nothing
// in this app uses them.
const ALLOWED = new Set(['clipboard-sanitized-write', 'clipboard-read', 'fullscreen']);

function permitted(permission, requestingUrl, origin) {
  return ALLOWED.has(permission) && isOwnPage(requestingUrl, origin);
}

module.exports = {
  SERVE_LINE, pythonCandidates, serveArgs, readServeLine, isOwnPage, permitted,
};
