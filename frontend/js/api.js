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
  aisKey: (key) => request('/api/vessels/key', { body: { key } }),
  probe: (body) => request('/api/probe', { body }),
  render: (body) => request('/api/render', { body }),
  renderFile: (body) => request('/api/render?download=1', { body, raw: true }),
};
