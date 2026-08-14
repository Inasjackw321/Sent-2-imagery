"""Multi-frame super-resolution: many dates fused into one sharper image.

A single satellite pass samples the ground on its own grid, and that grid is
never in quite the same place twice -- orbits repeat to within a few tens of
metres, so each date lands its pixel centres at a different sub-pixel phase.
Resample several dates onto one finer grid and each of them contributes a
*different* set of samples of the same ground. Line them up to a fraction of a
pixel, combine them, and undo the blur the sensor and the resampling put in,
and detail comes back that no single date carried.

The pipeline, in order:

    register    sub-pixel alignment by phase correlation (upsampled DFT)
    align       shift each date onto the reference, in the fine grid
    fuse        robust combine -- median-centred, outliers thrown away
    restore     Van Cittert deconvolution of the sampling blur, with an
                overshoot clamp so edges do not ring

Every step reports what it did: the shift it found for each date, how many
dates survived per pixel, and the measured change in sharpness and noise.
"""

from __future__ import annotations

import numpy as np
from scipy import ndimage

MaskedArray = np.ma.MaskedArray

def frames_wanted(scale: int) -> int:
    """Dates needed before a scale factor is properly supported.

    An N times finer grid has N^2 times as many pixels to fill, so N^2
    differently-phased looks is the point at which the fusion is solving a
    determined problem rather than interpolating between a handful of samples.
    """
    return max(2, int(scale) ** 2)


# ── Sub-pixel registration ─────────────────────────────────────


def _prepare(arr: MaskedArray | np.ndarray) -> np.ndarray:
    """A windowed, zero-mean copy that phase correlation can work on.

    Masked pixels become the scene mean so cloud holes do not register as
    features, and a Hann window stops the frame edges -- which the FFT treats
    as a hard discontinuity -- from dominating the correlation.
    """
    data = np.ma.filled(np.ma.masked_invalid(arr), np.nan).astype("float64")
    finite = np.isfinite(data)
    if not finite.any():
        return np.zeros(data.shape)
    data = np.where(finite, data, float(np.mean(data[finite])))
    data -= data.mean()

    h, w = data.shape
    window = np.hanning(max(h, 2))[:, None] * np.hanning(max(w, 2))[None, :]
    return data * window


def _upsampled_dft(data: np.ndarray, size: int, factor: float,
                   offsets: tuple[float, float]) -> np.ndarray:
    """Inverse DFT of `data`, sampled `factor` times finer around `offsets`.

    Evaluating the transform only over the few pixels that matter costs a
    couple of small matrix multiplies, where zero-padding the whole frame to
    the same fineness would cost `factor**2` times the memory of the image.
    """
    for n, offset in zip(data.shape[::-1], offsets[::-1]):
        kernel = np.exp(
            -2j * np.pi * (np.arange(size) - offset)[:, None] * np.fft.fftfreq(n, factor)
        )
        data = np.tensordot(kernel, data, axes=(1, -1))
    return data


def _band_weight(shape: tuple[int, int], cutoff: float) -> np.ndarray:
    """Soft low-pass over the frequencies that carry real structure.

    Phase correlation divides out the magnitude of every frequency, which is
    what makes it immune to brightness differences -- and also what makes it
    amplify the empty bins above the sensor's band, where there is nothing but
    rounding error, until they outvote the signal. Rolling the whitened
    spectrum off past the useful band is what keeps the peak trustworthy on
    imagery that has already been resampled once.
    """
    fy = np.fft.fftfreq(shape[0])[:, None]
    fx = np.fft.fftfreq(shape[1])[None, :]
    return np.exp(-0.5 * (np.hypot(fy, fx) / max(cutoff, 1e-3)) ** 2)


def _confidence(correlation: np.ndarray, peak: tuple[int, int], radius: int = 4) -> float:
    """How far the winning peak stands clear of its best rival.

    Farmland in strips, a city on a grid, orchard rows -- repeating ground
    gives the correlation several peaks of nearly equal height and no way to
    tell which is the true offset. That ratio is what catches it: a real match
    towers over everything else, an ambiguous one barely leads.
    """
    rivals = correlation.copy()
    h, w = correlation.shape
    rows = (np.arange(-radius, radius + 1) + peak[0]) % h
    cols = (np.arange(-radius, radius + 1) + peak[1]) % w
    rivals[np.ix_(rows, cols)] = 0.0
    best_rival = float(rivals.max())
    return float(correlation[peak]) / best_rival if best_rival > 1e-12 else np.inf


def estimate_shift(reference, moving, upsample: int = 20, max_shift: float = 8.0,
                   cutoff: float = 0.08, confidence: float = 5.0) -> tuple[float, float]:
    """Shift (dy, dx), in pixels, that brings `moving` onto `reference`.

    Phase correlation: the cross-power spectrum of two images that differ only
    by a translation is a pure phase ramp, whose inverse transform is a single
    spike at the offset. Normalising away the magnitudes is what makes it
    immune to one date being brighter, hazier or differently stretched than
    another -- only the geometry is compared.
    """
    ref = _prepare(reference)
    mov = _prepare(moving)
    if ref.shape != mov.shape or not ref.any() or not mov.any():
        return 0.0, 0.0

    src = np.fft.fft2(ref)
    tgt = np.fft.fft2(mov)
    product = src * tgt.conj()
    magnitude = np.abs(product)
    alive = magnitude > 1e-6 * float(magnitude.max())
    product = np.where(alive, product / (magnitude + 1e-12), 0.0)
    product *= _band_weight(product.shape, cutoff)

    correlation = np.abs(np.fft.ifft2(product))
    peak = np.unravel_index(np.argmax(correlation), correlation.shape)
    shape = np.array(correlation.shape)

    # An unconvincing peak means the dates cannot be told apart to better than
    # a pixel. They arrive georeferenced already, so leaving one where the
    # satellite put it beats moving it on a guess.
    if _confidence(correlation, peak) < confidence:
        return 0.0, 0.0

    # The correlation wraps around, so a peak past the midpoint is a negative
    # shift rather than an enormous positive one.
    shifts = np.array(peak, dtype="float64")
    shifts = np.where(shifts > shape // 2, shifts - shape, shifts)

    if upsample > 1:
        size = int(np.ceil(upsample * 1.5))
        centre = np.fix(size / 2.0)
        offsets = tuple(centre - s * upsample for s in shifts)
        fine = _upsampled_dft(product.conj(), size, float(upsample), offsets).conj()
        fine_peak = np.unravel_index(np.argmax(np.abs(fine)), fine.shape)
        shifts = shifts + (np.array(fine_peak, dtype="float64") - centre) / upsample

    dy, dx = float(shifts[0]), float(shifts[1])
    # A wild answer means the correlation found no real match (thick cloud, a
    # flooded field, a different season). Refusing it is better than smearing
    # the stack: an unshifted date still contributes its own sampling phase.
    if not np.isfinite(dy) or not np.isfinite(dx) or max(abs(dy), abs(dx)) > max_shift:
        return 0.0, 0.0
    return dy, dx


def shift_band(band: MaskedArray, dy: float, dx: float) -> MaskedArray:
    """Move a band by a fractional number of pixels, mask and all."""
    if abs(dy) < 1e-4 and abs(dx) < 1e-4:
        return band
    mask = np.ma.getmaskarray(band)
    fill = float(np.ma.median(band)) if (~mask).any() else 0.0
    data = ndimage.shift(np.ma.filled(band, fill).astype("float32"), (dy, dx),
                         order=3, mode="nearest", prefilter=True)
    moved_mask = ndimage.shift(mask.astype("float32"), (dy, dx),
                               order=1, mode="constant", cval=1.0) > 0.35
    return np.ma.masked_array(data.astype("float32"), mask=moved_mask)


# ── Robust fusion ──────────────────────────────────────────────


def robust_mean(layers: MaskedArray, tolerance: float = 2.5) -> tuple[MaskedArray, np.ndarray]:
    """Average the dates that agree, drop the ones that do not.

    A plain mean is the best noise reduction there is, and the worst possible
    handling of a cloud edge the mask missed. Centring on the median and
    keeping only samples within a few robust deviations of it gets the noise
    reduction of a mean with the cloud immunity of a median.
    """
    count = layers.shape[0]
    if count == 1:
        return layers[0], (~np.ma.getmaskarray(layers[0])).astype("int16")

    median = np.ma.median(layers, axis=0)
    if count == 2:
        merged = np.ma.mean(layers, axis=0)
        return merged, (~np.ma.getmaskarray(layers)).sum(axis=0).astype("int16")

    deviation = np.ma.abs(layers - median)
    mad = np.ma.median(deviation, axis=0)
    spread = np.ma.filled(mad, 0.0) * 1.4826

    # Below the noise floor the MAD collapses to nothing and would reject
    # perfectly good samples, so keep a floor tied to the band's own range.
    valid = layers.compressed()
    if valid.size:
        lo, hi = np.percentile(valid, [2, 98])
        floor = max(float(hi - lo) * 0.02, 1e-6)
    else:
        floor = 1e-6
    limit = np.maximum(spread * tolerance, floor)

    keep = ~np.ma.getmaskarray(layers) & (np.ma.filled(deviation, np.inf) <= limit)
    kept = keep.sum(axis=0)
    total = np.where(kept > 0, (np.ma.filled(layers, 0.0) * keep).sum(axis=0), 0.0)
    merged = np.where(kept > 0, total / np.maximum(kept, 1), np.ma.filled(median, 0.0))
    mask = np.ma.getmaskarray(median) & (kept == 0)
    return (np.ma.masked_array(merged.astype("float32"), mask=mask),
            kept.astype("int16"))


# ── Restoring the blur that sampling put in ────────────────────


def deconvolve(image: np.ndarray, sigma: float, amount: float = 0.75,
               iterations: int = 3, headroom: float = 0.2) -> np.ndarray:
    """Van Cittert deconvolution: undo a known Gaussian blur, carefully.

    Each pass re-blurs the current estimate, compares it with what was
    measured, and feeds the difference back. Unconstrained it amplifies noise
    into ringing, so every pass is clamped into the local range of the input
    plus a little headroom -- detail is restored, halos are not invented.
    """
    if amount <= 0 or iterations <= 0 or sigma <= 0:
        return image

    estimate = image.astype("float32").copy()
    local_min = ndimage.minimum_filter(image, size=3)
    local_max = ndimage.maximum_filter(image, size=3)
    slack = (local_max - local_min) * headroom
    low, high = local_min - slack, local_max + slack

    for _ in range(int(iterations)):
        residual = image - ndimage.gaussian_filter(estimate, sigma)
        estimate = np.clip(estimate + amount * residual, low, high)
    return estimate.astype("float32")


# ── Measuring whether it worked ────────────────────────────────


def _fill(band: MaskedArray) -> tuple[np.ndarray, np.ndarray]:
    """A plain array plus its validity, with holes filled at the mean.

    Filling with zero would put a cliff around every cloud, and a measurement
    of fine detail would then be measuring the cliffs.
    """
    mask = np.ma.getmaskarray(band)
    data = np.ma.filled(band, 0.0).astype("float32")
    if mask.any():
        data = np.where(mask, float(np.ma.mean(band)) if (~mask).any() else 0.0, data)
    return data, ~mask


def _sharpness(image: np.ndarray, scale: float = 2.0,
               where: np.ndarray | None = None) -> float:
    """Energy in the band of detail that super-resolution can add.

    A band-pass, not a high-pass: structure coarser than a native pixel was
    already there in every date, and structure at the very finest scale is
    mostly noise, which a metric that counted it could be gamed by simply
    sharpening harder. What is left in between is the ground detail that only
    several differently-phased looks can resolve.
    """
    image = image.astype("float32")
    fine = ndimage.gaussian_filter(image, 0.6)
    coarse = ndimage.gaussian_filter(image, max(scale, 1.0))
    detail = fine - coarse
    return float(np.std(detail if where is None else detail[where]))


def _noise(image: np.ndarray, size: int = 3, where: np.ndarray | None = None) -> float:
    """Robust noise estimate: the spread of what a small median throws away.

    A median filter takes out speckle and sensor noise while leaving edges
    alone, so the residual is mostly noise, and its MAD is not fooled by the
    handful of real edges that do survive into it.
    """
    residual = image - ndimage.median_filter(image, size=max(3, int(size)))
    if where is not None:
        residual = residual[where]
    return float(np.median(np.abs(residual - np.median(residual))) * 1.4826)


def _guide(bands: dict[str, MaskedArray], keys: list[str]) -> MaskedArray:
    """One brightness image standing in for the scene during registration."""
    present = [k for k in keys if k in bands]
    if not present:
        present = list(bands)
    stacked = np.ma.stack([bands[k] for k in present])
    return np.ma.mean(stacked, axis=0)


# ── The pipeline ───────────────────────────────────────────────


def fuse(stacks: list[dict[str, MaskedArray]], scale: int = 2,
         restore: float = 0.75, register: bool = True, upsample: int = 20,
         tolerance: float = 2.5, reference: int = 0,
         dates: list[str] | None = None) -> tuple[dict[str, MaskedArray], dict]:
    """Fuse several dates, already read onto the same fine grid, into one image.

    The inputs must all share the grid -- read them at `scale` times the
    resolution you want out, so that each date arrives carrying its own
    sub-pixel sampling phase for the fusion to exploit.
    """
    if not stacks:
        raise ValueError("Nothing to fuse")
    keys = [k for k in stacks[0] if all(k in s for s in stacks)]
    if not keys:
        raise ValueError("The scenes have no bands in common")
    if len(stacks) == 1:
        return stacks[0], {}

    reference = max(0, min(int(reference), len(stacks) - 1))
    guide_keys = [k for k in ("red", "green", "blue", "nir", "pan", "vv") if k in keys] or keys
    guides = [_guide(s, guide_keys) for s in stacks]

    # Only the frequencies below the *native* Nyquist are honest: everything
    # above it is aliasing, and its phase turns with the sampling offset rather
    # than with the ground, which is exactly what the alignment must not chase.
    cutoff = 0.16 / max(float(scale), 1.0)

    shifts: list[tuple[float, float]] = []
    for i, guide in enumerate(guides):
        if i == reference or not register:
            shifts.append((0.0, 0.0))
        else:
            shifts.append(estimate_shift(guides[reference], guide,
                                         upsample=upsample, cutoff=cutoff))

    sigma = max(0.45 * float(scale), 0.5)
    fused: dict[str, MaskedArray] = {}
    stacked: dict[str, MaskedArray] = {}
    coverage = None

    for key in keys:
        aligned = np.ma.stack([
            shift_band(stack[key], dy, dx) for stack, (dy, dx) in zip(stacks, shifts)
        ])
        merged, kept = robust_mean(aligned, tolerance=tolerance)
        coverage = kept if coverage is None else coverage
        stacked[key] = merged

        # Holes are filled at the band mean, not at zero: deconvolving a cliff
        # rings, and the ringing would reach several pixels back into good data.
        data, _ = _fill(merged)
        if restore > 0:
            data = deconvolve(data, sigma=sigma, amount=float(restore))
        fused[key] = np.ma.masked_array(data, mask=np.ma.getmaskarray(merged))

    # Noise is judged on the stack before the deconvolution, against one date:
    # that is the question stacking answers. Sharpness is judged after it,
    # because that is the question the deconvolution answers. Both are judged
    # only where both pictures have data, and a few pixels back from the edge
    # of it -- otherwise the comparison is really a count of cloud holes, since
    # the fusion fills in holes that the one date it is measured against had.
    fused_guide, fused_valid = _fill(_guide(fused, guide_keys))
    stacked_guide, _ = _fill(_guide(stacked, guide_keys))
    ref_guide, ref_valid = _fill(guides[reference])
    where = ref_valid & fused_valid
    if not where.all():
        where = ndimage.binary_erosion(where, iterations=2 * int(scale) + 2)
    if not where.any():
        where = ref_valid & fused_valid

    window = 2 * int(scale) + 1
    ref_sharp = _sharpness(ref_guide, float(scale), where)
    out_sharp = _sharpness(fused_guide, float(scale), where)
    ref_noise = _noise(ref_guide, window, where)
    out_noise = _noise(stacked_guide, window, where)
    # Below this the reference simply has no measurable noise to remove, and a
    # ratio against it would be an artefact of floating point, not a result.
    measurable = ref_noise > max(1e-6, 1e-4 * float(np.ptp(ref_guide)))

    report = {
        "scale": int(scale),
        "scenes": len(stacks),
        "frames_wanted": frames_wanted(scale),
        "well_supported": len(stacks) >= frames_wanted(scale),
        "registered": bool(register),
        "sub_pixel_dates": sum(
            1 for i, (dy, dx) in enumerate(shifts)
            if i != reference and max(abs(dy), abs(dx)) > 1e-3),
        "mean_shift_px": round(float(np.mean([np.hypot(dy, dx) for dy, dx in shifts])), 3),
        "max_shift_px": round(float(np.max([np.hypot(dy, dx) for dy, dx in shifts])), 3),
        "shifts": [
            {
                "date": (dates[i] if dates and i < len(dates) else None),
                "dy": round(dy, 3), "dx": round(dx, 3),
                "reference": i == reference,
            }
            for i, (dy, dx) in enumerate(shifts)
        ],
        "samples_per_pixel": (round(float(np.mean(coverage)), 2)
                              if coverage is not None else 0.0),
        "restore": round(float(restore), 2),
        "psf_sigma_px": round(sigma, 2),
        "sharpness_gain_pct": (round((out_sharp / ref_sharp - 1.0) * 100, 1)
                               if ref_sharp > 1e-9 else 0.0),
        "noise_drop_pct": (round((1.0 - out_noise / ref_noise) * 100, 1)
                           if measurable else None),
    }
    return fused, report
