"""Tests for the Sentinel-3 and Sentinel-5P layers.

These two cannot go through the imagery pipeline -- they are published as
NetCDF granules and it reads windows out of cloud-optimised GeoTIFFs -- so
they arrive as live WMS layers from EUMETSAT instead.

Which means they inherit the lightning layer's problem, and its answer. A
product whose name says "Sentinel-3" can perfectly well be a 2019
reprocessing, and drawn on a live map it is indistinguishable from today. So
liveness is decided by arithmetic on the timestamps the service itself
publishes, and these tests attack that: an archive named like the live
product, a layer with no time at all, a range that ended years ago.
"""

from __future__ import annotations

import datetime as dt

import pytest

from backend import copernicus

NOW = dt.datetime(2026, 9, 11, 12, 0, tzinfo=dt.timezone.utc)
WMS = "http://www.opengis.net/wms"


def capabilities(*layers: str) -> str:
    return (f'<WMS_Capabilities xmlns="{WMS}"><Capability>'
            f'<Layer><Title>root</Title>{"".join(layers)}</Layer>'
            f"</Capability></WMS_Capabilities>")


def layer(name: str, *, title: str | None = None, extent: str | None = None) -> str:
    dim = (f'<Dimension name="time" units="ISO8601">{extent}</Dimension>'
           if extent is not None else "")
    return (f"<Layer><Name>{name}</Name><Title>{title or name}</Title>"
            f"{dim}</Layer>")


def iso(hours_ago: float) -> str:
    return (NOW - dt.timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")


class TestWhichSatellite:
    def test_sentinel_3_by_any_of_its_instruments(self):
        for name in ("copernicus:sentinel-3_olci", "eo:OLCI_TrueColour",
                     "x:slstr_lst", "s3a_something"):
            assert copernicus.family_of(name, "") == "sentinel-3", name

    def test_sentinel_5p_by_its_instrument_too(self):
        for name in ("copernicus:sentinel-5p_no2", "x:TROPOMI_CH4", "s5p_co"):
            assert copernicus.family_of(name, "") == "sentinel-5p", name

    def test_the_title_is_read_as_well_as_the_name(self):
        assert copernicus.family_of("x:abc123", "Sentinel-3 OLCI") == "sentinel-3"

    def test_everything_else_belongs_to_neither(self):
        for name, title in [("mtg_fd:li_afa", "Accumulated Flash Area"),
                            ("copernicus:sst", "Sea Surface Temperature"),
                            ("x:sentinel-2_l2a", "Sentinel-2")]:
            assert copernicus.family_of(name, title) is None, name

    def test_sentinel_2_is_not_swept_in(self):
        # It has its own place in the imagery flow, where it can be searched
        # by date and rendered. Offering it here as well would be two
        # different answers to the same question.
        assert copernicus.family_of("x:sentinel-2_l2a", "Sentinel-2 L2A") is None


class TestLivenessIsNotAboutNames:
    def by_key(self, got):
        return {f["key"]: f for f in got["families"]}

    def test_a_current_layer_is_offered(self):
        got = copernicus.sort_layers(capabilities(
            layer("copernicus:s3_olci", title="Sentinel-3 OLCI",
                  extent=iso(4))), now=NOW)
        assert [e["id"] for e in self.by_key(got)["sentinel-3"]["layers"]] \
            == ["copernicus:s3_olci"]

    def test_a_reprocessing_named_like_the_live_product_is_refused(self):
        got = copernicus.sort_layers(capabilities(
            layer("copernicus:s3_olci", title="Sentinel-3 OLCI",
                  extent="2019-01-01T00:00:00Z/2019-12-31T00:00:00Z/P1D")), now=NOW)
        found = self.by_key(got)["sentinel-3"]
        assert found["layers"] == []
        assert found["stale"] == 1

    def test_a_layer_with_no_time_at_all_is_never_live(self):
        # No timestamp is not evidence of freshness.
        got = copernicus.sort_layers(
            capabilities(layer("copernicus:s3_olci", title="Sentinel-3")), now=NOW)
        assert self.by_key(got)["sentinel-3"]["layers"] == []

    def test_the_window_is_wider_than_the_lightning_layer(self):
        # These are polar orbiters: a given place is passed over every day or
        # two, so the newest frame is genuinely hours old when everything is
        # working. Six hours would call a healthy service stale.
        from backend import mtg
        assert copernicus.LIVE_WITHIN > mtg.LIVE_WITHIN

    def test_the_boundary_is_the_declared_window(self):
        hours = copernicus.LIVE_WITHIN.total_seconds() / 3600
        inside = copernicus.sort_layers(capabilities(
            layer("x:olci", extent=iso(hours - 1))), now=NOW)
        outside = copernicus.sort_layers(capabilities(
            layer("x:olci", extent=iso(hours + 1))), now=NOW)
        assert len(self.by_key(inside)["sentinel-3"]["layers"]) == 1
        assert len(self.by_key(outside)["sentinel-3"]["layers"]) == 0

    def test_a_group_layer_with_no_name_is_skipped(self):
        xml = (f'<WMS_Capabilities xmlns="{WMS}"><Capability><Layer>'
               f"<Title>Sentinel-3 products</Title>"
               f"{layer('x:olci', extent=iso(2))}"
               f"</Layer></Capability></WMS_Capabilities>")
        got = copernicus.sort_layers(xml, now=NOW)
        assert [e["id"] for e in self.by_key(got)["sentinel-3"]["layers"]] == ["x:olci"]

    def test_nothing_relevant_is_two_empty_families(self):
        got = copernicus.sort_layers(capabilities(
            layer("mtg_fd:li_afa", extent=iso(1))), now=NOW)
        assert all(f["layers"] == [] for f in got["families"])

    def test_rubbish_is_refused_clearly(self):
        with pytest.raises(copernicus.CopernicusError, match="would not parse"):
            copernicus.sort_layers("<not xml")


class TestAWeekOfWholeEarth:
    """One instant is one orbit strip.

    A polar orbiter photographs a few hundred kilometres at a time. Asked for
    a single moment, the layer draws that one strip and nothing else, which on
    a world map reads as broken rather than as a satellite that has not been
    over the rest of the world yet. A whole day is every pass that day, and at
    three hundred metres that is the globe.
    """

    def test_a_week_of_whole_days_is_offered(self):
        days = copernicus.days_offered(NOW)
        assert len(days) == copernicus.DAYS_OFFERED
        assert days == sorted(days)
        assert days[-1] == "2026-09-11"
        assert days[0] == "2026-09-05"

    def test_they_are_dates_rather_than_instants(self):
        # The whole point: a date spans a day's worth of passes. An instant
        # spans one.
        for day in copernicus.days_offered(NOW):
            assert len(day) == 10 and day.count("-") == 2

    def test_the_week_ends_at_the_newest_frame_not_at_the_clock(self):
        # A service that is two days behind should offer the two days before
        # that, not seven days ending today with two of them empty.
        days = copernicus.days_offered(NOW - dt.timedelta(days=2))
        assert days[-1] == "2026-09-09"

    def test_no_frames_means_no_days(self):
        assert copernicus.days_offered(None) == []

    def test_every_live_layer_carries_its_week(self):
        got = copernicus.sort_layers(capabilities(
            layer("x:olci", title="Sentinel-3 OLCI", extent=iso(3))), now=NOW)
        entry = [f for f in got["families"] if f["key"] == "sentinel-3"][0]["layers"][0]
        assert len(entry["days"]) == copernicus.DAYS_OFFERED
        assert entry["whole_week"] == f"{entry['days'][0]}/{entry['days'][-1]}"

    def test_the_whole_week_is_a_range_a_wms_understands(self):
        got = copernicus.demo()["families"][1]["layers"][0]
        start, _, end = got["whole_week"].partition("/")
        assert start < end
        assert dt.date.fromisoformat(start) < dt.date.fromisoformat(end)

    def test_the_demo_carries_it_too(self):
        for family in copernicus.demo()["families"]:
            if not family.get("spans_days"):
                continue
            for entry in family["layers"]:
                assert entry["days"] and entry["whole_week"], family["key"]

    def test_a_geostationary_satellite_is_offered_no_week(self):
        # It photographs the whole disc every ten minutes, so one frame is
        # already the whole picture. Compositing a day of them would blend a
        # hundred and forty frames of a moving sky into mud, and a day stepper
        # would be offering something meaningless.
        got = copernicus.sort_layers(capabilities(
            layer("mtg_fd:rgb_truecolour", title="MTG FCI True Colour",
                  extent=iso(0.5))), now=NOW)
        entry = [f for f in got["families"] if f["key"] == "mtg"][0]["layers"][0]
        assert entry["days"] == []
        assert entry["whole_week"] is None

    def test_the_demo_agrees_with_that(self):
        # Otherwise the offline build grows a day stepper the live one never
        # shows, which is the kind of difference nobody finds until a
        # screenshot from the demo is used to explain the real thing.
        for family in copernicus.demo()["families"]:
            if family.get("spans_days"):
                continue
            for entry in family["layers"]:
                assert entry["days"] == [] and entry["whole_week"] is None


class TestTheAnswer:
    def test_the_time_handed_to_wms_ends_in_z(self):
        got = copernicus.sort_layers(capabilities(
            layer("x:olci", extent="2026-09-11T08:00:00+00:00")), now=NOW)
        entry = got["families"][1]["layers"][0]
        assert entry["time_default"].endswith("Z")

    def test_every_family_carries_what_the_panel_draws(self):
        for family in copernicus.demo()["families"]:
            assert set(family) >= {"key", "short", "label", "colour",
                                   "resolution", "about", "layers", "stale"}
            assert family["colour"].startswith("#")

    def test_the_demo_is_the_shape_of_a_live_answer(self):
        got = copernicus.demo()
        assert set(got) >= {"families", "wms", "attribution", "live_within_hours"}
        for family in got["families"]:
            for entry in family["layers"]:
                assert set(entry) >= {"id", "title", "time_default", "live"}
                assert entry["live"] is True

    def test_the_demo_covers_both_satellites(self):
        # Otherwise the build with no network only ever draws one of them.
        got = {f["key"]: f for f in copernicus.demo()["families"]}
        assert got["sentinel-3"]["layers"]
        assert got["sentinel-5p"]["layers"]

    def test_nothing_here_is_also_in_the_imagery_flow(self):
        # This is the rule that matters, rather than the exact list: anything
        # you can draw a box around and pick a date for belongs there, and
        # offering it here as well would be two different answers to the same
        # question. Sentinel-1, Sentinel-2 and Landsat are all in that flow.
        from backend import config
        keys = {f["key"] for f in copernicus.FAMILIES}
        assert not (keys & set(config.SATELLITES))

    def test_every_family_says_how_its_orbit_behaves(self):
        # Both of these change what the panel offers and what counts as live,
        # and a family that forgot to say would silently get the polar
        # orbiter's answers.
        for family in copernicus.FAMILIES:
            assert isinstance(family["spans_days"], bool), family["key"]
            assert copernicus.live_within(family) > dt.timedelta(0)

    def test_a_geostationary_family_is_held_to_a_tighter_clock(self):
        # A satellite publishing every ten minutes is broken long before a
        # polar orbiter's day and a half is up.
        by_key = {f["key"]: f for f in copernicus.FAMILIES}
        assert copernicus.live_within(by_key["mtg"]) < copernicus.LIVE_WITHIN
        assert copernicus.live_within(by_key["sentinel-3"]) == copernicus.LIVE_WITHIN

    def test_the_lightning_layer_is_not_offered_twice(self):
        # It flies on Meteosat, so every pattern broad enough to catch
        # Meteosat's pictures also catches it -- and it already has its own
        # panel, its own freshness rule and its own way of drawing.
        for name, title in [("mtg_fd:li_afa", "Accumulated Flash Area"),
                            ("mtg_fd:li_aff", "Accumulated Flash Fraction"),
                            ("x:lightning_density", "Lightning"),
                            ("msg_fes:li_flash", "SEVIRI flash")]:
            assert copernicus.family_of(name, title) is None, name

    def test_but_meteosat_pictures_still_are(self):
        # The exclusion has to be narrow enough to leave the imagery behind.
        for name, title in [("mtg_fd:rgb_truecolour", "MTG FCI True Colour"),
                            ("msg_fes:rgb_naturalcolour", "MSG SEVIRI Natural"),
                            ("x:meteosat_ir108", "Meteosat infrared")]:
            assert copernicus.family_of(name, title) == "mtg", name

    def test_metop_is_found_by_each_of_its_instruments(self):
        for name in ("metop:avhrr_ndvi", "x:ASCAT_winds", "eo:iasi_ozone",
                     "metop-b_something"):
            assert copernicus.family_of(name, "") == "metop", name
