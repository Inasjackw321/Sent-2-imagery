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

# A name no table and no gazetteer will ever hold.
#
# These tests are about the plumbing around a lookup -- the rate limit, the
# cache, the country filter -- and they used real Latin place names as
# stand-ins. Once those names went into the built-in table, it answered first
# and the stub gazetteer underneath was never reached, so every one of these
# passed while testing nothing. An invented name cannot be short-circuited.
NOWHERE = "Zzyzxgrad"


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


class TestWhatSortOfPlace:
    """A drone was drawn in the middle of the Sea of Azov.

    "place" in OpenStreetMap is not only settlements: it also covers seas,
    oceans, straits, bays, islands, islets and peninsulas, all of which passed
    the category check. A report says a drone is over somewhere; the Sea of
    Azov is a somewhere, it is not what the report meant, and nothing about
    the coordinates afterwards says so.
    """

    def answer(self, category="place", kind="town", lat="46.2", lon="36.5"):
        return [{"lat": lat, "lon": lon, "display_name": "somewhere",
                 "category": category, "type": kind,
                 "boundingbox": ["46.0", "46.4", "36.3", "36.7"]}]

    def test_water_is_refused(self):
        for kind in ("sea", "ocean", "strait", "bay", "lake", "reservoir",
                     "river", "lagoon"):
            assert gazetteer.read_place(self.answer(kind=kind)) is None, kind

    def test_so_is_land_that_is_not_a_settlement(self):
        for kind in ("island", "islet", "archipelago", "peninsula", "desert",
                     "plain", "cape", "mountain_range"):
            assert gazetteer.read_place(self.answer(kind=kind)) is None, kind

    def test_settlements_are_taken(self):
        for kind in ("city", "town", "village", "hamlet", "borough", "suburb",
                     "municipality", "quarter", "neighbourhood"):
            assert gazetteer.read_place(self.answer(kind=kind)), kind

    def test_administrative_areas_are_taken(self):
        for kind in ("administrative", "region", "province", "district"):
            got = gazetteer.read_place(self.answer(category="boundary", kind=kind))
            assert got, kind

    def test_a_type_nobody_thought_of_is_refused_rather_than_accepted(self):
        # The list is an allowlist for a reason: an unexpected type refused is
        # one report in the panel instead of on the map, which is visible and
        # recoverable. An unexpected type accepted is a confident marker in
        # the wrong place.
        for kind in ("brewery", "quarry", "something_new", "farmyard"):
            assert gazetteer.read_place(self.answer(kind=kind)) is None, kind

    def test_a_result_with_no_type_at_all_is_still_taken(self):
        # Nominatim does not always send one, and refusing on its absence
        # would throw away good answers to guard against a bad one.
        got = gazetteer.read_place(self.answer(kind=""))
        assert got

    def test_the_first_settlement_is_used_even_behind_a_sea(self):
        # The whole point of asking for several candidates.
        found = (self.answer(kind="sea")
                 + self.answer(kind="town", lat="49.9", lon="36.2"))
        got = gazetteer.read_place(found)
        assert got and got["lat"] == 49.9


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
        gazetteer.find("Zzyzxgrad", "ua")
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
        gazetteer.find("Zzyzxgrad oblast", "ua")
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
            gazetteer.find("Zzyzxgrad", "ua")


class TestRemembering:
    def test_the_same_place_is_only_looked_up_once(self, monkeypatch):
        calls = []
        monkeypatch.setattr(gazetteer, "_ask", lambda name, countries: (
            calls.append(name), {"lat": 1.0, "lon": 2.0, "name": name, "kind": "town"})[1])
        for _ in range(5):
            gazetteer.find("Zzyzxgrad", "ua")
        assert calls == ["Zzyzxgrad"]

    def test_the_name_is_matched_regardless_of_case_and_spacing(self, monkeypatch):
        calls = []
        monkeypatch.setattr(gazetteer, "_ask", lambda name, countries: (
            calls.append(name), {"lat": 1.0, "lon": 2.0, "name": name, "kind": "town"})[1])
        gazetteer.find(f"{NOWHERE} oblast", "ua")
        gazetteer.find(f"  {NOWHERE.lower()}   OBLAST ", "ua")
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
        first = gazetteer.find("Zzyzxgrad", "ua")
        first["lat"] = 99.0
        assert gazetteer.find("Zzyzxgrad", "ua")["lat"] == 1.0

    def test_seeding_works_and_avoids_the_network(self, monkeypatch):
        monkeypatch.setattr(gazetteer, "_ask", lambda name, countries: pytest.fail(
            "the gazetteer went to the network for a place it had been given"))
        gazetteer.remember("Zzyzxgrad", "ua", {"lat": 1.0, "lon": 2.0,
                                             "name": "Zzyzxgrad", "kind": "town"})
        assert gazetteer.find("Zzyzxgrad", "ua")["lon"] == 2.0


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
        gazetteer.find("Zzyzxgrad", "ua")
        # Without this, "Zzyzxgrad" is as likely to match a street in another
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
            gazetteer.find("Zzyzxgrad", "ua")


class TestTheBuiltInTableComesFirst:
    """What makes the map fill in at once instead of over a minute.

    Nominatim is rate-limited to one request a second, by its own policy and
    by this module honouring it. That is fine for a place nobody has asked
    about and absurd for "Харків", which is asked for every poll and has not
    moved since 1654. Measured: placing a night's reports went from 14.5s to
    0.0s once the table answered first.
    """

    def setup_method(self):
        gazetteer.forget()
        # Nothing here may reach the network: the outline worker is a real
        # background thread and it would otherwise spend the suite's time
        # waiting on a host this environment cannot reach anyway.
        self.asked = []
        self.original = gazetteer._ask
        gazetteer._ask = self.watched

    def teardown_method(self):
        gazetteer._ask = self.original

    def watched(self, name, countries=""):
        self.asked.append(name)
        return None

    def test_a_known_name_is_answered_without_a_request(self):
        got = gazetteer.find("Харків", "ua")
        assert got is not None
        assert got["name"] == "Харків"
        assert self.asked == []

    def test_an_unknown_name_still_goes_to_nominatim(self):
        # The table is a cache that ships with the app, not a different way of
        # deciding where things are. Anything not in it behaves as before.
        gazetteer.find("Обоян", "ru")
        assert self.asked == ["Обоян"]

    def test_a_learned_outline_beats_the_built_in_centre(self):
        """Otherwise the background upgrade is thrown away every poll.

        The table holds a centre and an extent -- enough to put a mark down,
        not the shape of the province. Once a real boundary has been fetched
        it has to win, or find() goes on returning the circle forever and the
        worker's work is invisible.
        """
        real = {"lat": 51.0, "lon": 34.2, "name": "Сумська область",
                "kind": "administrative", "category": "boundary",
                "bbox": [50.0, 52.0, 33.0, 35.4],
                "shape": {"type": "Polygon", "coordinates": [[[33, 50]]]}}
        gazetteer.remember("Сумська область", "ua", real)
        got = gazetteer.find("Сумська область", "ua")
        assert got["shape"] == real["shape"]

    def test_a_region_from_the_table_queues_its_outline(self):
        # Asserted through improve_later rather than through the queue length:
        # the worker drains the queue in the background, so its length a
        # moment later is a race, while "has this already been asked for" is
        # not. False means find() got there first.
        gazetteer.find("Сумська область", "ua")
        assert gazetteer.improve_later("Сумська область", "ua") is False

    def test_a_city_from_the_table_does_not(self):
        # A strike in a town is a point in it. Drawing the municipal boundary
        # round one would claim the damage followed the council's border, and
        # it would spend the rate limit doing so.
        gazetteer.find("Харків", "ua")
        assert gazetteer.improve_later("Харків", "ua") is True

    def test_the_same_outline_is_not_queued_twice(self):
        gazetteer.find("Сумська область", "ua")
        for _ in range(5):
            assert gazetteer.improve_later("Сумська область", "ua") is False

    def test_queueing_an_outline_does_not_block(self):
        # The whole point: the mark is on the map from the built-in centre
        # while the boundary is still being waited for.
        started = time.time()
        for name in ("Сумська область", "Харківська область",
                     "Київська область", "Полтавська область"):
            gazetteer.find(name, "ua")
        assert time.time() - started < 0.5

    def test_the_outline_worker_gives_way_to_the_foreground(self):
        # It shares the one-a-second gate. Without a pause of its own it takes
        # every other slot, and a report waiting to be placed queues behind a
        # boundary nobody is looking at yet.
        assert gazetteer.IMPROVE_EVERY >= 1.0

    def test_forgetting_clears_what_was_queued_too(self):
        gazetteer.find("Сумська область", "ua")
        assert gazetteer.improve_later("Сумська область", "ua") is False
        gazetteer.forget()
        assert gazetteer.improve_later("Сумська область", "ua") is True


class TestALearnedOutlineIsActuallyUsed:
    """The boundary that was fetched, remembered, and never read.

    find() has always preferred a learned outline over the built-in centre.
    The trouble is that the hot path does not go through find(): tracker's
    _look reads the built-in table directly, because that is what turned an
    eleven-second poll into half a second. That table has centres and extents
    and no shapes, so once it answered, the outline the background worker had
    already fetched was never looked at again.

    Ukraine did not show it. NEPTUN publish Ukraine's boundaries and those
    come from their file, not from here. Russia has no such file, so every
    Russian warning was drawn as a marker with no province under it while the
    Ukrainian ones were properly shaped -- two halves of one layer looking
    completely different, for a reason that had nothing to do with what was
    being reported.
    """

    def setup_method(self):
        gazetteer.forget()

    def teardown_method(self):
        gazetteer.forget()

    def test_an_outline_learned_earlier_can_be_read_back(self):
        shape = {"type": "Polygon", "coordinates": [[[39.0, 51.0], [40.0, 51.0],
                                                     [40.0, 52.0], [39.0, 51.0]]]}
        gazetteer.remember("Воронежская область", "ru",
                           {"name": "Воронежская область", "lat": 51.6,
                            "lon": 39.2, "category": "boundary",
                            "bbox": (50.0, 38.0, 52.0, 41.0), "shape": shape})
        assert gazetteer.outline("Воронежская область", "ru") == shape

    def test_a_name_nobody_has_fetched_has_no_outline(self):
        assert gazetteer.outline("Воронежская область", "ru") is None

    def test_an_answer_without_a_shape_is_not_an_outline(self):
        # The built-in entry is exactly this: a centre and an extent. Reading
        # it back as an outline would put the bug back in a new place.
        gazetteer.remember("Липецкая область", "ru",
                           {"name": "Липецкая область", "lat": 52.6,
                            "lon": 39.6, "category": "boundary",
                            "bbox": (51.0, 38.0, 54.0, 41.0)})
        assert gazetteer.outline("Липецкая область", "ru") is None

    def test_it_never_asks(self):
        # It is read on the hot path, once per mark per poll. A lookup here
        # would put the rate limit back into the thing it was taken out of.
        asked = []
        original = gazetteer._ask
        gazetteer._ask = lambda name, countries="": asked.append(name)
        try:
            assert gazetteer.outline("Обоянь", "ru") is None
        finally:
            gazetteer._ask = original
        assert asked == []


class TestAnOutlineIsAskedForMoreThanOnce:
    """One failed request used to mean no outline until the process restarted.

    A name went into _asked_for_shapes when it was queued and never came out,
    so Nominatim being down for a minute, one rate-limit, or one answer that
    happened to arrive without a polygon left that region flat for the day.

    On the map it did not look like a fetch that failed. It looked like a
    permanent difference between two halves of the layer -- Ukraine's
    warnings shaded their province and Russia's did not -- because Ukraine's
    outlines come from NEPTUN's boundary file and never touch this queue.
    """

    def setup_method(self):
        gazetteer.forget()
        self.original = gazetteer._ask
        # The background worker is a real thread, and it races the calls to
        # _improve() below for the same queue. Telling improve_later that one
        # is already running keeps the draining in this test's hands, which
        # is the only way to assert what the SECOND ask returns.
        self.worker = gazetteer._improver
        gazetteer._improver = StillRunning()

    def teardown_method(self):
        gazetteer._ask = self.original
        gazetteer._improver = self.worker
        gazetteer.forget()

    def test_a_failed_ask_can_be_asked_again(self):
        assert gazetteer.improve_later("Брянская область", "ru") is True
        # Queued, so a second ask now is refused -- that part is unchanged.
        assert gazetteer.improve_later("Брянская область", "ru") is False
        gazetteer._try_again("Брянская область", "ru")
        assert gazetteer.improve_later("Брянская область", "ru") is True

    def test_an_answer_with_no_polygon_counts_as_a_failure(self):
        # Nominatim does not always return the same candidate for a name, so
        # an answer without a shape is worth another ask rather than a
        # verdict on the name.
        gazetteer.improve_later("Курская область", "ru")
        gazetteer._try_again("Курская область", "ru")
        assert gazetteer.improve_later("Курская область", "ru") is True

    def test_it_gives_up_eventually(self):
        # A name that has failed this many times probably has no polygon, and
        # asking forever would be a request every poll for something that is
        # not going to arrive.
        for _ in range(gazetteer.MOST_SHAPE_TRIES):
            gazetteer.improve_later("Нигдеевская область", "ru")
            gazetteer._try_again("Нигдеевская область", "ru")
        assert gazetteer.improve_later("Нигдеевская область", "ru") is False

    def test_a_success_clears_the_count(self):
        """A region that failed and then arrived starts again from zero.

        Taken right to the edge on purpose: failed MOST_SHAPE_TRIES - 1
        times, so one more strike would be the one that gives up on it.

        Checked on the tally itself rather than through improve_later, which
        answers False afterwards for a different and correct reason -- the
        outline is known now, so there is nothing to queue. That refusal
        would hide whether the count was cleared.
        """
        shape = {"type": "Polygon", "coordinates": [[[39.0, 51.0], [40.0, 51.0],
                                                     [40.0, 52.0], [39.0, 51.0]]]}
        for _ in range(gazetteer.MOST_SHAPE_TRIES - 1):
            gazetteer._try_again("Воронежская область", "ru")
        gazetteer._ask = lambda name, countries="": {
            "name": name, "lat": 51.6, "lon": 39.2, "category": "boundary",
            "bbox": [50.5, 52.5, 38.0, 41.0], "shape": shape}
        gazetteer.improve_later("Воронежская область", "ru")
        gazetteer._improve()
        assert gazetteer.outline("Воронежская область", "ru") == shape

        assert gazetteer._key("Воронежская область", "ru") \
            not in gazetteer._shape_tries
        assert gazetteer.stats()["outlines_given_up"] == 0

    def test_a_region_given_up_on_is_visible_rather_than_silent(self):
        for _ in range(gazetteer.MOST_SHAPE_TRIES):
            gazetteer._try_again("Нигдеевская область", "ru")
        assert gazetteer.stats()["outlines_given_up"] == 1

    def test_the_worker_retries_a_failed_ask(self):
        """The whole loop, not just the bookkeeping.

        Two answers: the first raises, the second carries a polygon. With the
        old behaviour the name was spent after the first and the outline was
        never learned.
        """
        shape = {"type": "Polygon", "coordinates": [[[33.0, 52.0], [34.0, 52.0],
                                                     [34.0, 53.0], [33.0, 52.0]]]}
        answers = [GazetteerErrorRaiser(), {
            "name": "Брянская область", "lat": 52.9, "lon": 33.5,
            "category": "boundary", "bbox": [52.0, 53.8, 31.0, 35.5],
            "shape": shape}]

        def flaky(name, countries=""):
            answer = answers.pop(0)
            if isinstance(answer, GazetteerErrorRaiser):
                raise gazetteer.GazetteerError("the gazetteer is rate limiting")
            return answer

        gazetteer._ask = flaky
        monkey = gazetteer.IMPROVE_EVERY
        gazetteer.IMPROVE_EVERY = 0.0
        try:
            gazetteer.improve_later("Брянская область", "ru")
            gazetteer._improve()
            assert gazetteer.outline("Брянская область", "ru") is None
            # The next poll asks for every region it draws, and this one is
            # now askable again.
            assert gazetteer.improve_later("Брянская область", "ru") is True
            gazetteer._improve()
            assert gazetteer.outline("Брянская область", "ru") == shape
        finally:
            gazetteer.IMPROVE_EVERY = monkey


class GazetteerErrorRaiser:
    """A marker for "this answer should raise". Not an exception itself."""


class StillRunning:
    """Stands in for a live outline worker, so no real thread is started."""

    def is_alive(self):
        return True
