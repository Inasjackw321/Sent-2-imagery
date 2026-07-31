// Shared application state with a minimal pub/sub.

const listeners = new Map();

export const store = {
  config: null,          // /api/config payload
  source: null,          // selected satellite key
  aoi: null,             // GeoJSON polygon
  aoiInfo: null,         // { area_km2, bounds, center }
  scenes: [],            // search results
  selected: new Set(),   // scene ids ticked for timelapse / comparison
  activeSceneId: null,
  captures: [],          // rendered images: { id, name, src, image, meta }
  activeCaptureId: null,
  adjustments: null,     // current edit stack (set by editor)
  frames: [],            // timelapse frames: { captureId, label }
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

let captureSeq = 0;

export function addCapture({ src, image, meta, name }) {
  const capture = {
    id: `cap-${++captureSeq}`,
    name: name || defaultCaptureName(meta),
    src, image, meta,
    createdAt: Date.now(),
  };
  store.captures.push(capture);
  store.activeCaptureId = capture.id;
  emit('captures', store.captures);
  emit('activeCapture', capture);
  return capture;
}

export function defaultCaptureName(meta) {
  if (!meta) return 'Image';
  if (meta.mode === 'change') {
    return `Δ ${meta.index.toUpperCase()} ${meta.scene_a.date} → ${meta.scene_b.date}`;
  }
  const what = meta.index ? meta.index.toUpperCase() : (meta.label ?? 'Image');
  return `${meta.scene?.date ?? ''} · ${what}`;
}

export const getCapture = (id) => store.captures.find((c) => c.id === id) ?? null;
export const activeCapture = () => getCapture(store.activeCaptureId);

export function setActiveCapture(id) {
  if (!getCapture(id)) return;
  store.activeCaptureId = id;
  emit('activeCapture', activeCapture());
}

export function removeCapture(id) {
  const i = store.captures.findIndex((c) => c.id === id);
  if (i < 0) return;
  store.captures.splice(i, 1);
  store.frames = store.frames.filter((f) => f.captureId !== id);
  if (store.activeCaptureId === id) {
    store.activeCaptureId = store.captures.at(-1)?.id ?? null;
    emit('activeCapture', activeCapture());
  }
  emit('captures', store.captures);
  emit('frames', store.frames);
}

/** Every capture that is demo/synthetic taints anything built from it. */
export const isDemo = (captures = store.captures) =>
  captures.some((c) => c?.meta?.demo) || Boolean(store.config?.demo);
