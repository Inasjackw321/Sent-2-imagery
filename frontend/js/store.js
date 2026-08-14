// Shared application state with a minimal pub/sub.

const listeners = new Map();

export const store = {
  config: null,          // /api/config payload
  aoi: null,             // GeoJSON polygon
  aoiInfo: null,         // { area_km2, bounds, center }
  dates: [],             // search results, newest first
  selected: new Set(),   // ids of the dates ticked for merging
  activeDateId: null,    // the one date shown when nothing is merged
  image: null,           // the imagery on screen: { src, meta }
};

export function on(event, fn) {
  if (!listeners.has(event)) listeners.set(event, new Set());
  listeners.get(event).add(fn);
  return () => listeners.get(event).delete(fn);
}

export function emit(event, payload) {
  for (const fn of listeners.get(event) ?? []) {
    try { fn(payload); } catch (err) { console.error(`[${event}]`, err); }
  }
}

export function setImage(image) {
  store.image = image;
  emit('image', image);
  return image;
}
