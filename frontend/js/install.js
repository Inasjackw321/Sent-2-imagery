// Progressive-web-app plumbing: service worker, install prompt, standalone chrome.

import { $, toast } from './ui.js';

const isStandalone = () =>
  window.matchMedia('(display-mode: standalone)').matches
  || window.matchMedia('(display-mode: window-controls-overlay)').matches
  || window.navigator.standalone === true;

export function initInstall() {
  if (isStandalone()) document.body.classList.add('is-app');

  registerServiceWorker();
  wireInstallButton();
  wireFullScreen();
  applyStartupTab();
}

/**
 * Full screen, for when the map is the only thing you want on the monitor.
 *
 * The button tracks the browser rather than its own idea of the state: the
 * user can leave full screen with Escape or F11 without touching it, and a
 * button still offering to "exit" after that is a button that lies.
 */
function wireFullScreen() {
  const button = $('#fullBtn');
  if (!button) return;

  // Safari on iPhone has no Fullscreen API at all. Offering a control that
  // cannot do anything is worse than not offering one.
  if (!document.documentElement.requestFullscreen) {
    button.hidden = true;
    return;
  }

  button.addEventListener('click', async () => {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await document.documentElement.requestFullscreen({ navigationUI: 'hide' });
    } catch (err) {
      toast(`Full screen was refused: ${err.message}`, 'err');
    }
  });

  document.addEventListener('fullscreenchange', () => {
    const on = Boolean(document.fullscreenElement);
    document.body.classList.toggle('is-fullscreen', on);
    $('#fullIcon').textContent = on ? '⤡' : '⤢';
    button.title = on ? 'Leave full screen (Esc)' : 'Full screen';
    button.setAttribute('aria-label', button.title);
    button.classList.toggle('is-on', on);
  });
}

function registerServiceWorker() {
  if (!('serviceWorker' in navigator)) return;
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch((err) => {
      // Not fatal: the app works fine without offline caching.
      console.warn('Service worker not registered:', err.message);
    });
  });
}

function wireInstallButton() {
  const button = $('#installBtn');
  if (!button) return;
  let deferred = null;

  window.addEventListener('beforeinstallprompt', (event) => {
    event.preventDefault();
    deferred = event;
    button.hidden = isStandalone();
  });

  button.addEventListener('click', async () => {
    if (!deferred) return;
    button.disabled = true;
    deferred.prompt();
    const { outcome } = await deferred.userChoice;
    deferred = null;
    button.disabled = false;
    if (outcome === 'accepted') button.hidden = true;
  });

  window.addEventListener('appinstalled', () => {
    button.hidden = true;
    toast('Installed — you can now open EarthViewer from your desktop.', 'ok');
  });
}

/** Manifest shortcuts land on /?tab=timelapse and friends. */
function applyStartupTab() {
  const wanted = new URLSearchParams(location.search).get('tab');
  if (!wanted) return;
  const tab = document.querySelector(`#tabs .tab[data-tab="${CSS.escape(wanted)}"]`);
  tab?.click();
}
