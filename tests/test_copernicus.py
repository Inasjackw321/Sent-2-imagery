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

    def test_the_families_offered_are_the_ones_that_cannot_be_imagery(self):
        # Sentinel-1 and Sentinel-2 belong in the date-search flow and are
        # deliberately not here; these two cannot go there at all.
        from backend import config
        keys = {f["key"] for f in copernicus.FAMILIES}
        assert keys == {"sentinel-3", "sentinel-5p"}
        assert not (keys & set(config.SATELLITES))
