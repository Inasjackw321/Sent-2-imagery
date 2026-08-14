"""Making the imagery look better.

These operate on physical band values (reflectance, dB, metres) before the
stretch, or on the normalised 0-1 image just after it -- the two places where
each correction actually belongs.

Before the stretch:
    cloud-free compositing, haze removal, denoising
After it:
    adaptive contrast, white balance, detail (unsharp mask)
"""

from __future__ import annotations

import numpy as np
from scipy import ndimage

MaskedArray = np.ma.MaskedArray


# ── Multi-scene compositing ────────────────────────────────────


def composite(stacks: list[dict[str, MaskedArray]], method: str = "median") -> dict:
    """Merge several dates into one clean image.

    With clouds masked out, taking the median through the stack removes them:
    a pixel only has to be clear in half the scenes. This is the single most
    effective way to get a usable picture of a cloudy place.
    """
    if not stacks:
        raise ValueError("Nothing to composite")
    if len(stacks) == 1:
        return stacks[0]

    keys = [k for k in stacks[0] if all(k in s for s in stacks)]
    out: dict[str, MaskedArray] = {}

    for key in keys:
        layers = np.ma.stack([s[key] for s in stacks])
        if method == "mean":
            merged = np.ma.mean(layers, axis=0)
        elif method == "max":
            merged = np.ma.max(layers, axis=0)
        elif method == "min":
            merged = np.ma.min(layers, axis=0)
        elif method == "first":
            # Fill gaps from later scenes without changing clear pixels.
            merged = layers[0]
            for layer in layers[1:]:
                gaps = np.ma.getmaskarray(merged)
                if not gaps.any():
                    break
                merged = np.ma.masked_array(
                    np.where(gaps, layer.data, merged.data),
                    mask=gaps & np.ma.getmaskarray(layer),
                )
        else:
            merged = np.ma.median(layers, axis=0)

        out[key] = np.ma.masked_array(
            np.ma.filled(merged, 0.0).astype("float32"),
            mask=np.ma.getmaskarray(merged),
        )
    return out


def composite_report(stacks: list[dict[str, MaskedArray]], key: str | None = None) -> dict:
    """How much of the frame each extra date rescued."""
    if not stacks:
        return {}
    key = key or next(iter(stacks[0]))
    per_scene = [float((~np.ma.getmaskarray(s[key])).mean()) for s in stacks if key in s]
    covered = np.zeros_like(np.ma.getmaskarray(stacks[0][key]), dtype=bool)
    for scene in stacks:
        if key in scene:
            covered |= ~np.ma.getmaskarray(scene[key])
    return {
        "scenes": len(per_scene),
        "best_single_pct": round(max(per_scene, default=0.0) * 100, 2),
        "combined_pct": round(float(covered.mean()) * 100, 2),
    }


# ── Haze removal ───────────────────────────────────────────────


def dark_object_subtraction(bands: dict[str, MaskedArray], percentile: float = 1.0,
                            strength: float = 1.0) -> dict:
    """Remove atmospheric haze by subtracting each band's darkest honest value.

    Deep shadow and clear water should read near zero; whatever they actually
    read is path radiance scattered by the atmosphere. Shorter wavelengths
    scatter more, so blue is lifted the most -- subtracting per band removes the
    blue-grey cast without touching the colour of the scene itself.
    """
    if strength <= 0:
        return bands
    out = {}
    for key, band in bands.items():
        valid = band.compressed()
        if valid.size == 0:
            out[key] = band
            continue
        floor = float(np.percentile(valid, percentile))
        # Keep a sliver so genuinely black pixels do not clip to nothing.
        corrected = band.data - floor * strength * 0.98
        out[key] = np.ma.masked_array(
            np.clip(corrected, 0.0, None).astype("float32"),
            mask=np.ma.getmaskarray(band),
        )
    return out


# ── Denoising ──────────────────────────────────────────────────


def denoise(bands: dict[str, MaskedArray], strength: float = 0.0) -> dict:
    """Median filtering: removes speckle and sensor noise, keeps edges.

    Radar imagery in particular is unreadable until it has been despeckled.
    """
    if strength <= 0:
        return bands
    size = 3 if strength < 0.66 else 5
    blend = min(strength / 0.66, 1.0) if strength < 0.66 else 1.0
    out = {}
    for key, band in bands.items():
        filtered = ndimage.median_filter(np.ma.filled(band, 0.0).astype("float32"), size=size)
        mixed = band.data * (1 - blend) + filtered * blend
        out[key] = np.ma.masked_array(mixed.astype("float32"),
                                      mask=np.ma.getmaskarray(band))
    return out


# ── Post-stretch corrections (operate on 0-1 images) ───────────


def clahe(image: np.ndarray, clip_limit: float = 2.0, tiles: int = 8) -> np.ndarray:
    """Contrast-limited adaptive histogram equalisation.

    A global stretch has to compromise between a bright desert and a dark
    forest in the same frame. This equalises within tiles instead, clipping the
    histogram first so flat areas do not turn into noise, then blends the tile
    mappings so no seams show.
    """
    if clip_limit <= 0:
        return image
    h, w = image.shape[:2]
    tiles = max(2, min(int(tiles), 16))
    bins = 256

    ty = np.linspace(0, h, tiles + 1).astype(int)
    tx = np.linspace(0, w, tiles + 1).astype(int)
    quantised = np.clip(image * (bins - 1), 0, bins - 1).astype(np.uint8)

    mappings = np.empty((tiles, tiles, bins), dtype="float32")
    for i in range(tiles):
        for j in range(tiles):
            tile = quantised[ty[i]:ty[i + 1], tx[j]:tx[j + 1]]
            if tile.size == 0:
                mappings[i, j] = np.linspace(0, 1, bins)
                continue
            hist = np.bincount(tile.ravel(), minlength=bins).astype("float32")
            # Clip the peaks and hand the excess back out evenly.
            limit = max(clip_limit * tile.size / bins, 1.0)
            excess = np.maximum(hist - limit, 0).sum()
            hist = np.minimum(hist, limit) + excess / bins
            cdf = np.cumsum(hist)
            mappings[i, j] = (cdf - cdf[0]) / max(cdf[-1] - cdf[0], 1e-6)

    # Bilinear blend between the four surrounding tile centres.
    centre_y = (ty[:-1] + ty[1:]) / 2.0
    centre_x = (tx[:-1] + tx[1:]) / 2.0
    rows = np.arange(h)
    cols = np.arange(w)

    fy = np.interp(rows, centre_y, np.arange(tiles))
    fx = np.interp(cols, centre_x, np.arange(tiles))
    y0 = np.floor(fy).astype(int)
    x0 = np.floor(fx).astype(int)
    y1 = np.minimum(y0 + 1, tiles - 1)
    x1 = np.minimum(x0 + 1, tiles - 1)
    wy = (fy - y0)[:, None]
    wx = (fx - x0)[None, :]

    idx = quantised
    top = (mappings[y0[:, None], x0[None, :], idx] * (1 - wx)
           + mappings[y0[:, None], x1[None, :], idx] * wx)
    bottom = (mappings[y1[:, None], x0[None, :], idx] * (1 - wx)
              + mappings[y1[:, None], x1[None, :], idx] * wx)
    return np.clip(top * (1 - wy) + bottom * wy, 0, 1).astype("float32")


def apply_clahe_rgb(rgb: np.ndarray, clip_limit: float = 2.0, tiles: int = 8,
                    strength: float = 1.0) -> np.ndarray:
    """Equalise brightness only, so colours are not pulled about."""
    if clip_limit <= 0 or strength <= 0:
        return rgb
    luma = rgb @ np.array([0.2126, 0.7152, 0.0722], dtype="float32")
    equalised = clahe(luma, clip_limit, tiles)
    equalised = luma * (1 - strength) + equalised * strength
    with np.errstate(divide="ignore", invalid="ignore"):
        gain = np.where(luma > 1e-4, equalised / luma, 1.0)
    return np.clip(rgb * np.nan_to_num(gain, nan=1.0)[..., None], 0, 1).astype("float32")


def white_balance(rgb: np.ndarray, strength: float = 1.0) -> np.ndarray:
    """Grey-world balance: assume the average of the scene should be neutral."""
    if strength <= 0:
        return rgb
    means = rgb.reshape(-1, 3).mean(axis=0)
    target = float(means.mean())
    with np.errstate(divide="ignore", invalid="ignore"):
        gains = np.where(means > 1e-5, target / means, 1.0)
    gains = 1.0 + (np.nan_to_num(gains, nan=1.0) - 1.0) * strength
    return np.clip(rgb * gains, 0, 1).astype("float32")


def unsharp(rgb: np.ndarray, amount: float = 0.0, radius: float = 1.2,
            headroom: float = 0.10) -> np.ndarray:
    """Local contrast boost — makes field edges and streets crisp.

    The correction is capped before it is applied: without that, a strong
    setting draws bright outlines around coastlines and field boundaries, which
    reads as an artefact rather than as detail.
    """
    if amount <= 0:
        return rgb
    blurred = np.dstack([
        ndimage.gaussian_filter(rgb[..., c], sigma=max(radius, 0.3)) for c in range(3)
    ])
    detail = np.clip(rgb - blurred, -headroom, headroom)
    return np.clip(rgb + detail * amount, 0, 1).astype("float32")


def vibrance(rgb: np.ndarray, amount: float = 0.0) -> np.ndarray:
    """Lift muted colours and leave already-saturated ones alone."""
    if amount == 0:
        return rgb
    luma = (rgb @ np.array([0.2126, 0.7152, 0.0722], dtype="float32"))[..., None]
    spread = np.abs(rgb - luma).max(axis=2, keepdims=True)
    weight = 1.0 - np.clip(spread * 2.0, 0, 1)          # least saturated gain most
    return np.clip(luma + (rgb - luma) * (1 + amount * weight), 0, 1).astype("float32")
