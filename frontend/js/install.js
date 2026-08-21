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
  applyStartupTab();
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
