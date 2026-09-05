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
            "type": "town"}
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
