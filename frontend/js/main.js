// Bootstrap: load the config, start the map and the panel.

import { api } from './api.js';
import { store } from './store.js';
import { $, toast } from './ui.js';
import { initMap, initPlaceSearch } from './map.js';
import { initImagery } from './imagery.js';
import { initInstall } from './install.js';

async function main() {
  // Do this first, so the install button and offline shell work even if the
  // backend is slow or unreachable.
  initInstall();

  try {
    store.config = await api.config();
  } catch (err) {
    toast(`Could not reach the backend: ${err.message}`, 'err');
    return;
  }

  if (store.config.demo) {
    $('#demoBadge').hidden = false;
    $('#sourceBadge').textContent = 'Synthetic imagery';
    toast('Demo mode: imagery is synthetic, not real Sentinel-2 data.');
  }

  initMap();
  initPlaceSearch();
  initImagery();
}

main();
