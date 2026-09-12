"""Tests for the air-threat layer.

The feature has two ways of being wrong and only one of them is loud.

The loud one is a crash: Telegram changes its markup, the model returns prose
where JSON was asked for, nothing appears. That is a nuisance.

The quiet one is a marker in a place nobody reported. The first version of
this asked the model for coordinates, which is the wrong job to give a model:
asked for a latitude it always produces one, right for a capital city and
recalled, interpolated or invented for anywhere smaller, with nothing in the
number to say which. Drawn on a map next to real reports it is
indistinguishable from evidence.

So the tests here are mostly about refusal and about the seam. The model is
asked for names and its numbers are ignored; a gazetteer decides where things
are and is allowed to say it does not know; and a report that cannot be placed
must still reach the reader as text, because the previous version dropped
those silently and made a patchy night look like a broken feature.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import time

import pytest

from backend import ollama, tracker


class Reply:
    """The shape of a requests response, for the bits these tests touch."""

    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload
        self.text = text or ""
        self.headers = {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def fake_find(name, countries=""):
    """A gazetteer that knows a handful of places and nothing else."""
    known = {
        "Нікополь": (47.5665, 34.4053), "Нікополем": (47.5665, 34.4053), "Nikopol": (47.5665, 34.4053),
        "Kharkiv": (49.9935, 36.2304), "Kyiv": (50.4501, 30.5234),
    }
    for key, (lat, lon) in known.items():
        if key.lower() in name.lower() or name.lower() in key.lower():
            return {"lat": lat, "lon": lon, "name": key, "kind": "town",
                    "category": "place",
                    "bbox": [lat - 0.05, lat + 0.05, lon - 0.05, lon + 0.05],
                    "shape": None}
    return None

# One message, in the shape Telegram's public preview actually serves it.
PAGE = """
<div class="tgme_widget_message " data-post="eRadarrua/12345">
  <div class="tgme_widget_message_text js-message_text">
    &#1041;&#1055;&#1051;&#1040; &#1085;&#1072; Nikopol,<br/>&#1082;&#1091;&#1088;&#1089; &#1085;&#1072; Marhanets
  </div>
  <span class="tgme_widget_message_meta">
    <time datetime="2026-09-01T11:52:00+00:00">14:52</time>
  </span>
</div>
<div class="tgme_widget_message " data-post="eRadarrua/12346">
  <div class="tgme_widget_message_text js-message_text">Second post</div>
  <time datetime="2026-09-01T11:55:00+00:00">14:55</time>
</div>
"""

PLACES = {
    "Nikopol": (47.5665, 34.4053),
    "Kherson": (46.6354, 32.6169),
    "Kharkiv oblast": (49.7, 36.3),
    "Kyiv": (50.4501, 30.5234),
}


def gazetteer(name, countries=""):
    """A gazetteer that knows four places and admits the rest."""
    found = PLACES.get(name)
    if not found:
        return None
    return {"lat": found[0], "lon": found[1], "name": f"{name}, Ukraine", "kind": "city"}


def one(**over):
    return {"kind": "drone", "place": "Nikopol", "region": None, "toward": None,
            "course": None, "count": 1, "summary": "Drone over Nikopol", **over}


class TestReadingTheChannel:
    def test_each_post_keeps_its_own_id_and_time(self):
        got = tracker.parse_preview(PAGE, "eRadarrua")
        assert [p["id"] for p in got] == ["eRadarrua/12345", "eRadarrua/12346"]
        assert got[0]["when"] == "2026-09-01T11:52:00+00:00"
        assert got[0]["channel"] == "eRadarrua"

    def test_the_text_comes_out_as_text(self):
        body = tracker.parse_preview(PAGE, "eRadarrua")[0]["text"]
        assert "Nikopol" in body and "Marhanets" in body
        assert "<" not in body and "&#" not in body

    def test_a_post_does_not_swallow_the_next_one(self):
        got = tracker.parse_preview(PAGE, "eRadarrua")
        assert "Second post" not in got[0]["text"]

    def test_a_post_with_no_text_is_skipped(self):
        page = ('<div class="tgme_widget_message " data-post="c/1">'
                '<div class="tgme_widget_message_text"></div>'
                '<time datetime="2026-09-01T11:00:00+00:00">x</time></div>')
        assert tracker.parse_preview(page, "c") == []

    def test_a_page_of_something_else_is_empty_not_an_error(self):
        assert tracker.parse_preview("<html><body>nope</body></html>", "c") == []


class TestReadingTheModel:
    def test_the_shape_that_was_asked_for(self):
        got = tracker.read_events(json.dumps({"events": [
            {"id": "c/1", "kind": "drone", "place": "Nikopol",
             "toward": "Kherson", "count": 3, "summary": "Three drones"}]}))
        assert len(got) == 1
        assert got[0]["place"] == "Nikopol" and got[0]["count"] == 3
        assert got[0]["id"] == "c/1"

    def test_a_code_fence_is_survived(self):
        got = tracker.read_events('```json\n{"events": [{"kind": "drone", "place": "Nikopol"}]}\n```')
        assert len(got) == 1

    def test_a_sentence_before_the_json_is_survived(self):
        got = tracker.read_events('Here you go:\n{"events": [{"kind": "cruise", "place": "Kyiv"}]}')
        assert got[0]["kind"] == "cruise"

    def test_a_bare_list_is_survived(self):
        assert len(tracker.read_events('[{"kind": "drone", "place": "Nikopol"}]')) == 1

    def test_json_that_is_not_json_is_refused_clearly(self):
        with pytest.raises(tracker.TrackerError, match="not JSON"):
            tracker.read_events("I could not find any events in these messages.")

    def test_a_kind_nobody_offered_becomes_unknown_rather_than_a_gap(self):
        got = tracker.read_events('{"events": [{"kind": "doom ray", "place": "Kyiv"}]}')
        assert got[0]["kind"] == "unknown"
        assert got[0]["kind"] in tracker.KINDS

    def test_rubbish_among_good_events_does_not_take_them_with_it(self):
        got = tracker.read_events(json.dumps({"events": [
            "not a dict", {"kind": "drone", "place": "Nikopol"}]}))
        assert len(got) == 1


class TestThePlaceNamesTheModelGives:
    """What comes back as a place name and is not one."""

    def test_a_model_that_shrugs_is_not_given_a_place(self):
        for shrug in ("unknown", "N/A", "not specified", "various", "-", "none"):
            got = tracker.read_events(json.dumps({"events": [
                {"kind": "drone", "place": shrug}]}))
            assert got[0]["place"] is None, shrug

    def test_coordinates_in_the_place_field_are_refused(self):
        # It is told not to give coordinates. When it does anyway they arrive
        # here, and a gazetteer asked for "47.5, 34.4" either misses or
        # matches something absurd -- so the string is rejected as a name.
        for smuggled in ("47.5665, 34.4053", "49.99 N 36.23 E", "50.45,30.52"):
            got = tracker.read_events(json.dumps({"events": [
                {"kind": "drone", "place": smuggled}]}))
            assert got[0]["place"] is None, smuggled

    def test_a_real_place_name_survives_all_of_that(self):
        got = tracker.read_events(json.dumps({"events": [
            {"kind": "drone", "place": "Kamianets-Podilskyi"}]}))
        assert got[0]["place"] == "Kamianets-Podilskyi"

    def test_heading_for_where_it_already_is_is_not_a_journey(self):
        got = tracker.read_events(json.dumps({"events": [
            {"kind": "drone", "place": "Nikopol", "toward": "nikopol"}]}))
        assert got[0]["toward"] is None

    def test_whatever_coordinates_the_model_sent_are_not_kept(self):
        # The single most important assertion in this file. The model may
        # return lat/lon; nothing downstream may ever see them.
        got = tracker.read_events(json.dumps({"events": [
            {"kind": "drone", "place": "Nikopol", "lat": 12.3, "lon": 45.6,
             "heading": 275}]}))[0]
        assert "lat" not in got and "lon" not in got and "heading" not in got


class TestCompassCourses:
    """The other half of the Kaharlyk picture.

    "повз Кагарлик курсом на північ" -- past Kaharlyk, on a course north --
    gives a direction and names no destination. The version that only
    understood destinations left the heading null, so the marker was drawn
    with the starburst that means "brought down": the opposite of what the
    report said, on a thing that was still flying.
    """

    def test_the_eight_points(self):
        for word, degrees in [("N", 0), ("NE", 45), ("E", 90), ("SE", 135),
                              ("S", 180), ("SW", 225), ("W", 270), ("NW", 315)]:
            assert tracker.read_course(word) == degrees

    def test_the_in_between_points_too(self):
        assert tracker.read_course("NNE") == 22.5
        assert tracker.read_course("wsw") == 247.5

    def test_written_out_in_words(self):
        assert tracker.read_course("north") == 0
        assert tracker.read_course("South-West") == 225
        assert tracker.read_course("northeast") == 45

    def test_anything_else_is_no_course_rather_than_a_guess(self):
        for junk in (None, "", "  ", "up", "towards Kyiv", "45", "north-ish", 12):
            assert tracker.read_course(junk) is None, junk

    def test_a_course_becomes_a_heading_with_no_destination_needed(self):
        got = tracker.place_event(one(place="Nikopol", course=0.0), "ua", lookup=gazetteer)
        assert got["placed"] is True
        assert got["heading"] == 0.0
        assert got["motion"] == "track"

    def test_a_named_destination_still_beats_a_compass_course(self):
        # The course is a direction; a destination is a direction AND a place
        # to stop. Where a report gives both, the one with more in it wins.
        got = tracker.place_event(one(place="Nikopol", toward="Kherson", course=0.0),
                                "ua", lookup=gazetteer)
        assert got["heading"] != 0.0
        assert got["dest_km"] is not None

    def test_a_strike_is_given_no_course_however_the_sentence_reads(self):
        got = tracker.read_events(json.dumps({"events": [
            {"kind": "explosion", "place": "Kyiv", "course": "N", "toward": "Lviv"}]}))[0]
        assert got["course"] is None and got["toward"] is None


class TestKindsAndHowTheyLast:
    """What replaced the speed tests.

    Every kind used to carry a speed, and the tests here checked that a jet
    drone outran a propeller one and that missiles outran both. None of that
    exists: nothing is carried anywhere, so a speed would be a number the app
    holds and never uses. What still matters about a kind is how it is drawn,
    how it ranks in the stream, and how long it stays.
    """

    def test_every_kind_is_complete(self):
        for name, look in tracker.KINDS.items():
            assert look["colour"].startswith("#"), name
            assert look["label"] and look["rank"], name
            assert look["motion"] in ("track", "orbit", "still"), name

    def test_no_kind_carries_a_speed_any_more(self):
        # Guarding against it coming back with the movement it existed for.
        for name, look in tracker.KINDS.items():
            assert "speed" not in look, name

    def test_the_derived_tables_agree_with_the_one_they_come_from(self):
        assert tracker.MOTION == {k: v["motion"] for k, v in tracker.KINDS.items()}
        assert tracker.KEEP == {
            k: v.get("keep", tracker.KEEP_MINUTES) for k, v in tracker.KINDS.items()}

    def test_a_strike_stays_far_longer_than_anything_in_flight(self):
        # The whole point of the per-kind clock: where a strike happened is
        # still true six hours later, and a report of a drone crossing an
        # oblast is not.
        assert tracker.KEEP["explosion"] >= 6 * tracker.KEEP["drone"]
        for kind in ("drone", "jet_drone", "cruise", "ballistic", "recon"):
            assert tracker.KEEP[kind] < tracker.KEEP["explosion"], kind

    def test_a_strike_outranks_everything_in_the_stream(self):
        assert tracker.KINDS["explosion"]["rank"] == max(
            look["rank"] for look in tracker.KINDS.values())

    def test_only_the_things_that_do_not_fly_stand_still(self):
        for name in tracker.NOT_AIRBORNE:
            assert tracker.MOTION[name] == "still", name
        assert set(tracker.NOT_AIRBORNE) == {"explosion", "alert"}

    def test_recon_loiters_rather_than_travelling(self):
        # Kept as a fact about the kind even though nothing circles any more:
        # it is why a recon drone's mark means "over here somewhere" rather
        # than "at this point".
        assert tracker.MOTION["recon"] == "orbit"


class TestTellingSimilarPlacesApart:
    """The rest of the Kaharlyk failure: the wrong Kaharlyk."""

    def test_the_region_is_used_to_ask_a_narrower_question(self):
        asked = []

        def watching(name, countries):
            asked.append(name)
            return {"lat": 49.85, "lon": 30.81, "name": name, "kind": "town"}

        tracker.place_event(one(place="Kaharlyk", region="Kyiv oblast"),
                          "ua", lookup=watching)
        assert asked[0] == "Kaharlyk, Kyiv oblast"

    def test_the_bare_name_is_tried_when_the_region_finds_nothing(self):
        # The gazetteer may spell the oblast differently, and a right town
        # found without the region beats no town at all.
        asked = []

        def only_bare(name, countries):
            asked.append(name)
            if "," in name:
                return None
            return {"lat": 49.85, "lon": 30.81, "name": name, "kind": "town"}

        got = tracker.place_event(one(place="Kaharlyk", region="Kyivshchyna"),
                                "ua", lookup=only_bare)
        assert asked == ["Kaharlyk, Kyivshchyna", "Kaharlyk"]
        assert got["placed"] is True

    def test_a_region_already_in_the_name_is_not_repeated(self):
        asked = []

        def watching(name, countries):
            asked.append(name)
            return {"lat": 49.7, "lon": 36.3, "name": name, "kind": "state"}

        tracker.place_event(one(place="Kharkiv oblast", region="Kharkiv oblast"),
                          "ua", lookup=watching)
        assert asked == ["Kharkiv oblast"]


class TestPlacing:
    def test_a_known_place_gets_the_gazetteer_position(self):
        got = tracker.place_event(one(), "ua", lookup=gazetteer)
        assert got["placed"] is True
        assert (got["lat"], got["lon"]) == PLACES["Nikopol"]
        assert got["place_match"] == "Nikopol, Ukraine"

    def test_an_unknown_place_is_not_placed_and_says_why(self):
        got = tracker.place_event(one(place="Nowheresville"), "ua", lookup=gazetteer)
        assert got["placed"] is False
        assert got["lat"] is None
        assert "Nowheresville" in got["why_unplaced"]

    def test_no_place_at_all_is_not_placed_and_says_why(self):
        got = tracker.place_event(one(place=None), "ua", lookup=gazetteer)
        assert got["placed"] is False
        assert got["why_unplaced"] == "the report names no place"

    def test_a_gazetteer_that_is_down_does_not_take_the_report_with_it(self):
        def broken(name, countries):
            raise tracker.gazetteer.GazetteerError("the gazetteer is rate limiting")
        got = tracker.place_event(one(), "ua", lookup=broken)
        assert got["placed"] is False
        assert "rate limiting" in got["why_unplaced"]

    def test_a_destination_gives_a_course_and_a_distance(self):
        got = tracker.place_event(one(toward="Kherson"), "ua", lookup=gazetteer)
        # Kherson is south-west of Nikopol.
        assert 220 < got["heading"] < 245
        assert got["dest_km"] == pytest.approx(174, abs=10)

    def test_the_course_is_computed_not_taken_on_trust(self):
        # Whatever the model thought the bearing was never reaches here: the
        # only source of a heading is the two gazetteer positions.
        got = tracker.place_event(one(toward="Kherson"), "ua", lookup=gazetteer)
        assert got["heading"] == pytest.approx(
            tracker.bearing(*PLACES["Nikopol"], *PLACES["Kherson"]), abs=0.01)

    def test_a_destination_the_gazetteer_does_not_know_leaves_it_still(self):
        # Placed, because the report's own location is known -- but with no
        # course, because there is nothing to compute one from.
        got = tracker.place_event(one(toward="Nowheresville"), "ua", lookup=gazetteer)
        assert got["placed"] is True
        assert got["heading"] is None and got["dest_km"] is None

    def test_a_destination_that_resolves_to_the_same_point_is_not_a_journey(self):
        def same(name, countries):
            return {"lat": 50.0, "lon": 30.0, "name": name, "kind": "city"}
        got = tracker.place_event(one(toward="Kyiv"), "ua", lookup=same)
        assert got["heading"] is None and got["dest_km"] is None

    def test_a_strike_does_not_travel_however_the_report_reads(self):
        # "Explosions in Kherson, drones heading for Mykolaiv" is one message.
        # The strike is where it is; only the airborne thing has a course.
        got = tracker.place_event(
            one(kind="explosion", place="Nikopol", toward="Kherson"),
            "ua", lookup=gazetteer)
        assert got["placed"] is True
        assert got["heading"] is None

    def test_an_air_alert_does_not_travel_either(self):
        got = tracker.place_event(
            one(kind="alert", place="Nikopol", toward="Kherson"), "ua", lookup=gazetteer)
        assert got["heading"] is None


class TestHowLongThingsStay:
    """A strike does not move, so nothing about it decays.

    The twenty-minute default is about dead reckoning, not about news: a
    position extrapolated from one report gets worse every second. That
    reasoning applies to something in flight and to nothing else. Where a
    strike happened is where it happened, and that is as true six hours later
    as it was at the time.
    """

    def test_a_strike_is_held_for_hours(self):
        assert tracker.keep_minutes("explosion") >= 180

    def test_things_in_flight_still_go_quickly(self):
        for kind in ("drone", "jet_drone", "cruise", "ballistic", "recon",
                     "aircraft", "helicopter"):
            assert tracker.keep_minutes(kind) == tracker.KEEP_MINUTES, kind

    def test_a_kind_that_says_nothing_gets_the_default(self):
        assert tracker.keep_minutes("nothing like this") == tracker.KEEP_MINUTES

    def test_an_old_strike_is_kept_and_an_old_drone_is_not(self):
        now = time.time()
        old = now - 3 * 3600
        assert tracker._alive({"kind": "explosion", "seen": old}, now) is True
        assert tracker._alive({"kind": "drone", "seen": old}, now) is False

    def test_a_strike_does_go_eventually(self):
        now = time.time()
        gone = now - (tracker.keep_minutes("explosion") + 1) * 60
        assert tracker._alive({"kind": "explosion", "seen": gone}, now) is False

    def test_a_report_never_leaves_the_list_while_its_marker_is_on_the_map(self):
        # A burst over a town with nothing in the panel to explain it is worse
        # than either on its own, and the alert window is shorter than a
        # strike's life, so the stream has to stretch to cover it.
        for kind in tracker.KINDS:
            held = max(tracker.ALERT_MINUTES, tracker.keep_minutes(kind))
            assert held >= tracker.keep_minutes(kind), kind

    def test_expiry_keeps_the_strike_and_drops_the_drone(self):
        now = time.time()
        tracker.reset()
        try:
            with tracker._lock:
                tracker._events.extend([
                    {"kind": "explosion", "seen": now - 3 * 3600, "heading": None,
                     "origin_lat": 50.0, "origin_lon": 30.0},
                    {"kind": "drone", "seen": now - 3 * 3600, "heading": None,
                     "origin_lat": 50.0, "origin_lon": 30.0},
                ])
                tracker._alerts.extend([
                    {"kind": "explosion", "seen": now - 3 * 3600, "placed": True},
                    {"kind": "drone", "seen": now - 3 * 3600, "placed": True},
                ])
                tracker._expire(now)
                assert [e["kind"] for e in tracker._events] == ["explosion"]
                assert [a["kind"] for a in tracker._alerts] == ["explosion"]
        finally:
            tracker.reset()

    def test_the_demo_shows_a_fresh_strike_and_an_old_one(self):
        # Otherwise the build with no network only ever draws markers at full
        # strength, and whether an old strike reads as old cannot be checked.
        ages = sorted(e["age_minutes"] for e in tracker.demo()["events"]
                      if e["kind"] == "explosion")
        assert len(ages) >= 2
        assert ages[0] < 30 and ages[-1] > 120

    def test_the_lifetimes_reach_the_browser(self):
        # The browser fades a marker against its OWN lifetime, so it needs the
        # table rather than the one default.
        for feed in (tracker.demo(), tracker.current()):
            assert feed["keep"]["explosion"] > feed["keep"]["drone"]
            assert set(feed["keep"]) == set(tracker.KINDS)


class TestTheAreaAnAlertCovers:
    """An air-raid warning is about a region, not a point.

    The size has to come from the gazetteer rather than a constant, because
    the two things this draws are enormously different: a strike in a village
    is a couple of kilometres across and a warning over an oblast is a
    hundred. One size for both would either lose the strike in a blob or
    shrink the oblast to a dot.
    """

    def test_an_oblast_is_drawn_far_wider_than_a_town(self):
        oblast = tracker.area_km({"bbox": [49.2, 51.6, 29.2, 32.2], "kind": "administrative"})
        town = tracker.area_km({"bbox": [49.84, 49.87, 30.79, 30.83], "kind": "town"})
        assert oblast > town * 10

    def test_it_is_half_the_diagonal_of_what_the_gazetteer_measured(self):
        box = [49.2, 51.6, 29.2, 32.2]
        want = tracker.separation(box[0], box[2], box[1], box[3]) / 2
        assert tracker.area_km({"bbox": box}) == pytest.approx(min(want, tracker.AREA_MAX_KM),
                                                             abs=0.2)

    def test_a_place_with_no_extent_falls_back_on_what_sort_it_is(self):
        assert tracker.area_km({"kind": "village"}) < tracker.area_km({"kind": "city"})
        assert tracker.area_km({"kind": "city"}) < tracker.area_km({"kind": "administrative"})

    def test_a_place_with_nothing_at_all_still_gets_a_sensible_size(self):
        assert 1 < tracker.area_km({}) < tracker.AREA_MAX_KM

    def test_a_country_sized_box_is_capped(self):
        # Ukraine's own bounding box would otherwise shade a continent, and a
        # warning is never that. A cap is a smaller lie than the alternative.
        assert tracker.area_km({"bbox": [44.0, 52.4, 22.1, 40.2]}) == tracker.AREA_MAX_KM

    def test_a_degenerate_box_does_not_give_a_zero_radius(self):
        assert tracker.area_km({"bbox": [50.0, 50.0, 30.0, 30.0], "kind": "town"}) > 0

    def test_the_area_reaches_the_events_that_need_it(self):
        for event in tracker.demo()["events"]:
            assert event["area_km"] > 0, event["place"]

    def test_the_demo_has_both_a_wide_alert_and_a_narrow_strike(self):
        # Otherwise the build with no network draws one size and hides
        # whether the other is right.
        sizes = [e["area_km"] for e in tracker.demo()["events"]
                 if e["motion"] == "still"]
        assert len(sizes) >= 2
        assert max(sizes) > min(sizes)


class TestRegionWideAlerts:
    """A warning naming a region is about the region.

    A circle over the middle of an oblast both misses ground the warning
    covers and covers ground it does not, and at the size of a province that
    is not a rounding error. Where the gazetteer knows the boundary, that is
    what gets drawn.
    """

    RING = {"type": "Polygon",
            "coordinates": [[[30, 50], [31, 50], [31, 51], [30, 51], [30, 50]]]}

    def looks_up(self, **over):
        def found(name, countries=""):
            return {"lat": 50.4, "lon": 30.5, "name": name,
                    "kind": "administrative", "category": "boundary",
                    "shape": self.RING, **over}
        return found

    def test_a_warning_over_a_region_gets_its_outline(self):
        got = tracker.place_event(one(kind="alert", place="Kyiv oblast"),
                                "ua", lookup=self.looks_up())
        assert got["region_wide"] is True
        assert got["shape"] == self.RING

    def test_a_warning_over_a_town_does_not(self):
        # A town under a warning is a circle. Drawing the municipal boundary
        # would say the warning stops at the council's border, which is not
        # what an air-raid warning means.
        got = tracker.place_event(one(kind="alert", place="Beirut"), "ua",
                                lookup=self.looks_up(kind="city", category="place"))
        assert got["region_wide"] is False
        assert got["shape"] is None
        assert got["area_km"] > 0

    def test_a_drone_located_only_to_a_region_shows_that_region(self):
        # Not because the region is under anything -- it is not -- but because
        # that is all anyone knows about where the drone is. Drawn as a marker
        # on the oblast's centroid, it claimed a position good to a few
        # kilometres from a report that gave one good to a couple of hundred.
        got = tracker.place_event(one(kind="drone", place="Kyiv oblast"),
                                "ua", lookup=self.looks_up())
        assert got["region_scope"] == "located"
        assert got["shape"] is not None
        # But it is emphatically not a claim that the region is under attack.
        assert got["region_wide"] is False

    def test_the_two_reasons_for_an_outline_are_kept_apart(self):
        covers = tracker.place_event(one(kind="alert", place="Kyiv oblast"),
                                   "ua", lookup=self.looks_up())
        located = tracker.place_event(one(kind="drone", place="Kyiv oblast"),
                                    "ua", lookup=self.looks_up())
        assert covers["region_scope"] == "covers"
        assert located["region_scope"] == "located"
        assert covers["region_wide"] and not located["region_wide"]

    def test_a_town_gets_no_region_scope_at_all(self):
        got = tracker.place_event(one(kind="drone", place="Beirut"), "ua",
                                lookup=self.looks_up(kind="city", category="place"))
        assert got["region_scope"] is None

    def test_a_strike_reported_across_a_region_does_shade_it(self):
        # "вибухи на Київщині" says explosions somewhere in the oblast and
        # does not say where. The region is the honest extent of that.
        got = tracker.place_event(one(kind="explosion", place="Kyiv oblast"),
                                "ua", lookup=self.looks_up())
        assert got["region_wide"] is True

    def test_a_region_with_no_outline_falls_back_to_a_circle(self):
        got = tracker.place_event(one(kind="alert", place="Kyiv oblast"), "ua",
                                lookup=self.looks_up(shape=None))
        assert got["region_wide"] is False
        assert got["shape"] is None
        assert got["area_km"] > 0

    def test_what_counts_as_a_region(self):
        for kind in ("administrative", "state", "province", "county",
                     "governorate", "emirate", "country"):
            assert tracker.is_region({"kind": kind}), kind
        for kind in ("town", "city", "village", "suburb", "hamlet"):
            assert not tracker.is_region({"kind": kind, "category": "place"}), kind

    def test_a_boundary_match_is_a_region_whatever_it_is_called(self):
        assert tracker.is_region({"category": "boundary", "kind": "something new"})

    def test_the_demo_shows_both_reasons_for_an_outline(self):
        scopes = {e.get("region_scope") for e in tracker.demo()["events"]}
        assert {"covers", "located"} <= scopes

    def test_the_demo_shows_a_region_alert_beside_a_town_one(self):
        # Otherwise the build with no network only draws circles, and whether
        # a boundary renders at all cannot be checked.
        alerts = [e for e in tracker.demo()["events"] if e["kind"] == "alert"]
        assert len(alerts) >= 2
        assert any(e["region_wide"] for e in alerts)
        assert any(not e["region_wide"] for e in alerts)
        for event in alerts:
            if event["region_wide"]:
                assert event["shape"]["type"] in ("Polygon", "MultiPolygon")


class TestEveryKindIsDrawable:
    """The map has a drawing per kind, and it lives in the browser.

    Two files that must agree and cannot import each other, so the agreement
    is checked here rather than hoped for. A kind added to the table below
    with no silhouette beside it falls back to a plain arrow and is silently
    indistinguishable from a drone, which is the thing having per-kind icons
    was meant to fix.
    """

    def silhouettes(self):
        import pathlib
        import re as regex
        source = (pathlib.Path(__file__).resolve().parent.parent
                  / "frontend" / "js" / "tracker.js").read_text(encoding="utf-8")
        block = source[source.index("const SILHOUETTE = {"):]
        block = block[:block.index("\n};")]
        return set(regex.findall(r"^  (\w+):", block, regex.M))

    def test_everything_that_flies_in_a_line_has_its_own_drawing(self):
        drawn = self.silhouettes()
        for name, look in tracker.KINDS.items():
            if look["motion"] != "track" or name == "unknown":
                continue
            assert name in drawn, f"{name} would fall back to a generic arrow"

    def test_nothing_is_drawn_that_the_backend_does_not_offer(self):
        assert self.silhouettes() <= set(tracker.KINDS)

    def test_the_kinds_drawn_by_behaviour_are_not_also_given_silhouettes(self):
        # Recon circles, strikes burst, warnings are a triangle. Those read by
        # what they do, not by what they look like.
        for name in ("recon", "explosion", "alert"):
            assert name not in self.silhouettes(), name


class TestSayingWhichChannelGaveWhat:
    """Because "I cannot see reports from the other accounts" is otherwise
    unanswerable from either end.

    A channel can be unreachable, reachable but quiet, posting things this
    cannot read, or naming places the gazetteer does not know. Four quite
    different problems that all look like an empty map, and the panel could
    not tell them apart.
    """

    def read_one(self, monkeypatch, posts):
        tracker.reset()
        monkeypatch.setattr(tracker, "_fetch_channel", lambda name: [
            {"id": f"{name}/1", "channel": name, "text": posts[name],
             "when": dt.datetime.now(dt.timezone.utc).isoformat()}
        ] if name in posts else [])
        monkeypatch.setattr(tracker.gazetteer, "find", lambda name, countries="": {
            "lat": 50.0, "lon": 30.0, "name": name, "kind": "town",
            "category": "place"} if "Кагарлик" in name else None)
        return {row["channel"]: row for row in tracker.poll()["sources"]}

    def test_the_demo_shows_the_breakdown_too(self):
        # The section that exists to answer "why can I not see anything" was
        # the one part of the panel the build with no network never drew.
        rows = tracker.demo()["sources"]
        assert {r["channel"] for r in rows} == {c["name"] for c in tracker.CHANNELS}
        # All four states a channel can be in, so each renders at least once.
        assert any(r["problem"] for r in rows)
        assert any(r["posts"] == 0 and not r["problem"] for r in rows)
        assert any(r["read"] and not r["placed"] for r in rows)
        assert any(r["placed"] for r in rows)

    def test_every_channel_is_accounted_for(self, monkeypatch):
        try:
            rows = self.read_one(monkeypatch, {})
            assert set(rows) == {c["name"] for c in tracker.CHANNELS}
        finally:
            tracker.reset()

    def test_a_channel_that_posted_nothing_says_so(self, monkeypatch):
        try:
            rows = self.read_one(monkeypatch, {})
            assert rows["kpszsu"]["posts"] == 0
            assert rows["kpszsu"]["problem"] is None
        finally:
            tracker.reset()

    def test_a_channel_that_could_not_be_reached_says_why(self, monkeypatch):
        tracker.reset()

        def refusing(name):
            if name == "lpr1_treugolnik":
                raise tracker.TrackerError("lpr1_treugolnik answered 404")
            return []

        monkeypatch.setattr(tracker, "_fetch_channel", refusing)
        try:
            rows = {r["channel"]: r for r in tracker.poll()["sources"]}
            assert "404" in rows["lpr1_treugolnik"]["problem"]
        finally:
            tracker.reset()

    def test_read_and_placed_are_counted_apart(self, monkeypatch):
        # The difference between "this channel is unreadable" and "this
        # channel names places the gazetteer does not know" -- which need
        # completely different fixes.
        try:
            rows = self.read_one(monkeypatch, {
                "eRadarrua": "БпЛА повз Кагарлик курсом на північ",
                # Readable as a drone, with no place name in it at all -- so
                # there is nothing a gazetteer could be asked for. Not the
                # same problem as an unreadable post, and it needs a different
                # fix.
                "war_monitor": "БпЛА курсом на північ",
            })
            assert rows["eRadarrua"]["read"] == 1
            assert rows["eRadarrua"]["placed"] == 1
            assert rows["war_monitor"]["read"] == 1
            assert rows["war_monitor"]["placed"] == 0
        finally:
            tracker.reset()


class TestBothSidesOfTheBorder:
    def test_the_monitoring_channels_may_place_in_russia(self):
        # They report Belgorod and Bryansk as much as Sumy. Without the
        # country in the list those names cannot resolve at all.
        by_name = {c["name"]: c for c in tracker.CHANNELS}
        assert "ru" in by_name["war_monitor"]["countries"].split(",")
        assert "ru" in by_name["eRadarrua"]["countries"].split(",")

    def test_the_luhansk_channel_looks_in_russia_first(self):
        # Its place names are in Russia and the occupied east. Ukraine first
        # would have a gazetteer answer a Donbas town with the pre-war
        # administrative name of somewhere else.
        by_name = {c["name"]: c for c in tracker.CHANNELS}
        assert by_name["lpr1_treugolnik"]["countries"].split(",")[0] == "ru"

    def test_ukraine_still_leads_for_every_ukrainian_channel(self):
        for channel in tracker.CHANNELS:
            if channel["region"] != "Ukraine":
                continue
            assert channel["countries"].split(",")[0] == "ua", channel["name"]

    def test_a_name_is_tried_as_written_before_any_guess_at_its_case(self):
        asked = []

        def only_nominative(name, countries=""):
            asked.append(name)
            return {"lat": 50.6, "lon": 36.6, "name": name, "kind": "city",
                    "category": "place"} if name == "Белгород" else None

        got = tracker.place_event(one(place="Белгороде"), "ru", lookup=only_nominative)
        assert asked[0] == "Белгороде"
        assert got["placed"] is True

    def test_the_demo_reports_from_russia_too(self):
        places = [e["place"] for e in tracker.demo()["events"]]
        assert any("Белгород" in str(p) for p in places)


class TestNothingMoves:
    """Markers stay where the report put them.

    They used to be carried along their reported course at a typical speed
    for their kind, and loitering drones flown in circles. Both were labelled
    as estimates and both are gone: a map where everything drifts is hard to
    read, the marks wander off the places the reports actually named, and a
    mark sliding across a province looks tracked whatever the panel says.
    """

    def event(self, kind, minutes_ago, **over):
        now = time.time()
        return now, {"kind": kind, "heading": 90.0, "motion": tracker.MOTION[kind],
                     "seen": now - minutes_ago * 60,
                     "origin_lat": 50.0, "origin_lon": 30.0, **over}

    def test_a_fast_thing_an_hour_old_is_still_where_it_was_reported(self):
        now, event = self.event("ballistic", 60)
        got = tracker.project(event, now)
        assert (got["lat"], got["lon"]) == (50.0, 30.0)
        assert got["projected"] is False

    def test_nothing_of_any_kind_moves(self):
        for kind in tracker.KINDS:
            now, event = self.event(kind, 45)
            got = tracker.project(event, now)
            assert (got["lat"], got["lon"]) == (50.0, 30.0), kind

    def test_a_loitering_drone_does_not_circle(self):
        now, event = self.event("recon", 3)
        first = tracker.project(event, now)
        event["seen"] = now - 9 * 60
        later = tracker.project(event, now)
        assert (first["lat"], first["lon"]) == (later["lat"], later["lon"])

    def test_the_course_is_still_known_because_the_icon_points_along_it(self):
        now, event = self.event("drone", 5)
        assert tracker.project(event, now)["heading"] == 90.0

    def test_the_age_is_still_counted_because_the_fade_needs_it(self):
        now, event = self.event("drone", 7)
        assert tracker.project(event, now)["age_minutes"] == pytest.approx(7, abs=0.1)

    def test_things_still_expire_on_age(self):
        # The only reason a mark leaves now. It used to also leave by
        # arriving somewhere, which was a journey nobody watched.
        now = time.time()
        assert tracker._alive({"kind": "drone", "seen": now - 60}, now) is True
        assert tracker._alive({"kind": "drone", "seen": now - 3600}, now) is False


class TestTheGeometry:
    """What is left of it: the distance between two points.

    advance() -- walking a marker along a bearing -- went with the movement it
    existed for, and its tests with it. separation() stays, and matters more
    than it did: concentrate mode groups marks by it, so an error here would
    not show as a marker in the wrong place but as a mass with the wrong
    number on it, which is harder to notice and worse to believe.
    """

    def test_a_degree_of_latitude_is_about_111_km(self):
        assert tracker.separation(50.0, 30.0, 51.0, 30.0) == pytest.approx(111.2, abs=0.5)

    def test_a_degree_of_longitude_shrinks_towards_the_pole(self):
        # The flat-earth answer would make these equal, and would make every
        # mass in a northern oblast wider than it is.
        equator = tracker.separation(0.0, 30.0, 0.0, 31.0)
        fifty = tracker.separation(50.0, 30.0, 50.0, 31.0)
        assert equator == pytest.approx(111.2, abs=0.5)
        assert fifty == pytest.approx(71.5, abs=0.5)

    def test_it_is_symmetric_and_zero_at_a_point(self):
        assert tracker.separation(49.99, 36.23, 49.99, 36.23) == 0
        assert tracker.separation(50.0, 30.0, 47.8, 35.1) == pytest.approx(
            tracker.separation(47.8, 35.1, 50.0, 30.0))

    def test_a_known_distance_between_two_real_cities(self):
        # Kyiv to Kharkiv, about 410 km great-circle. Taken from neither this
        # code nor its output.
        assert tracker.separation(50.45, 30.52, 49.99, 36.23) == pytest.approx(
            410, abs=12)

    def test_the_bearing_to_a_destination_is_still_computed(self):
        # Nothing travels along it, but the glyph points along it and the
        # popup says it, so it still has to be right.
        assert tracker.bearing(50.0, 30.0, 51.0, 30.0) == pytest.approx(0, abs=0.5)
        assert tracker.bearing(50.0, 30.0, 50.0, 31.0) == pytest.approx(90, abs=0.5)
        assert tracker.bearing(50.0, 30.0, 49.0, 30.0) == pytest.approx(180, abs=0.5)


class TestNothingIsSilentlyDropped:
    """The complaint that prompted the rewrite.

    A report the gazetteer cannot place used to vanish. Six reports arriving
    and none of them placing looked exactly like a feed that had stopped, and
    there was no way from the interface to tell the two apart.
    """

    def test_an_unplaceable_report_still_reaches_the_reader(self):
        got = tracker.demo()
        unplaced = [a for a in got["alerts"] if not a["placed"]]
        assert unplaced, "the demo must exercise the unplaceable path"
        for alert in unplaced:
            assert alert["summary"]
            assert alert["why_unplaced"]

    def test_an_unplaceable_report_is_not_on_the_map(self):
        got = tracker.demo()
        mapped = {e["id"] for e in got["events"]}
        for alert in got["alerts"]:
            if not alert["placed"]:
                assert alert["id"] not in mapped

    def test_the_counts_say_how_many_of_each(self):
        got = tracker.demo()
        assert got["reports"]["placed"] == sum(1 for a in got["alerts"] if a["placed"])
        assert got["reports"]["unplaced"] >= 1
        assert (got["reports"]["placed"] + got["reports"]["unplaced"]
                == len(got["alerts"]))

    def test_placed_counts_reports_not_surviving_markers(self):
        # These are different numbers and conflating them was a real bug: a
        # track that has arrived or aged off the map was placed perfectly
        # well, and counting it as unplaced makes the gazetteer look broken
        # in exactly the number a reader checks to see whether it is.
        got = tracker.demo()
        assert got["reports"]["placed"] >= len(got["events"])
        placed_ids = {a["id"] for a in got["alerts"] if a["placed"]}
        assert {e["id"] for e in got["events"]} <= placed_ids

    def test_alerts_outlive_tracks(self):
        # A position extrapolated for an hour is fiction; "a strike was
        # reported in Kharkiv an hour ago" is still true.
        assert tracker.ALERT_MINUTES > tracker.KEEP_MINUTES

    def test_alerts_come_newest_first(self):
        seen = [a["seen"] for a in tracker.demo()["alerts"]]
        assert seen == sorted(seen, reverse=True)


class TestWhenTheModelWillNotPlay:
    """The model failing is routine, and must never be fatal.

    What this class used to test was a hosted service's rate limit: a list of
    free models to fall through, Retry-After parsing, an exponential rest so
    that a limit did not keep itself alive. None of that exists -- the model
    runs on this machine -- so what is left is the two failures a local daemon
    actually has, plus the one that was a real crash and is worth keeping a
    test for whatever the model is.
    """

    def test_a_200_with_null_content_does_not_crash(self, monkeypatch):
        # The failure the user hit, kept because the shape of it survives the
        # change of provider. A model answers 200 with content: null -- a
        # refusal, legitimately -- and calling .strip() on None raises
        # AttributeError, which is not an error class anything caught, so it
        # went past every handler and 500ed the endpoint instead of falling
        # back to reading the reports plainly.
        monkeypatch.setattr(ollama.requests, "post",
                            lambda *a, **k: Reply(200, {"message": {"content": None}}))
        with pytest.raises(ollama.OllamaError):
            ollama.ask("prompt", "[]", model="m")

    def test_an_empty_string_is_the_same(self, monkeypatch):
        monkeypatch.setattr(ollama.requests, "post",
                            lambda *a, **k: Reply(200, {"message": {"content": "   "}}))
        with pytest.raises(ollama.OllamaError):
            ollama.ask("prompt", "[]", model="m")

    def test_a_reply_with_no_message_at_all(self, monkeypatch):
        monkeypatch.setattr(ollama.requests, "post",
                            lambda *a, **k: Reply(200, {"done": True}))
        with pytest.raises(ollama.OllamaError):
            ollama.ask("prompt", "[]", model="m")

    def test_read_events_refuses_anything_that_is_not_text(self):
        for junk in (None, "", "   ", 42, [], {}):
            with pytest.raises(tracker.TrackerError):
                tracker.read_events(junk)

    def test_a_daemon_that_is_not_there_says_so_and_says_the_remedy(self, monkeypatch):
        def refuse(*a, **k):
            raise ollama.requests.RequestException("connection refused")
        monkeypatch.setattr(ollama.requests, "post", refuse)
        with pytest.raises(ollama.NotRunning) as caught:
            ollama.ask("prompt", "[]", model="m")
        # The message is the whole value of this class existing: one obvious
        # fix, said rather than left as a network error.
        assert "ollama serve" in str(caught.value)

    def test_a_model_that_was_never_pulled_says_how_to_pull_it(self, monkeypatch):
        monkeypatch.setattr(ollama.requests, "post",
                            lambda *a, **k: Reply(404, {"error": "model not found"}))
        with pytest.raises(ollama.ModelMissing) as caught:
            ollama.ask("prompt", "[]", model="gemma3:4b")
        assert "ollama pull gemma3:4b" in str(caught.value)

    def test_the_poll_still_reads_the_reports_when_the_model_is_gone(self, monkeypatch):
        # The important one. A failed model step must degrade to reading by
        # rule, not to an empty map -- which is what the previous version did
        # and what got it called broken.
        tracker.reset()
        monkeypatch.setattr(tracker, "_fetch_channel", lambda channel: [
            {"id": f"{channel}/1", "channel": channel,
             "when": dt.datetime.now(dt.timezone.utc).isoformat(),
             "text": "Шахед над Нікополем"},
        ])

        def no_daemon(*a, **k):
            raise ollama.NotRunning("Ollama is not answering. Start it with `ollama serve`.")
        monkeypatch.setattr(tracker, "_call_model", no_daemon)
        monkeypatch.setattr(tracker.gazetteer, "find", fake_find)

        got = tracker.poll()
        assert got["alerts"], "a dead model left nothing in the stream at all"
        assert all(a["by"] == "rules" for a in got["alerts"])
        assert "ollama serve" in got["state"]

    def test_and_says_which_way_it_read_them(self, monkeypatch):
        # So the panel can say "working, less well" rather than showing red.
        tracker.reset()
        monkeypatch.setattr(tracker, "_fetch_channel", lambda channel: [
            {"id": f"{channel}/1", "channel": channel,
             "when": dt.datetime.now(dt.timezone.utc).isoformat(),
             "text": "Шахед над Нікополем"},
        ])
        monkeypatch.setattr(tracker, "_call_model",
                            lambda batch: (_ for _ in ()).throw(
                                ollama.OllamaError("nope")))
        monkeypatch.setattr(tracker.gazetteer, "find", fake_find)
        got = tracker.poll()
        assert got["read_by"]["model"] == 0
        assert got["read_by"]["rules"] >= 1


class TestChoosingAModel:
    """Which model, from what is actually installed.

    "gemma 4" was asked for. There is no Gemma 4 -- the family is Gemma 3 and
    ":4b" in its tags is four billion parameters, not a version -- so nothing
    is hard-coded and the installed list decides. These tests are about that
    not going wrong quietly on a machine that pulled a different size.
    """

    def setup_method(self):
        ollama.prefer(None)
        ollama._tags, ollama._tags_at, ollama._chosen = [], 0.0, None

    def have(self, monkeypatch, *names):
        monkeypatch.setattr(ollama.requests, "get", lambda *a, **k: Reply(
            200, {"models": [{"name": n} for n in names]}))

    def test_a_family_name_matches_any_size_of_it(self):
        assert ollama.matches("gemma3", "gemma3:4b")
        assert ollama.matches("gemma3", "gemma3:27b")
        assert not ollama.matches("gemma3", "gemma2:9b")

    def test_a_size_matches_only_itself_and_its_quantisations(self):
        assert ollama.matches("gemma3:4b", "gemma3:4b")
        assert ollama.matches("gemma3:4b", "gemma3:4b-instruct-q4_K_M")
        assert not ollama.matches("gemma3:4b", "gemma3:12b")

    def test_gemma_is_preferred_because_it_was_asked_for(self, monkeypatch):
        self.have(monkeypatch, "llama3.3:70b", "gemma3:4b", "qwen2.5:7b")
        assert ollama.choose() == "gemma3:4b"

    def test_the_larger_gemma_wins_when_both_are_there(self, monkeypatch):
        self.have(monkeypatch, "gemma3:4b", "gemma3:27b")
        assert ollama.choose() == "gemma3:27b"

    def test_a_machine_with_no_gemma_still_works(self, monkeypatch):
        # The point of the fallback list: being told to pull another model is
        # a worse answer than using the perfectly good one already there.
        self.have(monkeypatch, "qwen2.5:14b")
        assert ollama.choose() == "qwen2.5:14b"

    def test_an_unusual_model_is_used_rather_than_refused(self, monkeypatch):
        self.have(monkeypatch, "some-local-finetune:latest")
        assert ollama.choose() == "some-local-finetune:latest"

    def test_a_named_model_beats_the_preference_order(self, monkeypatch):
        # If somebody has pulled a model for this and says to use it,
        # second-guessing them would be wrong.
        self.have(monkeypatch, "gemma3:27b", "qwen2.5:7b")
        ollama.prefer("qwen2.5")
        assert ollama.choose() == "qwen2.5:7b"

    def test_a_named_model_that_is_not_there_says_how_to_get_it(self, monkeypatch):
        self.have(monkeypatch, "gemma3:4b")
        ollama.prefer("mixtral:8x7b")
        with pytest.raises(ollama.ModelMissing) as caught:
            ollama.choose()
        assert "ollama pull mixtral:8x7b" in str(caught.value)

    def test_a_daemon_with_nothing_pulled_says_what_to_pull(self, monkeypatch):
        self.have(monkeypatch)
        with pytest.raises(ollama.ModelMissing) as caught:
            ollama.choose()
        assert "ollama pull" in str(caught.value)

    def test_a_daemon_that_is_not_running_is_its_own_answer(self, monkeypatch):
        def refuse(*a, **k):
            raise ollama.requests.RequestException("refused")
        monkeypatch.setattr(ollama.requests, "get", refuse)
        with pytest.raises(ollama.NotRunning):
            ollama.installed(refresh=True)
        # And as a status, it is a fact rather than an exception, because the
        # panel has to draw something.
        got = ollama.status()
        assert got["ready"] is False
        assert got["kind"] == "NotRunning"
        assert "ollama serve" in got["problem"]

    def test_the_status_carries_the_model_when_there_is_one(self, monkeypatch):
        self.have(monkeypatch, "gemma3:4b")
        got = ollama.status()
        assert got["ready"] is True
        assert got["model"] == "gemma3:4b"
        assert "gemma3:4b" in got["installed"]

    def test_json_is_read_through_whatever_a_model_wraps_it_in(self):
        want = {"events": [{"id": "a"}]}
        for text in ('{"events": [{"id": "a"}]}',
                     '```json\n{"events": [{"id": "a"}]}\n```',
                     '```\n{"events": [{"id": "a"}]}\n```',
                     'Here is the JSON: {"events": [{"id": "a"}]}',
                     '  {"events": [{"id": "a"}]}  '):
            assert ollama.read_json(text) == want, text

    def test_something_that_is_not_json_is_refused_clearly(self):
        for junk in ("no idea", "", "[1,2,3]", "null"):
            with pytest.raises(ollama.OllamaError):
                ollama.read_json(junk)


class TestConcentrateMode:
    """Grouping marks into a mass.

    Tested by distance rather than by looking at it, which is the reason the
    clustering is in the backend at all. A mass with the wrong number on it is
    worse than a missing one: it is a claim, and it is believable.
    """

    def marks(self, *points, kind="drone"):
        return [{"id": f"m{i}", "kind": kind, "lat": lat, "lon": lon, "seen": 1000 + i}
                for i, (lat, lon) in enumerate(points)]

    def test_marks_close_together_make_one_mass(self):
        got = tracker.massed(self.marks((50.4, 30.5), (50.45, 30.55), (50.5, 30.6)))
        assert len(got) == 1
        assert got[0]["count"] == 3

    def test_two_apart_are_not_a_mass(self):
        # Two is a coincidence. Three is a pattern.
        assert tracker.massed(self.marks((50.4, 30.5), (50.45, 30.55))) == []

    def test_marks_far_apart_are_left_alone(self):
        got = tracker.massed(self.marks((50.4, 30.5), (46.5, 30.7), (49.9, 36.2)))
        assert got == []

    def test_a_corridor_holds_together_through_its_middle(self):
        # Single-link, deliberately: a line of drones following a river is one
        # incursion even when its ends are further apart than the threshold.
        # Nearest-centre clustering would cut this in half.
        chain = self.marks((50.9, 34.8), (50.58, 34.48), (50.83, 33.88),
                           (50.75, 33.47), (51.24, 33.2))
        got = tracker.massed(chain)
        assert len(got) == 1
        assert got[0]["count"] == 5
        ends = tracker.separation(50.9, 34.8, 51.24, 33.2)
        assert ends > tracker.MASS_WITHIN_KM, "the ends must be beyond the threshold"

    def test_two_separate_groups_stay_separate(self):
        near_kyiv = self.marks((50.4, 30.5), (50.45, 30.55), (50.5, 30.6))
        near_odesa = self.marks((46.4, 30.7), (46.45, 30.75), (46.5, 30.8))
        got = tracker.massed(near_kyiv + near_odesa)
        assert len(got) == 2
        assert {m["count"] for m in got} == {3}

    def test_the_threshold_is_the_one_declared(self):
        # Just inside and just outside, at a latitude these reports come from.
        inside = tracker.MASS_WITHIN_KM - 5
        outside = tracker.MASS_WITHIN_KM + 5
        for km, expect in ((inside, 1), (outside, 0)):
            degrees = km / 111.195
            got = tracker.massed(self.marks(
                (50.0, 30.0), (50.0 + degrees, 30.0), (50.0 + 2 * degrees, 30.0)))
            assert len(got) == expect, km

    def test_strikes_are_never_massed(self):
        # Six explosions in a city are six strikes, not one blurry strike, and
        # a circle over the top would hide the detail that matters most.
        got = tracker.massed(self.marks(
            (50.4, 30.5), (50.42, 30.52), (50.44, 30.54), kind="explosion"))
        assert got == []

    def test_alerts_are_never_massed(self):
        got = tracker.massed(self.marks(
            (50.4, 30.5), (50.42, 30.52), (50.44, 30.54), kind="alert"))
        assert got == []

    def test_a_mass_carries_what_the_map_needs_to_draw_it(self):
        got = tracker.massed(self.marks((50.4, 30.5), (50.45, 30.55), (50.5, 30.6)))[0]
        assert set(got) >= {"lat", "lon", "radius_km", "bbox", "count", "kinds",
                            "label", "ids", "seen"}
        west, south, east, north = got["bbox"]
        assert west <= east and south <= north
        assert south <= got["lat"] <= north
        assert west <= got["lon"] <= east

    def test_the_centre_sits_where_the_marks_are(self):
        """The mean, not the middle of the bounding box.

        They differ whenever a group is lopsided, which a corridor always is:
        four marks over one town and a fifth fifty kilometres away. The
        bounding-box middle lands between them, on empty ground, and draws a
        circle centred where nothing was reported. The mean lands on the dense
        end, which is where the attack is.
        """
        marks = self.marks((50.40, 30.50), (50.41, 30.51), (50.42, 30.52),
                           (50.43, 30.53), (50.85, 30.95))
        got = tracker.massed(marks)[0]
        west, south, east, north = got["bbox"]
        box_lat, box_lon = (south + north) / 2, (west + east) / 2
        mean_lat = sum(m["lat"] for m in marks) / len(marks)
        mean_lon = sum(m["lon"] for m in marks) / len(marks)
        assert got["lat"] == pytest.approx(mean_lat, abs=1e-3)
        assert got["lon"] == pytest.approx(mean_lon, abs=1e-3)
        # And the two really are different here, or this proves nothing.
        assert abs(box_lat - mean_lat) > 0.05, "the test case is not lopsided"
        assert abs(box_lon - mean_lon) > 0.05, "the test case is not lopsided"
        # The centre is nearer the four than the one.
        dense = tracker.separation(got["lat"], got["lon"], 50.415, 30.515)
        lone = tracker.separation(got["lat"], got["lon"], 50.85, 30.95)
        assert dense < lone

    def test_the_radius_reaches_every_member(self):
        marks = self.marks((50.4, 30.5), (50.45, 30.55), (50.5, 30.6))
        got = tracker.massed(marks)[0]
        for mark in marks:
            assert tracker.separation(got["lat"], got["lon"],
                                      mark["lat"], mark["lon"]) <= got["radius_km"] + 1e-6

    def test_marks_at_one_spot_still_draw_as_something(self):
        # A zero-radius circle is invisible, and several drones reported at
        # one town is exactly the case this mode is for.
        got = tracker.massed(self.marks((50.4, 30.5), (50.4, 30.5), (50.4, 30.5)))
        assert got[0]["radius_km"] >= 8

    def test_the_label_names_the_kind_when_they_all_agree(self):
        got = tracker.massed(self.marks((50.4, 30.5), (50.45, 30.55), (50.5, 30.6)))
        assert got[0]["label"] == "3 × drone"

    def test_and_does_not_when_they_do_not(self):
        mixed = self.marks((50.4, 30.5), (50.45, 30.55))
        mixed += [{"id": "c", "kind": "cruise", "lat": 50.5, "lon": 30.6, "seen": 1}]
        assert tracker.massed(mixed)[0]["label"] == "3 tracks"

    def test_every_id_is_accounted_for_exactly_once(self):
        # The frontend hides the marks a mass represents, so a duplicated or
        # missing id means a mark drawn twice or lost.
        marks = self.marks((50.4, 30.5), (50.45, 30.55), (50.5, 30.6),
                           (46.4, 30.7), (46.45, 30.75), (46.5, 30.8))
        ids = [i for m in tracker.massed(marks) for i in m["ids"]]
        assert len(ids) == len(set(ids)) == 6

    def test_bad_coordinates_cannot_take_the_whole_thing_down(self):
        marks = self.marks((50.4, 30.5), (50.45, 30.55), (50.5, 30.6))
        marks += [{"id": "bad", "kind": "drone", "lat": None, "lon": None, "seen": 1},
                  {"id": "worse", "kind": "drone", "seen": 1}]
        got = tracker.massed(marks)
        assert len(got) == 1 and got[0]["count"] == 3

    def test_the_biggest_mass_comes_first(self):
        big = self.marks((50.4, 30.5), (50.45, 30.55), (50.5, 30.6), (50.42, 30.58))
        small = self.marks((46.4, 30.7), (46.45, 30.75), (46.5, 30.8))
        got = tracker.massed(big + small)
        assert [m["count"] for m in got] == [4, 3]

    def test_the_cache_is_only_reused_for_the_same_marks(self):
        # It is keyed on which marks are alive, because marks expire between
        # polls as well as arriving on them -- a cache keyed on the count
        # would serve a stale mass for a changed map.
        first = self.marks((50.4, 30.5), (50.45, 30.55), (50.5, 30.6))
        assert tracker.masses_now(first)[0]["count"] == 3
        assert tracker.masses_now(first) is tracker.masses_now(first)
        moved = self.marks((50.4, 30.5), (50.45, 30.55), (46.5, 30.8))
        assert tracker.masses_now(moved) == []


class TestDemo:
    def test_it_answers_in_the_shape_the_page_expects(self):
        got = tracker.demo()
        assert set(got) >= {"events", "count", "alerts", "state", "keep_minutes",
                            "alert_minutes", "kinds", "channels", "regions",
                            "reports", "sources",
                            # What replaced "model"/"keyed": whether a local
                            # model is available, which one, and the command
                            # to fix it when there is not.
                            "ollama",
                            # Concentrate mode, sent with every answer so the
                            # switch does not need a round trip.
                            "masses", "mass_within_km", "mass_least"}
        assert set(got["ollama"]) >= {"ready", "host", "model", "installed"}
        for event in got["events"]:
            assert set(event) >= {"id", "kind", "lat", "lon", "origin_lat",
                                  "origin_lon", "heading", "seen", "placed"}
            assert event["kind"] in tracker.KINDS
            assert math.isfinite(event["lat"]) and math.isfinite(event["lon"])

    def test_the_demo_shows_marks_with_and_without_a_course(self):
        # Nothing moves, but a glyph points along a reported course where
        # there is one, so both drawings need exercising offline.
        headings = {e["heading"] is None for e in tracker.demo()["events"]}
        assert headings == {True, False}

    def test_the_demo_carries_masses_for_concentrate_mode(self):
        # The mode is invisible without a cluster to draw, and a feature whose
        # only demonstration needs a network is a feature nobody checks.
        got = tracker.demo()
        assert len(got["masses"]) >= 2
        assert any(m["count"] >= 3 for m in got["masses"])
        for mass in got["masses"]:
            assert set(mass) >= {"lat", "lon", "radius_km", "bbox", "count", "ids"}

    def test_no_demo_mass_contains_a_strike_or_an_alert(self):
        kinds = {e["id"]: e["kind"] for e in tracker.demo()["events"]}
        for mass in tracker.demo()["masses"]:
            for ident in mass["ids"]:
                assert kinds.get(ident) not in tracker.NOT_AIRBORNE

    def test_the_demo_covers_every_way_a_marker_can_behave(self):
        # Otherwise the build with no network exercises one drawing path and
        # hides what the others do -- which is how a thing flying north came
        # to be drawn with the mark for something that had been shot down.
        events = tracker.demo()["events"]
        assert {e["motion"] for e in events} >= {"track", "orbit", "still"}
        # A track whose report gave no direction at all. It must not be drawn
        # with an arrow, so the demo has to contain one to check that.
        assert any(e["motion"] == "track" and e["heading"] is None for e in events)
        # And one whose direction came from a compass course rather than a
        # named destination.
        assert any(e["heading"] is not None and e["dest_km"] is None
                   for e in events)

    def test_the_demo_courses_are_computed_from_its_own_gazetteer(self):
        for event in tracker.demo()["events"]:
            if event["dest_km"] is None:
                continue
            assert event["heading"] == pytest.approx(
                tracker.bearing(event["origin_lat"], event["origin_lon"],
                              event["dest_lat"], event["dest_lon"]), abs=0.01)

    def test_it_actually_runs_rather_than_resetting_every_call(self):
        # The first version rebuilt its events against the clock on each call,
        # so they were forever the same few minutes old: nothing aged, nothing
        # arrived, nothing expired, and the endings were unreachable.
        tracker._demo_epoch = 0.0
        try:
            first = {e["id"]: e["age_minutes"] for e in tracker.demo()["events"]}
            tracker._demo_epoch -= 120           # as if two minutes had passed
            for event in tracker.demo()["events"]:
                assert event["age_minutes"] == pytest.approx(first[event["id"]] + 2,
                                                             abs=0.2)
        finally:
            tracker._demo_epoch = 0.0

    def test_it_lets_things_arrive_and_expire_and_then_starts_again(self):
        # One cycle outlives everything in flight. It does not outlive a
        # strike, which is held for six hours by design -- so what must be
        # empty at the end of a cycle is the flying things, not the map.
        tracker._demo_epoch = 0.0
        try:
            tracker.demo()
            tracker._demo_epoch -= tracker.DEMO_CYCLE - 1
            late = tracker.demo()["events"]
            assert [e for e in late if e["kind"] != "explosion"] == []
            tracker._demo_epoch -= 10
            assert tracker.demo()["events"]
        finally:
            tracker._demo_epoch = 0.0

    def test_the_channels_the_app_offers_are_the_ones_it_reads(self):
        names = {c["name"] for c in tracker.CHANNELS}
        assert tracker.demo()["channels"] == [c["name"] for c in tracker.CHANNELS]
        # The four that were asked for, and only those. Written out rather
        # than derived, because the point is that this list is a decision
        # somebody made and not whatever happens to be in the tuple.
        assert names == {"eRadarrua", "kpszsu", "war_monitor", "lpr1_treugolnik"}

    def test_every_channel_says_which_countries_to_look_in(self):
        # Without it "Sumy" is as likely to match a street in another
        # hemisphere as the oblast capital.
        for channel in tracker.CHANNELS:
            assert channel["countries"] and channel["region"]
            for code in channel["countries"].split(","):
                assert len(code) == 2 and code.islower(), channel
