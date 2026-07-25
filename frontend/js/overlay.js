// Cartographic furniture drawn onto a canvas: scale bar, north arrow, legend,
// stats, graticule, titles, annotations. Shared by the timelapse and graphic tabs.

import { fmt } from './ui.js';
import { editState } from './editor.js';

export const THEMES = {
  dark: {
    bg: '#0d1015', panel: 'rgba(13,16,21,.82)', line: 'rgba(255,255,255,.16)',
    ink: '#f2f6fb', dim: '#9fb0c6', accent: '#4cc2ff', shadow: 'rgba(0,0,0,.65)',
  },
  light: {
    bg: '#f4f6f9', panel: 'rgba(255,255,255,.9)', line: 'rgba(0,0,0,.16)',
    ink: '#131922', dim: '#5a6a80', accent: '#0b78c4', shadow: 'rgba(0,0,0,.22)',
  },
  paper: {
    bg: '#f3efe4', panel: 'rgba(250,247,239,.92)', line: 'rgba(60,50,35,.22)',
    ink: '#221c12', dim: '#6b5c46', accent: '#a8621b', shadow: 'rgba(80,60,30,.24)',
  },
  none: {
    bg: 'rgba(0,0,0,0)', panel: 'rgba(13,16,21,.72)', line: 'rgba(255,255,255,.2)',
    ink: '#ffffff', dim: '#d5dde8', accent: '#4cc2ff', shadow: 'rgba(0,0,0,.7)',
  },
};

const FONT = '"Segoe UI", system-ui, -apple-system, Roboto, sans-serif';
export const font = (size, weight = 400) => `${weight} ${size}px ${FONT}`;

/** Panel with a soft background, used behind every furniture element. */
export function plate(ctx, x, y, w, h, theme, radius = 8) {
  ctx.save();
  ctx.fillStyle = theme.panel;
  ctx.strokeStyle = theme.line;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.roundRect(x, y, w, h, radius);
  ctx.fill();
  ctx.stroke();
  ctx.restore();
}

export function shadowText(ctx, text, x, y, { size = 14, weight = 500, colour = '#fff',
  align = 'left', baseline = 'alphabetic', shadow = 'rgba(0,0,0,.7)' } = {}) {
  ctx.save();
  ctx.font = font(size, weight);
  ctx.textAlign = align;
  ctx.textBaseline = baseline;
  if (shadow) {
    ctx.shadowColor = shadow;
    ctx.shadowBlur = size * 0.5;
    ctx.shadowOffsetY = 1;
  }
  ctx.fillStyle = colour;
  ctx.fillText(text, x, y);
  ctx.restore();
}

// ── Geography of a (possibly rotated / cropped) capture ────────

const MERC_R = 6378137.0;
const toLon = (x) => (x / MERC_R) * (180 / Math.PI);
const toLat = (y) => (2 * Math.atan(Math.exp(y / MERC_R)) - Math.PI / 2) * (180 / Math.PI);

/**
 * Geographic bounds of what is actually visible after rotate / flip / crop.
 * Exact for the 90° rotations the editor offers.
 *
 * `inset` narrows the result to a centred sub-rectangle, given as fractions of
 * the image — that is how a cover-fitted panel reports the part it really shows.
 */
export function visibleBounds(capture, inset = null) {
  const grid = capture?.meta?.grid;
  if (!grid) return null;
  const state = editState(capture) ?? { rotate: 0, flipH: false, flipV: false, crop: null };
  const srcW = capture.image.naturalWidth;
  const srcH = capture.image.naturalHeight;

  const quarter = ((state.rotate % 360) + 360) % 360;
  const swap = quarter === 90 || quarter === 270;
  const outW = swap ? srcH : srcW;
  const outH = swap ? srcW : srcH;

  const m = new DOMMatrix()
    .translate(outW / 2, outH / 2)
    .rotate(quarter)
    .scale(state.flipH ? -1 : 1, state.flipV ? -1 : 1)
    .translate(-srcW / 2, -srcH / 2);
  const inv = m.inverse();

  const crop = state.crop ?? { x: 0, y: 0, w: outW, h: outH };
  const fx = Math.min(inset?.fx ?? 1, 1);
  const fy = Math.min(inset?.fy ?? 1, 1);
  const vw = crop.w * fx;
  const vh = crop.h * fy;
  const vx = crop.x + (crop.w - vw) / 2;
  const vy = crop.y + (crop.h - vh) / 2;

  const corners = [
    [vx, vy], [vx + vw, vy], [vx + vw, vy + vh], [vx, vy + vh],
  ].map(([x, y]) => {
    const p = inv.transformPoint(new DOMPoint(x, y));
    const [x0, y0, x1, y1] = grid.bounds3857;
    return [
      x0 + (p.x / srcW) * (x1 - x0),
      y1 - (p.y / srcH) * (y1 - y0),
    ];
  });

  const lons = corners.map((c) => toLon(c[0]));
  const lats = corners.map((c) => toLat(c[1]));
  return {
    west: Math.min(...lons), east: Math.max(...lons),
    south: Math.min(...lats), north: Math.max(...lats),
    centre: [(Math.min(...lons) + Math.max(...lons)) / 2,
             (Math.min(...lats) + Math.max(...lats)) / 2],
  };
}

/** Which way is north on screen, after the editor's rotations and flips. */
export function northAngle(capture) {
  const state = editState(capture);
  if (!state) return 0;
  const quarter = ((state.rotate % 360) + 360) % 360;
  const m = new DOMMatrix().rotate(quarter).scale(state.flipH ? -1 : 1, state.flipV ? -1 : 1);
  const p = m.transformPoint(new DOMPoint(0, -1));
  return Math.atan2(p.x, -p.y) * (180 / Math.PI);
}

/** Ground metres per drawn pixel, accounting for any resampling on the way in. */
export function metresPerPixel(capture, drawnWidth, sourceWidth) {
  const res = capture?.meta?.grid?.ground_res_m;
  if (!res) return null;
  return res * (sourceWidth / drawnWidth);
}

// ── Furniture ──────────────────────────────────────────────────

const NICE = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500];

export function drawScaleBar(ctx, x, y, maxWidth, mpp, theme, s = 1) {
  if (!mpp || !Number.isFinite(mpp)) return 0;
  const targetM = mpp * maxWidth;
  const magnitude = 10 ** Math.floor(Math.log10(targetM));
  let best = magnitude;
  for (const n of NICE) {
    const candidate = n * magnitude / 10;
    if (candidate <= targetM) best = candidate;
  }
  const barPx = best / mpp;
  const h = 7 * s;

  ctx.save();
  const label = fmt.distance(best);
  const boxW = Math.max(barPx, ctx.measureText(label).width) + 20 * s;
  plate(ctx, x - 8 * s, y - 22 * s, boxW, 40 * s, theme, 7 * s);

  // Alternating black/white segments, the classic map convention.
  const segments = 4;
  for (let i = 0; i < segments; i++) {
    ctx.fillStyle = i % 2 ? theme.ink : theme.bg === 'rgba(0,0,0,0)' ? '#000' : theme.bg;
    ctx.fillRect(x + (i * barPx) / segments, y, barPx / segments, h);
  }
  ctx.strokeStyle = theme.ink;
  ctx.lineWidth = Math.max(1, s);
  ctx.strokeRect(x, y, barPx, h);

  shadowText(ctx, '0', x, y - 5 * s, { size: 10 * s, colour: theme.dim, shadow: null });
  shadowText(ctx, label, x + barPx, y - 5 * s,
    { size: 10 * s, colour: theme.ink, align: 'right', weight: 600, shadow: null });
  ctx.restore();
  return barPx;
}

export function drawNorthArrow(ctx, cx, cy, size, theme, angle = 0) {
  ctx.save();
  ctx.translate(cx, cy);
  ctx.rotate((angle * Math.PI) / 180);
  ctx.beginPath();
  ctx.moveTo(0, -size);
  ctx.lineTo(size * 0.42, size * 0.62);
  ctx.lineTo(0, size * 0.26);
  ctx.closePath();
  ctx.fillStyle = theme.ink;
  ctx.fill();
  ctx.beginPath();
  ctx.moveTo(0, -size);
  ctx.lineTo(-size * 0.42, size * 0.62);
  ctx.lineTo(0, size * 0.26);
  ctx.closePath();
  ctx.fillStyle = theme.dim;
  ctx.fill();
  ctx.restore();
  shadowText(ctx, 'N', cx, cy - size * 1.25, {
    size: size * 0.72, weight: 700, colour: theme.ink, align: 'center', shadow: theme.shadow,
  });
}

export function drawLegend(ctx, legend, x, y, w, theme, s = 1) {
  if (!legend) return 0;
  const barH = 11 * s;
  const h = 46 * s;
  plate(ctx, x, y, w, h, theme, 8 * s);
  shadowText(ctx, legend.label, x + 10 * s, y + 15 * s,
    { size: 11 * s, weight: 600, colour: theme.ink, shadow: null });

  const gradient = ctx.createLinearGradient(x + 10 * s, 0, x + w - 10 * s, 0);
  for (const stop of legend.stops) gradient.addColorStop(stop.pos, stop.color);
  ctx.fillStyle = gradient;
  ctx.fillRect(x + 10 * s, y + 21 * s, w - 20 * s, barH);
  ctx.strokeStyle = theme.line;
  ctx.lineWidth = 1;
  ctx.strokeRect(x + 10 * s, y + 21 * s, w - 20 * s, barH);

  shadowText(ctx, legend.vmin.toFixed(2), x + 10 * s, y + 41 * s,
    { size: 10 * s, colour: theme.dim, shadow: null });
  shadowText(ctx, legend.vmax.toFixed(2), x + w - 10 * s, y + 41 * s,
    { size: 10 * s, colour: theme.dim, align: 'right', shadow: null });
  return h;
}

export function drawClassLegend(ctx, classes, x, y, w, theme, s = 1) {
  const rowH = 17 * s;
  const h = 12 * s + classes.length * rowH;
  plate(ctx, x, y, w, h, theme, 8 * s);
  classes.forEach((cls, i) => {
    const cy = y + 8 * s + i * rowH;
    ctx.fillStyle = cls.color;
    ctx.fillRect(x + 10 * s, cy + 3 * s, 11 * s, 11 * s);
    shadowText(ctx, cls.label, x + 27 * s, cy + 12 * s,
      { size: 11 * s, colour: theme.ink, shadow: null });
    shadowText(ctx, `${cls.percent}%`, x + w - 10 * s, cy + 12 * s,
      { size: 11 * s, colour: theme.dim, align: 'right', shadow: null });
  });
  return h;
}

export function drawStats(ctx, rows, x, y, w, theme, s = 1, title = 'Statistics') {
  const rowH = 17 * s;
  const h = 30 * s + rows.length * rowH;
  plate(ctx, x, y, w, h, theme, 8 * s);
  shadowText(ctx, title, x + 10 * s, y + 17 * s,
    { size: 11 * s, weight: 700, colour: theme.ink, shadow: null });
  rows.forEach(([label, value], i) => {
    const cy = y + 34 * s + i * rowH;
    shadowText(ctx, label, x + 10 * s, cy, { size: 11 * s, colour: theme.dim, shadow: null });
    shadowText(ctx, String(value), x + w - 10 * s, cy,
      { size: 11 * s, weight: 600, colour: theme.ink, align: 'right', shadow: null });
  });
  return h;
}

export function drawHistogram(ctx, histogram, x, y, w, h, theme, s = 1, accent) {
  if (!histogram?.counts?.length) return 0;
  plate(ctx, x, y, w, h, theme, 8 * s);
  const counts = histogram.counts;
  const max = Math.max(...counts) || 1;
  const innerX = x + 10 * s;
  const innerW = w - 20 * s;
  const innerY = y + 20 * s;
  const innerH = h - 34 * s;
  const barW = innerW / counts.length;

  ctx.save();
  ctx.fillStyle = accent ?? theme.accent;
  counts.forEach((c, i) => {
    const barH = (c / max) * innerH;
    ctx.fillRect(innerX + i * barW, innerY + innerH - barH, Math.max(barW - 1, 1), barH);
  });
  ctx.restore();

  shadowText(ctx, 'Distribution', innerX, y + 14 * s,
    { size: 10 * s, weight: 600, colour: theme.dim, shadow: null });
  shadowText(ctx, histogram.bins[0].toFixed(2), innerX, y + h - 6 * s,
    { size: 9 * s, colour: theme.dim, shadow: null });
  shadowText(ctx, (histogram.bins.at(-1) + histogram.width).toFixed(2), innerX + innerW, y + h - 6 * s,
    { size: 9 * s, colour: theme.dim, align: 'right', shadow: null });
  return h;
}

export function drawGraticule(ctx, x, y, w, h, bounds, theme, s = 1, divisions = 4) {
  if (!bounds) return;
  ctx.save();
  ctx.strokeStyle = theme.line;
  ctx.lineWidth = Math.max(1, s * 0.75);
  ctx.setLineDash([4 * s, 5 * s]);
  for (let i = 1; i < divisions; i++) {
    const gx = x + (w * i) / divisions;
    const gy = y + (h * i) / divisions;
    ctx.beginPath(); ctx.moveTo(gx, y); ctx.lineTo(gx, y + h); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(x, gy); ctx.lineTo(x + w, gy); ctx.stroke();
  }
  ctx.restore();

  for (let i = 1; i < divisions; i++) {
    const lon = bounds.west + ((bounds.east - bounds.west) * i) / divisions;
    const lat = bounds.north - ((bounds.north - bounds.south) * i) / divisions;
    shadowText(ctx, `${Math.abs(lon).toFixed(3)}°${lon >= 0 ? 'E' : 'W'}`,
      x + (w * i) / divisions, y + h - 5 * s,
      { size: 9.5 * s, colour: theme.dim, align: 'center', shadow: theme.shadow });
    shadowText(ctx, `${Math.abs(lat).toFixed(3)}°${lat >= 0 ? 'N' : 'S'}`,
      x + 5 * s, y + (h * i) / divisions,
      { size: 9.5 * s, colour: theme.dim, baseline: 'middle', shadow: theme.shadow });
  }
}

/**
 * Where on Earth the area sits, as a labelled lat/lon diagram. Deliberately a
 * graticule rather than a coastline: no basemap is embedded, so drawing a
 * vague landmass would be inventing geography.
 */
export function drawLocator(ctx, x, y, w, h, centre, theme, s = 1) {
  plate(ctx, x, y, w, h, theme, 8 * s);
  shadowText(ctx, 'Position on Earth', x + 10 * s, y + 14 * s,
    { size: 10 * s, weight: 600, colour: theme.dim, shadow: null });

  const mapX = x + 10 * s;
  const mapY = y + 20 * s;
  const mapW = w - 20 * s;
  const mapH = h - 30 * s;
  const lonToX = (lon) => mapX + ((lon + 180) / 360) * mapW;
  const latToY = (lat) => mapY + ((90 - lat) / 180) * mapH;

  ctx.save();
  ctx.beginPath();
  ctx.rect(mapX, mapY, mapW, mapH);
  ctx.clip();

  ctx.strokeStyle = theme.line;
  ctx.lineWidth = Math.max(1, s * 0.6);
  for (let lon = -150; lon <= 150; lon += 30) {
    ctx.beginPath();
    ctx.moveTo(lonToX(lon), mapY);
    ctx.lineTo(lonToX(lon), mapY + mapH);
    ctx.stroke();
  }
  for (let lat = -60; lat <= 60; lat += 30) {
    ctx.beginPath();
    ctx.moveTo(mapX, latToY(lat));
    ctx.lineTo(mapX + mapW, latToY(lat));
    ctx.stroke();
  }

  // Equator and prime meridian, so the marker can be read against something.
  ctx.strokeStyle = theme.dim;
  ctx.lineWidth = Math.max(1, s * 0.9);
  ctx.beginPath();
  ctx.moveTo(mapX, latToY(0));
  ctx.lineTo(mapX + mapW, latToY(0));
  ctx.moveTo(lonToX(0), mapY);
  ctx.lineTo(lonToX(0), mapY + mapH);
  ctx.stroke();

  const px = lonToX(centre[0]);
  const py = latToY(centre[1]);
  ctx.strokeStyle = theme.accent;
  ctx.lineWidth = Math.max(1, s * 0.8);
  ctx.setLineDash([3 * s, 3 * s]);
  ctx.beginPath();
  ctx.moveTo(mapX, py); ctx.lineTo(mapX + mapW, py);
  ctx.moveTo(px, mapY); ctx.lineTo(px, mapY + mapH);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = theme.accent;
  ctx.beginPath();
  ctx.arc(px, py, 3.5 * s, 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();

  ctx.save();
  ctx.strokeStyle = theme.line;
  ctx.lineWidth = 1;
  ctx.strokeRect(mapX, mapY, mapW, mapH);
  ctx.restore();

  shadowText(ctx, '180°W', mapX, mapY + mapH + 9 * s,
    { size: 8.5 * s, colour: theme.dim, shadow: null });
  shadowText(ctx, '0°', lonToX(0), mapY + mapH + 9 * s,
    { size: 8.5 * s, colour: theme.dim, align: 'center', shadow: null });
  shadowText(ctx, '180°E', mapX + mapW, mapY + mapH + 9 * s,
    { size: 8.5 * s, colour: theme.dim, align: 'right', shadow: null });

  shadowText(ctx, fmt.coord(centre[0], centre[1]), x + w / 2, y + h + 13 * s,
    { size: 10 * s, weight: 600, colour: theme.ink, align: 'center', shadow: null });
}

/** Progress strip for animations. */
export function drawTimelineBar(ctx, x, y, w, progress, theme, s = 1) {
  ctx.save();
  ctx.fillStyle = theme.line;
  ctx.fillRect(x, y, w, 3 * s);
  ctx.fillStyle = theme.accent;
  ctx.fillRect(x, y, w * Math.max(0, Math.min(1, progress)), 3 * s);
  ctx.restore();
}

export function drawWatermark(ctx, w, h, s = 1, text = 'SYNTHETIC DEMO DATA — NOT REAL IMAGERY') {
  ctx.save();
  ctx.translate(w / 2, h / 2);
  ctx.rotate(-Math.atan2(h, w));
  ctx.font = font(Math.max(13 * s, w * 0.028), 800);
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillStyle = 'rgba(255,180,84,.22)';
  ctx.strokeStyle = 'rgba(255,180,84,.32)';
  ctx.lineWidth = 1.5 * s;
  ctx.fillText(text, 0, 0);
  ctx.strokeText(text, 0, 0);
  ctx.restore();
}

// ── Annotations ────────────────────────────────────────────────

/** Annotations are stored in 0-1 space so they survive any export size. */
export function drawAnnotations(ctx, annotations, w, h, s = 1) {
  for (const a of annotations) {
    const x1 = a.x * w;
    const y1 = a.y * h;
    const x2 = (a.x2 ?? a.x) * w;
    const y2 = (a.y2 ?? a.y) * h;
    const lw = a.size * s * 1.2;

    ctx.save();
    ctx.strokeStyle = a.color;
    ctx.fillStyle = a.color;
    ctx.lineWidth = lw;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.shadowColor = 'rgba(0,0,0,.55)';
    ctx.shadowBlur = lw * 1.6;

    if (a.type === 'rect') {
      ctx.strokeRect(Math.min(x1, x2), Math.min(y1, y2), Math.abs(x2 - x1), Math.abs(y2 - y1));
    } else if (a.type === 'ellipse') {
      ctx.beginPath();
      ctx.ellipse((x1 + x2) / 2, (y1 + y2) / 2,
        Math.abs(x2 - x1) / 2, Math.abs(y2 - y1) / 2, 0, 0, Math.PI * 2);
      ctx.stroke();
    } else if (a.type === 'line' || a.type === 'arrow') {
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();
      if (a.type === 'arrow') {
        const angle = Math.atan2(y2 - y1, x2 - x1);
        const head = Math.max(lw * 3.2, 9 * s);
        ctx.beginPath();
        ctx.moveTo(x2, y2);
        ctx.lineTo(x2 - head * Math.cos(angle - 0.42), y2 - head * Math.sin(angle - 0.42));
        ctx.lineTo(x2 - head * Math.cos(angle + 0.42), y2 - head * Math.sin(angle + 0.42));
        ctx.closePath();
        ctx.fill();
      }
    } else if (a.type === 'text') {
      const size = Math.max(a.size * 5 * s, 11 * s);
      ctx.font = font(size, 650);
      ctx.textBaseline = 'middle';
      const width = ctx.measureText(a.text).width;
      ctx.shadowBlur = 0;
      ctx.fillStyle = 'rgba(10,13,18,.62)';
      ctx.beginPath();
      ctx.roundRect(x1 - 7 * s, y1 - size * 0.78, width + 14 * s, size * 1.56, 5 * s);
      ctx.fill();
      ctx.fillStyle = a.color;
      ctx.fillText(a.text, x1, y1);
    }
    ctx.restore();
  }
}
