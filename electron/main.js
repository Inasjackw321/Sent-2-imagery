// EarthViewer as an Electron app.
//
// The engine is still the Python backend: windowed reads of satellite
// imagery, the render pipeline, the tracker, the proxies. This file is the
// shell around it -- it starts `app.py --serve` on a free port, waits for the
// one line that gives its address, and shows that in a window it controls.
// Closing the window stops the engine.
//
// The window is locked down the way a browser tab is: no Node in the page, a
// sandboxed renderer, and the only thing it may show is this app's own page.
// Any other link opens in the system browser. The page already carries its
// own Content-Security-Policy from the backend, so nothing here relaxes it.

'use strict';

const { app, BrowserWindow, Menu, dialog, session, shell } = require('electron');
const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const http = require('node:http');
const net = require('node:net');
const path = require('node:path');

const { pythonCandidates, serveArgs, readServeLine, isOwnPage, permitted } =
  require('./shell.js');

// The repository root: one level up when run from the checkout, or the
// resources folder when packaged (see "extraResources" in package.json).
const ROOT = app.isPackaged
  ? path.join(process.resourcesPath, 'earthviewer')
  : path.resolve(__dirname, '..');

// How long the engine gets to say where it is. The first run installs the
// Python dependencies, which is minutes rather than seconds.
const START_TIMEOUT_MS = 10 * 60 * 1000;

let engine = null;     // the Python child process
let origin = null;     // http://localhost:PORT, once known
let win = null;

function freePort() {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.unref();
    probe.on('error', reject);
    probe.listen(0, '127.0.0.1', () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
  });
}

/** The first Python on this machine that actually runs. */
function findPython() {
  for (const [command, ...pre] of pythonCandidates(ROOT, process.platform, process.env)) {
    if (path.isAbsolute(command) && !fs.existsSync(command)) continue;
    const done = spawnSync(command, [...pre, '--version'], { encoding: 'utf8' });
    if (done.status === 0) return [command, ...pre];
  }
  return null;
}

function startEngine(port) {
  const python = findPython();
  if (!python) {
    return Promise.reject(new Error(
      'Python 3.10 or newer is needed and none was found. Install it from '
      + 'python.org, or set EARTHVIEWER_PYTHON to the one to use.'));
  }
  const [command, ...pre] = python;
  const args = [...pre, ...serveArgs(ROOT, port, process.argv)];
  engine = spawn(command, args, {
    cwd: ROOT,
    // UTF-8 output: Windows would otherwise give a pipe its old code page,
    // and the engine's messages would arrive garbled (or, before app.py
    // guarded against it, crash it outright on the first arrow).
    env: {
      ...process.env, PYTHONUNBUFFERED: '1', SENT2_NO_AUTO_NATIVE: '1',
      PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });
  return new Promise((resolve, reject) => {
    let said = '';
    const timer = setTimeout(
      () => reject(new Error('The engine did not start in time.\n\n' + said.slice(-2000))),
      START_TIMEOUT_MS);
    const listen = (chunk) => {
      const text = chunk.toString();
      process.stdout.write(text);
      said += text;
      const url = readServeLine(said);
      if (url) {
        clearTimeout(timer);
        resolve(url);
      }
    };
    engine.stdout.on('data', listen);
    engine.stderr.on('data', (chunk) => { said += chunk; process.stderr.write(chunk); });
    engine.on('exit', (code) => {
      clearTimeout(timer);
      reject(new Error(`The engine stopped (exit ${code}).\n\n${said.slice(-2000)}`));
      if (win && origin) {
        dialog.showErrorBox('EarthViewer', `The engine stopped unexpectedly (exit ${code}).`);
        app.quit();
      }
    });
  });
}

// How long to wait for the engine to answer once it has said where it is.
// The address is printed just before uvicorn starts listening, so the first
// few asks are refused.
const READY_TIMEOUT_MS = 60 * 1000;

/** Resolve once the engine answers /api/health, or reject after the timeout. */
function untilReady(url) {
  const until = Date.now() + READY_TIMEOUT_MS;
  return new Promise((resolve, reject) => {
    const ask = () => {
      const req = http.get(`${url}/api/health`, { timeout: 2000 }, (res) => {
        res.resume();
        if (res.statusCode === 200) resolve(url);
        else retry();
      });
      req.on('error', retry);
      req.on('timeout', () => { req.destroy(); });
    };
    const retry = () => {
      if (Date.now() > until) reject(new Error('The engine said where it was but never answered.'));
      else setTimeout(ask, 250);
    };
    ask();
  });
}

function stopEngine() {
  if (!engine || engine.exitCode !== null) return;
  if (process.platform === 'win32') {
    // A plain kill leaves uvicorn's own children behind on Windows.
    spawnSync('taskkill', ['/pid', String(engine.pid), '/T', '/F']);
  } else {
    engine.kill('SIGTERM');
  }
}

function lockDown(ses) {
  ses.setPermissionRequestHandler((contents, permission, callback, details) => {
    callback(permitted(permission, details.requestingUrl, origin));
  });
  ses.setPermissionCheckHandler((contents, permission, requestingOrigin) =>
    permitted(permission, requestingOrigin, origin));
}

function createWindow(url) {
  win = new BrowserWindow({
    width: 1500,
    height: 950,
    minWidth: 900,
    minHeight: 600,
    backgroundColor: '#0d1015',
    title: 'EarthViewer',
    icon: path.join(ROOT, 'frontend', 'icons', 'icon-512.png'),
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      spellcheck: false,
    },
  });
  win.once('ready-to-show', () => win.show());

  // New windows: this app's own pages stay out (it opens none), and anything
  // else -- a camera's own site, a Telegram post, a credit link -- goes to the
  // system browser rather than inside this shell.
  win.webContents.setWindowOpenHandler(({ url: target }) => {
    if (/^https?:\/\//.test(target) && !isOwnPage(target, origin)) {
      shell.openExternal(target);
    }
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (event, target) => {
    if (isOwnPage(target, origin)) return;
    event.preventDefault();
    if (/^https?:\/\//.test(target)) shell.openExternal(target);
  });
  win.on('closed', () => { win = null; });
  win.loadURL(url);
}

// One copy at a time: a second launch brings the first to the front.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (!win) return;
    if (win.isMinimized()) win.restore();
    win.focus();
  });

  app.whenReady().then(async () => {
    if (process.platform !== 'darwin') Menu.setApplicationMenu(null);
    lockDown(session.defaultSession);
    try {
      const url = await untilReady(await startEngine(await freePort()));
      origin = new URL(url).origin;
      createWindow(url);
    } catch (err) {
      dialog.showErrorBox('EarthViewer could not start', String(err.message || err));
      stopEngine();
      app.quit();
    }
  });

  app.on('window-all-closed', () => app.quit());
  app.on('before-quit', stopEngine);
  app.on('quit', stopEngine);
  process.on('exit', stopEngine);
}
