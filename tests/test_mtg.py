"""Tests for the MTG lightning layer's catalogue reading.

This feature has now failed three times, and only one of the failures was
interesting: the layer that matched on the word "lightning" and turned out to
be a twenty-year climatology, drawn on a live map as though it were tonight.
An empty layer is a nuisance; a convincing wrong one is a lie.

So the test that matters here is not "does it find the product" but "can
anything old reach the live list". The guard is arithmetic on the timestamps
the service itself publishes, and these tests attack it: an archive named
exactly like the live product, a live product named nothing like it, a range
that ends years ago, a layer with no time at all.
"""

from __future__ import annotations

import datetime as dt

import pytest

from backend import mtg

NOW = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.timezone.utc)
WMS = "http://www.opengis.net/wms"


def capabilities(*layers: str) -> str:
    return (f'<WMS_Capabilities xmlns="{WMS}"><Capability>'
            f'<Layer><Title>root</Title>{"".join(layers)}</Layer>'
            f"</Capability></WMS_Capabilities>")


def layer(name: str, *, title: str | None = None, extent: str | None = None,
          default: str | None = None) -> str:
    dim = ""
    if extent is not None or default is not None:
        attrs = f' default="{default}"' if default else ""
        dim = f'<Dimension name="time" units="ISO8601"{attrs}>{extent or ""}</Dimension>'
    return (f"<Layer><Name>{name}</Name><Title>{title or name}</Title>"
            f"{dim}</Layer>")


def iso(minutes_ago: float) -> str:
    return (NOW - dt.timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


class TestNewestTime:
    def test_a_single_instant(self):
        assert mtg.newest_time("2026-09-01T11:50:00Z") == dt.datetime(
            2026, 9, 1, 11, 50, tzinfo=dt.timezone.utc)

    def test_an_interval_takes_its_end_not_its_start(self):
        # The mistake this guards against would read a climatology's 1995 start
        # and call the layer ancient, or worse, read a live layer's start and
        # call it stale. The end is the newest frame on offer.
        got = mtg.newest_time("2026-08-01T00:00:00Z/2026-09-01T11:55:00Z/PT10M")
        assert got == dt.datetime(2026, 9, 1, 11, 55, tzinfo=dt.timezone.utc)

    def test_a_list_is_not_assumed_to_be_in_order(self):
        got = mtg.newest_time(
            "2026-09-01T11:00:00Z,2026-09-01T11:50:00Z,2026-09-01T11:20:00Z")
        assert got == dt.datetime(2026, 9, 1, 11, 50, tzinfo=dt.timezone.utc)

    def test_a_naive_stamp_is_read_as_utc(self):
        got = mtg.newest_time("2026-09-01T11:50:00")
        assert got == dt.datetime(2026, 9, 1, 11, 50, tzinfo=dt.timezone.utc)

    def test_nothing_usable_is_none_rather_than_an_error(self):
        for junk in ("", "   ", "not a date", ",,,", "yesterday"):
            assert mtg.newest_time(junk) is None

    def test_rubbish_mixed_with_a_real_stamp_still_yields_it(self):
        assert mtg.newest_time("nonsense,2026-09-01T11:50:00Z") is not None


class TestLivenessIsNotAboutNames:
    """The guard that the previous attempt did not have."""

    def test_an_archive_named_like_the_live_product_is_refused(self):
        # Same name, same words, nothing to tell them apart but the dates --
        # which is exactly the case that shipped broken last time.
        got = mtg.parse_layers(capabilities(
            layer("mtg_fd:li_afa", title="Accumulated Flash Area",
                  extent="1995-01-01T00:00:00Z/2013-12-31T00:00:00Z/P1D")),
            now=NOW)
        assert got["live"] == []
        assert [e["id"] for e in got["stale"]] == ["mtg_fd:li_afa"]

    def test_a_live_layer_is_taken_however_it_is_named(self):
        got = mtg.parse_layers(capabilities(
            layer("obscure:lfl_product", title="Something Or Other",
                  extent=f"2026-08-01T00:00:00Z/{iso(4)}/PT10M")), now=NOW)
        assert [e["id"] for e in got["live"]] == ["obscure:lfl_product"]

    def test_the_boundary_is_the_declared_window(self):
        just_inside = mtg.parse_layers(capabilities(
            layer("mtg_fd:li_afa", extent=iso(5 * 60 + 59))), now=NOW)
        just_outside = mtg.parse_layers(capabilities(
            layer("mtg_fd:li_afa", extent=iso(6 * 60 + 1))), now=NOW)
        assert len(just_inside["live"]) == 1
        assert len(just_outside["live"]) == 0
        assert len(just_outside["stale"]) == 1

    def test_a_layer_with_no_time_at_all_is_never_live(self):
        # No timestamp is not evidence of freshness. A layer that will not say
        # when its data is from cannot be shown as though it were now.
        got = mtg.parse_layers(capabilities(layer("mtg_fd:li_afa")), now=NOW)
        assert got["live"] == []
        assert got["stale"][0]["age_minutes"] is None

    def test_an_unparseable_time_is_never_live(self):
        got = mtg.parse_layers(capabilities(
            layer("mtg_fd:li_afa", extent="whenever")), now=NOW)
        assert got["live"] == []

    def test_age_is_reported_so_a_stale_layer_can_explain_itself(self):
        got = mtg.parse_layers(capabilities(
            layer("mtg_fd:li_afa", extent=iso(90))), now=NOW)
        assert got["live"][0]["age_minutes"] == 90


class TestTimeParameter:
    def test_the_time_handed_to_wms_ends_in_z(self):
        # It goes straight into a WMS TIME parameter. GeoServer tolerates
        # +00:00; not every server does, and the catalogue's own formatting is
        # not something to pass through unexamined.
        got = mtg.parse_layers(capabilities(
            layer("mtg_fd:li_afa",
                  extent="2026-08-01T00:00:00+00:00/2026-09-01T11:55:00+00:00/PT10M")),
            now=NOW)
        assert got["live"][0]["time_default"] == "2026-09-01T11:55:00Z"

    def test_it_is_the_newest_frame_not_the_catalogue_default(self):
        # A default may be anything the server likes, including the oldest.
        # What the map wants is the latest, so that is what is sent.
        got = mtg.parse_layers(capabilities(
            layer("mtg_fd:li_afa", default="2026-08-01T00:00:00Z",
                  extent=f"2026-08-01T00:00:00Z/{iso(6)}/PT10M")), now=NOW)
        assert got["live"][0]["time_default"] == iso(6)

    def test_the_demo_stamps_are_the_same_shape(self):
        for entry in mtg.demo()["live"] + mtg.demo()["imagery"]:
            assert entry["time_default"].endswith("Z"), entry["time_default"]


class TestSorting:
    def test_lightning_and_backdrop_are_kept_apart(self):
        got = mtg.parse_layers(capabilities(
            layer("mtg_fd:li_afa", title="Accumulated Flash Area", extent=iso(5)),
            layer("mtg_fd:fci_ir105", title="FCI Infrared 10.5", extent=iso(5)),
            layer("copernicus:sea_temp", title="Sea Surface Temperature", extent=iso(5)),
        ), now=NOW)
        assert [e["id"] for e in got["live"]] == ["mtg_fd:li_afa"]
        assert [e["id"] for e in got["imagery"]] == ["mtg_fd:fci_ir105"]

    def test_a_stale_backdrop_is_simply_dropped(self):
        # It is context, not evidence; an old one is no use and no loss.
        got = mtg.parse_layers(capabilities(
            layer("mtg_fd:fci_ir105", title="FCI Infrared", extent=iso(60 * 24))),
            now=NOW)
        assert got["imagery"] == []

    def test_a_group_layer_with_no_name_is_skipped(self):
        # WMS nests layers; the outer ones are headings and cannot be fetched.
        xml = (f'<WMS_Capabilities xmlns="{WMS}"><Capability><Layer>'
               f"<Title>MTG lightning</Title>"
               f"{layer('mtg_fd:li_afa', extent=iso(5))}"
               f"</Layer></Capability></WMS_Capabilities>")
        got = mtg.parse_layers(xml, now=NOW)
        assert [e["id"] for e in got["live"]] == ["mtg_fd:li_afa"]

    def test_nothing_relevant_is_three_empty_lists(self):
        got = mtg.parse_layers(capabilities(
            layer("copernicus:chlorophyll", extent=iso(5))), now=NOW)
        assert got == {"live": [], "stale": [], "imagery": []}

    def test_rubbish_is_refused_clearly(self):
        with pytest.raises(mtg.MTGError, match="would not parse"):
            mtg.parse_layers("<not xml")


class TestDemo:
    def test_it_answers_in_the_shape_the_page_expects(self):
        got = mtg.demo()
        assert set(got) >= {"live", "stale", "imagery", "wms", "attribution",
                            "coverage", "live_within_hours"}
        for entry in got["live"] + got["imagery"]:
            assert set(entry) >= {"id", "title", "time_default", "newest", "live"}

    def test_the_demo_layers_are_themselves_live(self):
        # Otherwise the demo build would exercise the stale path and hide
        # whatever the live one does.
        assert all(e["live"] for e in mtg.demo()["live"])

    def test_the_coverage_note_names_what_is_seen_and_what_is_not(self):
        note = mtg.demo()["coverage"].lower()
        assert "europe" in note
        assert "americas" in note
