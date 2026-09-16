// Thin wrapper over the backend API.

async function request(path, { method = 'POST', body, raw = false } = {}) {
  const res = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const data = await res.json();
      if (data.detail) detail = data.detail;
    } catch { /* non-JSON error body */ }
    throw new Error(detail);
  }
  return raw ? res.blob() : res.json();
}

export const api = {
  config: () => request('/api/config', { method: 'GET' }),
  describeAoi: (aoi) => request('/api/aoi', { body: { aoi } }),
  search: (body) => request('/api/search', { body }),
  geocode: (q) => request(`/api/geocode?q=${encodeURIComponent(q)}`, { method: 'GET' }),
  weather: (lon, lat) => request(`/api/weather?lon=${lon}&lat=${lat}`, { method: 'GET' }),
  passes: (lon, lat) => request(`/api/passes?lon=${lon}&lat=${lat}`, { method: 'GET' }),
  fires: ({ west, south, east, north, hours }) => request(
    `/api/fires?${new URLSearchParams({
      west: west.toFixed(4), south: south.toFixed(4),
      east: east.toFixed(4), north: north.toFixed(4), hours,
    })}`, { method: 'GET' }),
  vessels: ([west, south, east, north], source = 'digitraffic') => request(
    `/api/vessels?${new URLSearchParams({
      west: west.toFixed(4), south: south.toFixed(4),
      east: east.toFixed(4), north: north.toFixed(4), source,
    })}`, { method: 'GET' }),
  mtg: () => request('/api/mtg', { method: 'GET' }),
  tracker: () => request('/api/tracker', { method: 'GET' }),
  // Asked on its own, because the daemon can be started while the page is
  // open and the panel should be able to notice without a reload.
  ollama: () => request('/api/ollama', { method: 'GET' }),
  trackerDismiss: (id, restore = false) =>
    request('/api/tracker/dismiss', { body: { id, restore } }),
  // Names the Ollama model to read with; null lets the backend choose from
  // whatever is installed.
  copernicus: () => request('/api/copernicus', { method: 'GET' }),

  quakes: ({ west, south, east, north, hours, minMagnitude }) => request(
    `/api/quakes?${new URLSearchParams({
      west: west.toFixed(4), south: south.toFixed(4),
      east: east.toFixed(4), north: north.toFixed(4),
      hours, min_magnitude: minMagnitude,
    })}`, { method: 'GET' }),
  seismographs: ({ west, south, east, north }) => request(
    `/api/seismographs?${new URLSearchParams({
      west: west.toFixed(4), south: south.toFixed(4),
      east: east.toFixed(4), north: north.toFixed(4),
    })}`, { method: 'GET' }),
  // A URL rather than a request: it goes straight into an <img src>.
  traceUrl: ({ network, station, channel, loc = '', minutes }) =>
    `/api/seismographs/trace.png?${new URLSearchParams({
      network, station, channel, loc, minutes,
    })}`,
  aisKey: (key) => request('/api/vessels/key', { body: { key } }),
  aisTest: () => request('/api/vessels/test', { body: {} }),
  probe: (body) => request('/api/probe', { body }),
  render: (body) => request('/api/render', { body }),
  renderFile: (body) => request('/api/render?download=1', { body, raw: true }),
  // The province borders, for drawing a picture with no map tiles in it.
  // Asked for once, when somebody exports one -- see trackershot.js.
  trackerOutlines: () => request('/api/tracker/outlines', { method: 'GET' }),
  // NOTAMs are read from text somebody pasted rather than fetched from
  // anywhere -- see backend/notams.py for why there is no live source.
  readNotams: (text) => request('/api/notams/read', { body: { text } }),
  notamsDemo: () => request('/api/notams/demo', { method: 'GET' }),
};
