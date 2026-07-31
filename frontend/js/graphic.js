// Graphic tab: compose an annotated, publication-style figure from one or more captures.

import { store, on, getCapture, isDemo } from './store.js';
import { editedCanvas, slug } from './editor.js';
import * as chrome from './overlay.js';
import { $, $$, el, toast, download, fmt, debounce } from './ui.js';

const BASE_W = 1180;

const LAYOUTS = {
  single: { label: 'Single', glyph: '▭', panels: 1 },
  side_by_side: { label: 'Side by side', glyph: '▯▯', panels: 2 },
  stacked: { label: 'Stacked', glyph: '⬓', panels: 2 },
  grid: { label: 'Grid of four', glyph: '⊞', panels: 4 },
  swipe: { label: 'Before / after swipe', glyph: '◧', panels: 2 },
};

let layout = 'single';
let canvas;
let ctx;
let annotations = [];
let annotMode = 'none';
let drag = null;
const redraw = debounce(() => draw(), 30);

export function initGraphic() {
  canvas = $('#graphicCanvas');
  ctx = canvas.getContext('2d');

  const picker = $('#layoutPicker');
  for (const [key, spec] of Object.entries(LAYOUTS)) {
    picker.append(el('button', {
      class: `layout-opt ${key === layout ? 'is-active' : ''}`,
      dataset: { layout: key },
      onclick: () => {
        layout = key;
        $$('#layoutPicker .layout-opt').forEach((b) =>
          b.classList.toggle('is-active', b.dataset.layout === key));
        syncPanelSelect();
        draw();
      },
    }, el('span', {}, spec.glyph), spec.label));
  }

  for (const id of ['#gTitle', '#gSubtitle', '#gCaption', '#gCredit']) {
    $(id).addEventListener('input', redraw);
  }
  for (const id of ['#gTheme', '#gPanels', '#gScale', '#gNorth', '#gCoords', '#gLegend',
    '#gStats', '#gHisto', '#gDates', '#gGrid', '#gInset']) {
    $(id).addEventListener('change', () => draw());
  }

  $$('#annotTools .tool').forEach((btn) => btn.addEventListener('click', () => {
    $$('#annotTools .tool').forEach((b) => b.classList.toggle('is-active', b === btn));
    annotMode = btn.dataset.annot;
    canvas.classList.toggle('is-drawing', annotMode !== 'none');
  }));
  $('#annotUndo').addEventListener('click', () => { annotations.pop(); draw(); });
  $('#annotClear').addEventListener('click', () => { annotations = []; draw(); });

  canvas.addEventListener('pointerdown', onPointerDown);
  canvas.addEventListener('pointermove', onPointerMove);
  window.addEventListener('pointerup', onPointerUp);

  $('#gExport').addEventListener('click', exportGraphic);

  on('captures', syncPanelSelect);
  on('activeCapture', () => { syncPanelSelect(); draw(); });
  on('edited', redraw);
}

// ── Panel selection ────────────────────────────────────────────

function syncPanelSelect() {
  const select = $('#gPanels');
  const chosen = new Set(selectedIds());
  select.innerHTML = '';
  for (const capture of store.captures) {
    select.append(el('option', {
      value: capture.id,
      selected: chosen.has(capture.id) || (!chosen.size && capture.id === store.activeCaptureId),
    }, capture.name));
  }
  if (!store.captures.length) select.append(el('option', { value: '' }, 'Nothing captured yet'));
}

const selectedIds = () => [...$('#gPanels').selectedOptions].map((o) => o.value).filter(Boolean);

function panelCaptures() {
  const wanted = LAYOUTS[layout].panels;
  let ids = selectedIds();
  if (!ids.length && store.activeCaptureId) ids = [store.activeCaptureId];
  // Top up from the most recent captures so a layout is never half empty.
  for (const capture of store.captures) {
    if (ids.length >= wanted) break;
    if (!ids.includes(capture.id)) ids.push(capture.id);
  }
  return ids.slice(0, wanted).map(getCapture).filter(Boolean);
}

// ── Drawing ────────────────────────────────────────────────────

function wrapText(g, text, maxWidth) {
  const lines = [];
  for (const paragraph of text.split('\n')) {
    let line = '';
    for (const word of paragraph.split(/\s+/)) {
      const candidate = line ? `${line} ${word}` : word;
      if (g.measureText(candidate).width > maxWidth && line) {
        lines.push(line);
        line = word;
      } else {
        line = candidate;
      }
    }
    lines.push(line);
  }
  return lines;
}

function statRows(capture) {
  const meta = capture.meta;
  const rows = [];
  if (meta.mode === 'change') {
    rows.push(['Period', `${fmt.date(meta.scene_a.date)} → ${fmt.date(meta.scene_b.date)}`]);
    rows.push(['Interval', `${meta.days_apart} days`]);
    for (const cls of meta.classes ?? []) {
      rows.push([cls.label, `${cls.percent}% · ${fmt.area(cls.area_km2)}`]);
    }
  } else {
    if (meta.source?.platform) rows.push(['Satellite', meta.source.platform]);
    if (meta.scenes?.length > 1) rows.push(['Composite of', `${meta.scenes.length} dates`]);
    if (meta.scene?.date) rows.push(['Acquired', fmt.date(meta.scene.date)]);
    if (meta.scene?.cloud != null) rows.push(['Scene cloud', `${meta.scene.cloud}%`]);
    if (meta.enhancements?.length) rows.push(['Processing', meta.enhancements.join(', ')]);
    if (meta.stats) {
      rows.push(['Mean', meta.stats.mean]);
      rows.push(['Median', meta.stats.median]);
      rows.push(['Range', `${meta.stats.min} to ${meta.stats.max}`]);
    }
  }
  rows.push(['Area', fmt.area(meta.aoi_area_km2)]);
  if (meta.grid) rows.push(['Resolution', `${meta.grid.ground_res_m} m/pixel`]);
  if (meta.cloud_masked_pct) rows.push(['Cloud masked', `${meta.cloud_masked_pct}%`]);
  return rows;
}

function changeClasses(meta) {
  const colours = ['#b4463c', '#8a94a6', '#3f9b52'];
  return (meta.classes ?? []).map((cls, i) => ({ ...cls, color: colours[i] ?? '#888' }));
}

/** Compose the whole figure at the given scale. Returns the canvas. */
export function compose(scale = 1) {
  const captures = panelCaptures();
  const themeName = $('#gTheme').value;
  const theme = chrome.THEMES[themeName];
  const s = scale;
  const W = Math.round(BASE_W * s);
  const pad = 26 * s;
  const gap = 12 * s;

  const title = $('#gTitle').value.trim();
  const subtitle = $('#gSubtitle').value.trim();
  const caption = $('#gCaption').value.trim();
  const credit = $('#gCredit').value.trim();

  const out = document.createElement('canvas');
  const g = out.getContext('2d');

  // ── Measure ──────────────────────────────────────────────
  let headerH = 0;
  if (title) headerH += 34 * s;
  if (subtitle) headerH += 22 * s;
  if (headerH) headerH += 12 * s;

  const innerW = W - pad * 2;
  const sources = captures.map((c) => editedCanvas(c));
  const panelBoxes = layoutPanels(layout, innerW, gap, sources, s);
  const panelsH = panelBoxes.length
    ? Math.max(...panelBoxes.map((b) => b.y + b.h))
    : 220 * s;

  // The info blocks describe whichever panel actually carries a scale --
  // otherwise a true-colour panel in slot 1 would blank out the legend.
  const primary = captures.find((c) => c.meta?.legend) ?? captures[0];

  // Caption and info cards flow into two balanced columns below the imagery,
  // so a tall stats block never leaves half the figure empty.
  const colW = (innerW - gap) / 2;
  g.font = chrome.font(13 * s, 400);
  const captionLines = caption ? wrapText(g, caption, colW) : [];

  const blocks = [];
  if (captionLines.length) {
    blocks.push({
      h: captionLines.length * 19 * s + 6 * s,
      draw: (x, yy, w) => captionLines.forEach((line, i) =>
        chrome.shadowText(g, line, x, yy + 13 * s + i * 19 * s,
          { size: 13 * s, colour: theme.dim, shadow: null })),
    });
  }
  const legend = primary?.meta?.legend;
  if ($('#gLegend').checked && legend?.type === 'categorical') {
    const classes = legend.classes.slice(0, 8);
    blocks.push({
      h: (12 + classes.length * 17) * s,
      draw: (x, yy, w) => chrome.drawClassLegend(g, classes, x, yy, w, theme, s),
    });
  } else if ($('#gLegend').checked && legend) {
    blocks.push({
      h: 46 * s,
      draw: (x, yy, w) => chrome.drawLegend(g, legend, x, yy, w, theme, s),
    });
  }
  if (primary?.meta?.mode === 'change') {
    const classes = changeClasses(primary.meta);
    blocks.push({
      h: (12 + classes.length * 17) * s,
      draw: (x, yy, w) => chrome.drawClassLegend(g, classes, x, yy, w, theme, s),
    });
  }
  if ($('#gStats').checked && primary) {
    const rows = statRows(primary);
    blocks.push({
      h: (30 + rows.length * 17) * s,
      draw: (x, yy, w) => chrome.drawStats(g, rows, x, yy, w, theme, s),
    });
  }
  if ($('#gHisto').checked && primary?.meta?.histogram?.counts?.length) {
    blocks.push({
      h: 92 * s,
      draw: (x, yy, w) => chrome.drawHistogram(g, primary.meta.histogram, x, yy, w, 92 * s, theme, s),
    });
  }
  if ($('#gInset').checked && primary?.meta?.grid) {
    const bounds = chrome.visibleBounds(primary);
    if (bounds) {
      blocks.push({
        h: 118 * s,
        draw: (x, yy, w) => chrome.drawLocator(g, x, yy, w, 100 * s, bounds.centre, theme, s),
      });
    }
  }

  // Greedy shortest-column packing; run once to measure, once to draw.
  const packed = packColumns(blocks, gap);
  const bodyH = packed.height;
  const footerH = 30 * s;

  out.width = W;
  out.height = Math.round(headerH + panelsH + (bodyH ? bodyH + gap : 0) + footerH + pad * 2);

  // ── Background ───────────────────────────────────────────
  if (themeName !== 'none') {
    g.fillStyle = theme.bg;
    g.fillRect(0, 0, out.width, out.height);
  } else {
    g.clearRect(0, 0, out.width, out.height);
  }

  // ── Header ───────────────────────────────────────────────
  let y = pad;
  if (title) {
    chrome.shadowText(g, title, pad, y + 26 * s,
      { size: 27 * s, weight: 750, colour: theme.ink, shadow: null });
    y += 34 * s;
  }
  if (subtitle) {
    chrome.shadowText(g, subtitle, pad, y + 15 * s,
      { size: 15 * s, weight: 400, colour: theme.dim, shadow: null });
    y += 22 * s;
  }
  if (headerH) y += 12 * s;

  // ── Panels ───────────────────────────────────────────────
  if (!panelBoxes.length) {
    chrome.plate(g, pad, y, innerW, 200 * s, theme, 10 * s);
    chrome.shadowText(g, 'Capture an image first, then pick it under Panels.',
      W / 2, y + 105 * s,
      { size: 14 * s, colour: theme.dim, align: 'center', shadow: null });
  }

  panelBoxes.forEach((box, i) => {
    const capture = captures[i];
    const source = sources[i];
    const px = pad + box.x;
    const py = y + box.y;

    g.save();
    g.beginPath();
    g.roundRect(px, py, box.w, box.h, 8 * s);
    g.clip();
    g.fillStyle = '#000';
    g.fillRect(px, py, box.w, box.h);

    if (layout === 'swipe' && i === 1) {
      // Second image occupies the right half of the same frame.
      g.beginPath();
      g.rect(px + box.w / 2, py, box.w / 2, box.h);
      g.clip();
    }
    drawCover(g, source, px, py, box.w, box.h);
    g.restore();

    if (layout === 'swipe' && i === 1) {
      g.save();
      g.strokeStyle = theme.ink;
      g.lineWidth = 2 * s;
      g.beginPath();
      g.moveTo(px + box.w / 2, py);
      g.lineTo(px + box.w / 2, py + box.h);
      g.stroke();
      g.restore();
    }

    drawPanelFurniture(g, capture, source, px, py, box.w, box.h, theme, s,
      { isSwipe: layout === 'swipe', index: i, total: panelBoxes.length });

    g.save();
    g.strokeStyle = theme.line;
    g.lineWidth = 1;
    g.beginPath();
    g.roundRect(px, py, box.w, box.h, 8 * s);
    g.stroke();
    g.restore();
  });

  y += panelsH + (bodyH ? gap : 0);

  // ── Caption + info cards ─────────────────────────────────
  g.font = chrome.font(13 * s, 400);
  for (const placement of packed.placements) {
    placement.block.draw(pad + placement.column * (colW + gap), y + placement.y, colW);
  }

  y += bodyH;

  // ── Footer ───────────────────────────────────────────────
  const footY = out.height - pad - 4 * s;
  g.save();
  g.strokeStyle = theme.line;
  g.lineWidth = 1;
  g.beginPath();
  g.moveTo(pad, footY - 18 * s);
  g.lineTo(W - pad, footY - 18 * s);
  g.stroke();
  g.restore();

  const attributions = [...new Set(captures
    .map((c) => c.meta?.source?.attribution)
    .filter(Boolean))];
  const source = isDemo(captures)
    ? 'SYNTHETIC DEMO DATA — not real satellite imagery'
    : `${attributions.join(' · ') || 'Free and open satellite imagery'} · processed with Sent-2`;
  chrome.shadowText(g, credit || source, pad, footY,
    { size: 11 * s, colour: isDemo(captures) ? '#ffb454' : theme.dim, shadow: null });
  if (credit) {
    chrome.shadowText(g, source, W - pad, footY,
      { size: 11 * s, colour: isDemo(captures) ? '#ffb454' : theme.dim, align: 'right', shadow: null });
  }

  chrome.drawAnnotations(g, annotations, out.width, out.height, s);
  if (isDemo(captures)) chrome.drawWatermark(g, out.width, out.height, s);

  return out;
}

/** Place blocks into two columns, always filling the shorter one next. */
function packColumns(blocks, gap) {
  const columnY = [0, 0];
  const placements = blocks.map((block) => {
    const column = columnY[0] <= columnY[1] ? 0 : 1;
    const placement = { block, column, y: columnY[column] };
    columnY[column] += block.h + gap;
    return placement;
  });
  const height = Math.max(0, Math.max(...columnY, 0) - gap);
  return { placements, height };
}

/** Panel rectangles for a layout, sized from the images' aspect ratios. */
function layoutPanels(kind, innerW, gap, sources, s) {
  if (!sources.length) return [];
  const aspect = (c) => (c ? c.width / c.height : 1.5);

  if (kind === 'single' || kind === 'swipe' || sources.length === 1) {
    const w = innerW;
    const h = Math.min(w / aspect(sources[0]), 620 * s);
    return kind === 'swipe' && sources.length > 1
      ? [{ x: 0, y: 0, w, h }, { x: 0, y: 0, w, h }]
      : [{ x: 0, y: 0, w, h }];
  }

  if (kind === 'side_by_side') {
    const w = (innerW - gap) / 2;
    const h = Math.min(...sources.slice(0, 2).map((c) => w / aspect(c)), 520 * s);
    return [{ x: 0, y: 0, w, h }, { x: w + gap, y: 0, w, h }];
  }

  if (kind === 'stacked') {
    const h = Math.min(innerW / aspect(sources[0]), 330 * s);
    return [{ x: 0, y: 0, w: innerW, h }, { x: 0, y: h + gap, w: innerW, h }];
  }

  // grid of four
  const w = (innerW - gap) / 2;
  const h = Math.min(...sources.map((c) => w / aspect(c)), 300 * s);
  return sources.slice(0, 4).map((_, i) => ({
    x: (i % 2) * (w + gap), y: Math.floor(i / 2) * (h + gap), w, h,
  }));
}

/** Cover-fit an image into a box (fills the frame, centre-cropped). */
function drawCover(g, source, x, y, w, h) {
  const scale = Math.max(w / source.width, h / source.height);
  const dw = source.width * scale;
  const dh = source.height * scale;
  g.imageSmoothingQuality = 'high';
  g.drawImage(source, x + (w - dw) / 2, y + (h - dh) / 2, dw, dh);
}

function drawPanelFurniture(g, capture, source, x, y, w, h, theme, s, opts) {
  const meta = capture.meta;
  const pad = 12 * s;

  // The panel is cover-fitted, so only a centred part of the image is on show;
  // coordinates and the graticule have to describe that part, not the whole.
  const coverScale = Math.max(w / source.width, h / source.height);
  const inset = {
    fx: w / coverScale / source.width,
    fy: h / coverScale / source.height,
  };
  const bounds = chrome.visibleBounds(capture, inset);

  if ($('#gGrid').checked) {
    chrome.drawGraticule(g, x, y, w, h, $('#gCoords').checked ? bounds : null, theme, s);
  }

  if ($('#gDates').checked) {
    const label = opts.isSwipe
      ? (opts.index === 0 ? `Before · ${fmt.date(meta.scene?.date)}` : `After · ${fmt.date(meta.scene?.date)}`)
      : (meta.mode === 'change'
        ? `${fmt.date(meta.scene_a.date)} → ${fmt.date(meta.scene_b.date)}`
        : `${fmt.date(meta.scene?.date)}${meta.label ? ` · ${meta.label}` : ''}`);
    g.font = chrome.font(12 * s, 650);
    const width = g.measureText(label).width + 18 * s;
    const lx = opts.isSwipe && opts.index === 1 ? x + w - pad - width : x + pad;
    chrome.plate(g, lx, y + pad, width, 24 * s, theme, 6 * s);
    chrome.shadowText(g, label, lx + 9 * s, y + pad + 16 * s,
      { size: 12 * s, weight: 650, colour: theme.ink, shadow: null });
  }

  if ($('#gNorth').checked && opts.index === 0) {
    chrome.drawNorthArrow(g, x + w - pad - 16 * s, y + pad + 30 * s, 15 * s, theme,
      chrome.northAngle(capture));
  }

  if ($('#gScale').checked) {
    const mpp = chrome.metresPerPixel(capture, source.width * coverScale, source.width);
    chrome.drawScaleBar(g, x + pad + 8 * s, y + h - pad - 14 * s,
      Math.min(140 * s, w * 0.32), mpp, theme, s);
  }

  if ($('#gCoords').checked && !$('#gGrid').checked && opts.index === 0 && bounds) {
    chrome.shadowText(g, fmt.coord(bounds.centre[0], bounds.centre[1]),
      x + w - pad, y + h - pad,
      { size: 10.5 * s, colour: theme.dim, align: 'right', shadow: theme.shadow });
  }
}

function draw() {
  const out = compose(1);
  canvas.width = out.width;
  canvas.height = out.height;
  ctx.clearRect(0, 0, out.width, out.height);
  ctx.drawImage(out, 0, 0);

  const foot = $('#graphicFoot');
  foot.innerHTML = '';
  const factor = parseFloat($('#gScaleFactor').value);
  foot.append(
    el('span', { class: 'stat-chip' }, `${out.width} × ${out.height} px preview`),
    el('span', { class: 'stat-chip' },
      `${Math.round(out.width * factor)} × ${Math.round(out.height * factor)} px export`),
    el('span', { class: 'stat-chip' }, `${annotations.length} annotation${annotations.length === 1 ? '' : 's'}`),
  );
}

// ── Annotation interaction ─────────────────────────────────────

function canvasPoint(e) {
  const rect = canvas.getBoundingClientRect();
  return {
    x: (e.clientX - rect.left) / rect.width,
    y: (e.clientY - rect.top) / rect.height,
  };
}

function onPointerDown(e) {
  const p = canvasPoint(e);
  if (annotMode === 'none') {
    // Grab the nearest annotation handle to move it.
    let best = null;
    let bestDist = 0.02;
    for (const a of annotations) {
      const d = Math.hypot(a.x - p.x, a.y - p.y);
      if (d < bestDist) { best = a; bestDist = d; }
    }
    if (best) drag = { move: best, from: p, origin: { x: best.x, y: best.y, x2: best.x2, y2: best.y2 } };
    return;
  }

  if (annotMode === 'text') {
    const text = prompt('Label text');
    if (!text) return;
    annotations.push({
      type: 'text', text, x: p.x, y: p.y,
      color: $('#annotColor').value, size: parseFloat($('#annotSize').value),
    });
    draw();
    return;
  }

  drag = {
    create: {
      type: annotMode, x: p.x, y: p.y, x2: p.x, y2: p.y,
      color: $('#annotColor').value, size: parseFloat($('#annotSize').value),
    },
  };
  annotations.push(drag.create);
}

function onPointerMove(e) {
  if (!drag) return;
  const p = canvasPoint(e);
  if (drag.create) {
    drag.create.x2 = p.x;
    drag.create.y2 = p.y;
  } else if (drag.move) {
    const dx = p.x - drag.from.x;
    const dy = p.y - drag.from.y;
    drag.move.x = drag.origin.x + dx;
    drag.move.y = drag.origin.y + dy;
    if (drag.origin.x2 != null) {
      drag.move.x2 = drag.origin.x2 + dx;
      drag.move.y2 = drag.origin.y2 + dy;
    }
  }
  redraw();
}

function onPointerUp() {
  if (drag?.create) {
    const a = drag.create;
    const tiny = Math.hypot(a.x2 - a.x, a.y2 - a.y) < 0.005;
    if (tiny) annotations.pop();
  }
  if (drag) draw();
  drag = null;
}

// ── Export ─────────────────────────────────────────────────────

function exportGraphic() {
  if (!panelCaptures().length) {
    toast('Nothing to export yet — capture an image first', 'err');
    return;
  }
  const factor = parseFloat($('#gScaleFactor').value);
  const out = compose(factor);
  out.toBlob((blob) => {
    const name = $('#gTitle').value.trim() || 'sentinel2_graphic';
    download(blob, `${slug(name)}.png`);
    toast(`Graphic saved — ${out.width} × ${out.height} px`, 'ok');
  }, 'image/png');
}

export function refreshGraphic() {
  syncPanelSelect();
  draw();
}
