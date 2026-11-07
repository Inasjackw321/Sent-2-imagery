// Fire incidents on the Fire Temperature pictures: what is a detected fire,
// how big, and what is only a warm patch.
//
// A colour in an RGB is not a measurement. The Fire Temperature RGB puts the
// 3.8 micron channel in red and the two short-wave infrared channels in green
// and blue, so a fire shows red, then yellow, then white as it gets hotter --
// but a sunlit rock face, a warm town or the edge of a cloud can lift a pixel
// too, and the picture alone cannot tell them apart or say how much energy
// anything is putting out.
//
// The measurement exists: NASA FIRMS publishes every thermal detection the
// polar orbiters make, with its fire radiative power in megawatts and a
// confidence. So the two are put together:
//
//   * a FIRMS detection near the time of the frame is a DETECTED FIRE, drawn
//     with its megawatts, and nearby detections are grouped into incidents;
//   * a hot patch in the frame itself, with no detection near it, is drawn
//     as UNCONFIRMED HEAT -- worth a look, not a fire until something with a
//     thermal sensor agrees.

// Detections closer than this belong to one incident: a fire front is a run
// of 375 m pixels, and the same fire is seen again a few hours later a
// little way along.
export const CLUSTER_KM = 3;
// How long a detection stays on the frames after it was made. The polar
// orbiters pass a few times a day, so a fire seen at 01:00 is still the best
// evidence about that place at 05:00; a day later it is not.
export const HOLD_HOURS = 6;
// A hot patch in a Meteosat frame confirmed by a detection this near, made
// this recently. Meteosat's pixels are 1-3 km and its geolocation is good to
// about one pixel.
export const MATCH_KM = 6;
export const MATCH_HOURS = 12;
// The frame's own background, as heat contrast reckons it: the red channel's
// 97th percentile, and a margin above it before a pixel counts as hot.
export const BACKGROUND_PERCENTILE = 0.97;
export const HOT_MARGIN = 25;
export const HOT_SPAN = 60;
// The most hot patches reported per frame: past this it is a sunlit desert,
// not a list of fires.
export const MOST_SPOTS = 12;

const HOUR = 3600e3;

/** Great-circle distance in kilometres. */
export function kmBetween(a, b) {
  const rad = Math.PI / 180;
  const dLat = (b.lat - a.lat) * rad;
  const dLon = (b.lon - a.lon) * rad;
  const h = Math.sin(dLat / 2) ** 2
    + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * 6371 * Math.asin(Math.min(1, Math.sqrt(h)));
}

const CONFIDENCE = { low: 0, nominal: 1, high: 2 };

/**
 * Group detections into incidents: any two within CLUSTER_KM are the same
 * one, transitively. Biggest first.
 */
export function clusterIncidents(fires, km = CLUSTER_KM) {
  const list = (fires ?? []).filter((f) => Number.isFinite(f.lat) && Number.isFinite(f.lon));
  const parent = list.map((_, i) => i);
  const root = (i) => {
    while (parent[i] !== i) { parent[i] = parent[parent[i]]; i = parent[i]; }
    return i;
  };
  for (let i = 0; i < list.length; i += 1) {
    for (let j = i + 1; j < list.length; j += 1) {
      if (kmBetween(list[i], list[j]) <= km) parent[root(i)] = root(j);
    }
  }
  const groups = new Map();
  list.forEach((f, i) => {
    const r = root(i);
    if (!groups.has(r)) groups.set(r, []);
    groups.get(r).push(f);
  });
  const incidents = [...groups.values()].map((detections) => {
    const mw = (f) => Math.max(f.frp ?? 0, 0);
    const weight = detections.reduce((s, f) => s + (mw(f) || 0.1), 0);
    const peak = detections.reduce((a, f) => (mw(f) > mw(a) ? f : a), detections[0]);
    const times = detections.map((f) => f.acquired).sort();
    const best = detections.reduce((a, f) => (
      (CONFIDENCE[f.confidence_label] ?? 1) > (CONFIDENCE[a] ?? 1) ? f.confidence_label : a),
    'low');
    return {
      lat: detections.reduce((s, f) => s + f.lat * (mw(f) || 0.1), 0) / weight,
      lon: detections.reduce((s, f) => s + f.lon * (mw(f) || 0.1), 0) / weight,
      peakMw: mw(peak),
      totalMw: Math.round(detections.reduce((s, f) => s + mw(f), 0) * 10) / 10,
      count: detections.length,
      first: times[0],
      last: times[times.length - 1],
      peakAt: peak.acquired,
      sensors: [...new Set(detections.map((f) => f.sensor))],
      confidence: best,
      detections,
    };
  });
  incidents.sort((a, b) => b.peakMw - a.peakMw || b.count - a.count);
  return incidents.map((inc, i) => ({ ...inc, id: i + 1 }));
}

/** The detections that are evidence about the moment `time`. */
export function activeAt(fires, time, holdHours = HOLD_HOURS) {
  const t = Date.parse(String(time ?? '').split('/')[0]);
  if (!Number.isFinite(t)) return [];
  return (fires ?? []).filter((f) => {
    const seen = Date.parse(f.acquired);
    return seen <= t + 0.25 * HOUR && seen >= t - holdHours * HOUR;
  });
}

/** The red channel's value below which `share` of the pixels fall. */
function redPercentile(data, share) {
  const counts = new Uint32Array(256);
  for (let i = 0; i < data.length; i += 4) counts[data[i]] += 1;
  const want = (data.length / 4) * share;
  let seen = 0;
  for (let v = 0; v < 256; v += 1) {
    seen += counts[v];
    if (seen >= want) return v;
  }
  return 255;
}

/**
 * The hot patches in one frame of RGBA bytes: pixels whose red stands a clear
 * margin above the frame's own background, joined into patches. Positions
 * are fractions of the width and height. Strongest first.
 */
export function hotspots(data, width, height) {
  const background = redPercentile(data, BACKGROUND_PERCENTILE);
  const lo = background + HOT_MARGIN;
  if (lo >= 255) return [];
  const n = width * height;
  const hot = new Uint8Array(n);
  for (let p = 0; p < n; p += 1) {
    const i = p * 4;
    // Red has to lead: sunlit cloud is brighter in the short-wave channels
    // (green, blue) than in red. A fire runs red, then yellow, then white.
    if (data[i] >= lo && data[i] >= data[i + 1] && data[i + 3] !== 0) hot[p] = 1;
  }
  const spots = [];
  const stack = [];
  for (let start = 0; start < n; start += 1) {
    if (!hot[start]) continue;
    hot[start] = 0;
    stack.push(start);
    let count = 0; let sx = 0; let sy = 0; let peak = 0; let px = 0; let py = 0;
    while (stack.length) {
      const p = stack.pop();
      const x = p % width;
      const y = (p - x) / width;
      const red = data[p * 4];
      count += 1; sx += x; sy += y;
      if (red > peak) { peak = red; px = x; py = y; }
      for (const q of [p - 1, p + 1, p - width, p + width]) {
        if (q < 0 || q >= n || !hot[q]) continue;
        if ((q === p - 1 || q === p + 1) && Math.abs((q % width) - x) !== 1) continue;
        hot[q] = 0;
        stack.push(q);
      }
    }
    spots.push({
      x: (px + 0.5) / width,
      y: (py + 0.5) / height,
      cx: (sx / count + 0.5) / width,
      cy: (sy / count + 0.5) / height,
      pixels: count,
      peak,
      strength: Math.min(1, (peak - lo) / HOT_SPAN),
    });
  }
  // A frame that is hot everywhere is a warm surface, not fires.
  if (spots.reduce((s, x) => s + x.pixels, 0) > n * 0.2) return [];
  spots.sort((a, b) => b.peak - a.peak || b.pixels - a.pixels);
  return spots.slice(0, MOST_SPOTS);
}

/** Where a spot is on the ground, for a north-up Web Mercator picture of `box`. */
export function pointAt(spot, { west, south, east, north }) {
  const merc = (lat) => Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360));
  const unmerc = (y) => (360 / Math.PI) * Math.atan(Math.exp(y)) - 90;
  const top = merc(north);
  const bottom = merc(south);
  return {
    lon: west + (east - west) * spot.x,
    lat: unmerc(top - (top - bottom) * spot.y),
  };
}

/** The detection that confirms heat at `point` at `time`, or null. */
export function confirming(point, fires, time) {
  const t = Date.parse(String(time ?? '').split('/')[0]);
  let best = null;
  let bestKm = Infinity;
  for (const f of fires ?? []) {
    const seen = Date.parse(f.acquired);
    if (!(seen <= t + HOUR && seen >= t - MATCH_HOURS * HOUR)) continue;
    const km = kmBetween(point, f);
    if (km <= MATCH_KM && km < bestKm) { best = f; bestKm = km; }
  }
  return best;
}

/** "87 MW", "1.2 GW", "under 1 MW". */
export function mwSaid(mw) {
  if (mw == null || !Number.isFinite(mw)) return 'power not measured';
  if (mw >= 1000) return `${(mw / 1000).toFixed(1)} GW`;
  if (mw < 1) return 'under 1 MW';
  return `${mw >= 100 ? Math.round(mw) : Math.round(mw * 10) / 10} MW`;
}

/** A word for how big a fire is, from its fire radiative power. */
export function sizeWord(mw) {
  if (!(mw > 0)) return 'Fire';
  if (mw >= 500) return 'Very large fire';
  if (mw >= 100) return 'Large fire';
  if (mw >= 20) return 'Fire';
  return 'Small fire';
}

/** "6 Nov 01:12 UTC". */
export function whenSaid(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const day = d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' });
  return `${day} ${d.toISOString().slice(11, 16)} UTC`;
}

const SENSOR_SHORT = { 'viirs-snpp': 'VIIRS', 'viirs-noaa20': 'VIIRS', modis: 'MODIS' };

/** One incident in a line, and the line under it. */
export function incidentSaid(inc) {
  const sensors = [...new Set(inc.sensors.map((s) => SENSOR_SHORT[s] ?? s))].join(' + ');
  const span = inc.first === inc.last
    ? whenSaid(inc.first)
    : `${whenSaid(inc.first)} → ${whenSaid(inc.last)}`;
  return {
    head: `${sizeWord(inc.peakMw)} · ${mwSaid(inc.peakMw)} peak`,
    sub: `${inc.count} detection${inc.count === 1 ? '' : 's'} · ${sensors} · `
      + `${inc.confidence} confidence · ${span}`,
  };
}

/** The colour a fire is drawn in, by its megawatts: red, orange, yellow, white. */
export function fireColour(mw) {
  if (mw >= 100) return '#fff3b0';
  if (mw >= 30) return '#ffb703';
  if (mw >= 10) return '#fb8500';
  return '#e5383b';
}

/** Where a point falls in a north-up Web Mercator picture of `box`, as fractions. */
export function toPicture({ lat, lon }, { west, south, east, north }) {
  const merc = (v) => Math.log(Math.tan(Math.PI / 4 + (v * Math.PI) / 360));
  return {
    x: (lon - west) / (east - west),
    y: (merc(north) - merc(lat)) / (merc(north) - merc(south)),
  };
}

/**
 * What to draw for one moment: the detections that are evidence about it,
 * and the frame's own hot patches, each marked confirmed or not.
 */
export function marksFor(fires, spots, time, box) {
  const detections = activeAt(fires, time);
  const shown = new Set(detections);
  const heat = (spots ?? []).map((spot) => {
    const at = pointAt(spot, box);
    const match = confirming(at, fires, time);
    return { ...spot, ...at, match };
  });
  // A confirmed patch whose detection has aged off the frames is still
  // confirmed: draw that detection too, so the megawatts stay on it.
  for (const h of heat) if (h.match && !shown.has(h.match)) { detections.push(h.match); shown.add(h.match); }
  return { detections, unconfirmed: heat.filter((h) => !h.match) };
}

// ── Drawing, on a Leaflet map (needs the page) ─────────────────

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function detectionPopup(f) {
  const sensor = SENSOR_SHORT[f.sensor] ?? f.sensor;
  return `<div class="fire-popup"><div class="fire-popup-head">🔥 Detected fire — ${esc(mwSaid(f.frp))}</div>`
    + `<div>${esc(whenSaid(f.acquired))} · ${esc(sensor)} · ${esc(f.confidence_label)} confidence</div>`
    + `<div class="dim">Fire radiative power${f.brightness_k ? ` · ${Math.round(f.brightness_k)} K` : ''}`
    + ` · ${f.resolution_m ?? ''} m pixel · NASA FIRMS</div>`
    + `<div class="dim">${f.lat.toFixed(4)}, ${f.lon.toFixed(4)}</div></div>`;
}

const UNCONFIRMED = '<div class="fire-popup"><div class="fire-popup-head">Unconfirmed heat</div>'
  + '<div>A hot patch in this frame with no satellite fire detection within '
  + `${MATCH_KM} km in the ${MATCH_HOURS} hours before it.</div>`
  + '<div class="dim">Could be a small fire the polar orbiters have not passed over yet, '
  + 'a sun-warmed surface or a cloud edge. Not a fire until a thermal detection agrees.</div></div>';

/** Draw one moment's marks into a Leaflet layer group, replacing what was there. */
export function paintMarks(group, { detections, unconfirmed }, popup = {}) {
  group.clearLayers();
  for (const h of unconfirmed) {
    L.circleMarker([h.lat, h.lon], {
      radius: 9 + 6 * h.strength, color: '#ffd166', weight: 1.5, dashArray: '3 3',
      fill: false, pane: group.options.pane,
    }).bindPopup(UNCONFIRMED, popup).addTo(group);
    L.marker([h.lat, h.lon], {
      pane: group.options.pane, interactive: false,
      icon: L.divIcon({ className: 'heat-tag', html: '? heat', iconSize: null, iconAnchor: [-12, 8] }),
    }).addTo(group);
  }
  for (const f of detections) {
    const mw = Math.max(f.frp ?? 0, 0);
    L.circleMarker([f.lat, f.lon], {
      radius: 6 + 10 * Math.min(1, Math.sqrt(mw / 150)), color: '#ffffff', weight: 1.5,
      fillColor: fireColour(mw), fillOpacity: 0.85, pane: group.options.pane,
    }).bindPopup(detectionPopup(f), popup).addTo(group);
    L.marker([f.lat, f.lon], {
      pane: group.options.pane, interactive: false,
      icon: L.divIcon({ className: 'fire-tag', html: `🔥 ${esc(mwSaid(f.frp))}`,
        iconSize: null, iconAnchor: [-14, 9] }),
    }).addTo(group);
  }
}

/** The same marks drawn onto a picture of `box`, for the saved video. */
export function drawMarksOn(ctx, width, height, { detections, unconfirmed }, box) {
  ctx.save();
  ctx.font = '600 13px system-ui, sans-serif';
  ctx.textBaseline = 'middle';
  const label = (text, x, y, colour) => {
    const w = ctx.measureText(text).width + 10;
    ctx.fillStyle = 'rgba(10,12,16,.82)';
    ctx.fillRect(x, y - 10, w, 20);
    ctx.fillStyle = colour;
    ctx.fillText(text, x + 5, y);
  };
  for (const h of unconfirmed) {
    const { x, y } = toPicture(h, box);
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = '#ffd166';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(x * width, y * height, 10 + 6 * h.strength, 0, Math.PI * 2);
    ctx.stroke();
    ctx.setLineDash([]);
    label('? heat', x * width + 14, y * height, '#ffd166');
  }
  for (const f of detections) {
    const { x, y } = toPicture(f, box);
    const mw = Math.max(f.frp ?? 0, 0);
    ctx.fillStyle = fireColour(mw);
    ctx.strokeStyle = '#ffffff';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(x * width, y * height, 6 + 10 * Math.min(1, Math.sqrt(mw / 150)), 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    label(`🔥 ${mwSaid(f.frp)}`, x * width + 16, y * height, '#ffe08a');
  }
  ctx.restore();
}
