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
