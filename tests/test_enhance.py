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

from backend import composite, config, enhance, service  # noqa: E402

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
        assert spec["sat"], f"{name} belongs to no satellite"
        for _key in spec["sat"]:
            assert _key in config.SATELLITES, f"{name} claims unknown satellite {_key}"
        assert spec["res"] in (10, 20, 30, 60, 100)
        if spec.get("derive"):
            # A derived band is arithmetic on real ones, so it has no asset of
            # its own -- but the bands it is made of must exist and be its own
            # satellite's.
            for part in spec["derive"]:
                assert config.BANDS[part]["sat"] == spec["sat"]
            continue
        assert spec["asset"], f"{name} has no asset name"
        if "sentinel-2" in spec["sat"]:
            assert spec["s2"].startswith("B"), f"{name} has no Sentinel-2 band number"
        # A band on more than one satellite must say what it is called on each
        # of them, or the reader will go looking for the wrong asset.
        for _key in spec["sat"]:
            named = spec.get("assets", {}).get(_key, spec["asset"])
            assert named, f"{name} has no asset name on {_key}"


def test_an_unknown_visualisation_is_rejected():
    scene = {"id": "demo-2024-06-01-1-0", "date": "2024-06-01", "cloud": 0.0, "demo": True}
    request = {"aoi": {"bbox": [-0.1, 51.4, 0.0, 51.5]}, "scene": scene, "size": 64}
    with pytest.raises(service.RenderError, match="Unknown composite"):
        service.render({**request, "preset": "infrared_sausages"})
    with pytest.raises(service.RenderError, match="Unknown index"):
        service.render({**request, "mode": "index", "index": "ndxyz"})


# ── Tone mapping ───────────────────────────────────────────────


def _hsv(rgb):
    import colorsys

    return colorsys.rgb_to_hsv(*[float(v) for v in rgb])


def test_the_tone_curve_does_not_bend_the_colour():
    """Ground of one colour must not change hue as it gets brighter.

    Curving each channel on its own is what makes bright surfaces drift --
    the three climb at different rates, so the ratios between them, which are
    the colour, come out somewhere else. Warm sand turns orange, then yellow,
    then white. Curving the pixel once instead leaves the ratios alone.
    """
    ground = np.array([0.62, 0.42, 0.26], dtype="float32")
    true_hue, true_sat, _ = _hsv(ground)

    for exposure in (0.6, 1.2, 1.6, 2.1, 2.8):
        pixel = (ground * exposure).astype("float32")[None, None, :]
        hue, sat, val = _hsv(composite.tone_map(pixel, gamma=1.15, knee=0.72)[0, 0])
        assert hue == pytest.approx(true_hue, abs=1e-3), f"hue moved at {exposure}x"
        assert sat == pytest.approx(true_sat, abs=1e-3), f"saturation moved at {exposure}x"
        assert 0.0 <= val <= 1.0


def test_the_tone_curve_still_orders_bright_ground_by_brightness():
    """Preserving colour must not cost the ability to tell bright from brighter."""
    dim = composite.tone_map(np.float32([[[0.9, 0.6, 0.4]]]), 1.15, 0.72)[0, 0]
    bright = composite.tone_map(np.float32([[[1.4, 0.95, 0.62]]]), 1.15, 0.72)[0, 0]
    assert bright.max() > dim.max()
    assert bright.max() <= 1.0


def test_nothing_ever_needs_clipping():
    """The brightest channel is the one curved, so the result cannot overflow."""
    rng = np.random.default_rng(3)
    wild = (rng.random((32, 32, 3)) * 4).astype("float32")
    out = composite.tone_map(wild, gamma=1.3, knee=0.7)
    assert out.max() <= 1.0 and out.min() >= 0.0
    # And the hue survives even the wildest of it.
    for a, b in zip(wild.reshape(-1, 3)[:40], out.reshape(-1, 3)[:40]):
        if a.max() > 1e-3:
            assert _hsv(a)[0] == pytest.approx(_hsv(b)[0], abs=1e-3)


# ── Enhancement on ground the satellite never saw ──────────────
#
# Masked cloud and everything outside the swath arrive here as exactly black.
# Two of these operations then go wrong on it.
#
# Sharpening is the big one. It adds the difference between a pixel and the
# blur around it, and beside a black hole that difference is large and
# positive -- so a bright fringe traces the outline of every cloud that was
# supposedly removed. Measured: 0.690 where the ground is 0.600.
#
# CLAHE is the small one. Black puts a spike at the bottom of the histogram of
# every tile a cloud touches, but contrast limiting already clips most of that
# away, so the residue is a few percent of tone at worst. Worth fixing, not
# worth a story.
#
# Grey-world white balance, measured rather than assumed, turns out to need no
# mask at all -- twice over. A black hole scales all three channel means by the
# same factor and the ratio the correction is built from cancels it; and once
# the hole is filled with the scene average it cancels again, because adding
# copies of a mean to a set does not move its mean. The tests below record
# that, because it is the obvious-looking bug that is not there.


def _scene(fraction_masked: float, shade: float = 0.45):
    """A flat scene with a rectangular hole punched in it."""
    rgb = np.full((64, 64, 3), shade, dtype="float32")
    valid = np.ones((64, 64), dtype=bool)
    cut = int(64 * fraction_masked)
    if cut:
        valid[:cut, :] = False
        rgb[:cut, :] = 0.0
    return rgb, valid


def _patchy():
    """Half masked, and the visible half is not one flat colour."""
    rgb = np.zeros((64, 64, 3), dtype="float32")
    rgb[32:48] = [0.55, 0.40, 0.30]
    rgb[48:] = [0.28, 0.34, 0.22]
    valid = np.zeros((64, 64), dtype=bool)
    valid[32:] = True
    return rgb, valid


def test_white_balance_is_unmoved_by_how_much_was_masked():
    """The correction depends on the light, not on the weather.

    Two pictures of the same ground, one with twice the cloud cut out of it,
    get the same correction -- with no mask needed to arrange that.
    """
    rgb, valid = _patchy()
    more_cloud = rgb.copy()
    more_cloud[32:40] = 0.0

    a = enhance.white_balance(enhance.fill_invalid(rgb, valid), 1.0)
    b_valid = valid.copy()
    b_valid[32:40] = False
    b = enhance.white_balance(enhance.fill_invalid(more_cloud, b_valid), 1.0)
    assert np.allclose(a[60], b[60], atol=0.02)


def test_filling_the_hole_does_not_move_the_white_balance_either():
    """Adding copies of a mean to a set does not move its mean, so the fill is
    invisible to this too. Recorded so nobody re-adds a mask it cannot use."""
    rgb, valid = _patchy()
    filled = enhance.fill_invalid(rgb, valid)
    # The same ground with nothing masked at all, as its own picture.
    only = rgb[32:].copy()
    assert np.allclose(
        enhance.white_balance(filled, 1.0)[60],
        enhance.white_balance(only, 1.0)[28], atol=1e-4)


def _textured(masked_rows: int):
    """Ground with a real range of brightness in it, and a hole above it.

    The texture is the point. A flat patch has a one-spike histogram whichever
    way you count it, so it cannot tell a masked CLAHE from an unmasked one --
    which is exactly the mistake the first version of this test made.
    """
    ramp = np.linspace(0.15, 0.85, 64, dtype="float32")
    rgb = np.repeat(ramp[None, :, None], 64, axis=0).repeat(3, axis=2).copy()
    valid = np.ones((64, 64), dtype=bool)
    if masked_rows:
        valid[:masked_rows] = False
        rgb[:masked_rows] = 0.0
    return rgb, valid


def test_clahe_does_not_equalise_against_a_hole():
    """A tile half full of masked cloud must map its real pixels the way a tile
    with no cloud in it does.

    Black counted as data puts a spike at the bottom of the histogram, and the
    whole mapping above it shifts -- which is the pale blotch that appears
    exactly where a cloud was taken out.
    """
    clear, clear_valid = _textured(0)
    cloudy, cloudy_valid = _textured(32)

    a = enhance.apply_clahe_rgb(clear, clip_limit=2.0, tiles=4, valid=clear_valid)
    b = enhance.apply_clahe_rgb(cloudy, clip_limit=2.0, tiles=4, valid=cloudy_valid)
    # Row 60 is well inside the valid half of both.
    assert np.allclose(a[60], b[60], atol=0.03)


def test_counting_the_hole_shifts_the_tone_a_little():
    """Small, and measured rather than assumed.

    The hole has to cover most of a tile before it shows at all -- 44 of the
    64 rows here, against tiles 16 rows deep -- because contrast limiting
    clips the spike that black would otherwise put in the histogram.
    """
    cloudy, cloudy_valid = _textured(44)
    with_mask = enhance.apply_clahe_rgb(cloudy, clip_limit=2.0, tiles=4, valid=cloudy_valid)
    without = enhance.apply_clahe_rgb(cloudy, clip_limit=2.0, tiles=4)
    assert np.abs(with_mask[48:] - without[48:]).max() > 0.02


def test_a_tile_that_is_entirely_cloud_is_left_alone():
    """There is no histogram to build from, and equalising against nothing
    would produce a mapping out of thin air."""
    rgb = np.full((32, 32, 3), 0.4, dtype="float32")
    valid = np.zeros((32, 32), dtype=bool)
    out = enhance.apply_clahe_rgb(rgb, clip_limit=2.0, tiles=4, valid=valid)
    assert np.isfinite(out).all()


def test_masked_ground_is_filled_before_anything_blurs_it():
    """Sharpening spreads a pixel into its neighbours, so a black hole draws a
    dark outline of the removed cloud onto the real ground beside it."""
    rgb, valid = _scene(0.5, shade=0.6)
    filled = enhance.fill_invalid(rgb, valid)
    assert filled[10, 10, 0] == pytest.approx(0.6, abs=1e-4)
    # And the ground itself is untouched.
    assert filled[60, 10, 0] == pytest.approx(0.6, abs=1e-4)


def test_the_bright_fringe_is_what_the_fill_removes():
    """The measurement, not the impression: on a flat 0.600 field the ground
    next to a masked cloud is sharpened up to 0.690, and filled it stays put."""
    rgb, valid = _scene(0.5, shade=0.6)
    unfilled = enhance.unsharp(rgb, amount=0.9, radius=2.0)
    filled = enhance.unsharp(enhance.fill_invalid(rgb, valid), amount=0.9, radius=2.0)
    near = slice(32, 40)           # the first rows of real ground
    assert np.abs(unfilled[near, :, 0] - 0.6).max() > 0.08
    assert np.abs(filled[near, :, 0] - 0.6).max() < 0.005


def test_a_picture_with_nothing_masked_is_unchanged_by_the_fill():
    rgb, valid = _scene(0.0)
    assert enhance.fill_invalid(rgb, valid) is rgb
