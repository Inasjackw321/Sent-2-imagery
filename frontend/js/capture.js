// Highlight part of the imagery and take it away as a picture.
//
// The crop is taken from the rendered imagery itself rather than from the
// screen, so what comes out is at the full resolution that was fetched, not at
// whatever size the map happened to be showing it. The picture then carries a
// mark saying whose it is and, because the Copernicus licence asks for it, what
// took it and when.

import { store } from './store.js';
import * as adjust from './adjust.js';
import { toast, download } from './ui.js';

export const WATERMARK = '@Kaldockhi';

// Small crops still need a legible mark, big ones must not get a huge one.
const MARK_FRACTION = 0.030;
const MARK_MIN = 15;
const MARK_MAX = 46;

/**
 * Crop the imagery on screen to a rectangle and hand it back as a canvas.
 *
 * `rect` is in Web Mercator metres, which is the projection the overlay is
 * drawn in -- taking the corners as latitudes instead would shear the crop,
 * because Mercator does not space latitudes evenly.
 */
export function cropImagery(rect, image = store.image) {
  const grid = image?.meta?.grid;
  if (!image?.element || !grid?.bounds3857) return null;

  const [gx0, gy0, gx1, gy1] = grid.bounds3857;
  const left = Math.min(rect.x0, rect.x1);
  const right = Math.max(rect.x0, rect.x1);
  const top = Math.max(rect.y0, rect.y1);
  const bottom = Math.min(rect.y0, rect.y1);

  // The picture is only the part of the highlight that has imagery under it.
  const fx0 = clamp01((Math.max(left, gx0) - gx0) / (gx1 - gx0));
  const fx1 = clamp01((Math.min(right, gx1) - gx0) / (gx1 - gx0));
  const fy0 = clamp01((gy1 - Math.min(top, gy1)) / (gy1 - gy0));
  const fy1 = clamp01((gy1 - Math.max(bottom, gy0)) / (gy1 - gy0));
  if (fx1 - fx0 <= 0 || fy1 - fy0 <= 0) return null;

  // Whatever is on screen, including the adjustments, at its full size.
  const source = adjust.isNeutral(store.adjustments ?? {})
    ? image.element
    : adjust.renderToCanvas(image.element, store.adjustments);

  const sx = Math.round(fx0 * source.width);
  const sy = Math.round(fy0 * source.height);
  const sw = Math.max(1, Math.round((fx1 - fx0) * source.width));
  const sh = Math.max(1, Math.round((fy1 - fy0) * source.height));

  const canvas = document.createElement('canvas');
  canvas.width = sw;
  canvas.height = sh;
  const ctx = canvas.getContext('2d');
  ctx.drawImage(source, sx, sy, sw, sh, 0, 0, sw, sh);
  stamp(ctx, canvas, image.meta);
  return canvas;
}

const clamp01 = (v) => Math.min(1, Math.max(0, v));

/** Put the mark and the credit line in the bottom-right corner. */
function stamp(ctx, canvas, meta) {
  const size = Math.round(Math.min(MARK_MAX,
    Math.max(MARK_MIN, canvas.width * MARK_FRACTION)));
  const pad = Math.round(size * 0.7);
  const credit = creditLine(meta);

  ctx.save();
  ctx.textAlign = 'right';
  ctx.textBaseline = 'alphabetic';
  // A soft dark shadow is what keeps the mark readable over both a white
  // salt-flat and a black lake, without laying a box over the imagery.
  ctx.shadowColor = 'rgba(0,0,0,.75)';
  ctx.shadowBlur = Math.round(size * 0.5);
  ctx.shadowOffsetY = 1;

  const baseline = canvas.height - pad - (credit ? Math.round(size * 0.85) : 0);
  ctx.font = `600 ${size}px ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif`;
  ctx.fillStyle = 'rgba(255,255,255,.94)';
  ctx.fillText(WATERMARK, canvas.width - pad, baseline);

  if (credit) {
    const small = Math.max(9, Math.round(size * 0.46));
    ctx.font = `400 ${small}px ui-sans-serif, system-ui, -apple-system, sans-serif`;
    ctx.fillStyle = 'rgba(255,255,255,.72)';
    ctx.fillText(credit, canvas.width - pad, canvas.height - pad);
  }
  ctx.restore();
}

function creditLine(meta) {
  if (!meta) return '';
  const who = meta.source?.short ?? 'Sentinel';
  const dates = meta.scenes?.length ?? 1;
  const when = dates > 1
    ? `${dates} passes to ${meta.scene?.date ?? ''}`.trim()
    : meta.scene?.date ?? '';
  return [who, when].filter(Boolean).join(' · ');
}

/**
 * Copy the crop to the clipboard, saving it instead if that is not allowed.
 *
 * Writing an image to the clipboard needs a secure context and permission, and
 * neither is guaranteed. Falling back to a file means the picture is never
 * lost just because the browser said no.
 */
export async function copyRegion(rect) {
  const canvas = cropImagery(rect);
  if (!canvas) {
    toast('Highlight a part of the imagery — that box missed it', 'err');
    return null;
  }
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/png'));
  const name = filename();

  try {
    if (!navigator.clipboard?.write) throw new Error('clipboard unavailable');
    await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
    toast(`Copied — ${canvas.width}×${canvas.height} px, marked ${WATERMARK}`, 'ok');
  } catch {
    download(blob, name);
    toast(`Clipboard not allowed here — saved ${name} instead`);
  }
  return canvas;
}

/** Save the crop as a file, whatever the clipboard has to say about it. */
export async function saveRegion(rect) {
  const canvas = cropImagery(rect);
  if (!canvas) {
    toast('Highlight a part of the imagery — that box missed it', 'err');
    return null;
  }
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/png'));
  download(blob, filename());
  toast(`Saved — ${canvas.width}×${canvas.height} px, marked ${WATERMARK}`, 'ok');
  return canvas;
}

function filename() {
  const meta = store.image?.meta;
  const who = (meta?.satellite ?? 'sentinel').replace('-', '');
  return `${who}_${meta?.scene?.date ?? 'region'}_kaldockhi.png`;
}
