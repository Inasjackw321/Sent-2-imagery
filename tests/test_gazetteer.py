"""Tests for the gazetteer.

This module exists because of a specific failure. A language model asked for
coordinates always returns coordinates, and there is nothing in the number to
say whether it was recalled, interpolated or invented. Markers landed in the
wrong oblast and looked exactly like the correct ones.

So the tests that matter here are about refusing to answer. `find` returning
None is the feature: a place the gazetteer does not know must come back as not
known, not as a plausible pair of numbers.
"""

from __future__ import annotations

import time

import pytest

from backend import gazetteer


@pytest.fixture(autouse=True)
def clean():
    gazetteer.forget()
    yield
    gazetteer.forget()


def answer(**over):
    base = {"lat": "47.5665", "lon": "34.4053", "display_name": "Nikopol, Ukraine",
            "category": "place", "type": "town"}
    return [{**base, **over}]


class TestReadingAnAnswer:
    def test_a_normal_result(self):
        got = gazetteer.read_place(answer())
        assert got["lat"] == pytest.approx(47.5665)
        assert got["lon"] == pytest.approx(34.4053)
        assert got["name"] == "Nikopol, Ukraine"
        assert got["kind"] == "town"

    def test_nominatim_sends_strings_and_they_become_numbers(self):
        # It really does: "lat": "47.5665". Left as a string it would sail
        # through every range check and arrive at Leaflet as a NaN.
        got = gazetteer.read_place(answer())
        assert isinstance(got["lat"], float)

    def test_no_results_is_no_place(self):
        assert gazetteer.read_place([]) is None

    def test_rubbish_is_no_place_rather_than_an_exception(self):
        for junk in (None, {}, "nope", [None], [{"lat": "x", "lon": "y"}], [{}]):
            assert gazetteer.read_place(junk) is None

    def test_a_position_off_the_globe_is_refused(self):
        assert gazetteer.read_place(answer(lat="120")) is None
        assert gazetteer.read_place(answer(lon="-500")) is None


class TestRefusingThingsThatAreNotPlaces:
    """The Kaharlyk failure.

    A report reading "past Kaharlyk, on a course north" had its town name
    mangled to "Kagul". Nominatim's best match for that was озеро Кагул -- a
    lake, four hundred kilometres away in a different oblast -- and it answered
    with complete confidence. The marker went on the map next to correct ones.

    A drone is not reported over a lake, a shop or a roundabout. Refusing the
    whole category turns a wrong answer into no answer, which is the trade
    this module exists to make.
    """

    LAKE = {"lat": "45.9", "lon": "28.2", "display_name": "озеро Кагул",
            "category": "natural", "type": "water"}

    def test_a_lake_is_not_a_place_a_drone_is_reported_over(self):
        assert gazetteer.read_place([self.LAKE]) is None

    def test_the_settlement_behind_it_is_found_instead(self):
        # The real fix: several candidates are asked for, and the first one
        # that is actually a settlement wins rather than the first one at all.
        got = gazetteer.read_place([self.LAKE, *answer()])
        assert got["name"] == "Nikopol, Ukraine"

    def test_the_usual_suspects_are_all_refused(self):
        for category, kind in [("natural", "water"), ("waterway", "river"),
                               ("shop", "supermarket"), ("highway", "residential"),
                               ("building", "yes"), ("landuse", "farmland"),
                               ("amenity", "cafe"), ("leisure", "park")]:
            assert gazetteer.read_place(answer(category=category, type=kind)) is None, kind

    def test_settlements_and_administrative_areas_are_kept(self):
        for category, kind in [("place", "town"), ("place", "city"),
                               ("place", "village"), ("place", "hamlet"),
                               ("boundary", "administrative")]:
            got = gazetteer.read_place(answer(category=category, type=kind))
            assert got is not None, kind
            assert got["category"] == category

    def test_a_result_with_no_category_is_given_the_benefit_of_the_doubt(self):
        # Not every deployment sends one, and refusing everything unlabelled
        # would turn the guard into an outage.
        got = gazetteer.read_place([{"lat": "47.5", "lon": "34.4",
                                     "display_name": "Somewhere"}])
        assert got is not None

    def test_a_list_of_nothing_usable_is_none(self):
        assert gazetteer.read_place([self.LAKE, self.LAKE]) is None

    def test_several_candidates_are_asked_for(self, monkeypatch):
        # With limit=1 the lake would be the only answer available and the
        # filter above would turn a wrong marker into no marker -- better, but
        # the town is right there behind it.
        seen = {}

        class Reply:
            status_code, ok = 200, True

            @staticmethod
            def json():
                return answer()

        monkeypatch.setattr(gazetteer.requests, "get",
                            lambda url, params=None, **kw: (seen.update(params), Reply())[1])
        gazetteer._last_call = time.time() - 99
        gazetteer.find("Kaharlyk", "ua")
        assert seen["limit"] > 1


class TestOutlines:
    """A warning over an oblast is about the oblast, not a circle in it."""

    RING = {"type": "Polygon",
            "coordinates": [[[30, 50], [31, 50], [31, 51], [30, 51], [30, 50]]]}

    def test_a_polygon_is_kept(self):
        assert gazetteer.read_shape(self.RING) == self.RING

    def test_a_multipolygon_is_kept(self):
        # An oblast with an exclave, or a coastline with islands.
        many = {"type": "MultiPolygon", "coordinates": [self.RING["coordinates"]]}
        assert gazetteer.read_shape(many) is not None

    def test_a_point_or_a_line_is_not_an_area(self):
        # Nominatim returns these for plenty of places -- a river, a road, a
        # single node. Drawing a warning as a one-pixel dot or a squiggle is
        # worse than the circle it replaces.
        assert gazetteer.read_shape({"type": "Point", "coordinates": [30, 50]}) is None
        assert gazetteer.read_shape(
            {"type": "LineString", "coordinates": [[30, 50], [31, 51]]}) is None
        # A long one, which the size guard lets through: only the type check
        # stops a hundred-kilometre river being drawn as an air-raid warning.
        river = {"type": "LineString",
                 "coordinates": [[30 + i * 0.01, 50] for i in range(200)]}
        assert gazetteer.read_shape(river) is None
        assert gazetteer.read_shape(
            {"type": "GeometryCollection", "geometries": []}) is None

    def test_rubbish_is_refused_rather_than_thrown(self):
        for junk in (None, {}, [], "polygon", {"type": "Polygon"},
                     {"type": "Polygon", "coordinates": None}):
            assert gazetteer.read_shape(junk) is None

    def test_something_enormous_is_dropped_so_a_circle_is_used_instead(self):
        # A coastline at full resolution is a megabyte to draw one warning
        # with. Better no shape, and the caller falls back.
        huge = {"type": "Polygon",
                "coordinates": [[[i * 0.001, 50] for i in range(gazetteer.MAX_POINTS + 5)]]}
        assert gazetteer.read_shape(huge) is None

    def test_counting_points_reaches_into_nested_rings(self):
        assert gazetteer.count_points(self.RING) == 5
        assert gazetteer.count_points(
            {"type": "MultiPolygon",
             "coordinates": [self.RING["coordinates"], self.RING["coordinates"]]}) == 10
        assert gazetteer.count_points({"coordinates": []}) == 0

    def test_a_shape_is_carried_on_the_place(self):
        got = gazetteer.read_place(answer(geojson=self.RING))
        assert got["shape"] == self.RING

    def test_a_place_with_no_shape_simply_has_none(self):
        assert gazetteer.read_place(answer())["shape"] is None

    def test_the_outline_is_asked_for_but_simplified(self, monkeypatch):
        # At full resolution an oblast is tens of thousands of points. The
        # threshold is what makes asking for it affordable at all.
        seen = {}

        class Reply:
            status_code, ok = 200, True

            @staticmethod
            def json():
                return answer()

        monkeypatch.setattr(gazetteer.requests, "get",
                            lambda url, params=None, **kw: (seen.update(params), Reply())[1])
        gazetteer._last_call = time.time() - 99
        gazetteer.find("Kyiv oblast", "ua")
        assert seen.get("polygon_geojson") == 1
        assert 0 < float(seen.get("polygon_threshold", 0)) < 1


class TestNotKnowing:
    """The point of the module."""

    def test_an_unknown_place_is_none_not_a_guess(self, monkeypatch):
        monkeypatch.setattr(gazetteer, "_ask", lambda name, countries: None)
        assert gazetteer.find("Nowheresville", "ua") is None

    def test_an_empty_name_never_reaches_the_network(self, monkeypatch):
        asked = []
        monkeypatch.setattr(gazetteer, "_ask",
                            lambda name, countries: asked.append(name))
        for junk in ("", "  ", None, "x"):
            assert gazetteer.find(junk, "ua") is None
        assert asked == []

    def test_a_failure_to_reach_it_is_raised_not_swallowed(self, monkeypatch):
        def boom(name, countries):
            raise gazetteer.GazetteerError("the gazetteer could not be reached: down")
        monkeypatch.setattr(gazetteer, "_ask", boom)
        with pytest.raises(gazetteer.GazetteerError):
            gazetteer.find("Nikopol", "ua")


class TestRemembering:
    def test_the_same_place_is_only_looked_up_once(self, monkeypatch):
        calls = []
        monkeypatch.setattr(gazetteer, "_ask", lambda name, countries: (
            calls.append(name), {"lat": 1.0, "lon": 2.0, "name": name, "kind": "town"})[1])
        for _ in range(5):
            gazetteer.find("Nikopol", "ua")
        assert calls == ["Nikopol"]

    def test_the_name_is_matched_regardless_of_case_and_spacing(self, monkeypatch):
        calls = []
        monkeypatch.setattr(gazetteer, "_ask", lambda name, countries: (
            calls.append(name), {"lat": 1.0, "lon": 2.0, "name": name, "kind": "town"})[1])
        gazetteer.find("Kharkiv oblast", "ua")
        gazetteer.find("  kharkiv   OBLAST ", "ua")
        assert len(calls) == 1

    def test_two_countries_are_two_different_questions(self, monkeypatch):
        # "Tripoli" is a real place in Lebanon and a different real place in
        # Libya. Caching them under one key would answer one channel with the
        # other's coordinates.
        calls = []
        monkeypatch.setattr(gazetteer, "_ask", lambda name, countries: (
            calls.append(countries), {"lat": 1.0, "lon": 2.0, "name": countries,
                                      "kind": "city"})[1])
        gazetteer.find("Tripoli", "lb")
        gazetteer.find("Tripoli", "ly")
        assert calls == ["lb", "ly"]

    def test_a_miss_is_remembered_but_not_for_ever(self, monkeypatch):
        calls = []
        monkeypatch.setattr(gazetteer, "_ask",
                            lambda name, countries: calls.append(name))
        gazetteer.find("Nowhere", "ua")
        gazetteer.find("Nowhere", "ua")
        assert len(calls) == 1                        # not asked twice
        monkeypatch.setattr(gazetteer, "MISS_SECONDS", -1)
        gazetteer.find("Nowhere", "ua")
        assert len(calls) == 2                        # but not cached for ever

    def test_a_remembered_answer_cannot_be_mutated_by_its_caller(self, monkeypatch):
        monkeypatch.setattr(gazetteer, "_ask", lambda name, countries: {
            "lat": 1.0, "lon": 2.0, "name": name, "kind": "town"})
        first = gazetteer.find("Nikopol", "ua")
        first["lat"] = 99.0
        assert gazetteer.find("Nikopol", "ua")["lat"] == 1.0

    def test_seeding_works_and_avoids_the_network(self, monkeypatch):
        monkeypatch.setattr(gazetteer, "_ask", lambda name, countries: pytest.fail(
            "the gazetteer went to the network for a place it had been given"))
        gazetteer.remember("Nikopol", "ua", {"lat": 1.0, "lon": 2.0,
                                             "name": "Nikopol", "kind": "town"})
        assert gazetteer.find("Nikopol", "ua")["lon"] == 2.0


class TestPoliteness:
    def test_it_will_not_ask_twice_in_the_same_second(self):
        # Nominatim's usage policy is one request a second, and being cut off
        # would take the feature down rather than slow it.
        gazetteer._last_call = 0.0
        started = time.time()
        gazetteer.wait_turn()
        gazetteer.wait_turn()
        assert time.time() - started >= gazetteer.MIN_INTERVAL * 0.9

    def test_the_country_filter_is_actually_sent(self, monkeypatch):
        seen = {}

        class Reply:
            status_code = 200
            ok = True

            @staticmethod
            def json():
                return answer()

        def fake_get(url, params=None, **kw):
            seen.update(params or {})
            return Reply()

        monkeypatch.setattr(gazetteer.requests, "get", fake_get)
        gazetteer._last_call = time.time() - 99
        gazetteer.find("Sumy", "ua")
        # Without this, "Sumy" is as likely to match a street in another
        # hemisphere, and half the point is that reports land in the right
        # country.
        assert seen.get("countrycodes") == "ua"

    def test_being_rate_limited_says_so(self, monkeypatch):
        class Reply:
            status_code = 429
            ok = False

        monkeypatch.setattr(gazetteer.requests, "get", lambda *a, **k: Reply())
        gazetteer._last_call = time.time() - 99
        with pytest.raises(gazetteer.GazetteerError, match="rate limiting"):
            gazetteer.find("Sumy", "ua")
