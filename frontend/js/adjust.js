// The image-editing pipeline. Pure functions over ImageData so the editor,
// the timelapse frames and the graphic composer all get identical results.

export const DEFAULTS = {
  exposure: 0,     // stops, scaled
  contrast: 0,
  saturation: 0,
  temperature: 0,  // + warmer
  tint: 0,         // + magenta
  highlights: 0,
  shadows: 0,
  gamma: 1,
  clarity: 0,      // unsharp mask
  vignette: 0,
};

export const PRESETS = {
  Natural: {},
  Punchy: { contrast: 22, saturation: 18, clarity: 25, shadows: 8 },
  Soft: { contrast: -12, saturation: -6, highlights: -14, shadows: 14, gamma: 1.08 },
  Vivid: { saturation: 42, contrast: 14, exposure: 4 },
  Crisp: { clarity: 55, contrast: 10, highlights: -8 },
  Mono: { saturation: -100, contrast: 16, clarity: 18 },
  Cold: { temperature: -28, tint: -6, saturation: 8 },
  Warm: { temperature: 30, tint: 4, saturation: 6 },
  Faded: { contrast: -18, saturation: -22, shadows: 22, gamma: 1.15 },
};

export const withDefaults = (adj) => ({ ...DEFAULTS, ...(adj || {}) });

export const isNeutral = (adj) => {
  const a = withDefaults(adj);
  return Object.keys(DEFAULTS).every((k) => Math.abs(a[k] - DEFAULTS[k]) < 1e-6);
};

const clamp255 = (v) => (v < 0 ? 0 : v > 255 ? 255 : v);

/** Per-channel tone curves folded into three 256-entry lookup tables. */
function buildLuts(a) {
  const exposure = 2 ** (a.exposure / 50);
  const contrast = (100 + a.contrast) / 100;
  const shadow = a.shadows / 100;
  const highlight = a.highlights / 100;
  const invGamma = 1 / Math.max(a.gamma, 0.05);

  const temp = a.temperature / 100;
  const tint = a.tint / 100;
  const gain = [
    1 + temp * 0.32 - tint * 0.06,
    1 + tint * 0.22 - Math.abs(temp) * 0.02,
    1 - temp * 0.32 - tint * 0.06,
  ];

  const luts = [new Uint8ClampedArray(256), new Uint8ClampedArray(256), new Uint8ClampedArray(256)];
  for (let i = 0; i < 256; i++) {
    let base = (i / 255) * exposure;
    base += shadow * 0.55 * (1 - base) ** 2.2;
    base += highlight * 0.55 * base ** 2.2;
    base = (base - 0.5) * contrast + 0.5;
    base = base <= 0 ? 0 : base ** invGamma;
    for (let c = 0; c < 3; c++) luts[c][i] = clamp255(base * gain[c] * 255);
  }
  return luts;
}

/** Separable 3-tap blur used by the clarity (unsharp mask) stage. */
function blurRGB(src, w, h) {
  const tmp = new Float32Array(src.length);
  const out = new Float32Array(src.length);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4;
      const l = x > 0 ? i - 4 : i;
      const r = x < w - 1 ? i + 4 : i;
      for (let c = 0; c < 3; c++) tmp[i + c] = (src[l + c] + 2 * src[i + c] + src[r + c]) / 4;
    }
  }
  const row = w * 4;
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4;
      const u = y > 0 ? i - row : i;
      const d = y < h - 1 ? i + row : i;
      for (let c = 0; c < 3; c++) out[i + c] = (tmp[u + c] + 2 * tmp[i + c] + tmp[d + c]) / 4;
    }
  }
  return out;
}

/**
 * Apply an adjustment stack. Returns a new ImageData; the source is untouched.
 * Alpha is preserved so clipped-to-shape renders keep their transparency.
 */
export function applyAdjustments(source, adjustments) {
  const a = withDefaults(adjustments);
  const { width: w, height: h } = source;
  const out = new ImageData(new Uint8ClampedArray(source.data), w, h);
  const px = out.data;

  const luts = buildLuts(a);
  const sat = 1 + a.saturation / 100;

  for (let i = 0; i < px.length; i += 4) {
    let r = luts[0][px[i]];
    let g = luts[1][px[i + 1]];
    let b = luts[2][px[i + 2]];

    if (sat !== 1) {
      const luma = 0.2126 * r + 0.7152 * g + 0.0722 * b;
      r = clamp255(luma + (r - luma) * sat);
      g = clamp255(luma + (g - luma) * sat);
      b = clamp255(luma + (b - luma) * sat);
    }
    px[i] = r; px[i + 1] = g; px[i + 2] = b;
  }

  if (a.clarity > 0) {
    const amount = a.clarity / 100 * 1.4;
    const blurred = blurRGB(px, w, h);
    for (let i = 0; i < px.length; i += 4) {
      for (let c = 0; c < 3; c++) {
        px[i + c] = clamp255(px[i + c] + amount * (px[i + c] - blurred[i + c]));
      }
    }
  }

  if (a.vignette > 0) {
    const strength = a.vignette / 100;
    const cx = w / 2;
    const cy = h / 2;
    const maxR2 = cx * cx + cy * cy;
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const i = (y * w + x) * 4;
        const dx = x - cx;
        const dy = y - cy;
        const falloff = 1 - strength * ((dx * dx + dy * dy) / maxR2) ** 1.4;
        px[i] *= falloff; px[i + 1] *= falloff; px[i + 2] *= falloff;
      }
    }
  }

  return out;
}

/** Render a source image through the pipeline onto a fresh canvas. */
export function renderToCanvas(image, adjustments, { width, height } = {}) {
  const w = width || image.naturalWidth || image.width;
  const h = height || image.naturalHeight || image.height;
  const canvas = document.createElement('canvas');
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(image, 0, 0, w, h);
  if (!isNeutral(adjustments)) {
    ctx.putImageData(applyAdjustments(ctx.getImageData(0, 0, w, h), adjustments), 0, 0);
  }
  return canvas;
}

/**
 * Geometry ops applied before the tone pipeline. Rotation and flips come first,
 * then the crop -- so crop rectangles are always in the coordinates the user
 * actually sees on screen.
 */
export function transformCanvas(canvas, { rotate = 0, flipH = false, flipV = false, crop = null }) {
  let src = canvas;
  const quarter = ((rotate % 360) + 360) % 360;

  if (quarter !== 0 || flipH || flipV) {
    const swap = quarter === 90 || quarter === 270;
    const out = document.createElement('canvas');
    out.width = swap ? src.height : src.width;
    out.height = swap ? src.width : src.height;
    const ctx = out.getContext('2d');
    ctx.translate(out.width / 2, out.height / 2);
    ctx.rotate((quarter * Math.PI) / 180);
    ctx.scale(flipH ? -1 : 1, flipV ? -1 : 1);
    ctx.drawImage(src, -src.width / 2, -src.height / 2);
    src = out;
  }

  if (crop) {
    const x = Math.max(0, Math.round(crop.x));
    const y = Math.max(0, Math.round(crop.y));
    const c = document.createElement('canvas');
    c.width = Math.max(1, Math.min(Math.round(crop.w), src.width - x));
    c.height = Math.max(1, Math.min(Math.round(crop.h), src.height - y));
    c.getContext('2d').drawImage(src, x, y, c.width, c.height, 0, 0, c.width, c.height);
    src = c;
  }

  return src;
}
