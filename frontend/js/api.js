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
  render: (body) => request('/api/render', { body }),
  renderFile: (body) => request('/api/render?download=1', { body, raw: true }),
};
