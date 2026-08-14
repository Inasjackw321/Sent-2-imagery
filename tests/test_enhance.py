"""The image-quality tools: compositing dates and cleaning up the result.

These work on arrays rather than on files, so they are tested directly -- a
median composite either fills the gaps or it does not, and adaptive contrast
either equalises within a tile or it does not.

    python -m pytest tests/test_enhance.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config, enhance, service  # noqa: E402

# ── Compositing ────────────────────────────────────────────────


def _scene_with_hole(value: float, hole: slice) -> dict:
    data = np.full((32, 32), value, dtype="float32")
    mask = np.zeros((32, 32), dtype=bool)
    mask[hole] = True
    return {"red": np.ma.masked_array(data, mask=mask)}


def test_median_composite_fills_the_gaps():
    """Clouds in different places on different dates cancel out."""
    stacks = [
        _scene_with_hole(0.2, slice(0, 10)),
        _scene_with_hole(0.2, slice(10, 20)),
        _scene_with_hole(0.2, slice(20, 32)),
    ]
    merged = enhance.composite(stacks, "median")
    assert not np.ma.getmaskarray(merged["red"]).any()
    assert merged["red"].mean() == pytest.approx(0.2, abs=1e-6)


def test_composite_report_counts_the_rescue():
    stacks = [_scene_with_hole(0.2, slice(0, 16)), _scene_with_hole(0.2, slice(16, 32))]
    report = enhance.composite_report(stacks, "red")
    assert report["scenes"] == 2
    assert report["best_single_pct"] == pytest.approx(50.0, abs=0.1)
    assert report["combined_pct"] == pytest.approx(100.0, abs=0.1)


def test_first_valid_composite_keeps_the_earliest_clear_pixel():
    stacks = [_scene_with_hole(0.1, slice(0, 16)), _scene_with_hole(0.9, slice(0, 0))]
    merged = enhance.composite(stacks, "first")
    assert merged["red"][20, 0] == pytest.approx(0.1)   # kept from the first scene
    assert merged["red"][0, 0] == pytest.approx(0.9)    # gap filled from the second


def test_single_scene_composite_is_returned_untouched():
    stack = _scene_with_hole(0.3, slice(0, 4))
    assert enhance.composite([stack]) is stack


# ── Image quality ──────────────────────────────────────────────


def test_dark_object_subtraction_removes_the_haze_floor():
    """Haze lifts the whole band; subtracting the floor should bring it back."""
    clean = np.linspace(0.0, 0.5, 4096).reshape(64, 64).astype("float32")
    hazy = {"blue": np.ma.masked_array(clean + 0.08)}
    fixed = enhance.dark_object_subtraction(hazy, percentile=1.0, strength=1.0)
    assert fixed["blue"].min() == pytest.approx(0.0, abs=0.005)
    assert float(fixed["blue"].std()) == pytest.approx(float(clean.std()), rel=0.02)


def test_haze_removal_never_goes_negative():
    band = {"blue": np.ma.masked_array(np.full((16, 16), 0.05, dtype="float32"))}
    out = enhance.dark_object_subtraction(band, strength=1.0)
    assert out["blue"].min() >= 0.0


def test_clahe_lifts_a_low_contrast_image():
    rng = np.random.default_rng(0)
    flat = (0.45 + rng.normal(0, 0.02, (128, 128))).clip(0, 1).astype("float32")
    out = enhance.clahe(flat, clip_limit=3.0, tiles=8)
    assert out.std() > flat.std() * 3
    assert 0.0 <= out.min() and out.max() <= 1.0


def test_clahe_is_locally_adaptive():
    """A dark half and a bright half should each be stretched on their own terms.

    The input has to be peaked rather than a ramp: equalising an already-uniform
    histogram is meant to be a no-op.
    """
    rng = np.random.default_rng(7)
    image = np.zeros((128, 128), dtype="float32")
    image[:, :64] = np.clip(rng.normal(0.15, 0.02, (128, 64)), 0, 1)
    image[:, 64:] = np.clip(rng.normal(0.85, 0.02, (128, 64)), 0, 1)
    out = enhance.clahe(image, clip_limit=4.0, tiles=8)
    # Judge single tiles: a global std would just measure the dark/bright step.
    for region in (slice(0, 16), slice(80, 96)):
        assert out[16:32, region].std() > image[16:32, region].std() * 2


def test_clahe_off_is_a_no_op():
    image = np.full((32, 32), 0.5, dtype="float32")
    assert np.array_equal(enhance.clahe(image, clip_limit=0), image)


def test_clahe_rgb_keeps_hue():
    rng = np.random.default_rng(1)
    rgb = np.clip(rng.normal(0.4, 0.05, (64, 64, 3)), 0, 1).astype("float32")
    rgb[..., 0] *= 1.4                                  # a red cast
    out = enhance.apply_clahe_rgb(rgb, clip_limit=3.0)
    before = (rgb[..., 0] / np.maximum(rgb[..., 1], 1e-6)).mean()
    after = (out[..., 0] / np.maximum(out[..., 1], 1e-6)).mean()
    assert after == pytest.approx(before, rel=0.05)


def test_white_balance_neutralises_a_colour_cast():
    rgb = np.zeros((32, 32, 3), dtype="float32")
    rgb[..., 0], rgb[..., 1], rgb[..., 2] = 0.6, 0.4, 0.2
    out = enhance.white_balance(rgb, strength=1.0)
    assert out[..., 0].mean() == pytest.approx(out[..., 2].mean(), abs=0.02)


def test_unsharp_sharpens_an_edge_without_clipping():
    image = np.zeros((32, 32, 3), dtype="float32")
    image[:, 16:] = 0.6
    out = enhance.unsharp(image, amount=0.8)
    assert out[:, 15].mean() < image[:, 15].mean() + 1e-6      # darker just before
    assert out[:, 16].mean() > image[:, 16].mean()             # brighter just after
    assert out.min() >= 0.0 and out.max() <= 1.0


def test_denoise_removes_speckle():
    rng = np.random.default_rng(2)
    clean = np.full((64, 64), 0.4, dtype="float32")
    noisy = clean.copy()
    salt = rng.random((64, 64)) > 0.9
    noisy[salt] = 1.0
    out = enhance.denoise({"red": np.ma.masked_array(noisy)}, strength=1.0)
    assert float(out["red"].std()) < float(noisy.std()) / 3


def test_enhancements_are_reported_in_metadata():
    scene = {"id": "demo-2024-06-01-1-0", "date": "2024-06-01",
             "cloud": 0.0, "demo": True}
    result = service.render({
        "aoi": {"bbox": [-0.1, 51.4, 0.0, 51.5]}, "scene": scene, "size": 128,
        "preset": "true_color", "haze_removal": 1.0, "adaptive_contrast": 2.0,
        "sharpen": 0.3, "white_balance": 0.5,
    })
    applied = result["meta"]["enhancements"]
    assert "haze removal" in applied
    assert "adaptive contrast" in applied
    assert "detail" in applied
    assert "white balance" in applied


# ── What Sentinel-2 offers ─────────────────────────────────────


def test_every_visualisation_uses_bands_the_satellite_carries():
    """Nothing may be offered that cannot actually be rendered."""
    for key, spec in config.COMPOSITES.items():
        assert len(spec["bands"]) == 3, f"{key} needs exactly three channels"
        for band in spec["bands"]:
            assert band in config.BANDS, f"{key} wants a missing band: {band}"
    for key, spec in config.INDICES.items():
        assert spec["colormap"] in config.COLORMAPS
        for band in spec["bands"]:
            assert band in config.BANDS, f"{key} wants a missing band: {band}"


def test_every_band_knows_where_to_find_itself():
    for name, spec in config.BANDS.items():
        assert spec["asset"], f"{name} has no asset name"
        assert spec["s2"].startswith("B"), f"{name} has no Sentinel-2 band number"
        assert spec["res"] in (10, 20, 60)


def test_an_unknown_visualisation_is_rejected():
    scene = {"id": "demo-2024-06-01-1-0", "date": "2024-06-01", "cloud": 0.0, "demo": True}
    request = {"aoi": {"bbox": [-0.1, 51.4, 0.0, 51.5]}, "scene": scene, "size": 64}
    with pytest.raises(service.RenderError, match="Unknown composite"):
        service.render({**request, "preset": "infrared_sausages"})
    with pytest.raises(service.RenderError, match="Unknown index"):
        service.render({**request, "mode": "index", "index": "ndxyz"})
