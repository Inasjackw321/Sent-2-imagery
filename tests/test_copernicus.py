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

from backend import copernicus, mtg

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

    A satellite that STARES has the opposite problem and the opposite answer,
    which is the class below this one.
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
        assert len(entry["frames"]) == copernicus.DAYS_OFFERED

    def test_the_frames_are_in_order_with_the_newest_last(self):
        # The panel reads the last one as "live" and scrubs backwards from it,
        # so the order is not cosmetic.
        for family in copernicus.demo()["families"]:
            for entry in family["layers"]:
                stamps = [f["time"] for f in entry["frames"]]
                assert stamps == sorted(stamps), entry["id"]

    def test_a_day_is_asked_for_as_the_range_that_covers_it(self):
        # A bare date is midnight exactly on some servers, which is one
        # instant, which is one strip.
        got = copernicus.sort_layers(capabilities(
            layer("x:olci", title="Sentinel-3 OLCI", extent=iso(3))), now=NOW)
        entry = [f for f in got["families"] if f["key"] == "sentinel-3"][0]["layers"][0]
        assert entry["frames"][-1]["time"] == (
            "2026-09-11T00:00:00Z/2026-09-11T23:59:59Z")

    def test_the_demo_carries_them_too(self):
        for family in copernicus.demo()["families"]:
            if not family.get("spans_days"):
                continue
            for entry in family["layers"]:
                assert len(entry["frames"]) == copernicus.DAYS_OFFERED, family["key"]

    def test_nothing_offers_a_week_composited_into_one_picture(self):
        """There used to be one, and it was the default.

        A whole day of a polar orbiter's passes is already the whole globe, so
        seven of them added no coverage -- only a mode in which the picture was
        of no particular day, which is the opposite of what a live layer is
        for. Asked for: always live.
        """
        for family in copernicus.demo()["families"]:
            for entry in family["layers"]:
                assert "whole_week" not in entry, entry["id"]

    def test_a_geostationary_satellite_is_offered_no_days(self):
        # It photographs the whole disc every ten minutes, so one frame is
        # already the whole picture. Compositing a day of them would blend a
        # hundred and forty frames of a moving sky into mud.
        got = copernicus.sort_layers(capabilities(
            layer("mtg_fd:rgb_truecolour", title="MTG FCI True Colour",
                  extent=iso(0.5))), now=NOW)
        entry = [f for f in got["families"] if f["key"] == "mtg"][0]["layers"][0]
        assert all("/" not in frame["time"] for frame in entry["frames"])

    def test_the_demo_agrees_with_that(self):
        # Otherwise the offline build animates something the live one never
        # does, which is the kind of difference nobody finds until a
        # screenshot from the demo is used to explain the real thing.
        for family in copernicus.demo()["families"]:
            if family.get("spans_days"):
                continue
            for entry in family["layers"]:
                assert all("/" not in f["time"] for f in entry["frames"])


class TestTheAnswer:
    def test_the_time_handed_to_wms_ends_in_z(self):
        got = copernicus.sort_layers(capabilities(
            layer("x:olci", extent="2026-09-11T08:00:00+00:00")), now=NOW)
        entry = [f for f in got["families"]
                 if f["key"] == "sentinel-3"][0]["layers"][0]
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

    def test_meteosat_leads_the_panel(self):
        """The picture people mean by "the satellite".

        It is the only one here that updates while you watch and the only one
        that can be played as an animation, so it is what the panel opens on
        and the rest sit below it. Asked for.
        """
        assert copernicus.FAMILIES[0]["key"] == "mtg"
        assert copernicus.demo()["families"][0]["key"] == "mtg"

    def test_and_it_opens_on_its_true_colour(self):
        # Not on whichever of its products sorts first.
        first = copernicus.demo()["families"][0]["layers"][0]
        assert "truecolour" in first["id"]

    def test_leading_the_panel_takes_no_layer_from_anyone(self):
        # FAMILIES order is also match order, so putting Meteosat first could
        # in principle have it claim a layer another family was matching.
        for name in ("s5p_no2", "sentinel-3 olci", "metop_avhrr", "tropomi_ch4"):
            assert copernicus.family_of(name, name) != "mtg", name

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


class TestTheShortlist:
    """Four satellites with every product each is forty buttons.

    EUMETSAT serves a dozen or more products per spacecraft and the panel
    listed all of them, so the four or five anybody actually opens were buried
    among thirty-odd they never will. Nothing is taken away -- the rest are one
    button behind "all products" -- but what opens with the panel is now a
    handful rather than a catalogue.
    """

    def olci(self, *names: str) -> list[dict]:
        got = copernicus.sort_layers(capabilities(
            *[layer(f"x:{n}", title=f"Sentinel-3 OLCI {n}", extent=iso(3))
              for n in names]), now=NOW)
        return [f for f in got["families"]
                if f["key"] == "sentinel-3"][0]["layers"]

    def everyday(self, layers: list[dict]) -> list[str]:
        return [e["id"] for e in layers if e["everyday"]]

    def test_the_products_worth_opening_are_marked(self):
        got = self.olci("truecolour", "lst")
        assert self.everyday(got) == ["x:truecolour", "x:lst"]

    def test_and_they_come_out_in_the_order_they_are_wanted_in(self):
        # Not alphabetical. The first one is what the panel opens on, so
        # alphabetical order would be choosing what anyone looks at first by
        # the spelling of its filename.
        got = self.olci("lst", "truecolour")
        assert [e["id"] for e in got] == ["x:truecolour", "x:lst"]

    def test_and_the_rest_are_not(self):
        got = self.olci("truecolour", "aot_uncertainty_flags")
        assert self.everyday(got) == ["x:truecolour"]

    def test_a_long_shortlist_is_still_short(self):
        # The complaint is the length of the list, so matching more words
        # cannot be allowed to give the list back.
        many = ["truecolour", "natural", "lst", "chlorophyll", "fire", "frp",
                "true_colour"]
        got = self.olci(*many)
        assert len(many) > copernicus.MOST_EVERYDAY
        assert len(self.everyday(got)) == copernicus.MOST_EVERYDAY

    def test_nothing_is_dropped_from_the_catalogue(self):
        # Marked, not filtered. The panel's "all products" button has to have
        # something to show.
        got = self.olci("truecolour", "aot_uncertainty_flags", "whatever_else")
        assert len(got) == 3

    def test_a_family_that_matched_nothing_still_offers_something(self):
        """EUMETSAT renames things.

        A shortlist that went empty would leave the satellite with no products
        at all until someone shipped a new pattern, which is a worse list than
        the long one this is shortening.
        """
        got = self.olci("zzz_one", "zzz_two")
        assert self.everyday(got) == ["x:zzz_one", "x:zzz_two"]

    def test_and_not_more_than_a_handful_of_them_either(self):
        got = self.olci(*[f"zzz_{n}" for n in range(9)])
        assert len(self.everyday(got)) == copernicus.MOST_EVERYDAY

    def test_every_family_has_words_to_shortlist_by(self):
        # A family missing from the table would fall through to the "nothing
        # matched" rule on every catalogue, which is alphabetical order
        # pretending to be a choice.
        for family in copernicus.FAMILIES:
            assert copernicus.EVERYDAY.get(family["key"]), family["key"]

    def test_the_words_pick_out_what_each_satellite_is_for(self):
        # Spot-checked against what each of these is actually opened for, so a
        # table edited to something plausible but wrong is caught.
        for key, name in [("sentinel-5p", "s5p_no2_tropospheric"),
                          ("sentinel-5p", "tropomi_methane_ch4"),
                          ("sentinel-3", "s3_olci_truecolour"),
                          ("sentinel-3", "slstr_lst_day"),
                          ("mtg", "mtg_fd_rgb_truecolour"),
                          ("metop", "metop_ascat_winds")]:
            assert copernicus.is_everyday(key, name, name), name

    def test_and_leave_out_what_they_are_not(self):
        for key, name in [("sentinel-5p", "s5p_qa_value"),
                          ("sentinel-3", "olci_ogvi_uncertainty"),
                          ("mtg", "fci_solar_zenith_angle"),
                          ("metop", "iasi_channel_radiance_0421")]:
            assert not copernicus.is_everyday(key, name, name), name

    def test_the_demo_is_shortlisted_the_same_way(self):
        # Otherwise the offline build opens with every product and the live one
        # does not, which is the difference nobody notices until a screenshot
        # from one is used to explain the other.
        for family in copernicus.demo()["families"]:
            for entry in family["layers"]:
                assert isinstance(entry["everyday"], bool), entry["id"]

    def test_the_demo_opens_on_something(self):
        for family in copernicus.demo()["families"]:
            assert any(e["everyday"] for e in family["layers"]), family["key"]


class TestASatelliteThatStares:
    """Meteosat's history is hours, not days, and it can be played.

    A polar orbiter's consecutive frames are two different strips of the
    planet a day apart; played as a loop that is a slideshow. A geostationary
    satellite's consecutive frames are the same view ten minutes apart, which
    is the one thing here that is honestly an animation -- so it is the one
    thing here that offers instants rather than whole days.
    """

    def disc(self, extent=None, **kw):
        got = copernicus.sort_layers(capabilities(
            layer("mtg_fd:rgb_truecolour", title="MTG FCI True Colour",
                  extent=extent if extent is not None else iso(0.5))),
            now=NOW, **kw)
        return [f for f in got["families"] if f["key"] == "mtg"][0]["layers"][0]

    def test_it_offers_instants_a_few_minutes_apart(self):
        got = self.disc()
        assert len(got["frames"]) == copernicus.FRAMES_OFFERED
        assert got["frames"][-1]["time"] == "2026-09-11T11:30:00Z"
        assert got["frames"][-2]["time"] == "2026-09-11T11:20:00Z"

    def test_the_cadence_the_service_declares_is_the_one_used(self):
        # Assuming ten minutes where EUMETSAT said fifteen would animate
        # frames that do not exist, and every other one would come back blank.
        got = self.disc(extent=f"{iso(6)}/{iso(0.5)}/PT15M")
        assert got["step_minutes"] == 15
        assert got["frames"][-1]["time"] == "2026-09-11T11:30:00Z"
        assert got["frames"][-2]["time"] == "2026-09-11T11:15:00Z"

    def test_and_the_family_s_own_is_only_a_fallback(self):
        # A bare list of instants declares no period at all, which is common.
        assert self.disc()["step_minutes"] == \
            copernicus.ASSUMED_STEP_MINUTES["mtg"]

    def test_a_satellite_that_flies_over_still_gets_whole_days(self):
        got = copernicus.sort_layers(capabilities(
            layer("x:olci", title="Sentinel-3 OLCI",
                  extent=f"{iso(60)}/{iso(3)}/PT10M")), now=NOW)
        entry = [f for f in got["families"]
                 if f["key"] == "sentinel-3"][0]["layers"][0]
        # Even where the service declares a ten-minute cadence: one instant of
        # a polar orbiter is one orbit strip whatever the catalogue offers.
        assert len(entry["frames"]) == copernicus.DAYS_OFFERED
        assert "/" in entry["frames"][-1]["time"]

    def test_only_the_staring_one_says_it_animates(self):
        # On the LIVE path, not only the demo's. The demo builds the flag from
        # its own argument, so a demo-only check passes with sort_layers
        # setting every layer to animate -- measured: that mutation survived
        # this class until this test read the live answer.
        got = copernicus.sort_layers(capabilities(
            layer("mtg_fd:rgb_truecolour", title="MTG FCI True Colour",
                  extent=iso(0.5)),
            layer("x:olci_truecolour", title="Sentinel-3 OLCI true colour",
                  extent=iso(3)),
            layer("x:s5p_no2", title="Sentinel-5P NO2", extent=iso(3)),
            layer("x:metop_avhrr", title="Metop AVHRR", extent=iso(3))), now=NOW)
        seen = {f["key"]: f["layers"][0]["animates"] for f in got["families"]
                if f["layers"]}
        assert seen == {"mtg": True, "sentinel-3": False,
                        "sentinel-5p": False, "metop": False}

    def test_and_the_demo_says_the_same(self):
        for family in copernicus.demo()["families"]:
            for entry in family["layers"]:
                assert entry["animates"] is (family["key"] == "mtg"), entry["id"]

    def test_the_frames_are_labelled_by_the_clock(self):
        assert self.disc()["frames"][-1]["label"] == "11:30"

    def test_and_a_day_s_frames_by_the_date(self):
        got = copernicus.demo()["families"][1]["layers"][0]
        assert len(got["frames"][-1]["label"]) == 10

    def test_every_frame_carries_the_day_it_is_on(self):
        # The bar shows a date beside the clock, and four hours back from
        # half past midnight is yesterday.
        for frame in self.disc()["frames"]:
            assert len(frame["at"]) == 10 and frame["at"].count("-") == 2

    def test_a_layer_with_no_time_at_all_offers_no_frames(self):
        got = copernicus.sort_layers(capabilities(
            layer("mtg_fd:rgb_truecolour", title="MTG FCI")), now=NOW)
        assert all(f["layers"] == [] for f in got["families"])
        assert copernicus.frames_for(copernicus.FAMILIES[0], None, None) == []


class TestReadingACadence:
    """The third term of a WMS time interval, which is where a service that
    publishes on a cadence writes that cadence down."""

    def test_minutes(self):
        assert mtg.period_of("2026-09-01T00:00:00Z/2026-09-11T11:30:00Z/PT10M") \
            == dt.timedelta(minutes=10)

    def test_hours_and_days(self):
        assert mtg.period_of("a/b/PT3H") == dt.timedelta(hours=3)
        assert mtg.period_of("a/b/P1D") == dt.timedelta(days=1)

    def test_a_mixture(self):
        assert mtg.period_of("a/b/P1DT2H30M") == dt.timedelta(days=1, hours=2,
                                                              minutes=30)

    def test_the_finest_of_several_wins(self):
        # An archive at a frame a day beside a live interval at one every ten
        # minutes is a common shape, and the live one is what is animated.
        assert mtg.period_of("a/b/P1D,c/d/PT10M") == dt.timedelta(minutes=10)

    def test_a_bare_list_of_instants_declares_no_cadence(self):
        assert mtg.period_of(f"{iso(1)},{iso(2)}") is None

    def test_nor_does_an_interval_without_one(self):
        assert mtg.period_of(f"{iso(9)}/{iso(1)}") is None

    def test_nothing_is_not_a_cadence(self):
        assert mtg.period_of("") is None
        assert mtg.period_of("rubbish") is None

    def test_a_zero_period_is_refused(self):
        # It would divide the animation into an infinite number of frames all
        # at the same instant.
        assert mtg.period_of("a/b/PT0M") is None

    def test_years_and_months_are_not_read_as_something_else(self):
        # P1Y is not a fixed number of seconds. Reading it as one day, or as
        # one minute, would be worse than not reading it.
        assert mtg.period_of("a/b/P1Y") is None
        assert mtg.period_of("a/b/P3M") is None
