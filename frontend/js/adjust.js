// Adjusting the picture on screen: exposure, contrast, colour, clarity.
//
// This is the second half of making imagery look good, and it works on quite
// different material from the first. The enhancement in the render happens in
// reflectance, before the picture exists, and needs the imagery fetched again
// to change. These are ordinary photographic adjustments on the finished
// pixels, so they apply instantly and cost nothing to try.
//
// Pure functions over ImageData: the source is never modified.

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
  // The look of commercial aerial imagery: warm sand, deep shadows, hard
  // edges, and colour that is present rather than washed out.
  Aerial: { clarity: 60, contrast: 16, saturation: 16, highlights: -10,
            shadows: -6, temperature: 14, gamma: 0.98 },
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

/**
 * Render a source image through the pipeline onto a fresh canvas.
 *
 * `maxSide` scales the work down for a live preview: the map never shows more
 * than a screenful anyway, and a 4096 px render would otherwise be rebuilt in
 * full behind every twitch of a slider.
 */
export function renderToCanvas(image, adjustments, { maxSide = 0 } = {}) {
  const natural = { w: image.naturalWidth || image.width, h: image.naturalHeight || image.height };
  const shrink = maxSide && Math.max(natural.w, natural.h) > maxSide
    ? maxSide / Math.max(natural.w, natural.h) : 1;
  const w = Math.max(1, Math.round(natural.w * shrink));
  const h = Math.max(1, Math.round(natural.h * shrink));
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
