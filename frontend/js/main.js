// Bootstrap: load config, wire the tabs, start each panel.

import { api } from './api.js';
import { store } from './store.js';
import { $, $$, toast } from './ui.js';
import { initMap, initPlaceSearch, invalidateMap } from './map.js';
import { initCapture } from './capture.js';
import { initEditor, refreshEditor } from './editor.js';
import { initTimelapse, refreshTimelapse } from './timelapse.js';
import { initGraphic, refreshGraphic } from './graphic.js';

const REFRESH = {
  capture: invalidateMap,
  edit: refreshEditor,
  timelapse: refreshTimelapse,
  graphic: refreshGraphic,
};

function initTabs() {
  $$('#tabs .tab').forEach((tab) => tab.addEventListener('click', () => {
    const name = tab.dataset.tab;
    $$('#tabs .tab').forEach((t) => t.classList.toggle('is-active', t === tab));
    $$('.panel').forEach((p) => { p.hidden = p.dataset.panel !== name; });
    $$('.stage-view').forEach((v) => { v.hidden = v.dataset.view !== name; });
    REFRESH[name]?.();
  }));
}

async function main() {
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
  } else {
    $('#sourceBadge').textContent = `${store.config.collection} · ${new URL(store.config.stac_url).host}`;
  }

  initTabs();
  initMap();
  initPlaceSearch();
  initCapture();
  initEditor();
  initTimelapse();
  initGraphic();
}

main();
