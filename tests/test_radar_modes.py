"""The Sentinel-1 views added beyond the first three, and what each is for.

A radar picture is not a photograph and there is no single right way to draw
one. Each of these answers a question the others cannot:

  hard targets   where is the metal -- a view stretched so far that only
                 ships, corners and pylons survive it
  RVI            how much of the return came out of a volume rather than off
                 a surface, which separates canopy from bare soil
  texture        how much neighbouring pixels disagree, which is what a town
                 is and a field is not
  change         what is different between two passes, which is the question
                 radar is best at and the only one here that needs a pair

The tests are mostly about the failure modes. A view that shows everything
shows nothing; an index computed on the wrong scale is arithmetic; and a
difference between two passes of different geometry is a picture of the hills.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
import rasterio
from affine import Affine
from rasterio.crs import CRS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import composite, config, service  # noqa: E402
from backend.geo import normalise_aoi  # noqa: E402

UTM = CRS.from_epsg(32630)
RES = 20.0
SIZE = 256


def m(values):
    return np.ma.masked_array(values.astype("float32"),
                              mask=np.zeros(values.shape, dtype=bool))


def scene_bands(rng, what):
    """A synthetic dual-pol scene in the uncalibrated decibels this app reads.

    The separations are the real ones for C-band: water dark with its crossed
    channel on the instrument's noise floor, bare soil bright in VV and much
    dimmer in VH, canopy nearly as strong in both.
    """
    vv = np.empty((SIZE, SIZE), dtype="float64")
    vh = np.empty((SIZE, SIZE), dtype="float64")
    quarter = SIZE // 4
    for n, (co, cross) in enumerate(what):
        vv[n * quarter:(n + 1) * quarter] = co
        vh[n * quarter:(n + 1) * quarter] = cross
    vv += rng.normal(0, 1.0, vv.shape)
    vh += rng.normal(0, 1.0, vh.shape)
    return {"vv": m(vv), "vh": m(vh)}


GROUND = [(20.0, 15.0),    # water: crossed channel is the noise floor
          (38.0, 26.0),    # bare soil
          (40.0, 34.0),    # canopy
          (46.0, 36.0)]    # town


# ── Only the hard targets ──────────────────────────────────────


class TestHardTargets:
    def scene(self, ships=True):
        rng = np.random.default_rng(1)
        vv = np.full((SIZE, SIZE), 40.0)
        vv[:SIZE // 2] = 22.0                       # sea
        vv += rng.normal(0, 1.2, vv.shape)
        spots = [(20, 30), (35, 120), (60, 70), (70, 160)]
        if ships:
            for y, x in spots:
                vv[y:y + 2, x:x + 2] = 62.0         # metal on water
        return {"vv": m(vv), "vh": m(vv - 6.0)}, spots

    def drawn(self, bands, preset):
        rgb, _, _ = composite.render_composite(bands, preset, {})
        return rgb

    def test_ordinary_ground_goes_black(self):
        """The whole point. In the plain view a ship is a white dot among
        other white things; here there is nothing else left."""
        bands, _ = self.scene()
        plain = self.drawn(bands, "radar_grey")[SIZE // 2:].mean()
        hard = self.drawn(bands, "radar_ships")[SIZE // 2:].mean()
        assert hard < plain / 4, (plain, hard)
        assert hard < 40

    def test_every_ship_still_shows(self):
        bands, spots = self.scene()
        rgb = self.drawn(bands, "radar_ships")
        lit = rgb.mean(axis=2) > 200
        assert all(lit[y:y + 2, x:x + 2].any() for y, x in spots)

    def test_an_empty_sea_stays_empty(self):
        """A view that finds targets in a scene with none is a view that
        finds them anywhere."""
        bands, _ = self.scene(ships=False)
        rgb = self.drawn(bands, "radar_ships")
        assert (rgb.mean(axis=2) > 200).mean() < 0.0005

    def test_it_is_offered_for_radar_only(self):
        assert config.COMPOSITES["radar_ships"]["sat"] == ["sentinel-1"]


# ── The vegetation ratio ───────────────────────────────────────


class TestRVI:
    def index(self, bands):
        return composite.compute_index(bands, "rvi")

    def test_canopy_reads_high_and_bare_soil_low(self):
        got = self.index(scene_bands(np.random.default_rng(2), GROUND))
        q = SIZE // 4
        soil = float(got[q:2 * q].mean())
        canopy = float(got[2 * q:3 * q].mean())
        assert canopy > soil + 0.3, (soil, canopy)

    def test_it_stays_inside_its_own_range(self):
        got = self.index(scene_bands(np.random.default_rng(3), GROUND))
        assert got.min() >= 0.0 and got.max() <= 1.0

    def test_the_uncalibrated_scale_divides_out(self):
        """The gain this app cannot correct for is one multiplier on both
        channels, and a ratio of the two is blind to it. That is the reason
        an index like this is honest on an uncalibrated product where a
        backscatter figure is not."""
        bands = scene_bands(np.random.default_rng(4), GROUND)
        plain = self.index(bands)
        # Six decibels of gain on the whole scene: a different processing
        # baseline, or another catalogue's scaling.
        louder = {k: m(np.ma.filled(v) + 6.0) for k, v in bands.items()}
        assert np.allclose(np.ma.filled(plain), np.ma.filled(self.index(louder)),
                           atol=1e-6)

    def test_water_is_not_read_as_vegetation(self):
        """It reads high over water -- the crossed channel there is the
        instrument's noise floor rather than the surface -- so the one thing
        that must be true is that the app says so."""
        said = config.INDICES["rvi"]["hint"].lower()
        assert "water" in said and "noise" in said


# ── The texture ────────────────────────────────────────────────


class TestTexture:
    def scene(self):
        rng = np.random.default_rng(6)
        vv = np.empty((SIZE, SIZE))
        half = SIZE // 2
        vv[:half] = 34.0                             # a field: smooth
        vv[half:] = 40.0                             # a town: restless
        vv += rng.normal(0, 1.0, vv.shape)
        vv[half:] += rng.normal(0, 5.0, (SIZE - half, SIZE))
        return {"vv": m(vv)}

    def test_built_up_ground_reads_rougher_than_a_field(self):
        got = composite.compute_index(self.scene(), "radar_texture")
        half = SIZE // 2
        assert float(got[half:].mean()) > 2 * float(got[:half].mean())

    def test_brightness_alone_does_not_make_texture(self):
        """The reason this is not just another stretch of the same picture: a
        field ten decibels brighter is still a field."""
        rng = np.random.default_rng(7)
        flat = m(np.full((SIZE, SIZE), 30.0) + rng.normal(0, 1.0, (SIZE, SIZE)))
        bright = m(np.ma.filled(flat) + 10.0)
        quiet = composite.compute_index({"vv": flat}, "radar_texture")
        loud = composite.compute_index({"vv": bright}, "radar_texture")
        assert abs(float(quiet.mean()) - float(loud.mean())) < 0.05

    def test_the_edge_of_a_swath_is_not_drawn_as_texture(self):
        """Masked pixels held out of the arithmetic rather than filled with a
        zero, which would rule a wall of false roughness down the edge."""
        rng = np.random.default_rng(8)
        vv = np.full((SIZE, SIZE), 36.0) + rng.normal(0, 0.8, (SIZE, SIZE))
        mask = np.zeros(vv.shape, dtype=bool)
        mask[:, :SIZE // 2] = True                   # half the frame unseen
        band = np.ma.masked_array(vv.astype("float32"), mask=mask)
        got = composite.compute_index({"vv": band}, "radar_texture")
        seam = got[:, SIZE // 2 + 3:SIZE // 2 + 8]
        assert float(np.ma.filled(seam, 0).max()) < 4.0

    def test_nothing_is_claimed_where_almost_nothing_was_seen(self):
        """A lone pixel has no neighbourhood to measure a spread over. Left
        in, it comes back as a spread of zero -- which draws as "perfectly
        smooth ground" at exactly the places the swath is thinnest."""
        rng = np.random.default_rng(9)
        vv = np.full((SIZE, SIZE), 36.0) + rng.normal(0, 0.8, (SIZE, SIZE))
        mask = np.ones(vv.shape, dtype=bool)
        mask[40, 40] = False                         # one pixel, alone
        got = composite.compute_index(
            {"vv": np.ma.masked_array(vv.astype("float32"), mask=mask)},
            "radar_texture")
        assert np.ma.getmaskarray(got).all(), (
            "texture was claimed for a pixel with no neighbours to compare to")

    def test_but_an_ordinary_neighbourhood_is_kept(self):
        rng = np.random.default_rng(10)
        vv = np.full((SIZE, SIZE), 36.0) + rng.normal(0, 2.0, (SIZE, SIZE))
        got = composite.compute_index({"vv": m(vv)}, "radar_texture")
        assert got.count() == SIZE * SIZE


# ── What changed between two passes ────────────────────────────


def write_dn(path, values):
    with rasterio.open(path, "w", driver="GTiff", height=values.shape[0],
                       width=values.shape[1], count=1, dtype="uint16",
                       crs=UTM, nodata=0,
                       transform=Affine(RES, 0, 500000.0, 0, -RES, 5700000.0)) as dst:
        dst.write(values.astype("uint16"), 1)
    return str(path)


@pytest.fixture(scope="module")
def pair(tmp_path_factory):
    """Two passes over one place, with two things different between them."""
    root = tmp_path_factory.mktemp("change")
    rng = np.random.default_rng(3)
    before = np.full((SIZE, SIZE), 900.0) + rng.normal(0, 60, (SIZE, SIZE))
    after = before.copy()
    # Deliberately lopsided: more ground got brighter than got dimmer, so
    # the sign of the whole difference is a thing a test can read. With two
    # equal patches, a difference taken the wrong way round comes out
    # looking exactly the same.
    after[40:120, 40:120] *= 2.0        # +6 dB: rubble, new hard structures
    after[150:190, 150:190] *= 0.5      # -6 dB: under water
    names = ("vv", "vh")
    older = {n: write_dn(root / f"a_{n}.tif", before * (1 if n == "vv" else 0.5))
             for n in names}
    newer = {n: write_dn(root / f"b_{n}.tif", after * (1 if n == "vv" else 0.5))
             for n in names}
    from rasterio.warp import transform as warp

    lons, lats = warp(UTM, CRS.from_epsg(4326),
                      [500000.0, 500000.0 + SIZE * RES],
                      [5700000.0, 5700000.0 - SIZE * RES])
    area = normalise_aoi({"bbox": [lons[0] + 0.004, lats[1] + 0.004,
                                   lons[1] - 0.004, lats[0] - 0.004]})
    return older, newer, area


def a_pass(name, date, assets, **over):
    out = {"id": name, "satellite": "sentinel-1", "source": "earth-search",
           "date": date, "datetime": f"{date}T05:00:00Z", "assets": assets,
           "polarisations": ["VV", "VH"], "orbit": 36,
           "orbit_state": "descending", "mode": "IW", "demo": False}
    out.update(over)
    return out


def changed(pair, size=256, **over):
    older, newer, area = pair
    body = {"aoi": area, "mode": "change", "size": size,
            "scenes": [a_pass("older", "2026-09-01", older),
                       a_pass("newer", "2026-09-13", newer)]}
    body.update(over)
    return service.render(body)


class TestChangeBetweenTwoPasses:
    def test_the_ground_that_did_not_move_reads_as_no_change(self, pair):
        made = changed(pair)
        assert abs(made["meta"]["stats"]["median"]) < 0.5

    def test_what_got_brighter_and_dimmer_is_measured(self, pair):
        stats = changed(pair)["meta"]["stats"]
        # The two patches were doubled and halved in power, which is six
        # decibels each way.
        assert stats["max"] > 5.0
        assert stats["min"] < -5.0

    def test_it_says_which_dates_and_how_far_apart(self, pair):
        said = changed(pair)["meta"]["change"]
        assert said["older"] == "2026-09-01" and said["newer"] == "2026-09-13"
        assert said["days"] == 12
        assert said["band"] == "vv"

    def test_it_says_how_much_of_the_frame_moved(self, pair):
        # Eighty by eighty and forty by forty in a frame of 256: about eleven
        # per cent between them.
        moved = changed(pair)["meta"]["change"]["moved_pct"]
        assert 6.0 < moved < 20.0, moved

    def test_and_says_what_counted_as_moving(self, pair):
        """A bare percentage is unreadable. Half the ramp is the threshold,
        and it is fixed rather than taken off the scale slider -- somebody
        widening the scale is changing what they can see, not what moved."""
        said = changed(pair)["meta"]["change"]
        assert said["moved_above_db"] == config.CHANGE["range"][1] / 2
        wide = changed(pair, index_min=-30, index_max=30)["meta"]["change"]
        assert wide["moved_above_db"] == said["moved_above_db"]
        assert wide["moved_pct"] == said["moved_pct"]

    def test_brighter_than_before_reads_positive(self, pair):
        """The sign, which is the whole meaning of the picture. Inverted, a
        flood is drawn as construction and rubble as water -- and every test
        that only compares two orderings of the same pair passes anyway,
        because both come out wrong together."""
        stats = changed(pair)["meta"]["stats"]
        # More of this scene got brighter than got dimmer, so the average
        # difference is above zero and the far end of the spread is the
        # bright one.
        assert stats["mean"] > 0, stats
        assert abs(stats["max"]) >= abs(stats["min"]) - 1.0, stats

    def test_the_older_pass_is_subtracted_from_the_newer(self, pair):
        """Not the other way round. A difference with its sign inverted is a
        flood drawn as construction, which is worse than no picture."""
        older, newer, area = pair
        forwards = changed(pair)["meta"]["stats"]
        backwards = service.render({
            "aoi": area, "mode": "change", "size": 256,
            # Handed over in the wrong order on purpose: the render sorts
            # them by date, so this must come out identical.
            "scenes": [a_pass("newer", "2026-09-13", newer),
                       a_pass("older", "2026-09-01", older)],
        })["meta"]["stats"]
        assert forwards["median"] == backwards["median"]
        assert forwards["max"] == backwards["max"]

    def test_the_decibels_themselves_can_be_taken_away(self, pair):
        made = changed(pair, format="float_geotiff")
        assert made["media_type"] == "image/tiff"
        assert len(made["bytes"]) > 1000

    def test_it_carries_a_key_to_read_the_colours_by(self, pair):
        legend = changed(pair)["meta"]["legend"]
        assert legend["type"] == "continuous"
        assert legend["vmin"] < 0 < legend["vmax"]
        assert len(legend["stops"]) == 5


class TestWhatAChangeRefuses:
    """The one render in this app that says no.

    Everywhere else a mismatch is said out loud and the picture is drawn
    anyway, because somebody may want it and being told is enough. Not here:
    radar brightness depends on the angle the pulse arrives at, so a
    difference between two geometries is a picture of the terrain rather than
    of anything that happened -- every hillside a change, and nothing that
    changed standing out of them. There is no reading of that image worth
    having.
    """

    def test_one_pass_is_not_a_change(self, pair):
        older, _, area = pair
        with pytest.raises(service.RenderError, match="exactly two"):
            service.render({"aoi": area, "mode": "change", "size": 128,
                            "scenes": [a_pass("older", "2026-09-01", older)]})

    def test_three_passes_are_not_a_change_either(self, pair):
        older, newer, area = pair
        with pytest.raises(service.RenderError, match="exactly two"):
            service.render({"aoi": area, "mode": "change", "size": 128,
                            "scenes": [a_pass("a", "2026-09-01", older),
                                       a_pass("b", "2026-09-13", newer),
                                       a_pass("c", "2026-09-25", newer)]})

    def test_two_directions_are_refused(self, pair):
        older, newer, area = pair
        with pytest.raises(service.RenderError, match="cannot be subtracted"):
            service.render({"aoi": area, "mode": "change", "size": 128,
                            "scenes": [a_pass("a", "2026-09-01", older),
                                       a_pass("b", "2026-09-13", newer,
                                              orbit_state="ascending")]})

    def test_two_tracks_are_refused(self, pair):
        older, newer, area = pair
        with pytest.raises(service.RenderError, match="cannot be subtracted"):
            service.render({"aoi": area, "mode": "change", "size": 128,
                            "scenes": [a_pass("a", "2026-09-01", older),
                                       a_pass("b", "2026-09-13", newer, orbit=79)]})

    def test_the_refusal_names_both_passes(self, pair):
        older, newer, area = pair
        with pytest.raises(service.RenderError) as caught:
            service.render({"aoi": area, "mode": "change", "size": 128,
                            "scenes": [a_pass("a", "2026-09-01", older),
                                       a_pass("b", "2026-09-13", newer, orbit=79)]})
        said = str(caught.value)
        assert "track 36" in said and "track 79" in said

    def test_a_pass_whose_track_is_unknown_is_not_refused(self, pair):
        """Unknown is not the same as different, and refusing on it would
        refuse every change against a catalogue with thin properties."""
        older, newer, area = pair
        made = service.render({
            "aoi": area, "mode": "change", "size": 128,
            "scenes": [a_pass("a", "2026-09-01", older, orbit=None),
                       a_pass("b", "2026-09-13", newer)]})
        assert made["meta"]["mode"] == "change"

    def test_two_passes_with_no_channel_in_common(self, pair):
        older, newer, area = pair
        with pytest.raises(service.RenderError, match="no channel in common"):
            service.render({
                "aoi": area, "mode": "change", "size": 128,
                "scenes": [a_pass("a", "2026-09-01", older),
                           a_pass("b", "2026-09-13", newer,
                                  polarisations=["HH", "HV"])]})


class TestTheChangeSaysWhatItIs:
    def test_the_metadata_names_the_mode(self, pair):
        meta = changed(pair)["meta"]
        assert meta["mode"] == "change"
        assert meta["preset"] is None and meta["index"] is None
        assert "VV" in meta["label"]

    def test_both_passes_are_listed(self, pair):
        assert [s["date"] for s in changed(pair)["meta"]["scenes"]] == [
            "2026-09-01", "2026-09-13"]

    def test_there_is_nothing_to_warn_about_by_the_time_it_draws(self, pair):
        # Passes that would be worth warning about are refused outright, so
        # an empty warning here is a statement rather than an omission.
        assert changed(pair)["meta"]["sar_merge"] == ""

    def test_the_hint_explains_which_colour_is_which(self, pair):
        said = changed(pair)["meta"]["change"]["hint"].lower()
        assert "brighter" in said and "dimmer" in said
