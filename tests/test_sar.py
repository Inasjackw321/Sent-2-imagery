"""Tests for what a Sentinel-1 pass is, beyond a date.

An optical scene is nearly self-describing. A radar one is not: two passes
over the same field a day apart can be unrecognisable as the same place, and
nothing in the picture says why. The parameters that explain it were being
fetched and shown nowhere, and one of them -- which pair of polarisations the
instrument was transmitting in -- decides whether the scene can make the
picture at all.

The failure being fixed: asking for radar colour over sea ice came back
"Scene S1A_... has no vv asset", which reads as this app being broken rather
than as the satellite having been in a different mode.
"""

from __future__ import annotations

import pytest

from backend import config, sar

LAND = {"satellite": "sentinel-1", "polarisations": ["VV", "VH"],
        "mode": "IW", "product": "GRD", "orbit": 36,
        "orbit_state": "descending", "assets": {"vv": "a", "vh": "b"}}

ICE = {"satellite": "sentinel-1", "polarisations": ["HH", "HV"],
       "mode": "EW", "product": "GRD", "orbit": 14,
       "orbit_state": "ascending", "assets": {"hh": "a", "hv": "b"}}

OPTICAL = {"satellite": "sentinel-2", "date": "2026-09-01"}


class TestWhichPolarisationsAPassCarries:
    def test_what_the_catalogue_says(self):
        assert sar.polarisations(LAND) == ["VV", "VH"]
        assert sar.polarisations(ICE) == ["HH", "HV"]

    def test_the_assets_where_it_does_not_say(self):
        # A scene with thin properties and real assets is perfectly usable,
        # and refusing it over missing paperwork would refuse the picture.
        assert sar.polarisations(
            {"assets": {"VV": "a", "VH": "b", "thumbnail": "c"}}) == ["VV", "VH"]

    def test_they_come_out_in_a_fixed_order(self):
        # So the label reads "VV+VH" rather than whichever way round the
        # catalogue happened to list them.
        assert sar.polarisations(
            {"polarisations": ["VH", "VV"]}) == ["VV", "VH"]

    def test_a_single_polarisation_pass_says_so(self):
        # Real: some GRD products over open ocean carry VV alone.
        assert sar.polarisations({"polarisations": ["VV"]}) == ["VV"]

    def test_nothing_known_is_nothing_claimed(self):
        assert sar.polarisations({}) == []
        assert sar.polarisations({"assets": {"thumbnail": "x"}}) == []

    def test_the_pair_is_how_it_is_written(self):
        assert sar.pair_of(["VV", "VH"]) == "VV+VH"
        assert sar.pair_of(["HH"]) == "HH"
        assert sar.pair_of([]) == ""


class TestWhichPictureAPassCanMake:
    def test_a_land_pass_can_make_the_vv_pictures(self):
        for name in ("radar_color", "radar_grey", "radar_water",
                     "radar_interference"):
            assert sar.can_make(LAND, name), name

    def test_and_cannot_make_the_hh_ones(self):
        for name in ("radar_color_hh", "radar_grey_hh"):
            assert not sar.can_make(LAND, name), name

    def test_an_ice_pass_is_the_other_way_round(self):
        assert sar.can_make(ICE, "radar_color_hh")
        assert not sar.can_make(ICE, "radar_color")

    def test_which_is_the_whole_point(self):
        """Before this, an HH pass could make nothing at all.

        Every radar composite needed vv, so the app had no picture to offer
        for a pass over sea ice -- and said so as a missing asset four layers
        down rather than as a mode.
        """
        assert sar.composites_for(ICE)

    def test_a_vv_only_pass_can_still_make_the_plain_one(self):
        # It has no VH, so no false colour -- but backscatter on its own is a
        # picture, and refusing the whole pass would be refusing the ground.
        only = {"satellite": "sentinel-1", "polarisations": ["VV"]}
        assert sar.can_make(only, "radar_grey")
        assert not sar.can_make(only, "radar_color")

    def test_a_derived_band_needs_both_of_its_parts(self):
        # radar_color's third channel is VV minus VH, so a VV-only pass
        # cannot make it even though it has the first two channels' band.
        #
        # Asked of the derived band ON ITS OWN, because in the full list the
        # plain vh band demands VH as well and would answer for it -- a check
        # that looked right and proved nothing about derived bands.
        assert sar.missing_for({"polarisations": ["VV"]}, ["vvvh"]) == ["VH"]
        assert sar.missing_for({"polarisations": ["HH"]}, ["hhhv"]) == ["HV"]
        assert sar.missing_for({"polarisations": ["VV", "VH"]}, ["vvvh"]) == []

    def test_a_pass_nobody_knows_anything_about_is_not_refused(self):
        # Not the same as "it has none". A scene whose properties and assets
        # are both absent has not been fetched yet, and refusing here would
        # empty the picker for every scene in a list.
        assert sar.missing_for({}, ["vv", "vh"]) == []
        assert sar.can_make({}, "radar_color")

    def test_a_composite_nobody_has_cannot_be_made(self):
        assert not sar.can_make(LAND, "no_such_picture")

    def test_the_missing_ones_are_named_once_each(self):
        assert sar.missing_for(ICE, ["vv", "vh", "vv"]) == ["VV", "VH"]


class TestWhatAPassOpensOn:
    def test_a_land_pass_opens_on_the_usual_default(self):
        assert sar.default_composite(LAND) == \
            config.satellite("sentinel-1")["default_composite"]

    def test_and_does_so_because_it_is_the_default_not_because_it_sorts_first(
            self, monkeypatch):
        """The declared default beats whatever the table happens to list first.

        Today they are the same entry, so a version that ignored the default
        entirely would pass every other test here. Reordering the table is
        what tells the two apart -- and somebody reordering it for an
        unrelated reason should not quietly change which picture opens.
        """
        usual = config.satellite("sentinel-1")["default_composite"]
        shuffled = {k: v for k, v in config.COMPOSITES.items() if k != usual}
        shuffled[usual] = config.COMPOSITES[usual]
        monkeypatch.setattr(config, "COMPOSITES", shuffled)
        assert list(shuffled)[0] != usual
        assert sar.default_composite(LAND) == usual

    def test_an_ice_pass_opens_on_one_it_can_actually_draw(self):
        chosen = sar.default_composite(ICE)
        assert sar.can_make(ICE, chosen)
        assert chosen != config.satellite("sentinel-1")["default_composite"]

    def test_every_composite_offered_is_one_that_can_be_drawn(self):
        for scene in (LAND, ICE, {"satellite": "sentinel-1",
                                  "polarisations": ["VV"]}):
            for name in sar.composites_for(scene):
                assert sar.can_make(scene, name), name


class TestWhetherTwoPassesMayBeAveraged:
    """Merging dates over radar speckle-averages several passes.

    That is real and useful when the geometry matches. Averaging an ascending
    pass with a descending one averages two different lightings of the same
    hill: the result is not a cleaner picture of the ground, it is a blur of
    two.
    """

    def other_way(self, **over):
        return {**LAND, "orbit_state": "ascending", **over}

    def test_the_same_track_and_direction_is_fine(self):
        assert sar.comparable(LAND, {**LAND, "date": "2026-09-09"})
        assert sar.merge_trouble([LAND, {**LAND}]) == ""

    def test_opposite_directions_are_not(self):
        assert not sar.comparable(LAND, self.other_way())
        said = sar.merge_trouble([LAND, self.other_way()])
        assert "ascending and descending" in said

    def test_nor_are_different_repeat_tracks(self):
        assert not sar.comparable(LAND, {**LAND, "orbit": 87})
        said = sar.merge_trouble([LAND, {**LAND, "orbit": 87}])
        assert "repeat tracks" in said

    def test_the_direction_is_the_one_reported_when_both_differ(self):
        # It is the bigger statement: two tracks the same way round are at
        # least lit from the same side.
        said = sar.merge_trouble([LAND, self.other_way(orbit=87)])
        assert "ascending and descending" in said
        assert "repeat tracks" not in said

    def test_mixing_polarisation_pairs_is_said_too(self):
        said = sar.merge_trouble([LAND, {**ICE, "orbit": 36,
                                         "orbit_state": "descending"}])
        assert "polarisation pairs" in said

    def test_what_is_not_known_is_not_a_mismatch(self):
        # Refusing on an absent property would refuse every merge against a
        # catalogue with thin metadata.
        thin = {"satellite": "sentinel-1"}
        assert sar.comparable(LAND, thin)
        assert sar.merge_trouble([LAND, thin]) == ""

    def test_one_pass_is_not_a_merge(self):
        assert sar.merge_trouble([LAND]) == ""
        assert sar.merge_trouble([]) == ""

    def test_optical_scenes_are_not_measured_against_this_at_all(self):
        # Two that genuinely DIFFER. Sentinel-2 carries a relative orbit and
        # an orbit state too, so comparing identical optical scenes proves
        # nothing -- the check has to be that they are not looked at.
        one = {**OPTICAL, "orbit": 108, "orbit_state": "descending"}
        two = {**OPTICAL, "orbit": 22, "orbit_state": "ascending"}
        assert sar.merge_trouble([one, two]) == ""


class TestWhatIsSaidAboutAPass:
    def test_the_four_facts_in_order(self):
        assert sar.label(LAND) == "Descending · track 36 · IW · VV+VH"

    def test_an_ice_pass_reads_as_one(self):
        assert sar.label(ICE) == "Ascending · track 14 · EW · HH+HV"

    def test_an_optical_scene_gets_none_of_it(self):
        assert sar.label(OPTICAL) == ""
        assert sar.describe(OPTICAL) == {}

    def test_a_pass_missing_a_field_says_the_rest(self):
        assert sar.label({**LAND, "orbit": None}) == "Descending · IW · VV+VH"

    def test_the_description_explains_the_mode_rather_than_naming_it(self):
        # "EW" is not an explanation. A reader looking at a softer picture
        # needs to know it is a 40 m mode, not a bad render.
        said = sar.describe(ICE)
        assert said["mode_name"] == "Extra Wide"
        assert "40 m" in said["mode_about"]

    def test_and_says_which_way_the_radar_was_looking(self):
        said = sar.describe(LAND)
        assert said["pass_name"] == "Descending"
        assert "west" in said["pass_about"]

    def test_every_mode_the_instrument_has_is_explained(self):
        for mode in ("IW", "EW", "SM", "WV"):
            assert sar.MODES[mode]["name"] and sar.MODES[mode]["about"]

    def test_and_both_directions(self):
        for way in ("ascending", "descending"):
            assert sar.PASSES[way]["name"] and sar.PASSES[way]["about"]

    def test_the_description_carries_what_the_picker_needs(self):
        said = sar.describe(ICE)
        assert said["composites"] and said["default_composite"] in said["composites"]


class TestTheBandsAndCompositesThatShipped:
    def test_both_pairs_have_bands(self):
        for band in ("vv", "vh", "hh", "hv"):
            assert band in config.BANDS, band
            # Normalised to a list on load, so a band may be written with a
            # string and is read as one either way.
            said = config.BANDS[band]["sat"]
            assert list([said] if isinstance(said, str) else said) == \
                ["sentinel-1"], band

    def test_and_each_pair_has_its_ratio(self):
        assert config.BANDS["vvvh"]["derive"] == ("vv", "vh")
        assert config.BANDS["hhhv"]["derive"] == ("hh", "hv")

    def test_every_radar_band_says_which_polarisation_it_needs(self):
        # The table can_make reads. A band missing from it is a band that
        # silently needs nothing, so every picture would be offered.
        for band in ("vv", "vh", "hh", "hv"):
            assert config.BAND_POLARISATION[band] == band.upper()

    def test_every_radar_composite_is_makeable_by_some_real_pass(self):
        # A composite no pass can draw is a dead entry in the picker.
        for name, spec in config.COMPOSITES.items():
            if "sentinel-1" not in ([spec["sat"]] if isinstance(spec["sat"], str)
                                    else spec["sat"]):
                continue
            assert sar.can_make(LAND, name) or sar.can_make(ICE, name), name

    def test_the_hh_windows_are_not_the_vv_ones_renamed(self):
        # HH returns more than VV off the sea and HV rather less than VH. The
        # same numbers under different names would be a guess presented as a
        # calibration.
        assert config.COMPOSITES["radar_color_hh"]["windows"] != \
            config.COMPOSITES["radar_color"]["windows"]


class TestSarReachesThePicture:
    """The seams: the catalogue into a scene, and a scene into a render."""

    def test_a_scene_carries_its_sar_fields_out_of_the_catalogue(self):
        from backend import stac
        item = {
            "id": "S1A_IW_GRDH_TEST", "collection": "sentinel-1-grd",
            "properties": {
                "datetime": "2026-09-18T04:31:00Z",
                "sar:polarizations": ["VV", "VH"],
                "sar:instrument_mode": "IW",
                "sar:product_type": "GRD",
                "sat:relative_orbit": 36,
                "sat:orbit_state": "descending",
            },
            "assets": {"vv": {"href": "https://x/vv.tif"},
                       "vh": {"href": "https://x/vh.tif"}},
        }
        got = stac.scene_summary(item, "sentinel-1", "earth-search")
        assert got["polarisations"] == ["VV", "VH"]
        assert got["mode"] == "IW"
        assert got["product"] == "GRD"
        assert sar.label(got) == "Descending · track 36 · IW · VV+VH"

    def test_a_pass_with_no_sar_properties_is_still_a_scene(self):
        from backend import stac
        item = {
            "id": "S1A_THIN", "collection": "sentinel-1-grd",
            "properties": {"datetime": "2026-09-18T04:31:00Z"},
            "assets": {"vv": {"href": "https://x/vv.tif"}},
        }
        got = stac.scene_summary(item, "sentinel-1", "earth-search")
        assert got["polarisations"] == []
        # And the assets answer the question the properties did not.
        assert sar.polarisations(got) == ["VV"]

    def test_the_demo_pass_carries_the_same_fields_as_a_live_one(self):
        # Otherwise the offline build shows a different line under a scene,
        # and a screenshot from it explains a different app.
        from backend import stac
        # demo=True rather than the environment variable: config reads that
        # once at import, so setting it here would reach the catalogue.
        got = stac.search_scenes(
            {"type": "Polygon", "coordinates": [[[30, 50], [31, 50],
                                                 [31, 51], [30, 51],
                                                 [30, 50]]]},
            satellites="sentinel-1", limit=4, demo=True)["scenes"]
        assert got
        for scene in got:
            assert sar.label(scene), scene["id"]
        # And both directions turn up, or the warning about averaging two
        # lightings together is never exercised in the demo.
        assert len({s["orbit_state"] for s in got}) == 2

    def test_a_pass_that_cannot_make_the_picture_says_which_it_can(self):
        from backend import service
        with pytest.raises(service.RenderError) as caught:
            service.render({
                "aoi": {"type": "Polygon",
                        "coordinates": [[[30.4, 50.4], [30.5, 50.4],
                                         [30.5, 50.5], [30.4, 50.5],
                                         [30.4, 50.4]]]},
                "scene": ICE, "size": 128, "preset": "radar_color",
            })
        said = str(caught.value)
        assert "HH+HV" in said and "VV" in said
        assert "Radar colour (HH/HV)" in said
