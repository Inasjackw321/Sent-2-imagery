"""Tests for the NOTAM layer -- which airspace is shut, and when.

Written against the payload shape in the FAA's documentation, which is the
only thing available to check against: this environment cannot reach
external-api.faa.gov, so nothing here has been run against the live service.
Worth saying plainly rather than leaving to be discovered.

Most of what is asserted is about the two things that would put a closure in
the wrong place at the wrong size: a Q-line coordinate is degrees and minutes
run together with the hemisphere as a letter, and a Q-line radius is in
nautical miles.
"""

from __future__ import annotations

import time

import pytest

from backend import notams

# Their documented shape, with the fields this reads.
ONE = {"properties": {"coreNOTAMData": {"notam": {
    "number": "A1234/26",
    "location": "UKBV",
    "type": "N",
    "classification": "INTL",
    "coordinates": "5020N03030E",
    "radius": 30,
    "effectiveStart": "2026-01-01T00:00:00Z",
    "effectiveEnd": "2026-12-31T23:59:00Z",
    "text": "AIRSPACE CLOSED TO ALL CIVIL TRAFFIC",
}}}}


class TestReadingACoordinate:
    """"4915N02330E" is 49 degrees 15 minutes north, 23 degrees 30 east.

    Read as a number it is forty-nine million; read as decimal degrees it is
    4915. Either way the mark leaves the planet, so this refuses anything that
    is not exactly the documented shape.
    """

    def test_degrees_and_minutes(self):
        assert notams.read_coord("4915N02330E") == (49.25, 23.5)

    def test_degrees_minutes_and_seconds(self):
        assert notams.read_coord("491530N0233045E") == (49.25833, 23.5125)

    def test_the_southern_and_western_hemispheres(self):
        lat, lon = notams.read_coord("3345S07030W")
        assert lat < 0 and lon < 0
        assert (lat, lon) == (-33.75, -70.5)

    def test_lower_case_is_read_too(self):
        assert notams.read_coord("4915n02330e") == (49.25, 23.5)

    def test_anything_else_is_refused(self):
        for junk in ("49.15,23.30", "4915N", "", None, 42, [],
                     "nonsense", "4915X02330E", "491N02330E"):
            assert notams.read_coord(junk) is None, junk

    def test_a_position_off_the_planet_is_refused(self):
        # 99 degrees of latitude parses arithmetically and is not a place.
        assert notams.read_coord("9915N02330E") is None


class TestReadingANotice:
    def test_their_example_comes_out_whole(self):
        got = notams.read_notam(ONE)
        assert got["id"] == "A1234/26"
        assert got["location"] == "UKBV"
        assert got["placed"] is True
        assert (round(got["lat"], 4), round(got["lon"], 4)) == (50.3333, 30.5)

    def test_the_radius_is_nautical_miles(self):
        # Thirty nautical miles is 55.6 km, not 30. Reading it as kilometres
        # draws a closure at a bit over half its size.
        assert notams.read_notam(ONE)["radius_km"] == 55.56

    def test_a_notice_with_no_radius_gets_their_default(self):
        bare = {"properties": {"coreNOTAMData": {"notam": {
            **ONE["properties"]["coreNOTAMData"]["notam"], "radius": None}}}}
        assert notams.read_notam(bare)["radius_km"] == round(
            notams.DEFAULT_NM * notams.NM_KM, 2)

    def test_a_decimal_position_is_preferred_when_it_is_there(self):
        # It needs no interpretation; the Q-line string is the fallback.
        both = {"properties": {"coreNOTAMData": {"notam": {
            **ONE["properties"]["coreNOTAMData"]["notam"],
            "latitude": 48.5, "longitude": 35.0}}}}
        got = notams.read_notam(both)
        assert (got["lat"], got["lon"]) == (48.5, 35.0)

    def test_a_notice_with_no_readable_position_is_listed_not_placed(self):
        # The same rule the air tracker follows for a report it cannot
        # geocode: counted and listed, never guessed at.
        lost = {"properties": {"coreNOTAMData": {"notam": {
            **ONE["properties"]["coreNOTAMData"]["notam"],
            "coordinates": "NOT A COORDINATE"}}}}
        got = notams.read_notam(lost)
        assert got["placed"] is False
        assert got["lat"] is None
        assert got["why_unplaced"]

    def test_a_notice_with_no_text_or_number_is_not_a_notice(self):
        for missing in ("text", "number"):
            broken = dict(ONE["properties"]["coreNOTAMData"]["notam"])
            broken[missing] = None
            assert notams.read_notam(
                {"properties": {"coreNOTAMData": {"notam": broken}}}) is None

    def test_rubbish_is_refused_rather_than_guessed_at(self):
        for junk in (None, "a string", 42, [], {}):
            assert notams.read_notam(junk) is None

    def test_a_fir_wide_notice_is_marked_rather_than_drawn_as_a_disc(self):
        # A radius of hundreds of miles is a boundary, and a circle that size
        # would wash over half of Europe while claiming to be its edge.
        huge = {"properties": {"coreNOTAMData": {"notam": {
            **ONE["properties"]["coreNOTAMData"]["notam"], "radius": 300}}}}
        assert notams.read_notam(huge)["wide"] is True
        assert notams.read_notam(ONE)["wide"] is False


class TestWhetherItApplies:
    """A NOTAM is only true between its start and its end."""

    def note(self, start, end):
        return {"from": start, "to": end}

    def test_one_in_force_now(self):
        now = time.time()
        assert notams.in_force(self.note(now - 60, now + 60), now)

    def test_one_that_has_not_begun(self):
        now = time.time()
        assert not notams.in_force(self.note(now + 600, now + 1200), now)

    def test_one_that_has_ended(self):
        now = time.time()
        assert not notams.in_force(self.note(now - 1200, now - 600), now)

    def test_permanent_is_no_end_rather_than_an_end_in_the_past(self):
        now = time.time()
        assert notams.in_force(self.note(now - 600, None), now)

    def test_perm_reads_as_no_end(self):
        assert notams._moment("PERM") is None
        assert notams._moment("UFN") is None
        assert notams._moment("2026-01-01T00:00:00Z") is not None

    def test_a_time_with_no_offset_is_read_as_utc(self):
        # NOTAM times are UTC by definition. Reading one as local time on a
        # server whose timezone is an accident of deployment would put a
        # closure hours out at either end.
        import datetime as dt
        assert notams._moment("2026-01-01T00:00:00") == dt.datetime(
            2026, 1, 1, tzinfo=dt.timezone.utc).timestamp()


class TestAskingForSomethingTheServiceWillAnswer:
    """Why the layer showed nothing.

    A map of Ukraine is an 850-kilometre circle -- four hundred and sixty
    nautical miles -- and their API refuses a locationRadius past a hundred.
    So every request at the zoom anybody actually uses this map at was
    rejected before it was read, and the layer was doing the right thing with
    the answer it got: the answer was "no".

    The fix is both a cap and a better query. A country-sized view is one or
    two flight information regions, and a notice that closes a whole FIR is
    filed against the FIR rather than against a point in it.
    """

    def asked(self, monkeypatch, box, key=True):
        """What was actually put on the wire for a view, either way in."""
        seen = []

        def watch(params):
            seen.append(params)
            return {"items": [], "totalCount": 0}

        def watch_search(location, offset=0):
            seen.append({"icaoLocation": location, "via": "search"})
            return {"notamList": [], "totalNotamCount": 0}

        monkeypatch.setattr(notams, "CLIENT_ID", "id" if key else "")
        monkeypatch.setattr(notams, "CLIENT_SECRET", "secret" if key else "")
        monkeypatch.setattr(notams, "_ask", watch)
        monkeypatch.setattr(notams, "_ask_search", watch_search)
        notams.forget()
        notams.over(*box)
        return seen

    def test_a_country_sized_view_asks_by_region(self, monkeypatch):
        seen = self.asked(monkeypatch, (22.0, 44.0, 40.4, 52.5))
        assert seen, "nothing was asked at all"
        assert all("icaoLocation" in p for p in seen)
        codes = {p["icaoLocation"] for p in seen}
        assert "UKBV" in codes and "UKLV" in codes

    def test_it_never_asks_for_a_radius_the_service_refuses(self, monkeypatch):
        """Against the number in their documentation, not against our own.

        Asserting `<= MOST_NM` proved nothing: raising MOST_NM raises the cap
        AND the thing it is checked against, so the test passed with the cap
        set to the very value that was being rejected. A hundred nautical
        miles is their limit and it is written here as a hundred.
        """
        assert notams.MOST_NM <= 100.0
        # Anywhere, at any zoom, including the corner of the world with no
        # region in the table.
        for box in ((22.0, 44.0, 40.4, 52.5), (-120.0, 20.0, -100.0, 40.0),
                    (-10.0, -60.0, 60.0, 60.0), (30.2, 50.2, 30.8, 50.6)):
            for params in self.asked(monkeypatch, box):
                if "locationRadius" in params:
                    assert params["locationRadius"] <= 100, box

    def test_a_view_with_no_known_region_falls_back_to_a_radius(self, monkeypatch):
        seen = self.asked(monkeypatch, (-120.0, 20.0, -100.0, 40.0))
        assert len(seen) == 1
        assert "locationRadius" in seen[0]

    def test_and_says_so_when_that_leaves_part_of_it_unasked(self, monkeypatch):
        monkeypatch.setattr(notams, "CLIENT_ID", "id")
        monkeypatch.setattr(notams, "CLIENT_SECRET", "secret")
        monkeypatch.setattr(notams, "_ask",
                            lambda params: {"items": [], "totalCount": 0})
        notams.forget()
        wide = notams.over(-120.0, 20.0, -100.0, 40.0)
        assert wide["partial"] is True
        notams.forget()
        small = notams.over(-110.2, 29.8, -109.8, 30.2)
        assert small["partial"] is False

    def test_it_says_what_it_asked(self):
        # "It does not work" is otherwise unanswerable from the outside:
        # nothing closed and nothing asked look identical on a map.
        got = notams.demo()
        assert got["asked"]

    def test_the_regions_a_view_touches_are_nearest_first(self):
        """So a cap that bites drops the edges rather than the middle.

        Checked over Odesa rather than over Kyiv, because Kyiv's region is
        first in the table anyway -- that test passed with the sort deleted
        and was measuring the order the table happens to be written in.
        """
        over_odesa = notams.firs_over(29.0, 45.0, 34.0, 50.0)
        codes = [c for c, _ in over_odesa]
        assert codes[0] == "UKOV"
        # Kyiv's region is first in the table and further from this view, so
        # a table-order answer would put it ahead of Odesa's.
        assert codes.index("UKOV") < codes.index("UKBV")

    def test_a_view_over_one_region_asks_about_one(self):
        assert [c for c, _ in notams.firs_over(29.5, 50.0, 30.5, 51.0)] == ["UKBV"]

    def test_the_number_of_regions_asked_about_is_bounded(self):
        # Half of Europe is not a reason to make thirty requests.
        assert len(notams.firs_over(-10.0, 35.0, 60.0, 70.0)) <= notams.MOST_FIRS

    def test_every_region_in_the_table_is_a_four_letter_icao_code(self):
        for code, name, south, west, north, east in notams.FIRS:
            assert len(code) == 4 and code.isupper(), code
            assert name, code
            assert -90 <= south < north <= 90, code
            assert -180 <= west < east <= 180, code

    def test_one_notice_filed_against_two_regions_is_kept_once(self, monkeypatch):
        # A FIR boundary runs through plenty of them, and the same number
        # arriving twice would draw two circles on one point.
        one = {"properties": {"coreNOTAMData": {"notam": {
            "number": "A9/26", "location": "UKBV", "coordinates": "5020N03030E",
            "radius": 10, "effectiveStart": "2026-01-01T00:00:00Z",
            "effectiveEnd": "PERM", "text": "SAME NOTICE, TWO REGIONS"}}}}
        monkeypatch.setattr(notams, "CLIENT_ID", "id")
        monkeypatch.setattr(notams, "CLIENT_SECRET", "secret")
        monkeypatch.setattr(notams, "_ask",
                            lambda params: {"items": [one], "totalCount": 1})
        notams.forget()
        got = notams.over(22.0, 44.0, 40.4, 52.5)
        assert len(got["notams"]) == 1


class TestTheCircleThatCoversTheView:
    """Their API asks for a circle and a map shows a rectangle."""

    def test_it_covers_the_corners(self):
        west, south, east, north = 22.0, 44.0, 40.4, 52.5
        lat, lon, radius = notams.bounds_circle(west, south, east, north)
        from backend.tracker import separation
        for corner in ((south, west), (south, east), (north, west), (north, east)):
            assert separation(lat, lon, *corner) <= radius + 1, corner

    def test_the_centre_is_the_middle_of_the_box(self):
        lat, lon, _ = notams.bounds_circle(20.0, 40.0, 30.0, 50.0)
        assert (lat, lon) == (45.0, 25.0)


class TestWithNoKey:
    """An empty list and "there is no key" are different facts.

    A layer that showed the first for the second would be saying the sky is
    open when it has not asked.
    """

    def test_it_knows_whether_it_can_ask(self, monkeypatch):
        monkeypatch.setattr(notams, "CLIENT_ID", "")
        monkeypatch.setattr(notams, "CLIENT_SECRET", "")
        assert notams.configured() is False
        monkeypatch.setattr(notams, "CLIENT_ID", "abc")
        monkeypatch.setattr(notams, "CLIENT_SECRET", "def")
        assert notams.configured() is True

    def test_asking_without_one_is_an_error_rather_than_an_empty_answer(
            self, monkeypatch):
        monkeypatch.setattr(notams, "CLIENT_ID", "")
        monkeypatch.setattr(notams, "CLIENT_SECRET", "")
        monkeypatch.setattr(notams, "_ask", notams.unstubbed_ask)
        try:
            notams._ask({})
        except notams.NotamError as exc:
            assert "key" in str(exc)
        else:
            raise AssertionError("it asked with no key")


class TestTheDemo:
    """The offline build, through the same reader the live path uses."""

    def test_it_draws_what_it_can_and_lists_what_it_cannot(self):
        got = notams.demo()
        assert got["notams"], "nothing to draw"
        assert got["unplaced"], "the unplaceable case is not shown"

    def test_an_expired_notice_is_not_in_it(self):
        got = notams.demo()
        everything = [n["id"] for n in got["notams"] + got["unplaced"]]
        assert "A0005/26" not in everything

    def test_a_fir_wide_one_is_there_and_marked(self):
        wide = [n for n in notams.demo()["notams"] if n["wide"]]
        assert wide, "the too-wide-to-draw case is not shown"

    def test_every_invented_notice_says_it_is_invented(self):
        # The single worst thing this layer could do is show a convincing
        # fake airspace closure.
        got = notams.demo()
        for notice in got["notams"] + got["unplaced"]:
            assert "DEMO" in notice["text"] and "NOT A REAL NOTAM" in notice["text"]


class TestItWorksWithoutAKey:
    """Two rounds of "it still doesn't work" came down to this.

    The layer needed a client id and secret before it would show anything, so
    the answer to "it doesn't work" was "go and register" -- which is the
    difference between a feature and a promise. There is a keyless way in: the
    FAA's own NOTAM Search, which is what their public search page talks to.

    The documented API is still better and is still used when a key is there.
    """

    def wired(self, monkeypatch, key):
        seen = {"api": 0, "search": 0}

        def api(params):
            seen["api"] += 1
            return {"items": [], "totalCount": 0}

        def search(location, offset=0):
            seen["search"] += 1
            return {"notamList": [], "totalNotamCount": 0}

        monkeypatch.setattr(notams, "CLIENT_ID", "id" if key else "")
        monkeypatch.setattr(notams, "CLIENT_SECRET", "secret" if key else "")
        monkeypatch.setattr(notams, "_ask", api)
        monkeypatch.setattr(notams, "_ask_search", search)
        notams.forget()
        got = notams.over(22.0, 44.0, 40.4, 52.5)
        return seen, got

    def test_with_no_key_it_asks_the_keyless_one(self, monkeypatch):
        seen, got = self.wired(monkeypatch, key=False)
        assert seen["search"] > 0
        assert seen["api"] == 0
        assert got["asked"], "nothing was asked at all"
        assert got["source"] == "FAA NOTAM Search"

    def test_with_a_key_it_asks_the_documented_one(self, monkeypatch):
        seen, got = self.wired(monkeypatch, key=True)
        assert seen["api"] > 0
        assert seen["search"] == 0
        assert got["source"] == "FAA NOTAM API"

    def test_one_region_refusing_does_not_lose_the_others(self, monkeypatch):
        answered = []

        def flaky(location, offset=0):
            if location == "UKBV":
                raise notams.NotamError("NOTAM Search answered 500")
            answered.append(location)
            return {"notamList": [], "totalNotamCount": 0}

        monkeypatch.setattr(notams, "CLIENT_ID", "")
        monkeypatch.setattr(notams, "CLIENT_SECRET", "")
        monkeypatch.setattr(notams, "_ask_search", flaky)
        notams.forget()
        got = notams.over(22.0, 44.0, 40.4, 52.5)
        assert answered, "one refusal lost the whole view"
        assert any("UKBV" in t for t in got["trouble"])
        assert not any("UKBV" in a for a in got["asked"])

    def test_a_refusal_says_what_it_was_told(self, monkeypatch):
        # "It doesn't work" was unanswerable for two rounds because every
        # failure looked the same from outside.
        def refuse(location, offset=0):
            raise notams.NotamError("NOTAM Search answered 503")

        monkeypatch.setattr(notams, "CLIENT_ID", "")
        monkeypatch.setattr(notams, "CLIENT_SECRET", "")
        monkeypatch.setattr(notams, "_ask_search", refuse)
        notams.forget()
        got = notams.over(22.0, 44.0, 40.4, 52.5)
        assert got["asked"] == []
        assert any("503" in t for t in got["trouble"])

    def test_a_view_nobody_can_be_asked_about_says_so(self, monkeypatch):
        # No region in the table and no key: the search takes a location
        # rather than a circle, so there is nothing to ask it.
        monkeypatch.setattr(notams, "CLIENT_ID", "")
        monkeypatch.setattr(notams, "CLIENT_SECRET", "")
        notams.forget()
        got = notams.over(-120.0, 20.0, -100.0, 40.0)
        assert got["asked"] == []
        assert got["trouble"]

    def test_a_failure_is_not_cached_for_ten_minutes(self, monkeypatch):
        # A service having a moment should not make the layer dark until the
        # cache expires.
        calls = []

        def refuse(location, offset=0):
            calls.append(location)
            raise notams.NotamError("down")

        monkeypatch.setattr(notams, "CLIENT_ID", "")
        monkeypatch.setattr(notams, "CLIENT_SECRET", "")
        monkeypatch.setattr(notams, "_ask_search", refuse)
        notams.forget()
        notams.over(22.0, 44.0, 40.4, 52.5)
        first = len(calls)
        notams.over(22.0, 44.0, 40.4, 52.5)
        assert len(calls) > first, "a failure was cached"


class TestTheOtherSpellings:
    """Their two sources write the same facts differently.

    A reader that knew only one of them would place nothing at all from the
    other, which on a map is indistinguishable from an empty sky.
    """

    SEARCH_RECORD = {
        "notamNumber": "A1234/26",
        "facilityDesignator": "UKBV",
        "icaoMessage": "AIRSPACE CLOSED TO ALL CIVIL TRAFFIC",
        "latitude": "50-20-00.000N",
        "longitude": "030-30-00.000E",
        "radius": "30",
        "startDate": "01/15/2026 0000",
        "endDate": "PERM",
    }

    def test_a_search_record_reads(self):
        got = notams.read_notam(self.SEARCH_RECORD)
        assert got["id"] == "A1234/26"
        assert got["location"] == "UKBV"
        assert got["placed"] is True
        assert round(got["lat"], 4) == 50.3333
        assert round(got["lon"], 4) == 30.5
        assert got["radius_km"] == 55.56

    def test_the_dashed_angle_form(self):
        assert notams.read_angle("49-15-00.000N") == 49.25
        assert notams.read_angle("023-30-00.000E") == 23.5
        assert notams.read_angle("33-45-00S") == -33.75
        assert notams.read_angle("070-30-00W") == -70.5

    def test_and_refuses_anything_else(self):
        for junk in ("49.25", "", None, 42, "nonsense", "49-15"):
            assert notams.read_angle(junk) is None, junk

    def test_their_date_spelling(self):
        import datetime as dt
        want = dt.datetime(2026, 1, 15, tzinfo=dt.timezone.utc).timestamp()
        for spelling in ("01/15/2026 0000", "01/15/2026 00:00", "01/15/2026"):
            assert notams._moment(spelling) == want, spelling

    def test_a_time_it_cannot_read_is_none_rather_than_now(self):
        # in_force() reads no-end as permanent, so a misread end date would
        # keep an expired closure on the map for good.
        assert notams._moment("not a date") is None

    def test_the_list_is_found_under_either_name(self):
        assert notams._records({"items": [1, 2]}) == [1, 2]
        assert notams._records({"notamList": [1]}) == [1]
        # And a payload that has changed shape comes back empty rather than
        # handing over the first array it happens to contain.
        assert notams._records({"somethingElse": [1, 2, 3]}) == []
        assert notams._records({}) == []

    def test_the_total_is_found_under_either_name(self):
        assert notams._count({"totalCount": 7}) == 7
        assert notams._count({"totalNotamCount": 9}) == 9
        assert notams._count({}) == 0


RAW_PAGE = """<html><body>
<b>UKBV</b>
<pre>A1234/26 NOTAMN
Q) UKBV/QRTCA/IV/BO/W/000/999/5020N03030E030
A) UKBV B) 2601150000 C) PERM
E) AIRSPACE CLOSED TO ALL CIVIL TRAFFIC. RWY 09/27 &amp; TWY A.
F) SFC
G) FL660</pre>
<pre>A1235/26 NOTAMN
Q) UKBV/QXXXX/IV/BO/W/000/999/
A) UKBV B) 2601150000 C) PERM
E) NO POSITION IN THIS ONE</pre>
<pre>A1236/26 NOTAMN
Q) UKBV/QRTCA/IV/BO/W/000/999/4950N02400E012
A) UKBV B) 2001010000 C) 2001020000
E) ALREADY EXPIRED</pre>
</body></html>"""


class TestTheRawForm:
    """DINS hands back a page of raw ICAO NOTAMs rather than JSON.

    Every fact a mark needs -- where, how big, when, what it says -- lives in
    that text, and getting any of them wrong is a closure drawn in the wrong
    place or shown when it is over.
    """

    def one(self, which=0):
        blocks = notams.PRE.findall(RAW_PAGE)
        return notams.read_raw(notams._untag(blocks[which]), "UKBV")

    def test_the_position_comes_off_the_q_line(self):
        got = self.one()
        assert got["id"] == "A1234/26"
        assert got["location"] == "UKBV"
        assert round(got["lat"], 4) == 50.3333
        assert round(got["lon"], 4) == 30.5
        assert got["placed"] is True

    def test_the_radius_is_nautical_miles(self):
        # 030 on the Q-line is thirty nautical miles, not thirty kilometres.
        # Read as kilometres the closure is a bit over half its real size.
        assert self.one()["radius_km"] == 55.56

    def test_the_text_is_the_e_line_and_stops_at_the_next_one(self):
        said = self.one()["text"]
        assert said.startswith("AIRSPACE CLOSED TO ALL CIVIL TRAFFIC")
        # The F) and G) lines are levels, not part of what the notice says.
        assert "SFC" not in said and "FL660" not in said
        # And the page's escaping is undone rather than shown to anybody.
        assert "&" in said and "&amp;" not in said

    def test_the_dates_are_the_ten_digit_form(self):
        import datetime as dt
        got = self.one()
        assert got["from"] == dt.datetime(
            2026, 1, 15, tzinfo=dt.timezone.utc).timestamp()
        # PERM is no end at all, which is not an end in the past.
        assert got["to"] is None
        assert notams.in_force(got, got["from"] + 86_400) is True

    def test_a_notice_with_no_coordinate_is_listed_not_placed(self):
        got = self.one(1)
        assert got["id"] == "A1235/26"
        assert got["placed"] is False
        assert got["lat"] is None
        assert got["why_unplaced"]

    def test_an_expired_one_is_read_and_then_excluded(self):
        import time
        got = self.one(2)
        assert got["placed"] is True
        assert notams.in_force(got, time.time()) is False

    def test_a_coordinate_in_the_text_is_not_read_as_the_position(self):
        # A run of digits shaped like a coordinate turns up in E) lines as
        # bearings and boundary lists. Reading one as the position puts the
        # closure somewhere nobody filed it.
        got = notams.read_raw(
            "A1239/26 NOTAMN\nQ) UKBV/QRTCA/IV/BO/W/000/999/\n"
            "A) UKBV B) 2601150000 C) PERM\n"
            "E) AREA BOUNDED BY 5020N03030E THEN 5100N03100E")
        assert got["placed"] is False

    def test_rubbish_is_refused_rather_than_guessed_at(self):
        assert notams.read_raw("") is None
        assert notams.read_raw("nothing here at all") is None
        # A number but nothing said: a mark with no text is a dot nobody can
        # act on.
        assert notams.read_raw("A1234/26 NOTAMN\nA) UKBV") is None


class TestWhenOneDoorCloses:
    """Search refused every region with a 403. DINS is why that is survivable."""

    def setup_method(self):
        notams.forget()

    def test_dins_is_asked_when_search_refuses(self, monkeypatch):
        def refuse(*a, **kw):
            raise notams.NotamError("NOTAM Search refused the request (403)")

        monkeypatch.setattr(notams, "_ask_search", refuse)
        monkeypatch.setattr(notams, "_ask_dins", lambda code: RAW_PAGE)
        got = notams.over(28.0, 48.3, 35.6, 52.4)
        assert got["trouble"] == []
        assert [n["id"] for n in got["notams"]] == ["A1234/26"]
        assert [n["id"] for n in got["unplaced"]] == ["A1235/26"]
        assert "DINS" in got["source"]

    def test_and_both_refusals_are_named_when_both_refuse(self, monkeypatch):
        def refuse_search(*a, **kw):
            raise notams.NotamError("NOTAM Search refused the request (403)")

        def refuse_dins(*a, **kw):
            raise notams.NotamError("DINS refused the request (403)")

        monkeypatch.setattr(notams, "_ask_search", refuse_search)
        monkeypatch.setattr(notams, "_ask_dins", refuse_dins)
        got = notams.over(28.0, 48.3, 35.6, 52.4)
        assert got["notams"] == []
        assert got["trouble"]
        assert "Search refused" in got["trouble"][0]
        assert "DINS refused" in got["trouble"][0]

    def test_dins_is_not_asked_when_search_answers(self, monkeypatch):
        rang = []
        monkeypatch.setattr(notams, "_ask_search",
                            lambda code, offset=0: {"notamList": [],
                                                    "totalNotamCount": 0})
        monkeypatch.setattr(notams, "_ask_dins",
                            lambda code: rang.append(code) or RAW_PAGE)
        got = notams.over(28.0, 48.3, 35.6, 52.4)
        assert rang == []
        assert got["source"] == "FAA NOTAM Search"

    def test_a_page_with_no_notices_in_it_is_a_failure_not_an_empty_sky(
            self, monkeypatch):
        monkeypatch.setattr(notams, "_ask_dins", lambda code: "<html>no</html>")
        with pytest.raises(notams.NotamError):
            notams._walk_dins("UKBV", [])

    def test_a_quiet_region_is_not_reported_as_a_failure(self, monkeypatch):
        # A FIR with nothing filed against it is a real and common answer.
        # Reporting it as a failure put "DINS answered a page with no
        # notices in it" against five quiet regions at once.
        def refuse(*a, **kw):
            raise notams.NotamError("NOTAM Search refused the request (403)")

        monkeypatch.setattr(notams, "_ask_search", refuse)
        monkeypatch.setattr(
            notams, "_ask_dins",
            lambda code: RAW_PAGE if code == "UKBV" else
            "<html><body>No NOTAMs found</body></html>")
        got = notams.over(28.0, 48.3, 35.6, 52.4)
        assert got["trouble"] == []
        assert len(got["asked"]) > 1, "the quiet regions were not counted"
        assert [n["id"] for n in got["notams"]] == ["A1234/26"]

    def test_but_a_page_it_cannot_read_still_is_one(self, monkeypatch):
        monkeypatch.setattr(notams, "_ask_dins",
                            lambda code: "<html><body>???</body></html>")
        with pytest.raises(notams.NotamError):
            notams._walk_dins("UKBV", [])

    def test_search_is_asked_once_per_view_not_once_per_region(self,
                                                               monkeypatch):
        tried = []

        def refuse(location, offset=0):
            tried.append(location)
            raise notams.NotamError("NOTAM Search refused the request (403)")

        monkeypatch.setattr(notams, "_ask_search", refuse)
        monkeypatch.setattr(notams, "_ask_dins", lambda code: RAW_PAGE)
        got = notams.over(22.0, 44.0, 40.4, 52.5)
        assert len(got["asked"]) > 1, "this view should span several regions"
        assert len(tried) == 1, f"asked a refusing service {len(tried)} times"

    def test_and_a_later_view_tries_it_again(self, monkeypatch):
        # For this view rather than for ten minutes: a service having a
        # moment should cost one slow view, not an afternoon of never
        # asking it again.
        tried = []

        def refuse(location, offset=0):
            tried.append(location)
            raise notams.NotamError("NOTAM Search refused the request (403)")

        monkeypatch.setattr(notams, "_ask_search", refuse)
        monkeypatch.setattr(notams, "_ask_dins", lambda code: RAW_PAGE)
        notams.over(22.0, 44.0, 40.4, 52.5)
        notams.forget()
        notams.over(22.0, 44.0, 40.4, 52.5)
        assert len(tried) == 2

    def test_dins_does_not_claim_notices_are_missing(self, monkeypatch):
        # A notice filed against two adjacent FIRs is read twice and kept
        # once. Counting reads as "how many there are" made the panel say
        # notices had been left out when none had.
        def refuse(*a, **kw):
            raise notams.NotamError("NOTAM Search refused the request (403)")

        monkeypatch.setattr(notams, "_ask_search", refuse)
        monkeypatch.setattr(notams, "_ask_dins", lambda code: RAW_PAGE)
        got = notams.over(22.0, 44.0, 40.4, 52.5)
        assert got["capped"] is False
        # Three distinct notices, served for every region in the view. One of
        # them has expired, so two are live -- and "total" is the set, not
        # three times however many regions were asked.
        assert got["total"] == 3
        assert got["count"] == 2
