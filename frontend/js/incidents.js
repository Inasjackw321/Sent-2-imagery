// Detected fires on the live Fire Temperature picture, with their megawatts.
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
// confidence. Those near the time of the frame on screen are drawn over it.

// How long a detection stays on the frames after it was made. The polar
// orbiters pass a few times a day, so a fire seen at 01:00 is still the best
// evidence about that place at 05:00; a day later it is not.
export const HOLD_HOURS = 6;

const HOUR = 3600e3;

/** The detections that are evidence about the moment `time`. */
export function activeAt(fires, time, holdHours = HOLD_HOURS) {
  const t = Date.parse(String(time ?? '').split('/')[0]);
  if (!Number.isFinite(t)) return [];
  return (fires ?? []).filter((f) => {
    const seen = Date.parse(f.acquired);
    return seen <= t + 0.25 * HOUR && seen >= t - holdHours * HOUR;
  });
}

/** "87 MW", "1.2 GW", "under 1 MW". */
export function mwSaid(mw) {
  if (mw == null || !Number.isFinite(mw)) return 'power not measured';
  if (mw >= 1000) return `${(mw / 1000).toFixed(1)} GW`;
  if (mw < 1) return 'under 1 MW';
  return `${mw >= 100 ? Math.round(mw) : Math.round(mw * 10) / 10} MW`;
}

/** "6 Nov 01:12 UTC". */
export function whenSaid(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const day = d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' });
  return `${day} ${d.toISOString().slice(11, 16)} UTC`;
}

const SENSOR_SHORT = { 'viirs-snpp': 'VIIRS', 'viirs-noaa20': 'VIIRS', modis: 'MODIS' };

/** The colour a fire is drawn in, by its megawatts: red, orange, yellow, white. */
export function fireColour(mw) {
  if (mw >= 100) return '#fff3b0';
  if (mw >= 30) return '#ffb703';
  if (mw >= 10) return '#fb8500';
  return '#e5383b';
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

/** Draw the detections into a Leaflet layer group, replacing what was there. */
export function paintMarks(group, detections, popup = {}) {
  group.clearLayers();
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
