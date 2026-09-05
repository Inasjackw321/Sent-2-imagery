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

from backend import osint

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
        got = osint.parse_preview(PAGE, "eRadarrua")
        assert [p["id"] for p in got] == ["eRadarrua/12345", "eRadarrua/12346"]
        assert got[0]["when"] == "2026-09-01T11:52:00+00:00"
        assert got[0]["channel"] == "eRadarrua"

    def test_the_text_comes_out_as_text(self):
        body = osint.parse_preview(PAGE, "eRadarrua")[0]["text"]
        assert "Nikopol" in body and "Marhanets" in body
        assert "<" not in body and "&#" not in body

    def test_a_post_does_not_swallow_the_next_one(self):
        got = osint.parse_preview(PAGE, "eRadarrua")
        assert "Second post" not in got[0]["text"]

    def test_a_post_with_no_text_is_skipped(self):
        page = ('<div class="tgme_widget_message " data-post="c/1">'
                '<div class="tgme_widget_message_text"></div>'
                '<time datetime="2026-09-01T11:00:00+00:00">x</time></div>')
        assert osint.parse_preview(page, "c") == []

    def test_a_page_of_something_else_is_empty_not_an_error(self):
        assert osint.parse_preview("<html><body>nope</body></html>", "c") == []


class TestReadingTheModel:
    def test_the_shape_that_was_asked_for(self):
        got = osint.read_events(json.dumps({"events": [
            {"id": "c/1", "kind": "drone", "place": "Nikopol",
             "toward": "Kherson", "count": 3, "summary": "Three drones"}]}))
        assert len(got) == 1
        assert got[0]["place"] == "Nikopol" and got[0]["count"] == 3
        assert got[0]["id"] == "c/1"

    def test_a_code_fence_is_survived(self):
        got = osint.read_events('```json\n{"events": [{"kind": "drone", "place": "Nikopol"}]}\n```')
        assert len(got) == 1

    def test_a_sentence_before_the_json_is_survived(self):
        got = osint.read_events('Here you go:\n{"events": [{"kind": "cruise", "place": "Kyiv"}]}')
        assert got[0]["kind"] == "cruise"

    def test_a_bare_list_is_survived(self):
        assert len(osint.read_events('[{"kind": "drone", "place": "Nikopol"}]')) == 1

    def test_json_that_is_not_json_is_refused_clearly(self):
        with pytest.raises(osint.OsintError, match="did not return JSON"):
            osint.read_events("I could not find any events in these messages.")

    def test_a_kind_nobody_offered_becomes_unknown_rather_than_a_gap(self):
        got = osint.read_events('{"events": [{"kind": "doom ray", "place": "Kyiv"}]}')
        assert got[0]["kind"] == "unknown"
        assert got[0]["kind"] in osint.KINDS

    def test_rubbish_among_good_events_does_not_take_them_with_it(self):
        got = osint.read_events(json.dumps({"events": [
            "not a dict", {"kind": "drone", "place": "Nikopol"}]}))
        assert len(got) == 1


class TestThePlaceNamesTheModelGives:
    """What comes back as a place name and is not one."""

    def test_a_model_that_shrugs_is_not_given_a_place(self):
        for shrug in ("unknown", "N/A", "not specified", "various", "-", "none"):
            got = osint.read_events(json.dumps({"events": [
                {"kind": "drone", "place": shrug}]}))
            assert got[0]["place"] is None, shrug

    def test_coordinates_in_the_place_field_are_refused(self):
        # It is told not to give coordinates. When it does anyway they arrive
        # here, and a gazetteer asked for "47.5, 34.4" either misses or
        # matches something absurd -- so the string is rejected as a name.
        for smuggled in ("47.5665, 34.4053", "49.99 N 36.23 E", "50.45,30.52"):
            got = osint.read_events(json.dumps({"events": [
                {"kind": "drone", "place": smuggled}]}))
            assert got[0]["place"] is None, smuggled

    def test_a_real_place_name_survives_all_of_that(self):
        got = osint.read_events(json.dumps({"events": [
            {"kind": "drone", "place": "Kamianets-Podilskyi"}]}))
        assert got[0]["place"] == "Kamianets-Podilskyi"

    def test_heading_for_where_it_already_is_is_not_a_journey(self):
        got = osint.read_events(json.dumps({"events": [
            {"kind": "drone", "place": "Nikopol", "toward": "nikopol"}]}))
        assert got[0]["toward"] is None

    def test_whatever_coordinates_the_model_sent_are_not_kept(self):
        # The single most important assertion in this file. The model may
        # return lat/lon; nothing downstream may ever see them.
        got = osint.read_events(json.dumps({"events": [
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
            assert osint.read_course(word) == degrees

    def test_the_in_between_points_too(self):
        assert osint.read_course("NNE") == 22.5
        assert osint.read_course("wsw") == 247.5

    def test_written_out_in_words(self):
        assert osint.read_course("north") == 0
        assert osint.read_course("South-West") == 225
        assert osint.read_course("northeast") == 45

    def test_anything_else_is_no_course_rather_than_a_guess(self):
        for junk in (None, "", "  ", "up", "towards Kyiv", "45", "north-ish", 12):
            assert osint.read_course(junk) is None, junk

    def test_a_course_becomes_a_heading_with_no_destination_needed(self):
        got = osint.place_event(one(place="Nikopol", course=0.0), "ua", lookup=gazetteer)
        assert got["placed"] is True
        assert got["heading"] == 0.0
        assert got["motion"] == "track"

    def test_a_named_destination_still_beats_a_compass_course(self):
        # The course is a direction; a destination is a direction AND a place
        # to stop. Where a report gives both, the one with more in it wins.
        got = osint.place_event(one(place="Nikopol", toward="Kherson", course=0.0),
                                "ua", lookup=gazetteer)
        assert got["heading"] != 0.0
        assert got["dest_km"] is not None

    def test_a_strike_is_given_no_course_however_the_sentence_reads(self):
        got = osint.read_events(json.dumps({"events": [
            {"kind": "explosion", "place": "Kyiv", "course": "N", "toward": "Lviv"}]}))[0]
        assert got["course"] is None and got["toward"] is None


class TestKindsAndHowTheyMove:
    def test_every_kind_is_complete(self):
        for name, look in osint.KINDS.items():
            assert look["colour"].startswith("#"), name
            assert look["label"] and look["rank"], name
            assert look["motion"] in ("track", "orbit", "still"), name
            assert isinstance(look["speed"], float), name

    def test_the_derived_tables_agree_with_the_one_they_come_from(self):
        # Both ends read speeds and motion out of this, so a table that
        # disagreed with itself would put the marker in two places.
        assert osint.SPEEDS == {k: v["speed"] for k, v in osint.KINDS.items()}
        assert osint.MOTION == {k: v["motion"] for k, v in osint.KINDS.items()}

    def test_a_jet_drone_is_faster_than_a_propeller_one(self):
        assert osint.SPEEDS["jet_drone"] > osint.SPEEDS["drone"] * 2

    def test_missiles_outrun_everything_that_is_not_a_missile(self):
        slowest_missile = min(osint.SPEEDS["cruise"], osint.SPEEDS["ballistic"])
        for kind in ("drone", "jet_drone", "recon", "helicopter"):
            assert osint.SPEEDS[kind] < slowest_missile, kind
        assert osint.SPEEDS["ballistic"] > osint.SPEEDS["cruise"]

    def test_only_the_things_that_do_not_fly_stand_still(self):
        for name, look in osint.KINDS.items():
            if look["motion"] == "still":
                assert look["speed"] == 0.0, name
            else:
                assert look["speed"] > 0.0, name

    def test_recon_loiters_rather_than_travelling(self):
        assert osint.MOTION["recon"] == "orbit"


class TestOrbiting:
    def event(self, minutes_ago, kind="recon"):
        now = time.time()
        return now, {"kind": kind, "motion": osint.MOTION[kind], "heading": None,
                     "seen": now - minutes_ago * 60,
                     "origin_lat": 47.8388, "origin_lon": 35.1396}

    def test_it_goes_round_rather_than_staying_put(self):
        now, event = self.event(2)
        got = osint.project(event, now)
        assert got["orbiting"] is True
        assert osint.separation(got["lat"], got["lon"],
                                event["origin_lat"], event["origin_lon"]) \
            == pytest.approx(osint.ORBIT_KM, abs=0.1)

    def test_it_comes_back_round_to_where_it_started(self):
        # A full period must close the circle, or the marker drifts a little
        # further from the place it was reported over on every lap -- which
        # after an hour is a claim nobody made.
        now, event = self.event(0)
        start = osint.project(event, now)
        event["seen"] = now - osint.ORBIT_MINUTES * 60
        after = osint.project(event, now)
        assert after["lat"] == pytest.approx(start["lat"], abs=1e-6)
        assert after["lon"] == pytest.approx(start["lon"], abs=1e-6)

    def test_it_is_somewhere_else_half_a_lap_later(self):
        now, event = self.event(0)
        start = osint.project(event, now)
        event["seen"] = now - osint.ORBIT_MINUTES * 30
        half = osint.project(event, now)
        assert osint.separation(start["lat"], start["lon"],
                                half["lat"], half["lon"]) \
            == pytest.approx(2 * osint.ORBIT_KM, abs=0.2)

    def test_it_points_along_the_circle_not_at_the_middle(self):
        # The tangent, a quarter turn ahead of where it is on the ring.
        now, event = self.event(0)
        got = osint.project(event, now)
        outward = osint.bearing(event["origin_lat"], event["origin_lon"],
                                got["lat"], got["lon"])
        assert (got["heading"] - outward) % 360 == pytest.approx(90, abs=1)

    def test_it_never_arrives_anywhere(self):
        # It was not on a journey, so there is nothing for it to finish. It
        # leaves on age like everything else.
        now, event = self.event(600)
        event["dest_km"] = 5.0
        assert osint._arrived(event, now) is False
        assert osint.project(event, now)["arrived"] is False

    def test_a_tracking_kind_does_not_orbit(self):
        now, event = self.event(2, kind="drone")
        event["heading"] = 90.0
        got = osint.project(event, now)
        assert got.get("orbiting") is not True


class TestTellingSimilarPlacesApart:
    """The rest of the Kaharlyk failure: the wrong Kaharlyk."""

    def test_the_region_is_used_to_ask_a_narrower_question(self):
        asked = []

        def watching(name, countries):
            asked.append(name)
            return {"lat": 49.85, "lon": 30.81, "name": name, "kind": "town"}

        osint.place_event(one(place="Kaharlyk", region="Kyiv oblast"),
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

        got = osint.place_event(one(place="Kaharlyk", region="Kyivshchyna"),
                                "ua", lookup=only_bare)
        assert asked == ["Kaharlyk, Kyivshchyna", "Kaharlyk"]
        assert got["placed"] is True

    def test_a_region_already_in_the_name_is_not_repeated(self):
        asked = []

        def watching(name, countries):
            asked.append(name)
            return {"lat": 49.7, "lon": 36.3, "name": name, "kind": "state"}

        osint.place_event(one(place="Kharkiv oblast", region="Kharkiv oblast"),
                          "ua", lookup=watching)
        assert asked == ["Kharkiv oblast"]


class TestPlacing:
    def test_a_known_place_gets_the_gazetteer_position(self):
        got = osint.place_event(one(), "ua", lookup=gazetteer)
        assert got["placed"] is True
        assert (got["lat"], got["lon"]) == PLACES["Nikopol"]
        assert got["place_match"] == "Nikopol, Ukraine"

    def test_an_unknown_place_is_not_placed_and_says_why(self):
        got = osint.place_event(one(place="Nowheresville"), "ua", lookup=gazetteer)
        assert got["placed"] is False
        assert got["lat"] is None
        assert "Nowheresville" in got["why_unplaced"]

    def test_no_place_at_all_is_not_placed_and_says_why(self):
        got = osint.place_event(one(place=None), "ua", lookup=gazetteer)
        assert got["placed"] is False
        assert got["why_unplaced"] == "the report names no place"

    def test_a_gazetteer_that_is_down_does_not_take_the_report_with_it(self):
        def broken(name, countries):
            raise osint.gazetteer.GazetteerError("the gazetteer is rate limiting")
        got = osint.place_event(one(), "ua", lookup=broken)
        assert got["placed"] is False
        assert "rate limiting" in got["why_unplaced"]

    def test_a_destination_gives_a_course_and_a_distance(self):
        got = osint.place_event(one(toward="Kherson"), "ua", lookup=gazetteer)
        # Kherson is south-west of Nikopol.
        assert 220 < got["heading"] < 245
        assert got["dest_km"] == pytest.approx(174, abs=10)

    def test_the_course_is_computed_not_taken_on_trust(self):
        # Whatever the model thought the bearing was never reaches here: the
        # only source of a heading is the two gazetteer positions.
        got = osint.place_event(one(toward="Kherson"), "ua", lookup=gazetteer)
        assert got["heading"] == pytest.approx(
            osint.bearing(*PLACES["Nikopol"], *PLACES["Kherson"]), abs=0.01)

    def test_a_destination_the_gazetteer_does_not_know_leaves_it_still(self):
        # Placed, because the report's own location is known -- but with no
        # course, because there is nothing to compute one from.
        got = osint.place_event(one(toward="Nowheresville"), "ua", lookup=gazetteer)
        assert got["placed"] is True
        assert got["heading"] is None and got["dest_km"] is None

    def test_a_destination_that_resolves_to_the_same_point_is_not_a_journey(self):
        def same(name, countries):
            return {"lat": 50.0, "lon": 30.0, "name": name, "kind": "city"}
        got = osint.place_event(one(toward="Kyiv"), "ua", lookup=same)
        assert got["heading"] is None and got["dest_km"] is None

    def test_a_strike_does_not_travel_however_the_report_reads(self):
        # "Explosions in Kherson, drones heading for Mykolaiv" is one message.
        # The strike is where it is; only the airborne thing has a course.
        got = osint.place_event(
            one(kind="explosion", place="Nikopol", toward="Kherson"),
            "ua", lookup=gazetteer)
        assert got["placed"] is True
        assert got["heading"] is None

    def test_an_air_alert_does_not_travel_either(self):
        got = osint.place_event(
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
        assert osint.keep_minutes("explosion") >= 180

    def test_things_in_flight_still_go_quickly(self):
        for kind in ("drone", "jet_drone", "cruise", "ballistic", "recon",
                     "aircraft", "helicopter"):
            assert osint.keep_minutes(kind) == osint.KEEP_MINUTES, kind

    def test_a_kind_that_says_nothing_gets_the_default(self):
        assert osint.keep_minutes("nothing like this") == osint.KEEP_MINUTES

    def test_an_old_strike_is_kept_and_an_old_drone_is_not(self):
        now = time.time()
        old = now - 3 * 3600
        assert osint._alive({"kind": "explosion", "seen": old}, now) is True
        assert osint._alive({"kind": "drone", "seen": old}, now) is False

    def test_a_strike_does_go_eventually(self):
        now = time.time()
        gone = now - (osint.keep_minutes("explosion") + 1) * 60
        assert osint._alive({"kind": "explosion", "seen": gone}, now) is False

    def test_a_report_never_leaves_the_list_while_its_marker_is_on_the_map(self):
        # A burst over a town with nothing in the panel to explain it is worse
        # than either on its own, and the alert window is shorter than a
        # strike's life, so the stream has to stretch to cover it.
        for kind in osint.KINDS:
            held = max(osint.ALERT_MINUTES, osint.keep_minutes(kind))
            assert held >= osint.keep_minutes(kind), kind

    def test_expiry_keeps_the_strike_and_drops_the_drone(self):
        now = time.time()
        osint.reset()
        try:
            with osint._lock:
                osint._events.extend([
                    {"kind": "explosion", "seen": now - 3 * 3600, "heading": None,
                     "origin_lat": 50.0, "origin_lon": 30.0},
                    {"kind": "drone", "seen": now - 3 * 3600, "heading": None,
                     "origin_lat": 50.0, "origin_lon": 30.0},
                ])
                osint._alerts.extend([
                    {"kind": "explosion", "seen": now - 3 * 3600, "placed": True},
                    {"kind": "drone", "seen": now - 3 * 3600, "placed": True},
                ])
                osint._expire(now)
                assert [e["kind"] for e in osint._events] == ["explosion"]
                assert [a["kind"] for a in osint._alerts] == ["explosion"]
        finally:
            osint.reset()

    def test_the_demo_shows_a_fresh_strike_and_an_old_one(self):
        # Otherwise the build with no network only ever draws markers at full
        # strength, and whether an old strike reads as old cannot be checked.
        ages = sorted(e["age_minutes"] for e in osint.demo()["events"]
                      if e["kind"] == "explosion")
        assert len(ages) >= 2
        assert ages[0] < 30 and ages[-1] > 120

    def test_the_lifetimes_reach_the_browser(self):
        # The browser fades a marker against its OWN lifetime, so it needs the
        # table rather than the one default.
        for feed in (osint.demo(), osint.current()):
            assert feed["keep"]["explosion"] > feed["keep"]["drone"]
            assert set(feed["keep"]) == set(osint.KINDS)


class TestTheAreaAnAlertCovers:
    """An air-raid warning is about a region, not a point.

    The size has to come from the gazetteer rather than a constant, because
    the two things this draws are enormously different: a strike in a village
    is a couple of kilometres across and a warning over an oblast is a
    hundred. One size for both would either lose the strike in a blob or
    shrink the oblast to a dot.
    """

    def test_an_oblast_is_drawn_far_wider_than_a_town(self):
        oblast = osint.area_km({"bbox": [49.2, 51.6, 29.2, 32.2], "kind": "administrative"})
        town = osint.area_km({"bbox": [49.84, 49.87, 30.79, 30.83], "kind": "town"})
        assert oblast > town * 10

    def test_it_is_half_the_diagonal_of_what_the_gazetteer_measured(self):
        box = [49.2, 51.6, 29.2, 32.2]
        want = osint.separation(box[0], box[2], box[1], box[3]) / 2
        assert osint.area_km({"bbox": box}) == pytest.approx(min(want, osint.AREA_MAX_KM),
                                                             abs=0.2)

    def test_a_place_with_no_extent_falls_back_on_what_sort_it_is(self):
        assert osint.area_km({"kind": "village"}) < osint.area_km({"kind": "city"})
        assert osint.area_km({"kind": "city"}) < osint.area_km({"kind": "administrative"})

    def test_a_place_with_nothing_at_all_still_gets_a_sensible_size(self):
        assert 1 < osint.area_km({}) < osint.AREA_MAX_KM

    def test_a_country_sized_box_is_capped(self):
        # Ukraine's own bounding box would otherwise shade a continent, and a
        # warning is never that. A cap is a smaller lie than the alternative.
        assert osint.area_km({"bbox": [44.0, 52.4, 22.1, 40.2]}) == osint.AREA_MAX_KM

    def test_a_degenerate_box_does_not_give_a_zero_radius(self):
        assert osint.area_km({"bbox": [50.0, 50.0, 30.0, 30.0], "kind": "town"}) > 0

    def test_the_area_reaches_the_events_that_need_it(self):
        for event in osint.demo()["events"]:
            assert event["area_km"] > 0, event["place"]

    def test_the_demo_has_both_a_wide_alert_and_a_narrow_strike(self):
        # Otherwise the build with no network draws one size and hides
        # whether the other is right.
        sizes = [e["area_km"] for e in osint.demo()["events"]
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
        got = osint.place_event(one(kind="alert", place="Kyiv oblast"),
                                "ua", lookup=self.looks_up())
        assert got["region_wide"] is True
        assert got["shape"] == self.RING

    def test_a_warning_over_a_town_does_not(self):
        # A town under a warning is a circle. Drawing the municipal boundary
        # would say the warning stops at the council's border, which is not
        # what an air-raid warning means.
        got = osint.place_event(one(kind="alert", place="Beirut"), "ua",
                                lookup=self.looks_up(kind="city", category="place"))
        assert got["region_wide"] is False
        assert got["shape"] is None
        assert got["area_km"] > 0

    def test_a_drone_crossing_a_region_does_not_shade_it(self):
        # It is at a point on its way through. Shading the province would say
        # the whole of it is under something it is not.
        got = osint.place_event(one(kind="drone", place="Kyiv oblast"),
                                "ua", lookup=self.looks_up())
        assert got["region_wide"] is False
        assert got["shape"] is None

    def test_a_strike_reported_across_a_region_does_shade_it(self):
        # "вибухи на Київщині" says explosions somewhere in the oblast and
        # does not say where. The region is the honest extent of that.
        got = osint.place_event(one(kind="explosion", place="Kyiv oblast"),
                                "ua", lookup=self.looks_up())
        assert got["region_wide"] is True

    def test_a_region_with_no_outline_falls_back_to_a_circle(self):
        got = osint.place_event(one(kind="alert", place="Kyiv oblast"), "ua",
                                lookup=self.looks_up(shape=None))
        assert got["region_wide"] is False
        assert got["shape"] is None
        assert got["area_km"] > 0

    def test_what_counts_as_a_region(self):
        for kind in ("administrative", "state", "province", "county",
                     "governorate", "emirate", "country"):
            assert osint.is_region({"kind": kind}), kind
        for kind in ("town", "city", "village", "suburb", "hamlet"):
            assert not osint.is_region({"kind": kind, "category": "place"}), kind

    def test_a_boundary_match_is_a_region_whatever_it_is_called(self):
        assert osint.is_region({"category": "boundary", "kind": "something new"})

    def test_the_demo_shows_a_region_alert_beside_a_town_one(self):
        # Otherwise the build with no network only draws circles, and whether
        # a boundary renders at all cannot be checked.
        alerts = [e for e in osint.demo()["events"] if e["kind"] == "alert"]
        assert len(alerts) >= 2
        assert any(e["region_wide"] for e in alerts)
        assert any(not e["region_wide"] for e in alerts)
        for event in alerts:
            if event["region_wide"]:
                assert event["shape"]["type"] in ("Polygon", "MultiPolygon")


class TestCarryingAMarkerForward:
    def test_due_north_changes_only_the_latitude(self):
        lat, lon = osint.advance(50.0, 30.0, 0, 111.195)
        assert lat == pytest.approx(51.0, abs=0.01)
        assert lon == pytest.approx(30.0, abs=1e-9)

    def test_due_east_at_latitude_is_not_the_flat_earth_answer(self):
        # 111 km east at 50 N is about 1.56 degrees of longitude, not 1.0.
        _, lon = osint.advance(50.0, 30.0, 90, 111.195)
        assert lon == pytest.approx(31.556, abs=0.01)

    def test_crossing_the_date_line_comes_out_the_other_side(self):
        _, lon = osint.advance(0.0, 179.5, 90, 200)
        assert -180 <= lon <= 180 and lon < 0

    def test_the_bearing_agrees_with_the_advance_that_uses_it(self):
        # The two halves must be the same spherical model, or a marker aimed
        # at a town arrives somewhere else.
        start, dest = (49.99, 36.23), (50.45, 30.52)
        lat, lon = osint.advance(*start, osint.bearing(*start, *dest),
                                 osint.separation(*start, *dest))
        assert lat == pytest.approx(dest[0], abs=0.01)
        assert lon == pytest.approx(dest[1], abs=0.01)

    def test_a_projected_event_keeps_the_position_that_was_reported(self):
        now = time.time()
        event = {"kind": "cruise", "heading": 90.0, "seen": now - 600,
                 "origin_lat": 50.0, "origin_lon": 30.0}
        got = osint.project(event, now)
        assert got["projected"] is True
        assert (got["origin_lat"], got["origin_lon"]) == (50.0, 30.0)
        assert got["projected_km"] == pytest.approx(
            osint.SPEEDS["cruise"] / 6, abs=1)

    def test_a_report_with_no_course_does_not_move(self):
        now = time.time()
        event = {"kind": "drone", "heading": None, "seen": now - 900,
                 "origin_lat": 50.0, "origin_lon": 30.0}
        got = osint.project(event, now)
        assert (got["lat"], got["lon"]) == (50.0, 30.0)
        assert got["projected"] is False

    def test_a_ballistic_track_outruns_everything(self):
        # It used to be frozen in place, on the grounds that no single speed
        # describes a ballistic flight. True, but a stationary marker said
        # something worse -- that it had landed -- so it now moves at a figure
        # that is at least the right order of magnitude, and expires quickly.
        now = time.time()
        event = {"kind": "ballistic", "heading": 90.0, "seen": now - 300,
                 "origin_lat": 50.0, "origin_lon": 30.0}
        ballistic = osint.project(event, now)
        assert ballistic["projected"] is True
        cruise = osint.project({**event, "kind": "cruise"}, now)
        assert ballistic["projected_km"] > cruise["projected_km"] * 3


class TestArrival:
    def make(self, minutes_ago, dest_km=100.0, kind="cruise"):
        now = time.time()
        return now, {"kind": kind, "heading": 90.0, "seen": now - minutes_ago * 60,
                     "origin_lat": 50.0, "origin_lon": 30.0, "dest_km": dest_km}

    def test_it_stops_at_the_place_it_was_going_to(self):
        # 800 km/h for half an hour is 400 km; the destination is 100.
        now, event = self.make(30)
        got = osint.project(event, now)
        assert got["projected_km"] == pytest.approx(100, abs=0.5)
        assert got["arrived"] is True

    def test_it_is_still_travelling_before_it_gets_there(self):
        now, event = self.make(3)
        got = osint.project(event, now)
        assert got["arrived"] is False and 30 < got["projected_km"] < 50

    def test_an_arrived_track_is_taken_off_the_map(self):
        now, event = self.make(30)
        assert osint._arrived(event, now) is True

    def test_a_track_with_no_destination_never_arrives(self):
        now, event = self.make(600, dest_km=None)
        assert osint._arrived(event, now) is False

    def test_a_strike_never_arrives_because_it_never_left(self):
        now, event = self.make(600, kind="explosion")
        assert osint._arrived(event, now) is False


class TestNothingIsSilentlyDropped:
    """The complaint that prompted the rewrite.

    A report the gazetteer cannot place used to vanish. Six reports arriving
    and none of them placing looked exactly like a feed that had stopped, and
    there was no way from the interface to tell the two apart.
    """

    def test_an_unplaceable_report_still_reaches_the_reader(self):
        got = osint.demo()
        unplaced = [a for a in got["alerts"] if not a["placed"]]
        assert unplaced, "the demo must exercise the unplaceable path"
        for alert in unplaced:
            assert alert["summary"]
            assert alert["why_unplaced"]

    def test_an_unplaceable_report_is_not_on_the_map(self):
        got = osint.demo()
        mapped = {e["id"] for e in got["events"]}
        for alert in got["alerts"]:
            if not alert["placed"]:
                assert alert["id"] not in mapped

    def test_the_counts_say_how_many_of_each(self):
        got = osint.demo()
        assert got["reports"]["placed"] == sum(1 for a in got["alerts"] if a["placed"])
        assert got["reports"]["unplaced"] >= 1
        assert (got["reports"]["placed"] + got["reports"]["unplaced"]
                == len(got["alerts"]))

    def test_placed_counts_reports_not_surviving_markers(self):
        # These are different numbers and conflating them was a real bug: a
        # track that has arrived or aged off the map was placed perfectly
        # well, and counting it as unplaced makes the gazetteer look broken
        # in exactly the number a reader checks to see whether it is.
        got = osint.demo()
        assert got["reports"]["placed"] >= len(got["events"])
        placed_ids = {a["id"] for a in got["alerts"] if a["placed"]}
        assert {e["id"] for e in got["events"]} <= placed_ids

    def test_alerts_outlive_tracks(self):
        # A position extrapolated for an hour is fiction; "a strike was
        # reported in Kharkiv an hour ago" is still true.
        assert osint.ALERT_MINUTES > osint.KEEP_MINUTES

    def test_alerts_come_newest_first(self):
        seen = [a["seen"] for a in osint.demo()["alerts"]]
        assert seen == sorted(seen, reverse=True)


class TestWhenTheModelWillNotPlay:
    """A free tier with a daily ceiling is not an exceptional condition."""

    class Reply:
        def __init__(self, status=200, body=None, headers=None):
            self.status_code, self._body = status, body
            self.ok = 200 <= status < 300
            self.headers = headers or {}

        def json(self):
            if self._body is None:
                raise ValueError("no json")
            return self._body

    def content(self, value):
        return self.Reply(200, {"choices": [{"message": {"content": value}}]})

    def test_a_200_with_null_content_does_not_crash_the_endpoint(self, monkeypatch):
        # The failure the user hit. A model returns a 200 whose content field
        # is null -- legitimate, it does that for a refusal -- and calling
        # .strip() on None raises AttributeError, which is not any of the
        # error classes the endpoints catch, so it went past all of them and
        # 500ed instead of falling back to reading the reports plainly.
        monkeypatch.setattr(osint.requests, "post",
                            lambda *a, **k: self.content(None))
        with pytest.raises(osint.OsintError):
            osint._ask_model("m", [{"id": "x", "text": "y"}], "k")

    def test_an_empty_string_is_the_same(self, monkeypatch):
        monkeypatch.setattr(osint.requests, "post",
                            lambda *a, **k: self.content("   "))
        with pytest.raises(osint.OsintError):
            osint._ask_model("m", [{"id": "x", "text": "y"}], "k")

    def test_read_events_refuses_anything_that_is_not_text(self):
        for junk in (None, "", "   ", 42, [], {}):
            with pytest.raises(osint.OsintError):
                osint.read_events(junk)

    def test_a_rate_limit_is_its_own_kind_of_problem(self, monkeypatch):
        monkeypatch.setattr(osint.requests, "post",
                            lambda *a, **k: self.Reply(429))
        with pytest.raises(osint.RateLimited):
            osint._ask_model("m", [{"id": "x", "text": "y"}], "k")

    def test_it_tries_the_next_free_model_when_one_is_exhausted(self, monkeypatch):
        # Free models share a daily ceiling and the popular ones reach it
        # first, so one refusing is routine rather than a failure.
        tried = []

        def answering(url, json=None, **kw):
            tried.append(json["model"])
            if len(tried) < 3:
                return self.Reply(429)
            return self.content('{"events": [{"kind": "drone", "place": "Kyiv"}]}')

        monkeypatch.setattr(osint.requests, "post", answering)
        got = osint._call_model([{"id": "x", "text": "y"}], "k")
        assert len(tried) == 3
        assert got[0]["place"] == "Kyiv"

    def test_only_when_every_model_refuses_is_it_a_rate_limit(self, monkeypatch):
        monkeypatch.setattr(osint.requests, "post",
                            lambda *a, **k: self.Reply(429))
        with pytest.raises(osint.RateLimited):
            osint._call_model([{"id": "x", "text": "y"}], "k")

    def test_the_service_is_taken_at_its_word_about_when_to_come_back(self):
        assert osint.retry_after(self.Reply(429, headers={"Retry-After": "45"})) == 45
        soon = (time.time() + 300) * 1000
        got = osint.retry_after(self.Reply(429, headers={"X-RateLimit-Reset": str(soon)}))
        assert 250 < got < 350

    def test_a_missing_or_silly_header_is_no_answer_rather_than_an_error(self):
        for headers in ({}, {"Retry-After": "soon"}, {"X-RateLimit-Reset": "-1"}):
            assert osint.retry_after(self.Reply(429, headers=headers)) == 0.0

    def test_the_backoff_grows_and_is_forgotten_on_success(self):
        # The version before this had none at all, and only recorded that it
        # had polled AFTER the model answered -- so a rate limit meant the
        # next page refresh tried again immediately, and every one after that,
        # which is the one thing guaranteed to keep a rate limit in place.
        try:
            osint._wake()
            now = 1000.0
            osint._rest(now, 0)
            first = osint._resting_until - now
            osint._rest(now, 0)
            second = osint._resting_until - now
            assert second > first
            assert second <= osint.BACKOFF_MAX
            osint._wake()
            assert osint._resting_until == 0
        finally:
            osint._wake()

    def test_the_services_own_wait_wins_when_it_is_longer(self):
        try:
            osint._wake()
            osint._rest(1000.0, 800.0)
            assert osint._resting_until - 1000.0 == pytest.approx(800.0)
        finally:
            osint._wake()


class TestTheKey:
    def test_it_is_set_and_cleared_and_never_returned(self):
        try:
            assert osint.set_key("sk-test") is True
            assert osint.has_key() is True
            assert osint.set_key("   ") is False
            assert osint.has_key() is False
        finally:
            osint.set_key(None)

    def test_the_answer_never_carries_the_key(self):
        try:
            osint.set_key("sk-secret-value")
            blob = json.dumps(osint.current())
            assert "sk-secret-value" not in blob
            assert json.loads(blob)["keyed"] is True
        finally:
            osint.set_key(None)

    def test_the_layer_works_without_a_key_at_all(self, monkeypatch):
        # It used to refuse to do anything without one, which made it look
        # broken to anyone who had not signed up for an OpenRouter account.
        # The model reads these reports better; it is not required to read
        # them, and the rules reader needs no key, no quota and no network.
        osint.set_key(None)
        monkeypatch.setattr(osint, "_fetch_channel", lambda name: [{
            "id": f"{name}/1", "channel": name, "text": "БпЛА повз Кагарлик курсом на північ",
            "when": dt.datetime.now(dt.timezone.utc).isoformat(),
        }])
        monkeypatch.setattr(osint, "_call_model", lambda *a: pytest.fail(
            "the model was called with no key"))
        monkeypatch.setattr(osint.gazetteer, "find", lambda name, countries="": {
            "lat": 49.86, "lon": 30.81, "name": name, "kind": "town"})
        try:
            got = osint.poll()
        finally:
            osint.reset()
        assert got["count"] >= 1
        assert got["events"][0]["by"] == "rules"
        assert "no OpenRouter key" in got["state"]


class TestDemo:
    def test_it_answers_in_the_shape_the_page_expects(self):
        got = osint.demo()
        assert set(got) >= {"events", "count", "alerts", "state", "keep_minutes",
                            "alert_minutes", "speeds", "kinds", "channels",
                            "regions", "model", "keyed", "reports"}
        for event in got["events"]:
            assert set(event) >= {"id", "kind", "lat", "lon", "origin_lat",
                                  "origin_lon", "heading", "seen", "placed"}
            assert event["kind"] in osint.KINDS
            assert math.isfinite(event["lat"]) and math.isfinite(event["lon"])

    def test_the_demo_shows_both_a_moving_and_a_still_marker(self):
        headings = {e["heading"] is None for e in osint.demo()["events"]}
        assert headings == {True, False}

    def test_the_demo_covers_every_way_a_marker_can_behave(self):
        # Otherwise the build with no network exercises one drawing path and
        # hides what the others do -- which is how a thing flying north came
        # to be drawn with the mark for something that had been shot down.
        events = osint.demo()["events"]
        assert {e["motion"] for e in events} >= {"track", "orbit", "still"}
        # A track whose report gave no direction at all. It must not be drawn
        # with an arrow, so the demo has to contain one to check that.
        assert any(e["motion"] == "track" and e["heading"] is None for e in events)
        # And one whose direction came from a compass course rather than a
        # named destination.
        assert any(e["heading"] is not None and e["dest_km"] is None
                   for e in events)

    def test_the_demo_courses_are_computed_from_its_own_gazetteer(self):
        for event in osint.demo()["events"]:
            if event["dest_km"] is None:
                continue
            assert event["heading"] == pytest.approx(
                osint.bearing(event["origin_lat"], event["origin_lon"],
                              event["dest_lat"], event["dest_lon"]), abs=0.01)

    def test_it_actually_runs_rather_than_resetting_every_call(self):
        # The first version rebuilt its events against the clock on each call,
        # so they were forever the same few minutes old: nothing aged, nothing
        # arrived, nothing expired, and the endings were unreachable.
        osint._demo_epoch = 0.0
        try:
            first = {e["id"]: e["age_minutes"] for e in osint.demo()["events"]}
            osint._demo_epoch -= 120           # as if two minutes had passed
            for event in osint.demo()["events"]:
                assert event["age_minutes"] == pytest.approx(first[event["id"]] + 2,
                                                             abs=0.2)
        finally:
            osint._demo_epoch = 0.0

    def test_it_lets_things_arrive_and_expire_and_then_starts_again(self):
        # One cycle outlives everything in flight. It does not outlive a
        # strike, which is held for six hours by design -- so what must be
        # empty at the end of a cycle is the flying things, not the map.
        osint._demo_epoch = 0.0
        try:
            osint.demo()
            osint._demo_epoch -= osint.DEMO_CYCLE - 1
            late = osint.demo()["events"]
            assert [e for e in late if e["kind"] != "explosion"] == []
            osint._demo_epoch -= 10
            assert osint.demo()["events"]
        finally:
            osint._demo_epoch = 0.0

    def test_the_channels_the_app_offers_are_the_ones_it_reads(self):
        names = {c["name"] for c in osint.CHANNELS}
        assert osint.demo()["channels"] == [c["name"] for c in osint.CHANNELS]
        assert {"redlinkleb", "shin_persian"} <= names

    def test_every_channel_says_which_countries_to_look_in(self):
        # Without it "Sumy" is as likely to match a street in another
        # hemisphere as the oblast capital.
        for channel in osint.CHANNELS:
            assert channel["countries"] and channel["region"]
            for code in channel["countries"].split(","):
                assert len(code) == 2 and code.islower(), channel
