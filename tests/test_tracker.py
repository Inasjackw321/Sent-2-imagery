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

import pathlib
import re

import datetime as dt
import threading
import math
import time

import pytest

from backend import (gazetteer as gaz, neighbours, neptun, ollama, places,
                     reports, tracker)


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


class PhotoReply:
    """A streaming response, for the photo proxy's tests."""

    def __init__(self, status, body, kind, redirect=False):
        self.status_code = status
        self.ok = 200 <= status < 300
        self.headers = {"Content-Type": kind}
        self.is_redirect = redirect
        self.is_permanent_redirect = False
        self._body = body

    def iter_content(self, size):
        for at in range(0, len(self._body), size):
            yield self._body[at:at + size]


def knows_cyrillic(name, countries=""):
    """A gazetteer that knows the Cyrillic names these posts actually use.

    fake_find below matches a handful of Latin spellings, which was right when
    the model was asked to transliterate. It is asked for the original script
    now, so a test feeding real Ukrainian needs a gazetteer that speaks it.
    """
    known = {
        "Нікопол": (47.5665, 34.4053), "Харк": (49.9935, 36.2304),
        "Київ": (50.4501, 30.5234), "Кагарлик": (49.8556, 30.8125),
        "Волин": (51.2, 25.3), "Kyivia": (50.4501, 30.5234),
    }
    for stem, (lat, lon) in known.items():
        if stem.lower() in name.lower():
            return {"lat": lat, "lon": lon, "name": name, "kind": "town",
                    "category": "place",
                    "bbox": [lat - 0.05, lat + 0.05, lon - 0.05, lon + 0.05],
                    "shape": None}
    return None


def fake_find(name, countries=""):
    """A gazetteer that knows a handful of places and nothing else."""
    known = {
        "Нікополь": (47.5665, 34.4053), "Нікополем": (47.5665, 34.4053), "Nikopolia": (47.5665, 34.4053),
        "Kharkiv": (49.9935, 36.2304), "Kyivia": (50.4501, 30.5234),
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

# Invented names, on purpose.
#
# These were real Latin spellings -- Nikopol, Kherson, Kyiv -- used as
# stand-ins for a gazetteer the tests inject. Once those spellings went into
# the built-in table (the English-posting channel names Ukrainian places in
# Latin), the table answered first and the injected gazetteer was never
# reached: every test here would have passed with its stub deleted. A name
# nothing holds cannot be short-circuited.
PLACES = {
    "Nikopolia": (47.5665, 34.4053),
    "Khersonia": (46.6354, 32.6169),
    "Kharkivia oblast": (49.7, 36.3),
    "Kyivia": (50.4501, 30.5234),
}


def gazetteer(name, countries=""):
    """A gazetteer that knows four places and admits the rest."""
    found = PLACES.get(name)
    if not found:
        return None
    return {"lat": found[0], "lon": found[1], "name": f"{name}, Ukraine", "kind": "city"}


# The channel the app is configured to read. One, now that NEPTUN covers
# Ukraine -- and named through this rather than spelled out, because the list
# has changed twice and every test that hard-coded a name broke both times.
ONLY = tracker.CHANNELS[0]["name"]


def one(**over):
    return {"kind": "drone", "place": "Nikopolia", "region": None, "toward": None,
            "course": None, "count": 1, "summary": "Drone over Nikopolia", **over}


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
        got = tracker.place_event(one(place="Nikopolia", course=0.0), "ua", lookup=gazetteer)
        assert got["placed"] is True
        assert got["heading"] == 0.0
        assert got["motion"] == "track"

    def test_a_named_destination_still_beats_a_compass_course(self):
        # The course is a direction; a destination is a direction AND a place
        # to stop. Where a report gives both, the one with more in it wins.
        got = tracker.place_event(one(place="Nikopolia", toward="Khersonia", course=0.0),
                                "ua", lookup=gazetteer)
        assert got["heading"] != 0.0
        assert got["dest_km"] is not None

    def test_a_warning_is_given_no_course_however_the_sentence_reads(self):
        # A warning is about a place. "Тривога у Києві, БпЛА курсом на Львів"
        # is two facts, and carrying the drone's bearing onto the warning
        # would draw an arrow for something that is not an object.
        got = tracker._clean({"kind": "alert", "place": "Kyivia",
                              "course": "N", "toward": "Lviv", "count": 1,
                              "summary": "alert", "region": None})
        assert got["course"] is None and got["toward"] is None
        read = reports.read("Повітряна тривога у Харкові курсом на північ")
        assert read["course"] is None and read["toward"] is None


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

    def test_a_warning_stays_far_longer_than_anything_in_flight(self):
        # The whole point of the per-kind clock: a warning declared over an
        # oblast is still in force an hour later, and a report of a drone
        # crossing that oblast is not.
        assert tracker.KEEP["alert"] >= 4 * tracker.KEEP["drone"]
        for kind in ("drone", "jet_drone", "missile", "aircraft"):
            assert tracker.KEEP[kind] < tracker.KEEP["alert"], kind

    def test_only_the_things_that_do_not_fly_stand_still(self):
        for name in tracker.NOT_AIRBORNE:
            assert tracker.MOTION[name] == "still", name
        # One, now that strikes are not drawn. A warning is a statement about
        # a region rather than an object with a course.
        assert set(tracker.NOT_AIRBORNE) == {"alert"}

    def test_the_kinds_are_the_differences_worth_drawing(self):
        # Seven. Written out rather than derived, because which distinctions
        # this map makes is a decision somebody took and not whatever happens
        # to be in the table.
        #
        # "explosion" left. It is still READ -- see NOT_DRAWN and the class
        # below -- and it is no longer something the map draws.
        #
        # "bomb" joined when NEPTUN's feed did. A KAB is released from an
        # aircraft near the line and glides tens of kilometres; folding it
        # into "missile" would draw a hundreds-of-kilometres weapon where a
        # tens-of-kilometres one was reported, which is the wrong answer to
        # "how long have I got".
        #
        # "fpv" joined because the feed sends them and neither vocabulary had
        # a name for one, so every FPV was drawn as a grey ring labelled
        # Unidentified -- the map holding the answer and refusing to say it.
        # Its range is a few kilometres rather than a few hundred, so the
        # mark says the thing that launched it is CLOSE, which is the whole
        # of what makes it worth its own kind.
        assert set(tracker.KINDS) == {
            "drone", "jet_drone", "fpv", "missile", "bomb", "aircraft",
            "alert", "unknown"}

    def test_every_removed_kind_folds_somewhere_real(self):
        for was, becomes in tracker.FOLD.items():
            assert was not in tracker.KINDS, f"{was} was not actually removed"
            assert becomes in tracker.KINDS, f"{was} folds to nothing"
            assert tracker.fold_kind(was) == becomes

    def test_the_kinds_the_readers_still_produce_are_all_named(self):
        """Named one by one, not looped over FOLD.

        Looping over FOLD only checks the entries that are there, so deleting
        one passes -- and a deleted entry is not a small thing: both readers
        go on producing these names, the prompt still has "балістика" in it
        and reports.py still has a regex for cruise missiles, so a missing
        fold means the model says "cruise" and the map draws an "unknown" in
        grey. Every missile, every time.
        """
        assert tracker.fold_kind("cruise") == "missile"
        assert tracker.fold_kind("ballistic") == "missile"
        assert tracker.fold_kind("recon") == "drone"
        assert tracker.fold_kind("helicopter") == "aircraft"

    def test_the_rule_reader_never_produces_a_kind_that_folds_to_unknown(self):
        # The other end of the same worry, checked against the reader itself
        # rather than against a list: whatever reports.py can return, this app
        # must have somewhere to put.
        from backend import reports
        for text in ("Розвідувальний БпЛА над районом", "Балістика на Дніпро",
                     "Крилаті ракети з моря", "Шахед над містом",
                     "реактивний БпЛА", "Вибухи в Одесі",
                     "Повітряна тривога", "Гелікоптер у повітрі"):
            raw = reports.find_kind(text)
            folded = tracker.fold_kind(raw)
            # Somewhere to put it: a kind the map draws, or one it has
            # deliberately decided not to. What must never happen is the
            # third thing -- falling through to "unknown", which is a grey
            # ring in the sky over a sentence that said something specific.
            assert folded in tracker.KINDS or folded in tracker.NOT_DRAWN, \
                f"{text!r}: {raw} -> {folded}"
            if raw != "unknown":
                assert folded != "unknown", \
                    f"{text!r} read as {raw} and then thrown away"

    def test_a_kind_nobody_recognises_is_unknown_rather_than_dropped(self):
        for junk in ("hovercraft", "", None, 42, "recon_drone"):
            assert tracker.fold_kind(junk) == "unknown", junk

    def test_there_is_no_recon_kind_any_more(self):
        """Removed, and folded into "drone".

        The distinction it drew is not one this data supports. Telling a
        reconnaissance drone from an attack drone means knowing the airframe;
        these reports say "БпЛА" most of the time; and a wrong guess between
        the two changes a mark from "something is coming" to "something is
        watching", which is the most consequential thing on this map to get
        wrong.

        The reader still recognises the phrase -- "розвідувальний БпЛА" is
        real and unambiguous when it appears -- and FOLD collapses it here,
        which is what makes putting it back a one-line change.
        """
        assert "recon" not in tracker.KINDS
        assert tracker.fold_kind("recon") == "drone"
        assert "orbit" not in tracker.MOTION.values()


class TestTellingSimilarPlacesApart:
    """The rest of the Kaharlyk failure: the wrong Kaharlyk."""

    def test_the_region_is_used_to_ask_a_narrower_question(self):
        asked = []

        def watching(name, countries):
            asked.append(name)
            return {"lat": 49.85, "lon": 30.81, "name": name, "kind": "town"}

        tracker.place_event(one(place="Kaharlykia", region="Kyivia oblast"),
                          "ua", lookup=watching)
        assert asked[0] == "Kaharlykia, Kyivia oblast"

    def test_the_bare_name_is_tried_when_the_region_finds_nothing(self):
        # The gazetteer may spell the oblast differently, and a right town
        # found without the region beats no town at all.
        asked = []

        def only_bare(name, countries):
            asked.append(name)
            if "," in name:
                return None
            return {"lat": 49.85, "lon": 30.81, "name": name, "kind": "town"}

        got = tracker.place_event(one(place="Kaharlykia", region="Kyivshchyna"),
                                "ua", lookup=only_bare)
        assert asked == ["Kaharlykia, Kyivshchyna", "Kaharlykia"]
        assert got["placed"] is True

    def test_a_region_already_in_the_name_is_not_repeated(self):
        asked = []

        def watching(name, countries):
            asked.append(name)
            return {"lat": 49.7, "lon": 36.3, "name": name, "kind": "state"}

        tracker.place_event(one(place="Kharkivia oblast", region="Kharkivia oblast"),
                          "ua", lookup=watching)
        assert asked == ["Kharkivia oblast"]


class TestPlacing:
    def test_a_known_place_gets_the_gazetteer_position(self):
        got = tracker.place_event(one(), "ua", lookup=gazetteer)
        assert got["placed"] is True
        assert (got["lat"], got["lon"]) == PLACES["Nikopolia"]
        assert got["place_match"] == "Nikopolia, Ukraine"

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
        got = tracker.place_event(one(toward="Khersonia"), "ua", lookup=gazetteer)
        # Khersonia is south-west of Nikopolia.
        assert 220 < got["heading"] < 245
        assert got["dest_km"] == pytest.approx(174, abs=10)

    def test_the_course_is_computed_not_taken_on_trust(self):
        # Whatever the model thought the bearing was never reaches here: the
        # only source of a heading is the two gazetteer positions.
        got = tracker.place_event(one(toward="Khersonia"), "ua", lookup=gazetteer)
        assert got["heading"] == pytest.approx(
            tracker.bearing(*PLACES["Nikopolia"], *PLACES["Khersonia"]), abs=0.01)

    def test_a_destination_the_gazetteer_does_not_know_leaves_it_still(self):
        # Placed, because the report's own location is known -- but with no
        # course, because there is nothing to compute one from.
        got = tracker.place_event(one(toward="Nowheresville"), "ua", lookup=gazetteer)
        assert got["placed"] is True
        assert got["heading"] is None and got["dest_km"] is None

    def test_a_destination_that_resolves_to_the_same_point_is_not_a_journey(self):
        def same(name, countries):
            return {"lat": 50.0, "lon": 30.0, "name": name, "kind": "city"}
        got = tracker.place_event(one(toward="Kyivia"), "ua", lookup=same)
        assert got["heading"] is None and got["dest_km"] is None

    def test_an_air_alert_does_not_travel_either(self):
        got = tracker.place_event(
            one(kind="alert", place="Nikopolia", toward="Khersonia"), "ua", lookup=gazetteer)
        assert got["heading"] is None


class TestHowLongThingsStay:
    """A warning does not move, so it decays differently.

    The twenty-minute default is about a position going stale, not about
    news: a report of a drone over an oblast stops describing anything
    current very quickly. A warning is a statement that stands until it is
    lifted, so it is held for as long as one usually runs.
    """

    def test_a_warning_is_held_for_longer_than_an_hour(self):
        assert tracker.keep_minutes("alert") >= 60

    def test_things_in_flight_still_go_quickly(self):
        for kind in ("drone", "jet_drone", "missile", "aircraft", "unknown"):
            assert tracker.keep_minutes(kind) == tracker.KEEP_MINUTES, kind

    def test_a_kind_that_says_nothing_gets_the_default(self):
        assert tracker.keep_minutes("nothing like this") == tracker.KEEP_MINUTES

    def test_an_hour_old_warning_is_kept_and_an_hour_old_drone_is_not(self):
        now = time.time()
        old = now - 3600
        assert tracker._alive({"kind": "alert", "seen": old}, now) is True
        assert tracker._alive({"kind": "drone", "seen": old}, now) is False

    def test_a_warning_does_go_eventually(self):
        now = time.time()
        gone = now - (tracker.keep_minutes("alert") + 1) * 60
        assert tracker._alive({"kind": "alert", "seen": gone}, now) is False

    def test_a_report_never_leaves_the_list_while_its_marker_is_on_the_map(self):
        # A mark on the map with nothing in the panel to explain it is worse
        # than either problem on its own, so the stream has to stretch to
        # cover the longest-lived mark there is.
        for kind in tracker.KINDS:
            held = max(tracker.ALERT_MINUTES, tracker.keep_minutes(kind))
            assert held >= tracker.keep_minutes(kind), kind

    def test_expiry_keeps_the_warning_and_drops_the_drone(self):
        now = time.time()
        tracker.reset()
        try:
            with tracker._lock:
                tracker._events.extend([
                    {"kind": "alert", "seen": now - 3600, "heading": None,
                     "origin_lat": 50.0, "origin_lon": 30.0},
                    {"kind": "drone", "seen": now - 3600, "heading": None,
                     "origin_lat": 50.0, "origin_lon": 30.0},
                ])
                tracker._alerts.extend([
                    {"kind": "alert", "seen": now - 3600, "placed": True},
                    {"kind": "drone", "seen": now - 3600, "placed": True},
                ])
                tracker._expire(now)
                # The MARKERS differ: a warning is held for ninety minutes
                # and a drone for twenty.
                assert [e["kind"] for e in tracker._events] == ["alert"]
                # The ROWS do not, and that is the rule working rather than
                # failing: the stream is held for max(ALERT_MINUTES, keep),
                # so a report always outlives its own marker. A drone whose
                # mark has gone still has the line that explains where it
                # went.
                assert [a["kind"] for a in tracker._alerts] == ["alert", "drone"]
        finally:
            tracker.reset()

    def test_the_demo_shows_a_fresh_mark_and_an_old_one(self):
        # Otherwise the build with no network only ever draws markers at full
        # strength, and whether an old mark reads as old cannot be checked.
        ages = sorted(e["age_minutes"] for e in tracker.demo()["events"]
                      if e["kind"] == "alert")
        assert len(ages) >= 2
        assert ages[0] < 30 and ages[-1] > 60

    def test_the_lifetimes_reach_the_browser(self):
        # The browser fades a marker against its OWN lifetime, so it needs the
        # table rather than the one default.
        for feed in (tracker.demo(), tracker.current()):
            assert feed["keep"]["alert"] > feed["keep"]["drone"]
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
        got = tracker.place_event(one(kind="alert", place="Kyivia oblast"),
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
        got = tracker.place_event(one(kind="drone", place="Kyivia oblast"),
                                "ua", lookup=self.looks_up())
        assert got["region_scope"] == "located"
        assert got["shape"] is not None
        # But it is emphatically not a claim that the region is under attack.
        assert got["region_wide"] is False

    def test_the_two_reasons_for_an_outline_are_kept_apart(self):
        covers = tracker.place_event(one(kind="alert", place="Kyivia oblast"),
                                   "ua", lookup=self.looks_up())
        located = tracker.place_event(one(kind="drone", place="Kyivia oblast"),
                                    "ua", lookup=self.looks_up())
        assert covers["region_scope"] == "covers"
        assert located["region_scope"] == "located"
        assert covers["region_wide"] and not located["region_wide"]

    def test_a_town_gets_no_region_scope_at_all(self):
        got = tracker.place_event(one(kind="drone", place="Beirut"), "ua",
                                lookup=self.looks_up(kind="city", category="place"))
        assert got["region_scope"] is None

    def test_a_warning_across_a_region_shades_it(self):
        # "тривога на Київщині" says the whole oblast is under it, and the
        # region is the honest extent of that.
        got = tracker.place_event(one(kind="alert", place="Kyivia oblast"),
                                "ua", lookup=self.looks_up())
        assert got["region_wide"] is True

    def test_a_region_is_one_before_its_outline_arrives(self):
        """Two questions that used to be answered by one field.

        "Did the report name a region" is about the match. "Can an area be
        drawn for it" is about whether a boundary has been fetched yet. They
        were the same test, so a report located to an oblast whose outline had
        not arrived counted as a POINT -- and was drawn as one, a mark sitting
        on the arithmetic centre of a province.

        Region-wide now, outline or not; the drawing waits for the boundary
        and hasArea() asks about that separately.
        """
        got = tracker.place_event(one(kind="alert", place="Kyivia oblast"), "ua",
                                lookup=self.looks_up(shape=None))
        assert got["region_wide"] is True
        assert got["region_scope"] == "covers"
        assert got["shape"] is None
        assert got["area_km"] > 0

    def test_a_track_located_to_a_region_says_so_without_an_outline(self):
        # The case the dots came from: a drone over an oblast, no boundary
        # fetched, drawn as a point in the middle of the province.
        got = tracker.place_event(one(kind="drone", place="Kyivia oblast"), "ua",
                                lookup=self.looks_up(shape=None))
        assert got["region_scope"] == "located"
        assert got["shape"] is None

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

    This class used to enforce a silhouette table: a delta wing for a Shahed, a
    swept wing for a jet drone, a finned body for a cruise missile, a dart for
    a ballistic one. They are gone, and the tests with them, because the idea
    was wrong rather than the drawings.

    At twenty pixels a delta wing and a swept delta wing are the same grey
    triangle, so the detail cost legibility and bought nothing. Worse, it
    implied a precision the data does not have: these reports say "a Shahed",
    and drawing a recognisable airframe suggests somebody identified a type.

    So everything in the air is one arrow and the kind is carried by colour.
    Which moves the burden here: the colours are now the only thing telling a
    drone from a cruise missile, and they have to actually do it.
    """

    def source(self):
        return (pathlib.Path(__file__).resolve().parent.parent
                / "frontend" / "js" / "tracker.js").read_text(encoding="utf-8")

    def test_there_is_one_arrow_and_no_silhouette_table(self):
        # Guarding against the per-airframe drawings coming back, along with
        # the precision they implied.
        text = self.source()
        assert "const SILHOUETTE" not in text
        assert "const ARROW =" in text
        assert "const BORROWED =" in text

    def test_every_kind_has_a_colour_to_be_told_apart_by(self):
        for name, look in tracker.KINDS.items():
            assert look["colour"].startswith("#") and len(look["colour"]) == 7, name

    def test_a_drone_and_a_missile_are_not_the_same_colour(self):
        # The whole weight of distinguishing them rests here now.
        drone = tracker.KINDS["drone"]["colour"]
        for kind in ("missile", "aircraft", "alert"):
            assert tracker.KINDS[kind]["colour"] != drone, kind

    def test_a_missile_is_drawn_like_a_drone_now(self):
        """One arrow for everything in flight, asked for and defensible.

        A slim arrow was added for missiles when a drone was #ff3b30 and a
        cruise missile #ff6a3b -- 58 apart out of 765, fine beside each other
        in a key and not fine across a map -- so the shape was carrying a
        distinction the palette could not. The palette can now: amber against
        purple is not a pair anybody confuses.

        What is left without it is that two arrow shapes at twenty pixels
        read as two kinds of aircraft, which is an airframe claim these
        reports do not support.
        """
        text = self.source()
        assert "const SLIM " not in text
        assert "SLIM_KINDS" not in text
        # And the one arrow is still pointed along the course, and is still
        # CALLED an arrow -- the shape name is what the drawing is known by in
        # the page, so a missile quietly keeping the old name would be a lie
        # told to anybody reading the marks rather than the source.
        block = text[text.index("  const borrowed = event.course_from"):]
        block = block[:block.index("\n}")]
        assert "const shape = `arrow" in block
        assert "facing" in block

    def test_the_colours_within_one_shape_are_far_enough_apart(self):
        # Distinct strings are not enough: two near-identical reds would pass
        # the test above and be indistinguishable. Checked within each group
        # that SHARES a drawing, because across groups the shape already tells
        # them apart and demanding colour distance too would be forcing an
        # amber warning to stop being amber.
        def rgb(hexed):
            return tuple(int(hexed[at:at + 2], 16) for at in (1, 3, 5))

        def apart(one, other):
            return sum(abs(x - y) for x, y in
                       zip(rgb(tracker.KINDS[one]["colour"]),
                           rgb(tracker.KINDS[other]["colour"])))

        # Everything in flight shares one arrow now, so the palette is the
        # whole of the distinction and every pair of them has to hold the gap
        # -- not just the pairs that happened to share a drawing.
        flying = tuple(name for name, look in tracker.KINDS.items()
                       if look["motion"] == "track")
        for i, one in enumerate(flying):
            for other in flying[i + 1:]:
                assert apart(one, other) >= 60, \
                    f"{one} and {other} are one shape and too close in colour"

    def test_the_kinds_drawn_by_behaviour_are_still_drawn_that_way(self):
        # A warning is a triangle over its area. It reads by what it is
        # rather than by a direction, and it is not an arrow: an arrow on a
        # warning would be pointing somewhere for no reason.
        for name in ("alert",):
            assert tracker.MOTION[name] == "still", name
        # And everything else is in the air and gets an arrow.
        for name in ("drone", "jet_drone", "missile", "aircraft", "unknown"):
            assert tracker.MOTION[name] == "track", name


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
        # One row per source: the channel and the feed.
        #
        # It used to demand all four channel states at once, and that worked
        # because there were five channels to put them in. With one channel
        # there is one state to see, and inventing a second to show another
        # would be the demo lying about which channels exist.
        assert {r["channel"] for r in rows} == (
            {c["name"] for c in tracker.CHANNELS} | {tracker.NEPTUN_SOURCE})
        # And every row carries the four numbers the panel reads, whatever
        # state it is in -- which is what this section is for.
        for row in rows:
            assert set(row) >= {"posts", "fresh", "read", "placed", "problem"}
        assert any(r["placed"] for r in rows)

    def test_every_channel_is_accounted_for(self, monkeypatch):
        try:
            rows = self.read_one(monkeypatch, {})
            # Every channel, and the feed, which is a source and not a
            # channel: it has no posts and no page, and it gets a row for the
            # same reason the channels do -- so "I cannot see anything from
            # NEPTUN" is answerable from the panel rather than by guessing.
            assert set(rows) == ({c["name"] for c in tracker.CHANNELS}
                                 | {tracker.NEPTUN_SOURCE})
        finally:
            tracker.reset()

    def test_a_channel_that_posted_nothing_says_so(self, monkeypatch):
        # Quiet and reachable, which is a different thing from unreachable and
        # needs a different fix. Written against whichever channel is
        # configured rather than a name, because that list is now one entry
        # and will change again.
        quiet = tracker.CHANNELS[0]["name"]
        try:
            rows = self.read_one(monkeypatch, {})
            assert rows[quiet]["posts"] == 0
            assert rows[quiet]["problem"] is None
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
            only = tracker.CHANNELS[0]["name"]
            rows = self.read_one(monkeypatch, {
                only: "БпЛА повз Кагарлик курсом на північ"})
            assert rows[only]["read"] == 1
            assert rows[only]["placed"] == 1

            # And the other half: readable as a drone, with no place name in
            # it at all, so there is nothing a gazetteer could be asked for.
            # Not the same problem as an unreadable post, and it needs a
            # different fix.
            rows = self.read_one(monkeypatch, {only: "БпЛА курсом на північ"})
            assert rows[only]["read"] == 1
            assert rows[only]["placed"] == 0
        finally:
            tracker.reset()


class TestBothSidesOfTheBorder:
    def test_every_channel_may_place_in_russia(self):
        # The one that is left reports Belgorod and Bryansk as much as Sumy.
        # Without the country in the list those names cannot resolve at all --
        # and the Russian side is the whole reason this channel is kept, now
        # that NEPTUN covers Ukraine.
        for channel in tracker.CHANNELS:
            assert "ru" in channel["countries"].split(","), channel["name"]

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
        # A town small enough that the built-in table has never heard of it,
        # on purpose: the table now answers first and would take Белгород --
        # which this used to use -- without asking the gazetteer at all.
        assert places.lookup("Обояні") is None
        asked = []

        def only_nominative(name, countries=""):
            asked.append(name)
            return {"lat": 51.2, "lon": 36.3, "name": name, "kind": "city",
                    "category": "place"} if name == "Обоян" else None

        got = tracker.place_event(one(place="Обояні"), "ru", lookup=only_nominative)
        assert asked[0] == "Обояні"
        assert got["placed"] is True

    def test_a_name_the_table_knows_never_reaches_the_gazetteer(self):
        """The whole point of the table, asserted as a cost.

        Nominatim answers one request a second. A poll that asks it for
        "Белгород" and "Сумська область" -- names that are three lines away in
        a dict -- spends a second apiece finding that out, and a night's
        reports took eleven seconds to place. Every form the reader can derive
        is checked against the table too, not just the name as written.
        """
        # Through the real gazetteer, with the network step under it made to
        # explode. Injecting a lookup would not do: an injected one is now
        # honoured instead of the table, on purpose, so this would be testing
        # the stub rather than the fast path it is about.
        asked = []

        def exploding(name, countries=""):
            asked.append(name)
            raise AssertionError(f"reached the network for {name}")

        book = tracker.gazetteer
        book.forget()
        was, book._ask = book._ask, exploding
        try:
            for name in ("Белгород", "Київ", "Сумська область", "Кременчуці",
                         "Харкові", "Одесі"):
                got = tracker.place_event(one(place=name), "ua,ru")
                assert got["placed"] is True, name
        finally:
            book._ask = was
        assert asked == [], asked

    def test_the_demo_reports_from_russia_too(self):
        named = [e["place"] for e in tracker.demo()["events"]]
        assert any("Белгород" in str(p) for p in named)


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
        now, event = self.event("missile", 60)
        got = tracker.project(event, now)
        assert (got["lat"], got["lon"]) == (50.0, 30.0)
        assert got["projected"] is False

    def test_nothing_of_any_kind_moves(self):
        for kind in tracker.KINDS:
            now, event = self.event(kind, 45)
            got = tracker.project(event, now)
            assert (got["lat"], got["lon"]) == (50.0, 30.0), kind

    def test_an_aircraft_does_not_move_either(self):
        # Replaces a test about a recon drone circling. There is no recon kind
        # and no orbiting motion any more: everything in the air is a static
        # arrow, and "aircraft" is the kind that used to be the other case.
        now, event = self.event("aircraft", 3)
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
        # Against the marks that came from reports. Derived warnings are not
        # reports -- nobody sent one -- so counting them here would make the
        # number stop meaning "reports the gazetteer could place". Nor is a
        # drone raised from a warning, for exactly the same reason.
        reported = [e for e in got["events"]
                    if not e.get("derived") and not e.get("from_warning")]
        assert got["reports"]["placed"] >= len(reported)
        placed_ids = {a["id"] for a in got["alerts"] if a["placed"]}
        # Every mark traces back to a row somebody can read. A mark raised
        # from a warning says which row is its own -- see "row" in
        # _drone_from_warning -- because one post is one row and it is the
        # second mark from that post rather than a second post.
        assert {e.get("row", e["id"]) for e in got["events"]} <= placed_ids

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


class TestPictures:
    """The pictures a report came with, and the proxy that fetches them.

    The proxy is the part that matters. It fetches a URL its caller supplies,
    which is a way into everything this process can reach and the caller
    cannot -- a metadata service, a container on the same network, a database
    on loopback. The allowlist is the whole of its security, so most of these
    tests are attempts to get past it.
    """

    def page(self, *styles, post="war_monitor/1", text="Вибухи у Харкові"):
        wraps = "".join(f'<a class="tgme_widget_message_photo_wrap" {s}></a>'
                        for s in styles)
        return (f'<div class="tgme_widget_message " data-post="{post}">{wraps}'
                f'<time datetime="2026-09-12T10:00:00+00:00"></time>'
                f'<div class="tgme_widget_message_text">{text}</div></div>')

    def test_a_photo_is_found_however_the_style_is_quoted(self):
        # Single, double, HTML-escaped double, and with a space after url(.
        # The first version accepted literal quotes only and silently missed
        # every &quot; one, which would have read as the channels having
        # stopped posting pictures.
        for style in (
            "style=\"background-image:url('https://cdn4.cdn-telegram.org/file/a.jpg')\"",
            'style="background-image:url(&quot;https://cdn1.cdn-telegram.org/file/b.jpg&quot;)"',
            "style='background-image:url(https://cdn2.cdn-telegram.org/file/c.jpg)'",
            "style=\"background-image: url('https://cdn3.cdn-telegram.org/file/d.jpg')\"",
        ):
            got = tracker.parse_preview(self.page(style), "war_monitor")
            assert len(got[0]["photos"]) == 1, style

    def test_a_picture_from_anywhere_else_is_not_taken_from_the_page(self):
        # Checked when read as well as when fetched. A rewritten page must not
        # be able to get an arbitrary URL into a stored event in the first
        # place.
        got = tracker.parse_preview(self.page(
            'style="background-image:url(https://evil.example/x.jpg)"',
            'style="background-image:url(https://cdn4.cdn-telegram.org/file/ok.jpg)"',
        ), "war_monitor")
        assert got[0]["photos"] == ["https://cdn4.cdn-telegram.org/file/ok.jpg"]

    def test_a_post_with_no_pictures_says_so_plainly(self):
        got = tracker.parse_preview(self.page(), "war_monitor")
        assert got[0]["photos"] == []

    def test_only_a_few_are_kept(self):
        many = [f'style="background-image:url(https://cdn4.cdn-telegram.org/f/{i}.jpg)"'
                for i in range(12)]
        got = tracker.parse_preview(self.page(*many), "war_monitor")
        assert len(got[0]["photos"]) == tracker.MOST_PHOTOS

    def test_the_post_is_linked_so_the_source_can_be_checked(self):
        got = tracker.parse_preview(self.page(post="kpszsu/99"), "kpszsu")
        assert got[0]["link"] == "https://t.me/kpszsu/99"

    def test_the_page_never_gets_a_telegram_url(self):
        # The browser talks to Telegram nowhere in this layer and must not
        # start for a thumbnail: an <img> at their CDN hands them the viewer's
        # address every time a popup opens.
        paths = tracker.photo_paths(["https://cdn4.cdn-telegram.org/file/a.jpg"])
        assert len(paths) == 1
        assert paths[0].startswith("/api/tracker/photo?u=")
        assert "cdn-telegram.org" not in paths[0].split("?u=")[0]

    def test_the_url_survives_being_put_in_a_query_string(self):
        from urllib.parse import parse_qs, urlparse
        url = "https://cdn4.cdn-telegram.org/file/a b.jpg?x=1&y=2"
        path = tracker.photo_paths([url])[0]
        assert parse_qs(urlparse(path).query)["u"] == [url]

    def test_rubbish_in_the_photo_list_is_dropped_rather_than_carried(self):
        assert tracker.photo_paths(None) == []
        assert tracker.photo_paths("not a list") == []
        assert tracker.photo_paths([None, 42, {}, "http://x", ""]) == []

    def test_the_proxy_refuses_everything_but_telegrams_own_cdn(self, monkeypatch):
        def must_not_fetch(*a, **k):
            raise AssertionError("the proxy opened a connection it should have refused")
        monkeypatch.setattr(tracker.requests, "get", must_not_fetch)
        for bad in [
            # Another host entirely.
            "https://evil.example/x.jpg",
            # The allowed name as a PREFIX of another domain, which is the
            # attack a naive startswith() check lets straight through.
            "https://cdn4.cdn-telegram.org.evil.example/x.jpg",
            # As a path or userinfo rather than the host.
            "https://evil.example/cdn4.cdn-telegram.org/x.jpg",
            "https://cdn4.cdn-telegram.org@evil.example/x.jpg",
            # Plain http, so it could be intercepted.
            "http://cdn4.cdn-telegram.org/x.jpg",
            # Schemes that reach things HTTP cannot.
            "file:///etc/passwd",
            "gopher://cdn4.cdn-telegram.org/x",
            # The classic targets of a URL-fetching endpoint.
            "https://169.254.169.254/latest/meta-data/",
            "https://127.0.0.1:11434/api/tags",
            "https://[::1]/x",
            # Not a string at all.
            None, 42, [],
        ]:
            with pytest.raises(tracker.TrackerError):
                tracker.fetch_photo(bad)

    def test_the_proxy_fetches_a_real_one(self, monkeypatch):
        monkeypatch.setattr(tracker.requests, "get", lambda *a, **k: PhotoReply(
            200, b"\x89PNG\r\n", "image/png"))
        body, kind = tracker.fetch_photo("https://cdn4.cdn-telegram.org/file/a.png")
        assert body == b"\x89PNG\r\n"
        assert kind == "image/png"

    def test_it_does_not_follow_a_redirect_off_the_allowed_host(self, monkeypatch):
        # A permitted host answering with a redirect elsewhere would otherwise
        # walk straight past the check that was the whole point.
        monkeypatch.setattr(tracker.requests, "get", lambda *a, **k: PhotoReply(
            302, b"", "text/html", redirect=True))
        with pytest.raises(tracker.TrackerError, match="redirect"):
            tracker.fetch_photo("https://cdn4.cdn-telegram.org/file/a.png")

    def test_it_asks_not_to_be_redirected_at_all(self, monkeypatch):
        seen = {}
        def record(url, **kw):
            seen.update(kw)
            return PhotoReply(200, b"x", "image/png")
        monkeypatch.setattr(tracker.requests, "get", record)
        tracker.fetch_photo("https://cdn4.cdn-telegram.org/file/a.png")
        assert seen.get("allow_redirects") is False

    def test_something_that_is_not_an_image_is_refused(self, monkeypatch):
        for kind in ("text/html", "application/json", "", "text/plain"):
            monkeypatch.setattr(tracker.requests, "get", lambda *a, **k: PhotoReply(
                200, b"<html>", kind))
            with pytest.raises(tracker.TrackerError, match="not an image"):
                tracker.fetch_photo("https://cdn4.cdn-telegram.org/file/a.png")

    def test_an_enormous_picture_is_cut_off_rather_than_read(self, monkeypatch):
        # Measured rather than trusted: Content-Length is a claim.
        huge = b"x" * (tracker.PHOTO_BYTES + 1024)
        monkeypatch.setattr(tracker.requests, "get", lambda *a, **k: PhotoReply(
            200, huge, "image/png"))
        with pytest.raises(tracker.TrackerError, match="too large"):
            tracker.fetch_photo("https://cdn4.cdn-telegram.org/file/a.png")

    def test_a_refusal_from_telegram_is_passed_on_as_one(self, monkeypatch):
        monkeypatch.setattr(tracker.requests, "get", lambda *a, **k: PhotoReply(
            404, b"", "text/html"))
        with pytest.raises(tracker.TrackerError, match="404"):
            tracker.fetch_photo("https://cdn4.cdn-telegram.org/file/a.png")

    def test_a_network_failure_is_a_tracker_error_not_a_crash(self, monkeypatch):
        def boom(*a, **k):
            raise tracker.requests.RequestException("down")
        monkeypatch.setattr(tracker.requests, "get", boom)
        with pytest.raises(tracker.TrackerError):
            tracker.fetch_photo("https://cdn4.cdn-telegram.org/file/a.png")

    def test_the_demo_has_pictures_on_a_report(self):
        # A feature whose only demonstration needs a network is a feature
        # nobody checks. It used to be the strikes, which is where a channel
        # actually puts them; with those gone one row is named explicitly so
        # it cannot quietly stop being any row at all.
        shown = [e for e in tracker.demo()["events"] if e.get("photos")]
        assert shown, "the demo demonstrates the picture path on nothing"
        for event in shown:
            assert event["place"] == tracker.DEMO_WITH_PICTURES

    def test_the_demo_pictures_are_obviously_not_photographs(self):
        # The single worst thing in this app to get wrong would be a
        # convincing invented picture of a strike.
        for event in tracker.demo()["events"]:
            for shot in event.get("photos") or []:
                assert shot.startswith("data:image/svg+xml"), shot
                assert "demo" in shot


class TestStrikesAreReadAndNotDrawn:
    """Explosions come off the map, and stay recognised.

    A strike was a star held for twenty-five hours. It is gone because the
    position was never good enough to earn a point on a map: it is read out of
    "вибухи в Харкові" -- explosions in Kharkiv -- which names a city of a
    million people, and the star went on the city centroid as though somebody
    had given a grid reference. Next to NEPTUN's map, which draws no strikes
    at all, it was the least trustworthy thing on the screen and also the
    loudest.

    The half that must NOT change is the reading. "Вибух" has to keep being
    recognised as an explosion, because a kind the reader has no name for
    falls through to "unknown" -- which would draw a grey ring in the sky over
    a report that explosions had been heard. That is a worse claim than the
    one just removed, and it is what these tests are mostly about.
    """

    def setup_method(self):
        tracker.reset()

    def teardown_method(self):
        tracker.reset()

    def test_it_is_not_a_kind_the_map_draws(self):
        assert "explosion" not in tracker.KINDS
        assert "explosion" in tracker.NOT_DRAWN

    def test_the_reader_still_recognises_one(self):
        assert reports.find_kind("Вибухи в Одесі") == "explosion"

    def test_and_it_survives_the_fold_as_itself(self):
        # Not "unknown". This is the whole reason NOT_DRAWN exists rather
        # than the entry simply being deleted.
        assert tracker.fold_kind("explosion") == "explosion"
        assert tracker.fold_kind("Explosion") == "explosion"

    def test_it_is_dropped_at_the_gate_every_source_goes_through(self):
        assert tracker._clean({"kind": "explosion", "place": "Харків",
                               "summary": "boom"}) is None

    def test_a_post_about_explosions_puts_nothing_on_the_map(self):
        tracker._record_all([{"id": "p/1", "channel": "x", "region": "Ukraine",
                              "countries": "ua",
                              "text": "Вибухи в Харкові"}]) \
            if hasattr(tracker, "_record_all") else None
        for got in reports.read_all("Вибухи в Харкові"):
            item = tracker._clean({**got, "id": "p/1"})
            assert item is None, got

    def test_nothing_in_the_demo_is_one(self):
        assert not [e for e in tracker.demo()["events"]
                    if e["kind"] == "explosion"]
        assert not [a for a in tracker.demo()["alerts"]
                    if a["kind"] == "explosion"]

    def test_the_reading_window_shrank_with_it(self):
        """Derived, so it fell on its own when the longest keep time went.

        It was twenty-five hours because a strike was held that long. Nothing
        is held that long now, and reading a day of channel history to find
        marks that expire in ninety minutes would be work for nobody.
        """
        assert tracker.LOOKBACK_MINUTES == max(tracker.KEEP.values())
        assert tracker.LOOKBACK_MINUTES <= 120

    def test_the_report_outlives_its_own_marker_never_the_other_way(self):
        # A marker on the map with no row in the panel to explain it is worse
        # than either problem alone.
        for kind in tracker.KINDS:
            held = max(tracker.ALERT_MINUTES, tracker.keep_minutes(kind))
            assert held >= tracker.keep_minutes(kind), kind

    def test_nothing_in_flight_is_held_anywhere_near_as_long(self):
        for kind, motion in tracker.MOTION.items():
            if motion == "still":
                continue
            assert tracker.keep_minutes(kind) <= 60, kind


class TestHowFarBackItReads:
    def test_it_reads_everything_that_could_still_be_on_the_map(self):
        """The window is the longest keep time, and it is derived from it.

        It used to be twenty minutes, with a comment arguing that reading
        further back would cost "a lot of somebody else's bandwidth". It costs
        none: Telegram's preview page is one fetch returning about twenty
        posts whatever you do, so every post is already downloaded and parsed
        before this decides whether to look at it. The window only decided how
        many to throw away.

        What it looked like was four channels reporting "20 posts, nothing
        new" and an empty map, because on any quiet half-hour nothing on the
        page is inside twenty minutes.
        """
        assert tracker.LOOKBACK_MINUTES == max(tracker.KEEP.values())

    def test_it_is_long_enough_for_the_kind_held_longest(self):
        # A strike is held for a day. Reading back less than that meant a
        # freshly opened app could never show one older than twenty minutes,
        # however long it was meant to be kept.
        for kind, keep in tracker.KEEP.items():
            assert tracker.LOOKBACK_MINUTES >= keep, kind
        assert tracker.LOOKBACK_MINUTES >= tracker.ALERT_MINUTES

    def test_raising_a_keep_time_widens_the_window_on_its_own(self):
        # Derived, not written down twice. These fell out of step once
        # already: strikes were given a day and the read window stayed at
        # twenty minutes, so the extra day was unreachable.
        assert tracker._lookback_minutes() == tracker.LOOKBACK_MINUTES
        was = dict(tracker.KEEP)
        try:
            tracker.KEEP["explosion"] = 9999
            assert tracker._lookback_minutes() == 9999
        finally:
            tracker.KEEP.clear()
            tracker.KEEP.update(was)

    def test_it_is_at_least_as_long_as_the_gap_between_polls(self):
        # A lookback shorter than the poll interval would drop posts in the
        # gap between two reads.
        assert tracker.LOOKBACK_MINUTES * 60 >= tracker.MIN_POLL_SECONDS


class TestGivingEachMarkADirection:
    """Where an arrow's direction comes from, and where it must not.

    Every mark in the air is drawn as an arrow now, which makes the direction
    load-bearing: an arrow points somewhere whether or not anybody said so.
    There are three legitimate sources and one honest absence, and these tests
    are about not quietly turning the absence into the fourth source.
    """

    def test_a_stated_compass_course_is_used_and_recorded_as_stated(self):
        got = tracker.place_event(
            {"kind": "drone", "place": "Nikopolia", "course": 270.0,
             "count": 1, "summary": ""}, "ua", lookup=fake_find)
        assert got["heading"] == 270.0
        assert got["course_from"] == "stated"

    def test_a_destination_beats_a_compass_course(self):
        # A bearing between two named places is a better answer than "north",
        # so it wins where both are present.
        got = tracker.place_event(
            {"kind": "drone", "place": "Kyivia", "toward": "Kharkiv",
             "course": 0.0, "count": 1, "summary": ""}, "ua", lookup=fake_find)
        assert got["course_from"] == "destination"
        # Kyiv to Kharkiv is roughly east-south-east, certainly not north.
        assert 80 < got["heading"] < 130, got["heading"]

    def test_no_course_anywhere_stays_none_rather_than_becoming_north(self):
        got = tracker.place_event(
            {"kind": "drone", "place": "Nikopolia", "count": 1, "summary": ""},
            "ua", lookup=fake_find)
        assert got["heading"] is None
        assert got["course_from"] is None

    def test_a_warning_is_given_no_course_at_all(self):
        # The rule is about motion rather than about the kind: something that
        # is not travelling has no course, and a warning is a place rather
        # than a direction.
        got = tracker.place_event(
            {"kind": "alert", "place": "Nikopolia", "course": 90.0,
             "count": 1, "summary": ""}, "ua", lookup=fake_find)
        assert got["heading"] is None

    def test_a_group_lends_its_course_to_the_marks_without_one(self):
        marks = [
            {"id": "a", "kind": "drone", "lat": 50.40, "lon": 30.50,
             "seen": 1, "heading": 270.0, "course_from": "stated"},
            {"id": "b", "kind": "drone", "lat": 50.45, "lon": 30.55,
             "seen": 2, "heading": 280.0, "course_from": "stated"},
            {"id": "c", "kind": "drone", "lat": 50.50, "lon": 30.60,
             "seen": 3, "heading": None, "course_from": None},
        ]
        masses = tracker.massed(marks)
        assert tracker.borrow_course(marks, masses) == 1
        borrowed = [m for m in marks if m["id"] == "c"][0]
        assert borrowed["heading"] == masses[0]["course"]
        assert borrowed["course_from"] == "group"

    def test_borrowing_never_overwrites_a_mark_with_its_own_course(self):
        marks = [
            {"id": "a", "kind": "drone", "lat": 50.40, "lon": 30.50,
             "seen": 1, "heading": 270.0, "course_from": "stated"},
            {"id": "b", "kind": "drone", "lat": 50.45, "lon": 30.55,
             "seen": 2, "heading": 275.0, "course_from": "stated"},
            {"id": "c", "kind": "drone", "lat": 50.50, "lon": 30.60,
             "seen": 3, "heading": 90.0, "course_from": "stated"},
        ]
        masses = tracker.massed(marks)
        assert tracker.borrow_course(marks, masses) == 0
        assert [m["heading"] for m in marks] == [270.0, 275.0, 90.0]
        assert all(m["course_from"] == "stated" for m in marks)

    def test_a_group_with_no_course_lends_nothing(self):
        marks = [{"id": f"m{i}", "kind": "drone", "lat": 50.4 + i * 0.05,
                  "lon": 30.5 + i * 0.05, "seen": i, "heading": None,
                  "course_from": None} for i in range(4)]
        masses = tracker.massed(marks)
        assert masses[0]["course"] is None
        assert tracker.borrow_course(marks, masses) == 0
        assert all(m["heading"] is None for m in marks)

    def test_a_group_pulling_in_opposite_directions_lends_nothing(self):
        # The important refusal. Averaging a group flying two ways gives a
        # direction neither of them is going, and lending that out would put
        # arrows on the map that contradict the reports they came from.
        marks = [
            {"id": "a", "kind": "drone", "lat": 50.40, "lon": 30.50,
             "seen": 1, "heading": 0.0, "course_from": "stated"},
            {"id": "b", "kind": "drone", "lat": 50.45, "lon": 30.55,
             "seen": 2, "heading": 180.0, "course_from": "stated"},
            {"id": "c", "kind": "drone", "lat": 50.50, "lon": 30.60,
             "seen": 3, "heading": None, "course_from": None},
        ]
        masses = tracker.massed(marks)
        assert masses[0]["course"] is None
        assert tracker.borrow_course(marks, masses) == 0
        assert marks[2]["heading"] is None

    def test_a_borrowed_course_says_how_many_it_came_from(self):
        # So the popup can say "averaged from two of six" rather than
        # implying all six were reported heading that way.
        marks = [
            {"id": "a", "kind": "drone", "lat": 50.40, "lon": 30.50,
             "seen": 1, "heading": 270.0, "course_from": "stated"},
            {"id": "b", "kind": "drone", "lat": 50.45, "lon": 30.55,
             "seen": 2, "heading": None, "course_from": None},
            {"id": "c", "kind": "drone", "lat": 50.50, "lon": 30.60,
             "seen": 3, "heading": None, "course_from": None},
        ]
        masses = tracker.massed(marks)
        assert masses[0]["course_from_count"] == 1
        tracker.borrow_course(marks, masses)
        assert marks[1]["course_from_count"] == 1

    def test_the_demo_shows_all_four_drawings(self):
        # Solid arrow from a report, solid from a destination, hollow from a
        # group, and a ring for no direction at all. If the demo cannot show
        # one of them, that drawing is unreachable without a network.
        got = {e.get("course_from") for e in tracker.demo()["events"]
               if e["kind"] not in tracker.NOT_AIRBORNE}
        assert got == {"stated", "destination", "group", None}, got


class TestAveragingBearings:
    """The general trajectory of a group.

    Bearings are circular, and the arithmetic mean of a circle is wrong in a
    way that only shows up sometimes -- which is the worst way for arithmetic
    to be wrong.
    """

    def test_the_wrap_at_north_does_not_break_it(self):
        # 350 and 10 average to due north. The arithmetic mean says 180:
        # due south, for two things both flying very nearly due north.
        assert tracker.mean_bearing([350.0, 10.0]) == 0.0

    def test_a_plain_average_still_works_where_there_is_no_wrap(self):
        assert tracker.mean_bearing([80.0, 100.0]) == 90.0
        assert tracker.mean_bearing([265.0, 275.0]) == 270.0

    def test_one_bearing_averages_to_itself(self):
        assert tracker.mean_bearing([47.0]) == 47.0

    def test_opposed_bearings_have_no_average(self):
        # And saying so is the point: there is no general trajectory for a
        # group flying two ways, and drawing one would invent an agreement.
        assert tracker.mean_bearing([0.0, 180.0]) is None
        assert tracker.mean_bearing([90.0, 270.0]) is None
        assert tracker.mean_bearing([0.0, 120.0, 240.0]) is None

    def test_a_mostly_agreed_group_still_averages(self):
        # One dissenter should not silence four that agree.
        got = tracker.mean_bearing([270.0, 275.0, 265.0, 272.0, 30.0])
        assert got is not None
        assert 240 < got < 300, got

    def test_nothing_averages_to_nothing(self):
        assert tracker.mean_bearing([]) is None

    def test_every_answer_is_a_bearing_a_map_can_use(self):
        import random
        rand = random.Random(11)
        for _ in range(400):
            some = [rand.uniform(0, 360) for _ in range(rand.randint(1, 6))]
            got = tracker.mean_bearing(some)
            assert got is None or 0 <= got < 360, (some, got)


class TestWarningsLastAnHour:
    def test_an_alert_is_kept_for_an_hour_and_a_half(self):
        """Ninety minutes, matching how long the report stays readable.

        It was sixty, and the sixty was what decided whether the warnings
        ALREADY IN FORCE when you open the app get drawn: one declared
        seventy minutes ago and still running showed as nothing at all. It
        also outlived its own mark -- the report sat in the stream for ninety
        minutes explaining a mark that had left at sixty.
        """
        assert tracker.KEEP["alert"] == 90
        assert tracker.KEEP["alert"] == tracker.ALERT_MINUTES

    def test_which_is_longer_than_something_in_flight(self):
        # A warning is not a position that decays, so the twenty-minute
        # default was wrong for it -- just less dramatically than for a strike.
        assert tracker.KEEP["alert"] > tracker.KEEP["drone"]

    def test_an_alert_from_fifty_minutes_ago_is_still_drawn(self):
        now = time.time()
        assert tracker._alive({"kind": "alert", "seen": now - 50 * 60}, now)

    def test_an_alert_from_two_hours_ago_is_not(self):
        now = time.time()
        assert not tracker._alive({"kind": "alert", "seen": now - 7200}, now)


class TestLoadingQuickly:
    """Two different speeds, and they were both being got wrong.

    How long the work takes, and how long a request waits for it. Those are
    separable and only the second is what "slow to load" means: a page that
    gets an empty answer in five milliseconds and fills in a second later feels
    immediate, and one that waits two seconds for a complete answer does not,
    even though the second is doing less total work.
    """

    def test_the_channels_are_read_all_at_once(self, monkeypatch):
        """Measured, not asserted from reading the code.

        Four fetches of 120 ms each: sequentially that is at least 480 ms, in
        parallel a little over 120. The gap is large enough that this cannot
        pass by accident on a slow machine.
        """
        order = []

        def slow(channel):
            order.append(("start", channel, time.time()))
            time.sleep(0.12)
            order.append(("done", channel, time.time()))
            return []

        tracker.reset()
        monkeypatch.setattr(tracker, "_fetch_channel", slow)
        began = time.time()
        tracker.poll()
        took = time.time() - began

        assert took < 0.35, f"took {took:.2f}s -- the fetches are sequential"
        # And they really did overlap, rather than one being fast.
        starts = [when for what, _, when in order if what == "start"]
        assert max(starts) - min(starts) < 0.1, "the fetches did not overlap"

    def test_one_channel_failing_does_not_lose_the_others(self, monkeypatch):
        """A fetch that blows up must not take the rest of the poll with it.

        In a thread pool an unexpected exception surfaces where the result is
        collected, not where it was raised. Run against a configured set of
        three rather than the one the app currently has -- the property is
        about the pool, and pinning it to today's channel list would mean this
        stops testing anything the moment that list changes again, which it
        has twice.
        """
        monkeypatch.setattr(tracker, "CHANNELS", (
            {"name": "a", "region": "Ukraine", "countries": "ua"},
            {"name": "b", "region": "Ukraine", "countries": "ua"},
            {"name": "c", "region": "Ukraine", "countries": "ua"},
        ))

        def mixed(channel):
            if channel == "a":
                raise RuntimeError("something nobody thought of")
            if channel == "b":
                raise tracker.TrackerError("b answered 500")
            return []

        tracker.reset()
        monkeypatch.setattr(tracker, "_fetch_channel", mixed)
        got = tracker.poll()
        rows = {r["channel"]: r for r in got["sources"]}
        assert rows["a"]["problem"]
        assert "500" in rows["b"]["problem"]
        # The one that worked is still accounted for, not missing.
        assert rows["c"]["problem"] is None

    def test_a_request_does_not_wait_for_the_channels(self, monkeypatch):
        # The first-paint measurement. The work takes the better part of a
        # second; the request must not.
        def slow(channel):
            time.sleep(0.4)
            return []

        tracker.reset()
        monkeypatch.setattr(tracker, "_fetch_channel", slow)
        began = time.time()
        got = tracker.refresh()
        waited = time.time() - began
        assert waited < 0.1, f"the request waited {waited:.2f}s"
        assert got["polling"] is True, "and it should say a read is in flight"
        # Let the thread finish so it does not run into the next test.
        for _ in range(60):
            if not tracker._polling:
                break
            time.sleep(0.05)

    def test_two_requests_together_start_only_one_read(self, monkeypatch):
        reads = []

        def counted(channel):
            reads.append(channel)
            time.sleep(0.15)
            return []

        tracker.reset()
        monkeypatch.setattr(tracker, "_fetch_channel", counted)
        for _ in range(5):
            tracker.refresh()
        for _ in range(60):
            if not tracker._polling:
                break
            time.sleep(0.05)
        # Four channels, once each -- not five times over.
        assert len(reads) == len(tracker.CHANNELS), reads

    def test_a_read_slower_than_the_floor_still_does_not_double_up(
            self, monkeypatch):
        """The case the in-flight check exists for, and the only one.

        When the floor has not passed, the floor stops a second read on its
        own -- so a test that only checks rapid requests passes with the
        in-flight check deleted. What it cannot cover is a read that takes
        longer than the floor, which is exactly when two would overlap: four
        page fetches and a model call can easily outlast thirty seconds on a
        slow machine, and two overlapping reads would double every request to
        Telegram and race each other writing the event list.
        """
        reads = []

        def slow(channel):
            reads.append(channel)
            time.sleep(0.4)
            return []

        tracker.reset()
        monkeypatch.setattr(tracker, "_fetch_channel", slow)
        assert tracker.start_poll() is True
        # Pretend the floor has elapsed while the read is still running.
        time.sleep(0.1)
        with tracker._lock:
            tracker._last_poll = time.time() - tracker.MIN_POLL_SECONDS - 1
        assert tracker.start_poll() is False, "a second read started mid-read"
        for _ in range(80):
            if not tracker._polling:
                break
            time.sleep(0.05)
        assert len(reads) == len(tracker.CHANNELS), reads

    def test_the_floor_is_claimed_when_a_read_starts_not_when_it_ends(
            self, monkeypatch):
        # Setting it at the end meant a slow read let the next request
        # straight through, which is how a rate limit used to keep itself
        # alive on the hosted model.
        tracker.reset()
        monkeypatch.setattr(tracker, "_fetch_channel",
                            lambda channel: (time.sleep(0.3), [])[1])
        assert tracker.start_poll() is True
        assert tracker._last_poll > 0, "the floor was not claimed up front"
        assert tracker.start_poll() is False, "a second read started anyway"
        for _ in range(60):
            if not tracker._polling:
                break
            time.sleep(0.05)

    def test_a_background_read_that_blows_up_does_not_wedge_the_next_one(
            self, monkeypatch):
        # The flag must be cleared whatever happens, or one bad poll stops
        # every poll after it for the life of the process -- a map that goes
        # permanently stale with nothing in the panel to say why.
        tracker.reset()
        monkeypatch.setattr(tracker, "poll",
                            lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert tracker.start_poll() is True
        for _ in range(60):
            if not tracker._polling:
                break
            time.sleep(0.05)
        assert tracker._polling is False

        # And the next one actually runs, which is the thing that matters and
        # which checking the flag alone does not prove.
        reads = []
        monkeypatch.undo()
        monkeypatch.setattr(tracker, "_fetch_channel",
                            lambda channel: (reads.append(channel), [])[1])
        with tracker._lock:
            tracker._last_poll = 0.0
        assert tracker.start_poll() is True
        for _ in range(60):
            if not tracker._polling:
                break
            time.sleep(0.05)
        assert len(reads) == len(tracker.CHANNELS), reads

    def test_the_floor_is_polite_to_telegram(self):
        # Faster to load is not the same as asking more often. Four channels
        # at this floor is about five hundred page fetches an hour, and there
        # is no version of this feature that justifies more.
        per_hour = 3600 / tracker.MIN_POLL_SECONDS * len(tracker.CHANNELS)
        assert per_hour <= 600, f"{per_hour:.0f} fetches an hour is too many"
        assert tracker.MIN_POLL_SECONDS >= 20


class TestReadingByRule:
    """No model in this path at all, and the digest is why.

    A model was here and was dropped, not for speed -- though it was two
    orders of magnitude slower than the four page fetches around it -- but
    because it was worse at the job. It transliterated names the gazetteer
    holds in Cyrillic ("Кагарлик" came back as "Kagul", a town in Moldova)
    and it flattened the movement digests the same way a single regex did.

    What reads those is structure. reports.read_all() returns one reading per
    named place, so the busiest post of the night is seventeen arrows rather
    than one ring in the middle of a province.
    """

    def feed(self, monkeypatch, texts, one_channel=True):
        if one_channel:
            monkeypatch.setattr(tracker, "CHANNELS", (
                {"name": "eRadarrua", "region": "Ukraine",
                 "countries": "ua,ru,by"},))
        when = dt.datetime.now(dt.timezone.utc).isoformat()
        monkeypatch.setattr(tracker, "_fetch_channel", lambda channel: [
            {"id": f"{channel}/{i}", "channel": channel, "when": when,
             "text": text, "photos": [], "link": None}
            for i, text in enumerate(texts)])
        monkeypatch.setattr(tracker.gazetteer, "find", knows_cyrillic)

    def test_the_map_fills_with_no_model_anywhere(self, monkeypatch):
        tracker.reset()
        self.feed(monkeypatch, ["Шахед над Нікополем курсом на північ",
                                "Повітряна тривога у Харкові"])
        got = tracker.poll()
        assert got["count"] == 2
        assert all(e["by"] == "rules" for e in got["events"])

    def test_a_post_the_rules_cannot_read_is_not_invented(self, monkeypatch):
        # Commentary and appeals for donations are not events, and a reader
        # that produced one for every post would be worse than a quiet one.
        tracker.reset()
        self.feed(monkeypatch, ["Підписуйтесь на наш канал"])
        assert tracker.poll()["count"] == 0

    def test_a_movement_digest_is_one_mark_per_town(self, monkeypatch):
        """The post in the screenshot, and what it used to produce.

        Fifteen settlements across five oblasts, every section stating a
        westerly course. It drew ONE mark -- on whichever oblast matched last,
        with no course at all, because the course is written in the
        instrumental ("рухаються західним курсом") rather than as "курсом на
        захід", and the single-reading path only looked for the latter.
        """
        tracker.reset()
        self.feed(monkeypatch, [
            "⚠️ Щодо руху ударних БпЛА: 🛸 Сумщина: 🛩 БпЛА в р—ні н.п. Путивль, "
            "Глухів, Кролевець, Буринь та Лебедин рухаються західним курсом; "
            "🛸 Чернігівщина: 🛩 БпЛА в р—ні н.п. Батурин, Сосниця, Ніжин, "
            "Козелець та Гончарівське рухаються західним курсом; "
            "🛸 Житомирщина: 🛩 БпЛА в р—ні н.п. Малин, Коростень та Нова Борова "
            "рухаються західним курсом."])
        got = tracker.poll()
        # The towns. The regions those towns are in also get a derived warning
        # each -- see derived_alerts() -- so this counts the marks the digest
        # itself produced rather than everything on the map.
        towns = [e for e in got["events"] if e["kind"] == "drone"]
        assert len(towns) >= 13, [e["summary"] for e in got["events"]]
        # Every one of them pointing west, which is what the post said and
        # what the single ring could not say at all.
        assert all(e["heading"] == 270.0 for e in towns), \
            sorted({e["heading"] for e in towns})
        # And on distinct spots -- one mark per town, not fifteen on a centre.
        spots = {(round(e["lat"], 3), round(e["lon"], 3)) for e in towns}
        assert len(spots) == len(towns)

    def test_a_digest_costs_no_more_requests_than_an_ordinary_post(
            self, monkeypatch):
        # Fifteen towns at Nominatim's one-a-second would be fifteen seconds
        # for one post. They are in the built-in table for exactly this.
        asked = []
        monkeypatch.setattr(tracker.gazetteer, "find",
                            lambda name, countries="": asked.append(name))
        tracker.reset()
        self.feed(monkeypatch, [
            "🛸 Сумщина: 🛩 БпЛА в р—ні н.п. Путивль, Глухів, Кролевець, Буринь "
            "та Лебедин рухаються західним курсом; 🛸 Рівненщина: 🛩 БпЛА в р—ні "
            "н.п. Корець та Здолбунів рухаються західним курсом."])
        got = tracker.poll()
        assert len([e for e in got["events"] if e["kind"] == "drone"]) == 7
        # Including the two derived region warnings, which are placed from the
        # built-in table or not placed at all -- a warning nobody declared is
        # not worth a second of Nominatim's rate limit.
        assert asked == [], asked

    def test_forgetting_a_post_takes_its_alert_with_it(self):
        # Both lists, or the panel keeps a row for a mark that is gone.
        tracker.reset()
        assert tracker.forget_source(None) == 0
        assert tracker.forget_source("nothing/0") == 0


class TestWhatEachChannelActuallyDid:
    """The panel said "20 posts, none readable" for all four channels at once.

    Which claims the app cannot read them. What was true was "nothing new" --
    twenty posts, none of them inside the twenty-minute window -- and that is
    not a fault at all. The tally has always carried the distinction; the
    message ignored it.
    """

    def read_one(self, monkeypatch, posts, ages=None):
        tracker.reset()
        ages = ages or {}
        now = dt.datetime.now(dt.timezone.utc)

        def fetch(channel):
            if channel not in posts:
                return []
            old = ages.get(channel, 0)
            when = (now - dt.timedelta(minutes=old)).isoformat()
            return [{"id": f"{channel}/1", "channel": channel, "when": when,
                     "text": posts[channel], "photos": [], "link": None}]

        monkeypatch.setattr(tracker, "_fetch_channel", fetch)
        monkeypatch.setattr(tracker.gazetteer, "find", fake_find)
        return {row["channel"]: row for row in tracker.poll()["sources"]}

    def test_a_post_inside_the_window_counts_as_fresh(self, monkeypatch):
        rows = self.read_one(monkeypatch,
                             {ONLY: "Шахед над Нікополем"}, {ONLY: 5})
        assert rows[ONLY]["posts"] == 1
        assert rows[ONLY]["fresh"] == 1

    def test_a_post_older_than_the_window_is_seen_but_not_fresh(self, monkeypatch):
        # The exact case that read as "none readable". One post, read from the
        # page perfectly well, simply too old to be news.
        #
        # Ninety minutes no longer qualifies -- the window is the longest keep
        # time now, so a post is only "not news" once nothing it could become
        # would still be drawn. Aged past that.
        stale = tracker.LOOKBACK_MINUTES + 60
        rows = self.read_one(monkeypatch,
                             {ONLY: "Шахед над Нікополем"}, {ONLY: stale})
        assert rows[ONLY]["posts"] == 1
        assert rows[ONLY]["fresh"] == 0
        assert rows[ONLY]["problem"] is None, \
            "nothing went wrong, so nothing should be reported as a problem"

    def test_the_tally_carries_the_four_numbers_the_panel_needs(self, monkeypatch):
        # posts, fresh, read, placed -- in the order the reading happens, so
        # the panel can say where it stopped.
        rows = self.read_one(monkeypatch, {ONLY: "Вибухи у Харкові"})
        for row in rows.values():
            assert set(row) >= {"posts", "fresh", "read", "placed", "problem"}

    def posts_at(self, monkeypatch, *ages):
        tracker.reset()
        now = dt.datetime.now(dt.timezone.utc)
        monkeypatch.setattr(tracker, "_fetch_channel", lambda channel: [
            {"id": f"{channel}/{i}", "channel": channel,
             "when": (now - dt.timedelta(minutes=old)).isoformat(),
             "text": "Шахед над Нікополем курсом на північ",
             "photos": [], "link": None}
            for i, old in enumerate(ages)])
        monkeypatch.setattr(tracker.gazetteer, "find", fake_find)
        return tracker.poll()

    def warnings_at(self, monkeypatch, *ages):
        """The same, but reporting warnings -- the longest-lived mark there is.

        A drone is held for twenty minutes, so a test written with drones
        cannot tell "not read" from "read and expired", and would pass with
        the read window set to anything at all. This used to use strikes,
        which were held for a day; they are not drawn at all now, so the
        longest-lived kind is a warning at ninety minutes.
        """
        tracker.reset()
        now = dt.datetime.now(dt.timezone.utc)
        monkeypatch.setattr(tracker, "_fetch_channel", lambda channel: [
            {"id": f"{channel}/{i}", "channel": channel,
             "when": (now - dt.timedelta(minutes=old)).isoformat(),
             "text": "Повітряна тривога у Кременчуці",
             "photos": [], "link": None}
            for i, old in enumerate(ages)])
        monkeypatch.setattr(tracker.gazetteer, "find", fake_find)
        return tracker.poll()

    def test_the_window_is_the_longest_a_mark_survives(self):
        assert tracker.LOOKBACK_MINUTES == max(tracker.KEEP.values())

    def test_everything_inside_it_is_taken(self, monkeypatch):
        got = self.posts_at(monkeypatch, 1, 5, 12, 19)
        # Four posts per channel, four channels.
        assert got["count"] == 4 * len(tracker.CHANNELS)

    def test_and_nothing_outside_it(self, monkeypatch):
        got = self.posts_at(monkeypatch, 25, 40, 180)
        assert got["count"] == 0

    def test_the_boundary_is_the_window(self, monkeypatch):
        # Tested with a warning rather than a drone, because a drone an hour
        # old is read and then immediately expired by its own keep time --
        # which would make this pass whatever the window was.
        inside = self.warnings_at(monkeypatch, tracker.LOOKBACK_MINUTES - 5)
        outside = self.warnings_at(monkeypatch, tracker.LOOKBACK_MINUTES + 5)
        assert inside["count"] == len(tracker.CHANNELS)
        assert outside["count"] == 0

    def test_a_warning_already_running_is_drawn_from_a_cold_start(self):
        """The failure this whole change was about.

        A warning is held for ninety minutes. With a twenty-minute read
        window a freshly opened app could only ever show one declared in the
        last twenty, so a warning that had been running for half an hour --
        the ordinary case, and the one somebody opens the page to see -- was
        unreachable on every start.
        """
        assert tracker.LOOKBACK_MINUTES >= tracker.KEEP["alert"]

    def test_an_old_post_is_read_by_rule_even_outside_the_model_batch(
            self, monkeypatch):
        """Everything gets read; only the model's share is capped.

        These were one cap. Four channels of twenty posts is eighty, the cap
        was forty, and the forty that lost were counted in the panel as read
        and then never looked at. They cost different things -- a rules read
        is a few regular expressions and a model read is seconds -- so they
        get different limits.
        """
        tracker.reset()
        many = 30
        monkeypatch.setattr(tracker, "_fetch_channel", lambda channel: [
            {"id": f"{channel}/{i}", "channel": channel,
             "when": (dt.datetime.now(dt.timezone.utc)
                      - dt.timedelta(minutes=i % 10)).isoformat(),
             "text": "Повітряна тривога у Кременчуці",
             "photos": [], "link": None}
            for i in range(many)])
        monkeypatch.setattr(tracker.gazetteer, "find", fake_find)
        got = tracker.poll()
        assert got["count"] == many * len(tracker.CHANNELS)
        # The channels. NEPTUN is a source too and has its own row, which is
        # not a channel and has no posts to have read.
        rows = [s for s in got["sources"] if s["channel"] != tracker.NEPTUN_SOURCE]
        assert all(s["read"] == many for s in rows), rows

    def test_an_old_post_is_remembered_so_it_is_not_read_twice(self, monkeypatch):
        # Otherwise every poll re-reads and re-discards the whole page.
        self.posts_at(monkeypatch, 40)
        assert len(tracker._seen) == len(tracker.CHANNELS)

    def test_a_post_with_an_unreadable_date_is_not_silently_a_mark(
            self, monkeypatch):
        # It cannot be placed in time, so it cannot be known to be current --
        # and a mark that might be from last week is worse than no mark.
        tracker.reset()
        monkeypatch.setattr(tracker, "_fetch_channel", lambda channel: [
            {"id": f"{channel}/1", "channel": channel, "when": "not a date",
             "text": "Шахед над Нікополем", "photos": [], "link": None}])
        monkeypatch.setattr(tracker.gazetteer, "find", fake_find)
        got = tracker.poll()
        assert got["count"] == 0


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
        mixed += [{"id": "c", "kind": "missile", "lat": 50.5, "lon": 30.6, "seen": 1}]
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
                            # Concentrate mode, sent with every answer so the
                            # switch does not need a round trip.
                            "masses", "mass_within_km", "mass_least"}
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
        # Two behaviours now, not three: "orbit" went with the recon kind.
        assert {e["motion"] for e in events} == {"track", "still"}
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
        # strike (twenty-five hours) or a warning (an hour), both by design --
        # so what must be empty at the end of a cycle is the flying things,
        # not the map. The test named only strikes until warnings were given
        # their own hour, at which point it started failing for a correct
        # reason, which is what it is for.
        tracker._demo_epoch = 0.0
        try:
            tracker.demo()
            tracker._demo_epoch -= tracker.DEMO_CYCLE - 1
            late = tracker.demo()["events"]
            still = [e for e in late if e["kind"] not in tracker.NOT_AIRBORNE]
            assert still == [], [e["kind"] for e in still]
            tracker._demo_epoch -= 10
            assert tracker.demo()["events"]
        finally:
            tracker._demo_epoch = 0.0

    def test_the_channels_the_app_offers_are_the_ones_it_reads(self):
        names = {c["name"] for c in tracker.CHANNELS}
        assert tracker.demo()["channels"] == [c["name"] for c in tracker.CHANNELS]
        # The ones that were asked for, and only those. Written out rather
        # than derived, because the point is that this list is a decision
        # somebody made and not whatever happens to be in the tuple.
        #
        # One, now. NEPTUN is the source for Ukraine -- it carries these
        # same channels' reports already read and already placed, with its
        # own alert feed keyed to real boundaries, so reading them here as
        # well was two paths to the same facts with only the worse one able
        # to put a mark in the wrong province.
        #
        # @lpr1_treugolnik stays because NEPTUN does not cover the Russian
        # side, which is the one thing this app would otherwise lose.
        assert names == {"lpr1_treugolnik"}

    def test_every_channel_says_which_countries_to_look_in(self):
        # Without it "Sumy" is as likely to match a street in another
        # hemisphere as the oblast capital.
        for channel in tracker.CHANNELS:
            assert channel["countries"] and channel["region"]
            for code in channel["countries"].split(","):
                assert len(code) == 2 and code.islower(), channel


class TestAWarningThatHasEnded:
    """"Відбій тривоги" is the opposite of "тривога" and contains it.

    That is the whole bug. The alert pattern matched the all-clear text, so
    the moment a region stood down, the map raised a fresh warning over it --
    and kept it there for the full hour, saying exactly the opposite of what
    the channel said. The lifted warnings in these channels outnumber the
    raised ones, because every alert eventually ends.
    """

    def test_the_reader_tells_them_apart(self):
        from backend import reports
        assert reports.find_kind("Повітряна тривога у Києві") == "alert"
        assert reports.find_kind("Відбій тривоги у Києві") == "all_clear"
        assert reports.find_kind("Отбой воздушной тревоги") == "all_clear"
        assert reports.find_kind("Відбій загрози") == "all_clear"

    def test_an_all_clear_takes_the_warning_off_the_map(self):
        tracker.reset()
        tracker._record(one(kind="alert", place="Київ",
                            summary="Air raid alert"),
                        {"id": "c/1", "channel": "c"}, "ua")
        assert len([e for e in tracker.current()["events"]
                    if e["kind"] == "alert"]) == 1

        tracker._record(one(kind=tracker.LIFTED, place="Київ",
                            summary="All clear"),
                        {"id": "c/2", "channel": "c"}, "ua")
        assert [e for e in tracker.current()["events"]
                if e["kind"] == "alert"] == []

    def test_an_all_clear_draws_nothing_of_its_own(self):
        # It is a report that something stopped. Putting a marker down for it
        # would be a second warning where there is now none.
        tracker.reset()
        tracker._record(one(kind=tracker.LIFTED, place="Київ",
                            summary="All clear"),
                        {"id": "c/1", "channel": "c"}, "ua")
        assert tracker.current()["events"] == []

    def test_it_is_still_worth_reading(self):
        tracker.reset()
        tracker._record(one(kind=tracker.LIFTED, place="Київ",
                            summary="All clear over Kyiv"),
                        {"id": "c/1", "channel": "c"}, "ua")
        stream = tracker.current()["alerts"]
        assert len(stream) == 1
        assert stream[0]["lifts"] is True

    def test_it_lifts_only_warnings_and_only_nearby_ones(self):
        """A stand-down in Kyiv does not clear Kharkiv, or unexplode anything.

        Both halves have bitten in other forms: a lift that took everything
        emptied the map on the first all-clear of the night, and one matched
        by name alone left warnings up wherever the two channels spelled the
        oblast differently.
        """
        tracker.reset()
        tracker._record(one(kind="alert", place="Київ", summary="Alert"),
                        {"id": "c/1", "channel": "c"}, "ua")
        tracker._record(one(kind="alert", place="Харків", summary="Alert"),
                        {"id": "c/2", "channel": "c"}, "ua")
        tracker._record(one(kind="missile", place="Київ", summary="Missile"),
                        {"id": "c/3", "channel": "c"}, "ua")
        tracker._record(one(kind="drone", place="Київ", summary="Drone"),
                        {"id": "c/4", "channel": "c"}, "ua")

        tracker._record(one(kind=tracker.LIFTED, place="Київ",
                            summary="All clear"),
                        {"id": "c/5", "channel": "c"}, "ua")

        left = tracker.current()["events"]
        kinds = sorted(e["kind"] for e in left)
        # The things in flight over Kyiv are untouched: an all-clear takes
        # down the WARNING, not the reports it was about.
        assert kinds == ["alert", "drone", "missile"]
        # The one left standing is Kharkiv's, four hundred kilometres away.
        alert = next(e for e in left if e["kind"] == "alert")
        assert tracker.separation(alert["lat"], alert["lon"],
                                  *places.CITIES["Харків"]) < 1

    def test_an_all_clear_nobody_can_place_lifts_nothing(self):
        # Better a warning that stays up for its hour than every warning in
        # the country cleared by a stand-down for a town no map knows.
        tracker.reset()
        tracker._record(one(kind="alert", place="Київ", summary="Alert"),
                        {"id": "c/1", "channel": "c"}, "ua")
        tracker._record(one(kind=tracker.LIFTED, place="Zzzzzborough",
                            summary="All clear"),
                        {"id": "c/2", "channel": "c"}, "ua")
        assert len(tracker.current()["events"]) == 1

    def test_the_lift_is_not_folded_into_an_ordinary_kind(self):
        # fold_kind() maps the reader's finer kinds onto the seven the map
        # draws. If LIFTED went through that, it would come out "unknown" and
        # be drawn as a grey mark -- a warning, at the moment one ended.
        assert tracker.fold_kind(tracker.LIFTED) == tracker.LIFTED
        assert tracker.LIFTED not in tracker.KINDS


class TestTheColoursOnTheMap:
    """Told apart by hue, because that is what a glance actually uses.

    A screen during a wave is fifty arrows, and nobody checks a key for each
    one. Yellow is a drone, red is a jet drone -- three times the speed, and
    the distinction that changes how long you have -- and purple is a missile.

    Pinned as values rather than "they differ", because which colour means
    which is the whole point: swapping red and yellow would pass any test that
    only asked for three distinct colours.
    """

    def test_the_three_that_matter(self):
        assert tracker.KINDS["drone"]["colour"] == "#ffd400"
        assert tracker.KINDS["jet_drone"]["colour"] == "#ff3b30"
        assert tracker.KINDS["missile"]["colour"] == "#a855f7"

    def test_nothing_else_borrows_one_of_them(self):
        # A strike was purple, which is the missile colour now. Shape tells
        # those two apart -- a star against an arrow -- but two things this
        # different should not share a hue as well.
        taken = [look["colour"] for look in tracker.KINDS.values()]
        assert len(taken) == len(set(taken)), taken

    def test_every_kind_has_one_and_it_is_a_colour_css_understands(self):
        import re
        for name, look in tracker.KINDS.items():
            assert re.fullmatch(r"#[0-9a-f]{6}", look["colour"]), name


class TestTheDemoShowsTheDigest:
    """The offline build has to reach the case, or the case goes unexamined.

    Three separate drawing bugs got as far as a screenshot because the demo
    could not produce the state they were in. The rest of the demo is a table
    of pre-read rows, which proves nothing about the code that turns a post
    into rows -- so one real digest goes through reports.read_all() exactly as
    a live post does.
    """

    def test_the_digest_is_read_rather_than_seeded(self):
        got = reports.read_all(tracker.DEMO_DIGEST)
        assert len(got) >= 12, [g["place"] for g in got]
        assert all(g["course"] == "W" for g in got)

    def test_its_marks_reach_the_demo_map(self):
        drawn = tracker.demo()["events"]
        named = {e["place"] for e in drawn}
        for town in ("Путивль", "Ніжин", "Нова Борова"):
            assert town in named, sorted(named)

    def test_they_are_drawn_on_their_own_spots_pointing_west(self):
        west = [e for e in tracker.demo()["events"] if e["heading"] == 270.0]
        assert len(west) >= 12
        spots = {(round(e["lat"], 3), round(e["lon"], 3)) for e in west}
        assert len(spots) == len(west), "marks stacked on one point"


class TestWarningsAreColouredByWhatTheyWarnAbout:
    """Yellow for a drone warning, red for a missile one.

    Both were amber, which said "a warning" and nothing else -- and the
    difference between the two is the difference between fifteen minutes and
    ninety seconds. The colours are the ones those things are drawn in when
    they are in the air, so the key is learnt once rather than twice.
    """

    def one(self, text):
        tracker.reset()
        plain = reports.read(text)
        plain["kind"] = tracker.fold_kind(plain["kind"])
        item = tracker._clean({**plain, "id": "c/1"})
        tracker._record(item, {"id": "c/1", "channel": "radarrussiia"}, "ru")
        return tracker.current()

    def test_a_drone_warning_carries_its_cause_to_the_map(self):
        got = self.one("Lipetsk Oblast Drone Alert")["events"]
        warning = [e for e in got if e["kind"] == "alert"]
        assert len(warning) == 1
        assert warning[0]["cause"] == "drone"
        # And a drone beside it -- see TestADroneWarningPutsADroneOnTheMap.
        assert [e["kind"] for e in got] == ["alert", "drone"]

    def test_a_missile_warning_carries_its_own(self):
        got = self.one("Voronezh Oblast Missile Alert")["events"]
        assert got[0]["kind"] == "alert" and got[0]["cause"] == "missile"

    def test_the_panel_row_carries_it_too(self):
        # The list is coloured the same way the mark is, or the two disagree
        # about what a reader is being told.
        assert self.one("Voronezh Oblast Missile Alert")["alerts"][0]["cause"] \
            == "missile"

    def test_a_warning_that_does_not_say_carries_nothing(self):
        got = self.one("Повітряна тривога у Києві")["events"]
        assert got[0]["cause"] is None

    def test_nothing_but_a_warning_carries_a_cause(self):
        # A drone in the air IS a drone, so a cause there would say the same
        # thing twice.
        for text in ("Шахед над Нікополем", "Балістика на Дніпропетровщині"):
            got = self.one(text)["events"]
            assert got[0].get("cause") is None, text

    def test_the_colours_a_cause_can_name_are_ones_the_map_has(self):
        # The page looks the cause up in the same kinds table it colours marks
        # from, so a cause naming a kind that is not there would fall back to
        # amber silently.
        for cause in ("drone", "missile"):
            assert cause in tracker.KINDS, cause


class TestTheDemoDoesNotMoveTheBorder:
    """The offline build must not teach the page a false country.

    NEPTUN's boundary file IS Ukraine's provinces, so everything in that
    index is tagged Ukrainian downstream. The demo seeded it from the whole
    built-in region table -- fifty Russian and Belarusian oblasts included --
    so the offline build told the page that Belgorod, Voronezh and Brest were
    Ukraine.

    Nothing looked broken. The live map was right, because NEPTUN's real file
    holds Ukraine and nothing else. Only the one build anybody can check
    without a network was wrong, and it was wrong about precisely the
    question the country filter exists to answer.
    """

    def setup_method(self):
        tracker.reset()
        neptun.forget()
        gaz.forget()

    teardown_method = setup_method

    def test_only_ukraines_regions_are_tagged_ukrainian(self):
        tracker.demo()
        ua = {o["name"].casefold() for o in tracker.outlines()
              if o["in"] == "ua"}
        strangers = {n.casefold() for n in places.ELSEWHERE_REGIONS}
        assert not (ua & strangers), sorted(ua & strangers)[:5]

    def test_and_ukraines_own_are_there(self):
        # The other half: a fix that seeded nothing would also pass above.
        tracker.demo()
        ua = {o["name"].casefold() for o in tracker.outlines()
              if o["in"] == "ua"}
        assert len(ua) >= len(places.UKRAINE_REGIONS) - 2

    def test_the_neighbours_still_get_their_boundaries(self):
        """From the file, the same as on the live map.

        The demo used to seed its own wobbly rings for these. It does not any
        more: real outlines ship with the app, and seeding would put a second
        and worse copy of every Russian province beside the real one.
        """
        tracker.demo()
        whose = {o["name"]: o["in"] for o in tracker.outlines()}
        assert whose.get("Белгородская область") == "ru"
        assert whose.get("Брестская область") == "by"
        # And not one of them is Ukraine, which is the whole point.
        assert "ua" not in {whose.get("Белгородская область"),
                            whose.get("Брестская область")}

    def test_the_table_is_split_rather_than_commented(self):
        # REGIONS is the two halves joined, so neither can drift from it.
        assert places.REGIONS == {**places.UKRAINE_REGIONS,
                                  **places.ELSEWHERE_REGIONS}
        assert not (set(places.UKRAINE_REGIONS) & set(places.ELSEWHERE_REGIONS))
        assert "Автономна Республіка Крим" in places.UKRAINE_REGIONS


class TestADroneWarningPutsADroneOnTheMap:
    """"Make it so drones are placed over reports in Russia."

    The Russian side's posts are overwhelmingly region-level -- "UAV danger in
    Rostov region" -- and those became a shaded oblast with a warning triangle
    and nothing in the air. Ukraine's map was full of drones because NEPTUN
    supplies real tracks; Russia's had none, from the same feed, for the same
    night.

    This reverses an earlier decision that region-centre marks are not drawn
    (see regionOnly in the page). That decision is still right where there is
    something better to draw: on the Ukrainian side a vague mark would sit
    beside sharp ones. On the Russian side the choice is between a vague mark
    and an empty map.
    """

    def one(self, text, channel="radarrussiia"):
        tracker.reset()
        plain = reports.read(text)
        plain["kind"] = tracker.fold_kind(plain["kind"])
        item = tracker._clean({**plain, "id": "c/1"})
        tracker._record(item, {"id": "c/1", "channel": channel}, "ru")
        return tracker.current()

    def drones(self, feed):
        return [e for e in feed["events"] if e["kind"] == "drone"]

    def test_a_drone_warning_puts_a_drone_on_the_map(self):
        got = self.drones(self.one("Rostov Oblast Drone Alert"))
        assert len(got) == 1
        assert got[0]["from_warning"] is True

    def test_it_sits_where_the_warning_sits(self):
        feed = self.one("Rostov Oblast Drone Alert")
        warning = [e for e in feed["events"] if e["kind"] == "alert"][0]
        drone = self.drones(feed)[0]
        assert (drone["lat"], drone["lon"]) == (warning["lat"], warning["lon"])

    def test_the_warning_is_still_there_as_well(self):
        # The drone is added beside it, not instead of it. The shaded region
        # is what says the warning covers the whole oblast.
        kinds = [e["kind"] for e in self.one("Rostov Oblast Drone Alert")["events"]]
        assert kinds == ["alert", "drone"]

    def test_the_drone_does_not_shade_the_region_a_second_time(self):
        """Two shadings of one report read as two reports.

        The warning beside it carries the outline; this is the point inside
        it.
        """
        drone = self.drones(self.one("Rostov Oblast Drone Alert"))[0]
        assert drone["shape"] is None
        assert drone["region_wide"] is False
        assert drone["region_scope"] is None

    def test_it_claims_no_course(self):
        """It came from a sentence about a region, not from a track.

        An arrow here would point somewhere nobody said, which is the
        invention this whole layer exists to avoid.
        """
        drone = self.drones(self.one("Rostov Oblast Drone Alert"))[0]
        assert drone["course"] is None
        assert drone.get("course_from") is None
        assert drone.get("heading") is None

    def test_it_is_not_marked_area_only(self):
        """Which would be truthful and would hide it.

        area_only means "there is no dot" and the page drops such marks, so
        setting it would make this whole feature draw nothing. What it is
        gets said by from_warning instead, and by the popup.
        """
        drone = self.drones(self.one("Rostov Oblast Drone Alert"))[0]
        assert drone["area_only"] is False

    def test_a_missile_warning_puts_nothing_in_the_air(self):
        """A warning about what may come is not a weapon in flight.

        Drawing a missile in the middle of an oblast would be a claim nobody
        made -- and unlike a drone incursion, a missile warning is routinely
        declared for ground nothing ever reaches.
        """
        assert self.drones(self.one("Voronezh Oblast Missile Alert")) == []

    def test_a_warning_that_does_not_say_what_it_is_about_puts_nothing(self):
        assert self.drones(self.one("Повітряна тривога у Києві")) == []

    def test_an_all_clear_puts_nothing(self):
        # A warning being lifted is the opposite of a drone arriving.
        feed = self.one("UAV alert cleared in Rostov region")
        assert self.drones(feed) == []

    def test_an_unplaceable_warning_puts_nothing(self):
        # There is nowhere to put it, and the alert stream already says so.
        feed = self.one("Drone Alert in Nowherewithatypo Oblast")
        assert self.drones(feed) == []

    def test_no_course_is_ever_borrowed_for_it(self):
        """The inference that must not compound.

        borrow_course lends a group's bearing to members of the same kind
        that lack one, and this mark IS a drone -- so a stream of real drones
        reported nearby would have lent it theirs, and the map would have
        drawn an arrow: a direction, at a position, neither of which anybody
        stated.
        """
        raised = {"id": "AO1D", "kind": "drone", "from_warning": True,
                  "heading": None, "course_from": None,
                  "lat": 47.5, "lon": 40.0}
        real = [{"id": "r1", "kind": "drone", "heading": 90.0,
                 "course_from": "stated", "lat": 47.5, "lon": 40.1},
                {"id": "r2", "kind": "drone", "heading": 90.0,
                 "course_from": "stated", "lat": 47.6, "lon": 40.0},
                {"id": "r3", "kind": "drone", "heading": None,
                 "course_from": None, "lat": 47.55, "lon": 40.05}]
        events = [raised, *real]
        tracker.borrow_course(events, [{"ids": [e["id"] for e in events]}])
        # The ordinary courseless drone in the group is lent one...
        assert real[2]["course_from"] == "group"
        assert real[2]["heading"] == 90.0
        # ...and the one raised from a warning is not.
        assert raised["heading"] is None
        assert raised.get("course_from") is None

    def test_it_says_what_it_is_rather_than_repeating_the_warning(self):
        """It is a drone mark, not a second copy of the warning.

        Copying the warning's own summary put "Air alert in Липецкая область"
        on a mark drawn as a drone, twice over in the popup, and said nothing
        about the one thing the mark actually is.
        """
        feed = self.one("Rostov Oblast Drone Alert")
        drone = self.drones(feed)[0]
        warning = [e for e in feed["events"] if e["kind"] == "alert"][0]
        assert drone["summary"] != warning["summary"]
        assert "warned of" in drone["summary"].lower()
        assert "Ростовская область" in drone["summary"]

    def test_it_does_not_count_as_a_report(self):
        """Nobody sent it.

        "Reports the gazetteer could place" is a number a reader checks to
        see whether the gazetteer is broken, and counting a mark this app
        invented would make it stop meaning that.
        """
        drone = self.drones(self.one("Rostov Oblast Drone Alert"))[0]
        assert drone["from_warning"] is True
        # And NOT under the old feature's flag. `derived` belongs to warnings
        # raised from drones -- asked for, then removed -- and this is the
        # mirror of that rather than the same thing. One flag for both would
        # let either quietly stand in for the other.
        assert drone.get("derived") is None

    def test_the_stream_gets_one_row_rather_than_two(self):
        # The panel lists what was REPORTED. One post arrived, so one row --
        # the drone is a second mark on the map, not a second report.
        feed = self.one("Rostov Oblast Drone Alert")
        assert len(feed["alerts"]) == 1
        assert feed["alerts"][0]["kind"] == "alert"

    def test_it_carries_the_report_with_it(self):
        # It is a mark somebody will click. Without the text and the link it
        # is an assertion with no way back to what it came from.
        feed = self.one("Rostov Oblast Drone Alert")
        drone = self.drones(feed)[0]
        assert drone["channel"] == "radarrussiia"
        assert drone["source"] == "c/1"
        assert drone["id"].endswith("D")
        # And it says which panel row it belongs to: its warning's, because
        # one post is one row.
        assert drone["row"] == feed["alerts"][0]["id"]

    def test_ukraine_does_not_get_these(self):
        """NEPTUN supplies real tracks there.

        Not by a country test here, but by where this sits: a channel reading
        inside Ukraine is shadowed by NEPTUN before it reaches this code, and
        NEPTUN's own alerts are recorded by a different function entirely.
        """
        tracker.reset()
        neptun.remember_shapes({"ua": {"type": "Polygon", "coordinates": [
            [[20.0, 44.0], [41.0, 44.0], [41.0, 53.0], [20.0, 53.0],
             [20.0, 44.0]]]}})
        plain = reports.read("Kyiv Oblast Drone Alert")
        plain["kind"] = tracker.fold_kind(plain["kind"])
        item = tracker._clean({**plain, "id": "c/2"})
        tracker._record(item, {"id": "c/2", "channel": "somechannel"}, "ua")
        assert self.drones(tracker.current()) == []


class TestTakingAMarkOffByHand:
    """Somebody watching this knows things the feed does not.

    A drone was shot down and the channel has not said so; a warning is stale;
    a report was plainly a duplicate. Without this the only answer is to wait
    out the keep time, which for a warning is an hour and a half.

    The care here is all about what a dismissal is NOT. It hides a mark; it
    does not edit what a channel said. The report stays in the stream, marked,
    so the record is not something a browser can rewrite and so somebody who
    dismissed the wrong thing can see it and put it back.
    """

    def some(self, monkeypatch):
        monkeypatch.setattr(tracker.gazetteer, "find",
                            lambda name, countries="": places.lookup(name))
        tracker.reset()
        for i, (kind, place) in enumerate([("drone", "Суми"),
                                           ("missile", "Харків"),
                                           ("alert", "Сумська область")]):
            item = tracker._clean({
                "kind": kind, "place": place, "id": f"p/{i}", "count": 1,
                "summary": f"{kind} over {place}", "region": None,
                "toward": None, "course": None, "cause": None})
            tracker._record(item, {"id": f"p/{i}", "channel": "x",
                                   "region": "Ukraine"}, "ua")
        return tracker.current()

    def test_a_drone_can_be_taken_off(self, monkeypatch):
        got = self.some(monkeypatch)
        drone = next(e for e in got["events"] if e["kind"] == "drone")
        assert tracker.dismiss(drone["id"]) == 1
        left = tracker.current()["events"]
        assert not [e for e in left if e["kind"] == "drone"]
        assert len(left) == 2

    def test_a_missile_can_be_taken_off(self, monkeypatch):
        got = self.some(monkeypatch)
        rocket = next(e for e in got["events"] if e["kind"] == "missile")
        tracker.dismiss(rocket["id"])
        assert not [e for e in tracker.current()["events"]
                    if e["kind"] == "missile"]

    def test_a_warning_can_be_cancelled(self, monkeypatch):
        got = self.some(monkeypatch)
        warning = next(e for e in got["events"] if e["kind"] == "alert")
        tracker.dismiss(warning["id"])
        assert not [e for e in tracker.current()["events"]
                    if e["kind"] == "alert"]

    def test_the_report_stays_in_the_stream_marked(self, monkeypatch):
        got = self.some(monkeypatch)
        drone = next(e for e in got["events"] if e["kind"] == "drone")
        tracker.dismiss(drone["id"])
        rows = tracker.current()["alerts"]
        dropped = [a for a in rows if a.get("dismissed")]
        assert dropped, "the report vanished with its mark"
        assert len(rows) == 3, "a dismissal must not delete the record"

    def test_it_stays_dismissed_when_the_post_is_read_again(self, monkeypatch):
        """The thing that makes it look broken if it is missed.

        A mark is rebuilt on every poll with a fresh identifier, so keying the
        dismissal on the mark alone means it returns a minute later as a
        different id. It is keyed on the POST too, which is stable.
        """
        got = self.some(monkeypatch)
        drone = next(e for e in got["events"] if e["kind"] == "drone")
        tracker.dismiss(drone["id"])

        item = tracker._clean({
            "kind": "drone", "place": "Суми", "id": "p/0", "count": 1,
            "summary": "drone over Суми", "region": None, "toward": None,
            "course": None, "cause": None})
        tracker._record(item, {"id": "p/0", "channel": "x",
                               "region": "Ukraine"}, "ua")
        assert not [e for e in tracker.current()["events"]
                    if e["kind"] == "drone"]

    def test_it_can_be_put_back(self, monkeypatch):
        got = self.some(monkeypatch)
        drone = next(e for e in got["events"] if e["kind"] == "drone")
        tracker.dismiss(drone["id"])
        assert tracker.restore(drone["id"]) is True

        item = tracker._clean({
            "kind": "drone", "place": "Суми", "id": "p/0", "count": 1,
            "summary": "drone over Суми", "region": None, "toward": None,
            "course": None, "cause": None})
        tracker._record(item, {"id": "p/0", "channel": "x",
                               "region": "Ukraine"}, "ua")
        assert [e for e in tracker.current()["events"] if e["kind"] == "drone"]

    def test_the_count_is_said_out_loud(self, monkeypatch):
        # "The map is missing things" and "I hid those" look identical from
        # across a room, and only one of them is a bug.
        got = self.some(monkeypatch)
        assert got["dismissed"] == 0
        tracker.dismiss(got["events"][0]["id"])
        assert tracker.current()["dismissed"] >= 1

    def test_nonsense_is_refused_quietly(self, monkeypatch):
        self.some(monkeypatch)
        before = len(tracker.current()["events"])
        assert tracker.dismiss("") == 0
        assert tracker.dismiss(None) == 0
        assert tracker.dismiss("no-such-mark") == 0
        assert len(tracker.current()["events"]) == before

    def test_it_cannot_be_grown_without_bound(self, monkeypatch):
        self.some(monkeypatch)
        for i in range(tracker.MAX_DISMISSED + 50):
            tracker.dismiss(f"junk-{i}")
        assert tracker.dismissed_now() <= tracker.MAX_DISMISSED + 2


class TestWarningsAreOutlinesOfRegions:
    def test_a_warning_carries_the_regions_extent(self, monkeypatch):
        # So one whose real boundary has not arrived can be drawn as that
        # rectangle rather than as a circle. A disc centred on an oblast is
        # not the shape of any province and reads as a blast radius.
        monkeypatch.setattr(tracker.gazetteer, "find",
                            lambda name, countries="": places.lookup(name))
        tracker.reset()
        item = tracker._clean({
            "kind": "alert", "place": "Сумська область", "id": "a/1",
            "count": 1, "summary": "alert", "region": None, "toward": None,
            "course": None, "cause": "drone"})
        tracker._record(item, {"id": "a/1", "channel": "x",
                               "region": "Ukraine"}, "ua")
        # Selected rather than indexed. current() can carry a derived warning
        # alongside the declared one, so events[0] is not reliably the mark
        # this test is about -- and when it is not, the failure is a TypeError
        # about None rather than anything that names the real problem.
        got = next(e for e in tracker.current()["events"]
                   if e["place"] == "Сумська область" and not e.get("derived"))
        south, north, west, east = got["bbox"]
        assert north > south and east > west
        assert south < places.REGIONS["Сумська область"][0] < north

    def test_a_town_carries_none(self, monkeypatch):
        # A drone over a town is a point in it. A rectangle round one would
        # claim the report had named the municipal border.
        monkeypatch.setattr(tracker.gazetteer, "find",
                            lambda name, countries="": places.lookup(name))
        tracker.reset()
        item = tracker._clean({
            "kind": "drone", "place": "Харків", "id": "e/1", "count": 1,
            "summary": "drone", "region": None, "toward": None,
            "course": None, "cause": None})
        tracker._record(item, {"id": "e/1", "channel": "x",
                               "region": "Ukraine"}, "ua")
        got = next(e for e in tracker.current()["events"]
                   if e["place"] == "Харків")
        assert got["bbox"] is None

    def test_a_warnings_outline_is_fetched_ahead_of_everything_else(self):
        """It is the only mark drawn AS its region rather than at a point.

        Everything else in that queue is already drawn correctly and merely
        gains detail, so a warning waiting behind a dozen of them is the one
        case where the queue order is visible to a reader.

        The drainer is held off rather than raced. The first version read the
        queue a moment after filling it and the background worker had usually
        emptied it -- so the assertion was guarded with an "if it is still
        there", which made it pass by not looking most of the time.
        """
        # tracker.gazetteer rather than a bare import: this file already has
        # a test helper of that name.
        book = tracker.gazetteer
        book.forget()

        # A worker that is alive but does nothing, so improve_later() sees one
        # running and does not start the real one.
        holding = threading.Event()
        idle = threading.Thread(target=holding.wait, daemon=True)
        idle.start()
        was, book._improver = book._improver, idle
        try:
            book.improve_later("Сумська область", "ua")
            book.improve_later("Полтавська область", "ua")
            book.improve_later("Харківська область", "ua", urgent=True)
            with book._lock:
                queued = [name for name, _ in book._wanted]
        finally:
            book._improver = was
            holding.set()

        assert queued == ["Харківська область", "Сумська область",
                          "Полтавська область"], queued


class TestTheDemoHonoursADismissal:
    """Or the button is inert offline, which looks exactly like it not working.

    The demo rebuilds its events from a seed on every call, so it had no
    memory of a dismissal at all: pressing × removed nothing and the mark was
    back before the next paint. Fifth time the offline build has been unable
    to reach a state the live path has.
    """

    def setup_method(self):
        tracker.reset()

    def teardown_method(self):
        tracker.reset()

    def test_a_demo_mark_can_be_taken_off_and_stays_off(self):
        before = tracker.demo()["events"]
        assert tracker.dismiss(before[0]["id"]) >= 0
        after = tracker.demo()["events"]
        assert len(after) == len(before) - 1
        # Twice, because the demo rebuilds from the seed each call and a
        # dismissal that only survives one call is not a dismissal.
        assert len(tracker.demo()["events"]) == len(before) - 1

    def test_its_report_is_marked_rather_than_deleted(self):
        got = tracker.demo()
        tracker.dismiss(got["events"][0]["id"])
        again = tracker.demo()
        assert len(again["alerts"]) == len(got["alerts"])
        assert any(a.get("dismissed") for a in again["alerts"])

    def test_it_can_be_put_back(self):
        before = tracker.demo()["events"]
        tracker.dismiss(before[0]["id"])
        tracker.restore(before[0]["id"])
        assert len(tracker.demo()["events"]) == len(before)

    def test_the_demo_says_how_many_are_hidden(self):
        assert tracker.demo()["dismissed"] == 0
        tracker.dismiss(tracker.demo()["events"][0]["id"])
        assert tracker.demo()["dismissed"] >= 1


class TestTheDrawingMoves:
    """The map carries a mark along; the popup and the trail say so.

    Checked against the source text because the arithmetic lives in the
    browser and there is no Leaflet here to run it in. These are the
    invariants that make the movement honest rather than the maths itself --
    the maths is checked in the browser, against the demo.
    """

    def source(self):
        return (pathlib.Path(__file__).resolve().parent.parent
                / "frontend" / "js" / "tracker.js").read_text(encoding="utf-8")

    def test_the_marks_are_carried_along_on_a_tick(self):
        text = self.source()
        assert "function slide()" in text
        assert "setInterval(slide, DRIFT_MS)" in text

    def test_the_tick_is_stopped_with_the_layer(self):
        # A timer left running against a cleared map is a leak that moves
        # markers that are no longer on it.
        text = self.source()
        assert "clearInterval(drifter)" in text

    def test_nothing_moves_without_a_speed_from_the_source(self):
        # The removed version of this flew marks at a speed looked up from a
        # table of what the type typically does. Every number here has to
        # have come from the feed.
        text = self.source()
        block = text[text.index("function driftKm"):]
        block = block[:block.index("\n}")]
        assert "event.speed_kmh" in block
        assert "speed > 0" in block
        assert "event.heading" in block

    def test_the_two_clocks_are_added_rather_than_compared(self):
        # The browser's clock is not the server's. Elapsed time from each end
        # is added; neither has to agree about what o'clock it is.
        text = self.source()
        block = text[text.index("function driftMinutes"):]
        block = block[:block.index("\n}")]
        assert "Date.now() - feedAt" in block
        assert "event.drift_minutes" in block

    def test_nothing_draws_a_track_behind_a_mark(self):
        """The lines are gone, and the machinery with them.

        There were two: the legs joining the positions a source had given,
        and a dashed tail for the part reckoned since the last of them. Both
        were honest, and on a map carrying forty marks both were the noisiest
        thing in the frame -- twenty legs per track, crossing each other,
        under arrows two dozen pixels wide.

        Removed rather than switched off, on both ends: a drawing nothing can
        reach is one nobody would notice breaking, and a trail kept in the
        backend to feed a drawing nobody makes is the kind of thing that rots.
        """
        text = self.source()
        for gone in ("function trailFor", "function liveLegFor",
                     "function refreshLive", "ao-trail", "held.trail",
                     "held.live"):
            assert gone not in text, gone
        css = (pathlib.Path(__file__).resolve().parent.parent
               / "frontend" / "css" / "app.css").read_text(encoding="utf-8")
        assert ".ao-trail" not in css

    def test_the_marks_still_move(self):
        # The trail is what went. A mark with a source's own course and speed
        # is still carried along, and the popup still says how far and from
        # when -- which is where that claim belonged all along.
        text = self.source()
        assert "function slide()" in text
        assert "Carried " in text

    def test_no_number_is_drawn_on_a_mark(self):
        """Said twice from the other side of the screen, and gone.

        It was three arrows for a report of three drones; then a plate
        reading "3" on one arrow; now nothing. Both drawings claimed the
        count was firm, and it is the least dependable thing in a report.
        NEPTUN draw no number either. How many the report claimed is in the
        popup, with the sentence it came from.
        """
        text = self.source()
        assert "function countPlate" not in text
        assert "function untold" not in text
        assert "ao-count-plate" not in text

    def test_the_count_is_still_said_in_the_popup(self):
        # Removed from the map, not from the app. What the report claimed is
        # worth reading; it is just not worth drawing as a fact.
        text = self.source()
        assert "reported together" in text

    def test_nothing_draws_a_strike_any_more(self):
        # Removed with the kind. A burst glyph nothing can reach is a drawing
        # that cannot be trusted, because nothing would notice it breaking.
        text = self.source()
        assert "explosion" not in text.replace(
            "a report of explosions names a city", "")
        assert "data-shape=\"burst\"" not in text
        assert "ao-flare" not in text

    def test_only_a_warning_colours_a_region(self):
        """The warning nobody declared.

        A track located only to a region used to shade that region too --
        faintly, in the kind's own colour, meaning "this is how precisely
        anybody knows where it is". On the screen that is a yellow province
        with a dashed border, which is what a drone warning looks like, and no
        viewer was going to read the difference between 0.05 fill and 0.42 as
        the difference between "somewhere in here" and "take cover".
        """
        block = self.source()
        block = block[block.index("const hasArea"):]
        block = block[:block.index(";\n")]
        assert "event.kind === 'alert'" in block
        assert "Boolean(event.shape)" in block
        # Not "or something else as well".
        assert "motionOf" not in block
        assert "region_scope" not in block

    def test_the_drawings_that_served_the_other_areas_are_gone(self):
        # A circle round an extent, a bounding box, a faint tint for a located
        # region: all unreachable now, and an unreachable branch is one nobody
        # would notice breaking.
        block = self.source()
        block = block[block.index("function areaFor"):]
        block = block[:block.index("\n}\n")]
        assert "L.circle" not in block
        assert "region_scope === 'located' ? 0.05" not in block
        assert "L.geoJSON" in block

    def test_a_report_that_names_only_a_province_is_not_drawn(self):
        """The "random drones".

        Such a report's lat/lon is the arithmetic centre of a province --
        NEPTUN's own words are "there is no dot" -- so the mark went in a
        field outside Lutsk because that is where the middle of Volyn oblast
        falls. It used to have the province faintly shaded behind it to say
        what it meant; that shading was a warning nobody declared and is
        gone, which left the dot on its own in the middle of nowhere.
        """
        text = self.source()
        block = text[text.index("const regionOnly ="):]
        block = block[:block.index(";\n")]
        assert "event.kind !== 'alert'" in block
        assert "event.area_only" in block
        assert "event.region_scope === 'located'" in block
        assert "if (regionOnly(event)) continue;" in text

    def test_a_warning_is_the_exception_and_still_shows(self):
        # A warning IS about the whole region, so the region is its true
        # extent rather than a stand-in for a position nobody gave.
        block = self.source()
        block = block[block.index("const regionOnly ="):]
        assert "event.kind !== 'alert'" in block[:block.index(";\n")]

    def test_the_panel_says_how_many_are_being_held_back(self):
        # "Twelve on the map" beside a list of twenty reports reads as the map
        # being broken. This is the difference between that and a filter.
        text = self.source()
        assert "region only" in text
        assert "if (regionOnly(event)) vague += 1;" in text

    def test_the_picture_is_offered_to_the_share_sheet(self):
        """A download is the wrong verb on a phone.

        An <a download> saves into Files, which is not where anybody looks for
        a picture -- "the image doesn\u2019t save to photos" is what that
        feels like, and it is not a fault in the picture. The share sheet is
        the route that offers "Save Image", and on iOS it is the only one.
        """
        ui = (pathlib.Path(__file__).resolve().parent.parent
              / "frontend" / "js" / "ui.js").read_text(encoding="utf-8")
        assert "export async function handOver" in ui
        assert "navigator.canShare" in ui and "navigator.share" in ui
        # And it still works where sharing a file is not possible.
        block = ui[ui.index("export async function handOver"):]
        block = block[:block.index("\n}")]
        assert "download(blob, filename)" in block
        assert "AbortError" in block, "cancelling is not a failure"
        text = self.source()
        assert "await handOver(blob," in text

    def test_the_picture_carries_the_warnings_too(self):
        """It used to leave them out, and that was asked for -- back when a
        warning was a triangle on a centroid and the picture was about the
        arrows.

        A warning is a shaded province now. Leaving it out meant a country
        whose only activity is warnings had nothing to draw at all: picking
        Russia on a night of two alerts and no drones produced "nothing in
        that area" and no picture.
        """
        block = self.source()
        block = block[block.index("async function saveShot"):]
        block = block[:block.index("\n}")]
        assert "if (event.kind === 'alert') continue;" not in block
        assert "warnings.push({ shape: event.shape" in block
        assert "warnings," in block, "they are collected and not handed over"

    def test_a_warning_with_no_boundary_is_still_in_the_picture(self):
        # It falls back to the same triangle the map draws. A warning left
        # out because its outline had not arrived is still a warning nobody
        # was told about.
        block = self.source()
        block = block[block.index("async function saveShot"):]
        block = block[:block.index("\n}")]
        # From the alert branch to the glyph that draws whatever is left.
        # Exactly ONE way out of it: the one for a warning that HAS a
        # boundary. A second would drop the ones that do not, which is the
        # warning most worth not dropping -- its region is unknown, so the
        # triangle is all anybody gets.
        span = block[block.index("if (event.kind === 'alert') {"):
                     block.index("const parts = glyphParts")]
        assert span.count("continue;") == 1, span.count("continue;")
        assert "glyphParts(event, colour" in block

    def test_the_picture_is_made_when_there_are_only_warnings(self):
        # The Russia case, which is the whole of why this changed.
        block = self.source()
        block = block[block.index("async function saveShot"):]
        # The refusal is now only for an area the app worked out itself; an
        # area somebody named is taken whatever is in it.
        assert "if (!marks.length && !warnings.length && !exactly) {" in block

    def test_the_picture_carries_the_watermark_and_the_credit(self):
        """NEPTUN's credit is a condition of use, not decoration.

        A picture carrying their tracks is the data, travelling somewhere
        this app's panel does not follow it -- so the credit has to be on the
        picture itself.
        """
        shot = (pathlib.Path(__file__).resolve().parent.parent
                / "frontend" / "js" / "trackershot.js").read_text(encoding="utf-8")
        assert "WATERMARK" in shot
        assert "from './capture.js'" in shot, "the watermark is defined once"
        assert "Data supplied by NEPTUN" in shot
        block = self.source()
        block = block[block.index("async function saveShot"):]
        said = block[:block.index("\n}")]
        assert "attribution" in said
        assert "Data supplied by NEPTUN" in said

    def test_the_credit_cannot_go_missing_from_a_picture(self):
        """It used to be droppable, and the one picture that dropped it was
        the worst one to drop it from.

        stamp() returned early on an empty credit, and the credit came from
        the feed -- so a picture taken before the first feed arrived, or while
        it was down, went out carrying NEPTUN's tracks with their name
        nowhere on it. That is the case the licence is about.
        """
        shot = (pathlib.Path(__file__).resolve().parent.parent
                / "frontend" / "js" / "trackershot.js").read_text(encoding="utf-8")
        block = shot[shot.index("function drawFurniture("):]
        block = block[:block.index("\n}")]
        assert "if (!credit) return;" not in block
        assert "credit || CREDIT" in block
        # And the fallback is the real credit rather than an empty string.
        line = shot[shot.index("const CREDIT ="):]
        assert "NEPTUN" in line[:line.index(";")]

    def test_the_picture_uses_the_same_artwork_as_the_map(self):
        # Two copies of a dozen paths would have drifted apart the first time
        # one of them was changed.
        text = self.source()
        assert "export function glyphParts" in text
        # The picture asks the same function the map does for its artwork,
        # rather than keeping a second copy of a dozen paths.
        assert "glyphParts(event, colour" in text
        assert "colourOf(event)" in text
        shot = (pathlib.Path(__file__).resolve().parent.parent
                / "frontend" / "js" / "trackershot.js").read_text(encoding="utf-8")
        assert "part.body" in shot

    def test_marks_are_kept_off_each_other(self):
        text = self.source()
        assert "function declump" in text
        assert "function footprint" in text

    def test_what_may_move_is_what_was_never_at_a_point(self):
        """The rule the whole thing rests on.

        A drone reported over Myrhorod is AT Myrhorod; move it and the map
        says something nobody said. A warning for Poltava oblast is not at any
        point -- it is drawn at the region's centroid because a label has to
        go somewhere -- and neither is a track located only to a region, which
        is NEPTUN's "there is no dot". So the second kind yields and the first
        does not.
        """
        text = self.source()
        block = text[text.index("const floats ="):]
        block = block[:block.index(";\n")]
        assert "event.kind === 'alert'" in block
        assert "event.area_only" in block
        assert "event.region_scope === 'located'" in block

    def test_a_reported_position_is_placed_first_and_never_moved(self):
        block = self.source()
        block = block[block.index("function declump"):]
        block = block[:block.index("\n}")]
        assert "if (floats(one.event)) continue;" in block
        assert "put.set(one, [0, 0]);" in block

    def test_the_footprint_is_measured_rather_than_assumed(self):
        """Why the first attempt did not fix the complaint.

        It reserved a 24-pixel square around each glyph, which is what an
        arrow looks like. A warning is a triangle with a plate reading "Air
        alert" under it -- three times as wide, and half again as far down the
        screen. The drone was never on the triangle; it was on the label.
        """
        block = self.source()
        block = block[block.index("function footprint"):]
        block = block[:block.index("\n}")]
        assert "getBoundingClientRect" in block
        assert "ao-tag" in block

    def test_the_position_itself_is_left_alone(self):
        # A margin on the icon, not a different latlng: the popup, the shaded
        # region and the trail all still belong to the place the report named.
        block = self.source()
        block = block[block.index("function shift"):]
        block = block[:block.index("\n}")]
        assert "marginLeft" in block and "marginTop" in block
        assert "setLatLng" not in block

    def test_it_is_redone_whenever_the_picture_changes(self):
        # Three things change what overlaps what: a new feed, a zoom (a pixel
        # is a different number of kilometres), and a mark drifting along.
        text = self.source()
        assert "map.on('zoomend', declump);" in text
        assert "  declump();\n}" in text
        assert "if (moved) declump();" in text

    def test_a_hidden_group_is_not_drawn(self):
        # Asked for as "just show drones and missiles". A group per button
        # rather than one combined mode, so "only drones" is sayable too.
        text = self.source()
        assert "const GROUPS = [" in text
        assert "if (!isShown(event)) continue;" in text

    def test_every_kind_the_backend_has_falls_into_a_group(self):
        # A kind added to the backend table and not here would silently stop
        # being drawable the moment anybody touched a filter button.
        text = self.source()
        block = text[text.index("const GROUPS = ["):]
        block = block[:block.index("\n];")]
        for kind in tracker.KINDS:
            assert f"'{kind}'" in block, f"{kind} is in no group"

    def test_a_mass_of_hidden_kinds_goes_with_them(self):
        # Concentrate mode replaces forty marks with one shape. If the shape
        # stayed when its kinds were switched off, the filter would appear to
        # do nothing in the one mode where that is hardest to spot.
        text = self.source()
        block = text[text.index("function drawMasses"):]
        assert "showing.has(groupOf(kind))" in block[:block.index("\n}")]

    def test_switching_a_group_off_does_not_hide_it_from_the_counts(self):
        # "No missiles on the map" and "missiles switched off" are different
        # things, and a filter that confuses them is a filter that lies.
        text = self.source()
        block = text[text.index("function toggleGroup"):]
        assert "reconcile(" in block[:block.index("\n}")]
        # Counted over the whole feed rather than over what is drawn.
        assert "for (const event of feed?.events ?? []) {" in text


class TestRussiaIsDrawnLikeUkraine:
    """One layer, one look, whichever side of the border a region is on.

    Ukraine's outlines come from NEPTUN's own boundary file. Russia's now
    come from one that ships with this app, because nobody publishes them
    and fetching fifty of them by name never worked. Before either, a
    Russian region was answered by the built-in table -- a centre and an
    extent and no shape -- and the visible result was a layer split in half:
    Ukrainian warnings shaded their province, Russian ones were a triangle
    on a dot.
    """

    SHAPE = {"type": "Polygon",
             "coordinates": [[[38.0, 50.5], [41.0, 50.5], [41.0, 52.5],
                              [38.0, 52.5], [38.0, 50.5]]]}

    def setup_method(self):
        gaz.forget()
        tracker.reset()
        neptun.forget()
        self.original = gaz._ask
        # Nothing may reach the network; the outline worker is a real thread.
        gaz._ask = lambda name, countries="": None

    def teardown_method(self):
        gaz._ask = self.original
        gaz.forget()
        tracker.reset()
        neptun.forget()

    def warning(self, place, countries):
        return tracker.place_event(
            {"kind": "alert", "place": place, "toward": None, "course": None,
             "count": 1, "summary": f"Air alert — {place}", "region": None},
            countries)

    def test_a_russian_warning_is_shaped_at_once(self):
        """No fetch, no wait, no difference from Ukraine.

        This is what the shipped boundaries bought. It used to be flat until
        the background worker had been round -- and on the deployment that
        prompted the file, the worker never came back at all, so every
        Russian warning was a triangle on a province centroid for the life
        of the process.
        """
        got = self.warning("Воронежская область", "ru,ua")
        assert got["placed"] is True
        assert got["shape"] is not None
        assert got["region_wide"] is True
        assert got["region_scope"] == "covers"

    def test_every_region_this_app_can_name_has_an_outline(self):
        """Which is what makes the two halves of the map look alike.

        The shipped file was built FROM the built-in table, so the two match
        by construction -- and this is what notices if a region is added to
        one and not the other, which would put a single flat triangle back
        on a map of shaded provinces.
        """
        missing = [n for n in places.ELSEWHERE_REGIONS
                   if not neighbours.shape_for(n)]
        assert missing == []

    def test_a_learned_outline_still_reaches_the_mark(self):
        """The path anything east of the file still takes.

        The gazetteer is the only source for a region the table does not
        hold, and its answer has to reach the event rather than being
        remembered and never read -- which is the bug this class was
        originally written for.
        """
        gaz.remember("Омская область", "ru,ua",
                     {"name": "Омская область", "lat": 55.0, "lon": 73.4,
                      "category": "boundary", "kind": "administrative",
                      "bbox": (54.0, 56.0, 72.0, 75.0),
                      "shape": self.SHAPE})
        got = tracker.place_event(
            {"kind": "alert", "place": "Омская область", "toward": None,
             "course": None, "count": 1, "summary": "Air alert",
             "region": None},
            "ru,ua", lookup=lambda name, countries="": gaz.find(name, countries))
        assert got["shape"] == self.SHAPE
        # And the same two fields a Ukrainian warning gets, because "the same
        # style" is these rather than a resemblance anybody has to eyeball.
        assert got["region_wide"] is True
        assert got["region_scope"] == "covers"

    def test_a_ukrainian_warning_is_the_same_two_fields(self):
        neptun.remember_shapes({"сумська область": self.SHAPE})
        got = self.warning("Сумська область", "ua")
        assert got["shape"] == self.SHAPE
        assert got["region_wide"] is True
        assert got["region_scope"] == "covers"

    def test_the_outline_is_asked_for_rather_than_waited_for(self):
        # It must not block the poll: the mark goes down now and gains its
        # province on a later one.
        self.warning("Липецкая область", "ru,ua")
        assert gaz.shapes_wanted() >= 0

    def test_a_town_does_not_get_a_boundary(self):
        # A strike in a town is a point in it. Drawing the municipal border
        # round one would claim the damage followed the council's line.
        gaz.remember("Белгород", "ru,ua",
                           {"name": "Белгород", "lat": 50.6, "lon": 36.6,
                            "category": "place", "bbox": None,
                            "shape": self.SHAPE})
        got = tracker.place_event(
            {"kind": "explosion", "place": "Белгород", "toward": None,
             "course": None, "count": 1, "summary": "Strike", "region": None},
            "ru,ua")
        assert got["shape"] is None


class TestRememberingWhatHasBeenRead:
    """The set that stops a re-read feed drawing everything twice.

    NEPTUN's message feed is read on every poll now, so this is load-bearing
    in a way it was not when it was read once: without it every post inside
    the catch-up window would produce a fresh mark every thirty seconds.
    """

    def setup_method(self):
        tracker.reset()

    def teardown_method(self):
        tracker.reset()

    def test_a_post_read_once_is_remembered(self):
        tracker.mark_seen("np-msg/1")
        assert "np-msg/1" in tracker._seen

    def test_it_does_not_grow_without_end(self):
        for i in range(tracker.MOST_SEEN + 500):
            tracker.mark_seen(f"np-msg/{i}")
        assert len(tracker._seen) == tracker.MOST_SEEN

    def test_the_oldest_go_and_the_newest_stay(self):
        # Not a clear: clearing would re-offer every post still inside the
        # catch-up window and draw a second mark for each, which is the one
        # failure this exists to prevent.
        for i in range(tracker.MOST_SEEN + 500):
            tracker.mark_seen(f"np-msg/{i}")
        assert "np-msg/0" not in tracker._seen
        assert f"np-msg/{tracker.MOST_SEEN + 499}" in tracker._seen


class TestAWarningAndATrackOnOneCentroid:
    """The collision the map kept producing, made reachable offline.

    A track located only to a region is drawn at that region's centroid. A
    warning for the same region is drawn at the same centroid. Neither is
    really there, and they land on top of each other every time -- which is
    the picture this arrived as: a drone arrow sitting on a warning triangle.

    The offline build could not reach it, so the demo carries both.
    """

    def marks(self):
        return [e for e in tracker.demo()["events"]
                if str(e.get("place")) == "Kharkiv oblast"]

    def test_the_demo_has_both(self):
        kinds = sorted(e["kind"] for e in self.marks())
        assert kinds == ["alert", "drone"]

    def test_and_they_are_on_the_same_point(self):
        spots = {(round(e["lat"], 4), round(e["lon"], 4)) for e in self.marks()}
        assert len(spots) == 1, "the collision this exists to show is not there"

    def test_neither_of_them_claims_to_be_at_it(self):
        # Which is what makes moving one of them honest. The warning covers
        # the region; the drone is located to it. The centroid is a place to
        # put a label, not a position either of them reported.
        for event in self.marks():
            assert event["region_scope"] in ("covers", "located"), event["kind"]
            assert event["shape"], "no region to be about"


class TestTheBordersForAPictureOfItsOwn:
    """Outlines the page can draw a map from, with no tiles involved.

    The map's own tiles come from another origin and Leaflet loads them
    without asking to read the pixels back, so a canvas that has drawn one
    cannot be exported at all. The picture draws its own map instead, and
    this is what it draws it from.
    """

    def setup_method(self):
        tracker.reset()
        neptun.forget()
        gaz.forget()

    def teardown_method(self):
        tracker.reset()
        neptun.forget()
        gaz.forget()

    SHAPE = {"type": "Polygon",
             "coordinates": [[[33.0, 50.0], [34.0, 50.0], [34.0, 51.0],
                              [33.0, 50.0]]]}

    def provinces(self):
        """The fetched boundaries, without the ones that ship with the app.

        outlines() always carries the shipped national borders and the
        neighbours' provinces now -- that is what makes the picture's red
        lines and Russia's shaded oblasts appear with no network -- so a
        test about what was FETCHED has to say so.
        """
        shipped = set(neighbours.provinces())
        return [o for o in tracker.outlines()
                if o["level"] != "country" and o["name"] not in shipped]

    def test_it_carries_the_boundaries_neptun_publish(self):
        neptun.remember_shapes({"сумська область": self.SHAPE})
        assert [o["shape"] for o in self.provinces()] == [self.SHAPE]

    def test_one_region_filed_under_several_names_comes_out_once(self):
        # index_shapes files the SAME object under a region's key and under
        # each of its names, so a pass keyed on names would send a province's
        # border two or three times.
        neptun.remember_shapes({
            "sumska": self.SHAPE, "сумська область": self.SHAPE,
            "sumy oblast": self.SHAPE})
        assert len(self.provinces()) == 1

    def test_two_different_regions_both_come_out(self):
        other = {"type": "Polygon",
                 "coordinates": [[[30.0, 50.0], [31.0, 50.0], [31.0, 51.0],
                                  [30.0, 50.0]]]}
        neptun.remember_shapes({"a": self.SHAPE, "b": other})
        assert len(self.provinces()) == 2

    def test_a_region_nobody_ships_comes_from_the_gazetteer(self):
        # Russia's western provinces ship with the app now. Anything east of
        # them is still learned one at a time, and without this it would have
        # no outline in the picture at all.
        gaz.remember("Омская область", "ru",
                     {"name": "Омская область", "lat": 55.0, "lon": 73.4,
                      "category": "boundary", "bbox": (54.0, 56.0, 72.0, 75.0),
                      "shape": self.SHAPE})
        assert [o["shape"] for o in self.provinces()] == [self.SHAPE]

    def test_the_shipped_borders_are_there_whatever_was_fetched(self):
        # The other half of the same fact, said once so the filter above is
        # not quietly hiding a regression.
        assert {o["in"] for o in tracker.outlines() if o["level"] == "country"} \
            == set(neighbours.frontiers())

    def test_it_is_bounded(self):
        many = {f"r{i}": {"type": "Polygon",
                          "coordinates": [[[i / 100, 50.0], [1 + i / 100, 50.0],
                                           [1 + i / 100, 51.0], [i / 100, 50.0]]]}
                for i in range(tracker.MOST_OUTLINES + 50)}
        neptun.remember_shapes(many)
        assert len(tracker.outlines()) == tracker.MOST_OUTLINES

    def test_nothing_known_is_just_what_ships(self):
        # It used to be an empty list. The borders and the neighbours'
        # provinces ship with the app now, so "nothing fetched" is those.
        assert self.provinces() == []
        assert len(tracker.outlines()) == (len(neighbours.frontiers())
                                           + len(neighbours.provinces()))


class TestTheNeighboursAreOnTheMapToo:
    """"Make it a view of Russia with warnings overlayed like Ukraine."

    Ukraine's warnings shade their oblast because NEPTUN publish the
    boundaries. Russia's did not, so the same warning came out as a triangle
    on a province's arithmetic centre -- and the difference between the two
    halves of the map was never about the warnings, it was about whether an
    outline existed.

    They were fetched by name, fifty at a time, which is the bulk querying
    Nominatim's policy forbids and which never arrived. They ship with the
    app now: Natural Earth, public domain, the fifty regions the built-in
    table can name.
    """

    def setup_method(self):
        tracker.reset()
        neptun.forget()
        gaz.forget()
        neighbours.forget()

    teardown_method = setup_method

    def levels(self):
        return [(o["in"], o["level"]) for o in tracker.outlines()]

    # ── what ships ──────────────────────────────────────────────

    def test_the_provinces_are_there_with_no_network_at_all(self):
        # The gazetteer is empty and stubbed, and Russia has its regions.
        got = self.levels()
        assert ("ru", "region") in got
        assert ("by", "region") in got

    def test_every_region_the_table_can_name_has_one(self):
        """Built FROM that table, so the two match by construction.

        This is what notices if a region is added to one and not the other,
        which would put a single flat triangle back on a map of shaded
        provinces.
        """
        missing = [n for n in places.ELSEWHERE_REGIONS
                   if not neighbours.shape_for(n)]
        assert missing == []

    def test_they_are_where_those_regions_are(self):
        """Read off the file rather than trusted.

        A boundary file with the right names and the wrong geometry shades
        the wrong province, which is the one failure this layer exists to
        avoid -- and it would look entirely plausible.
        """
        capitals = {
            "Белгородская область": (50.5977, 36.5858),
            "Курская область": (51.7373, 36.1874),
            "Ростовская область": (47.2357, 39.7015),
            "Республика Татарстан": (55.7963, 49.1088),
            "Гомельская область": (52.4345, 30.9754),
        }
        for name, (lat, lon) in capitals.items():
            holds = [n for n, row in neighbours.provinces().items()
                     if tracker._shape_holds(row["shape"], lat, lon)]
            assert holds == [name], f"{name}: {holds}"

    def test_each_is_filed_under_its_own_country(self):
        whose = {n: row["in"] for n, row in neighbours.provinces().items()}
        assert whose["Курская область"] == "ru"
        assert whose["Брестская область"] == "by"
        assert set(whose.values()) == {"ru", "by"}

    def test_a_missing_file_loses_the_shading_and_nothing_else(self):
        was = neighbours.PROVINCES_FILE
        try:
            neighbours.PROVINCES_FILE = was.with_name("not-a-file.json")
            neighbours.forget()
            assert neighbours.provinces() == {}
            assert neighbours.shape_for("Курская область") is None
            # The countries still draw; a province file that cannot be read
            # is not a reason to lose the frontiers as well.
            assert ("pl", "country") in self.levels()
        finally:
            neighbours.PROVINCES_FILE = was
            neighbours.forget()

    # ── what it does to a warning ───────────────────────────────

    def warning(self, place):
        return tracker.place_event(
            {"kind": "alert", "place": place, "toward": None, "course": None,
             "count": 1, "summary": f"Air alert — {place}", "region": None},
            "ru,ua")

    def test_a_russian_warning_shades_its_province_at_once(self):
        got = self.warning("Белгородская область")
        assert got["shape"] is not None
        assert got["region_wide"] is True
        assert got["region_scope"] == "covers"

    def test_the_english_name_gets_there_too(self):
        # The Russian-side channel posts in English; the alias table resolves
        # it, and the outline has to follow the name the table settled on.
        got = self.warning("Belgorod region")
        assert got["shape"] is not None
        assert got["region_wide"] is True

    def test_a_warning_at_a_town_shades_the_province_it_is_in(self):
        """And without asking anybody.

        The shipped boundaries are exact and free, so the first warning over
        a Russian town is shaded rather than the fourth.
        """
        out = tracker.place_event(
            {"kind": "alert", "place": "Белгород", "toward": None,
             "course": None, "count": 1, "summary": "Air alert", "region": None},
            "ru,ua")
        assert out["region_wide"] is True
        assert out["region_over"] == "Белгородская область"
        assert out["shape"] is not None

    def test_the_demo_shades_them_with_the_real_outlines_too(self):
        """The offline build must not disagree with the live one.

        The demo's warnings are written in English -- "Lipetsk oblast" --
        and the shipped boundaries are keyed in Russian. Handed on as
        written, the demo drew its own wobbly ring for every Russian region
        while the live map drew the real outline: the one build anybody can
        check being wrong about the thing it exists to demonstrate.
        """
        got = tracker.demo()
        shaded = {e["place_match"]: e["shape"] for e in got["events"]
                  if e["kind"] == "alert" and e.get("shape")}
        # Named specifically, and in Russian. The demo writes this warning as
        # "Lipetsk oblast"; if the table's answer is not carried through, the
        # mark comes out under the English spelling with a ring for a shape,
        # and a test that only looked at whatever WAS Russian would pass by
        # checking a different region entirely.
        assert "Липецкая область" in shaded, sorted(shaded)
        assert shaded["Липецкая область"] == neighbours.shape_for(
            "Липецкая область")
        for name, shape in shaded.items():
            real = neighbours.shape_for(name)
            if real:
                assert shape == real, name

    def test_a_drone_over_a_town_is_still_at_the_town(self):
        # The distinction this layer has spent a long time getting right.
        out = tracker.place_event(
            {"kind": "drone", "place": "Белгород", "toward": None,
             "course": None, "count": 1, "summary": "Drone", "region": None},
            "ru,ua")
        assert out["region_wide"] is False
        assert out["shape"] is None

    # ── how they reach the picture ──────────────────────────────

    def test_they_are_drawn_as_provinces_rather_than_frontiers(self):
        """A red line means a country ends. These are the lines inside one.

        Asked of the provinces by name rather than of the country tags:
        Belarus has both a national border and three oblasts here, and the
        border is quite properly a frontier.
        """
        level = {o["name"]: o["level"] for o in tracker.outlines()}
        for name in neighbours.provinces():
            assert level.get(name) == "region", f"{name}: {level.get(name)}"

    def test_a_province_is_not_drawn_twice(self):
        """Once from the file and once from what the gazetteer learned.

        It learns these names too -- from a warning reported over one, before
        the file was consulted -- and the de-duplication below it is by
        identity, so the two copies would both come through.
        """
        gaz.remember("Курская область", "ru", {
            "name": "Курская область", "lat": 51.7, "lon": 36.0,
            "category": "boundary", "bbox": (50.5, 52.5, 34.0, 39.0),
            "shape": {"type": "Polygon", "coordinates": [
                [[34.0, 50.5], [39.0, 50.5], [39.0, 52.5], [34.0, 50.5]]]}})
        # Folded, because the gazetteer hands its names back lower-cased --
        # counting the file's spelling alone found one copy whatever
        # happened, which is a test that proves nothing.
        names = [o["name"].casefold() for o in tracker.outlines()]
        assert names.count("курская область") == 1

    def test_the_borders_come_before_the_provinces(self):
        """Under the cap, the last thing to go is the line that says which
        country the ground is."""
        levels = [o["level"] for o in tracker.outlines()]
        borders = len(neighbours.frontiers())
        assert levels[:borders] == ["country"] * borders
        assert "country" not in levels[borders:]

    def test_ukraines_own_provinces_are_provinces(self):
        neptun.remember_shapes({"сумська область": {
            "type": "Polygon", "coordinates": [
                [[33.0, 50.0], [34.0, 50.0], [34.0, 51.0], [33.0, 50.0]]]}})
        assert ("ua", "region") in self.levels()
        assert ("ua", "country") not in self.levels()

    def test_there_is_room_for_all_of_it(self):
        """The cap cuts the tail, and the tail must not be a whole country."""
        ukraine = 170
        assert (len(neighbours.provinces()) + len(neighbours.frontiers())
                + ukraine) < tracker.MOST_OUTLINES

    # ── what is no longer asked for ─────────────────────────────

    def test_nothing_is_fetched_by_name_any_more(self):
        """The bulk querying is gone rather than reduced.

        A list of names asked for one at a time is what Nominatim's policy
        forbids and what never arrived; the file replaced it, so the
        machinery that walked the list has no reason to exist.
        """
        assert not hasattr(tracker, "want_neighbours")
        assert not hasattr(tracker, "neighbour_outlines")
        assert not hasattr(neighbours, "RUSSIA")
        assert not hasattr(neighbours, "REGIONS")
        assert not hasattr(neighbours, "EVERYTHING")

    def test_a_region_nobody_ships_is_still_learned_one_at_a_time(self):
        """Ordinary use of the gazetteer, and the only use left.

        Somewhere east of the file, mentioned in a report for the first
        time: one lookup, when a warning actually arrives.
        """
        gaz.remember("Омская область", "ru", {
            "name": "Омская область", "lat": 55.0, "lon": 73.4,
            "category": "boundary", "bbox": (54.0, 56.0, 72.0, 75.0),
            "shape": {"type": "Polygon", "coordinates": [
                [[72.0, 54.0], [75.0, 54.0], [75.0, 56.0], [72.0, 54.0]]]}})
        assert ("elsewhere", "region") in self.levels()


class TestADotOnlyWhereSomebodyGaveAPosition:
    """The dots that made no sense were province centroids.

    A report naming a region and nothing finer carries the arithmetic middle
    of that region as its position, and it used to be drawn there whenever the
    region's boundary had not been fetched yet -- because "is this a region"
    was answered by "do we have its outline", which is a different question.
    So the mark sat on an oblast's centre, usually right on the province's own
    label, and looked like a report about a field.
    """

    def region(self, shape):
        return {"lat": 49.1, "lon": 28.5, "name": "Vinnytsia oblast",
                "kind": "administrative", "category": "boundary",
                "bbox": [48.0, 50.2, 27.4, 29.6], "shape": shape}

    def test_a_region_says_so_whether_or_not_its_outline_arrived(self):
        for shape in (None, {"type": "Polygon",
                             "coordinates": [[[27, 48], [29, 48], [29, 50],
                                              [27, 48]]]}):
            got = tracker.place_event(
                one(kind="drone", place="Vinnytsia oblast"), "ua",
                lookup=lambda name, countries="", s=shape: self.region(s))
            assert got["region_scope"] == "located", shape

    def test_the_demo_has_a_region_with_no_outline_to_prove_it(self):
        """Otherwise the offline build cannot reach the case at all.

        Every other region in the demo is handed a boundary, so the happy
        path was the only path and "a mark on a province centroid" was
        something only the live map could show. Three drawing bugs have now
        reached a screenshot for exactly that reason.
        """
        marks = [e for e in tracker.demo()["events"]
                 if str(e.get("place")) == tracker.DEMO_WITHOUT_AN_OUTLINE_EN]
        assert marks, "the demo names no report in the unshaped region"
        for mark in marks:
            assert mark["shape"] is None
            assert mark["region_scope"] == "located"

    def test_everything_else_in_the_demo_still_has_one(self):
        # One region without a boundary is the case being reached; two would
        # be the boundary path quietly not working.
        bare = {e["place"] for e in tracker.demo()["events"]
                if e.get("region_scope") and not e.get("shape")}
        assert bare == {tracker.DEMO_WITHOUT_AN_OUTLINE_EN}

    def test_a_real_position_with_no_course_is_still_a_mark(self):
        """A dot is not the problem; a dot on a centroid was.

        NEPTUN give a real point for anything that is not areaOnly, so a
        MiG-31K over Bryansk oblast is where they say it is even though the
        finest name they gave is the province. It has no course, so it is
        drawn as a ring -- which says "here, direction unknown" and is true.
        """
        drawn = [e for e in tracker.demo()["events"]
                 if e["kind"] != "alert" and e.get("heading") is None
                 and not e.get("area_only") and e.get("region_scope") != "located"]
        assert drawn, "the demo draws no courseless mark at a real position"


class TestACourseIsLentWithinAKindOnly:
    """The orange arrows pointing the way the drones were going.

    A mass is whatever is within sixty kilometres of whatever else, so a
    guided bomb near a Shahed stream was in that stream's group and was given
    its bearing. A KAB is released from an aircraft near the line and glides
    tens of kilometres; a Shahed crosses an oblast at a fifth of the speed on
    its own errand. They are not one group going one way, and the sentence
    this rests on -- "things reported together are usually one group" -- is
    only true of things of the same kind.
    """

    def near(self, kind, heading, i):
        return {"id": f"{kind}{i}", "kind": kind, "lat": 47.8 + i * 0.05,
                "lon": 35.1 + i * 0.05, "seen": i, "heading": heading,
                "course_from": "stated" if heading is not None else None}

    def test_a_drone_lends_to_a_drone(self):
        marks = [self.near("drone", 270.0, 0), self.near("drone", 275.0, 1),
                 self.near("drone", None, 2)]
        tracker.borrow_course(marks, tracker.massed(marks, least=2))
        assert marks[2]["heading"] == pytest.approx(272.5, abs=0.6)
        assert marks[2]["course_from"] == "group"

    def test_a_drone_does_not_lend_to_a_bomb_or_a_missile(self):
        marks = [self.near("drone", 270.0, 0), self.near("drone", 275.0, 1),
                 self.near("bomb", None, 2), self.near("missile", None, 3)]
        tracker.borrow_course(marks, tracker.massed(marks, least=2))
        assert marks[2]["heading"] is None
        assert marks[3]["heading"] is None

    def test_a_missile_lends_to_a_missile_in_the_same_group(self):
        # The rule is same-kind, not drones-only.
        marks = [self.near("missile", 90.0, 0), self.near("missile", 95.0, 1),
                 self.near("missile", None, 2), self.near("drone", None, 3)]
        tracker.borrow_course(marks, tracker.massed(marks, least=2))
        assert marks[2]["heading"] is not None
        assert marks[3]["heading"] is None

    def test_a_borrowed_course_is_never_lent_on(self):
        """Across polls, which is the only way it can happen.

        Marks persist between polls with the course they were lent, and
        borrow_course runs again over them. Without excluding the borrowed
        ones from the pool, one stated course spreads outwards a mass at a
        time -- mark A lends to B, then B lends to C two hundred kilometres
        away -- and the popup's "averaged from the N that reported a course"
        becomes a fiction. Only what somebody actually stated is ever lent.
        """
        first = [self.near("drone", 270.0, 0), self.near("drone", None, 1)]
        tracker.borrow_course(first, tracker.massed(first, least=2))
        assert first[1]["course_from"] == "group"

        # A second poll: the stated mark has gone, the borrowed one remains,
        # and a new mark with no course turns up beside it.
        later = [dict(first[1]), self.near("drone", None, 2)]
        tracker.borrow_course(later, tracker.massed(later, least=2))
        assert later[1]["heading"] is None, "a borrowed course was lent on"

    def test_the_count_is_of_the_ones_that_actually_stated_it(self):
        marks = [self.near("drone", 270.0, 0), self.near("drone", 274.0, 1),
                 self.near("drone", None, 2)]
        tracker.borrow_course(marks, tracker.massed(marks, least=2))
        assert marks[2]["course_from_count"] == 2

    def test_a_group_flying_apart_lends_nothing(self):
        marks = [self.near("drone", 0.0, 0), self.near("drone", 180.0, 1),
                 self.near("drone", None, 2)]
        tracker.borrow_course(marks, tracker.massed(marks, least=2))
        assert marks[2]["heading"] is None


class TestAStandDownReachesTheRegionItNames:
    """Ninety kilometres is half a Ukrainian oblast. It is not half of Russia.

    The lift radius was a single number written for the provinces this app
    started with. The Russian side's subjects are nothing like that size --
    Rostov oblast is four hundred kilometres across, Bashkortostan is the
    size of Britain -- so a stand-down posted for one of those reached a
    fraction of the province it was calling off, and the warning stayed up
    over the rest of it until it timed out an hour later.
    """

    def setup_method(self):
        tracker.reset()

    def warn(self, lat, lon, ident):
        tracker._events.append({
            "id": ident, "kind": "alert", "lat": lat, "lon": lon,
            "seen": time.time(), "place": ident, "count": 1,
        })

    def test_a_big_region_lifts_its_whole_self(self):
        # A warning at the far edge of a three-hundred-kilometre region, and
        # the stand-down posted for the region as a whole.
        self.warn(50.0, 36.0, "near")
        self.warn(52.5, 36.0, "far")      # ~278 km from the stand-down
        took = tracker.lift_alerts(50.0, 36.0, 300.0)
        assert took == 2
        assert tracker._events == []

    def test_but_the_floor_still_applies_to_a_small_one(self):
        self.warn(50.0, 36.0, "here")
        self.warn(52.5, 36.0, "far away")
        # A point report carries no region extent, so the floor decides and
        # the far one keeps its warning.
        took = tracker.lift_alerts(50.0, 36.0)
        assert took == 1
        assert [e["id"] for e in tracker._events] == ["far away"]

    def test_the_record_widens_it_to_what_was_named(self):
        """The wiring, not the arithmetic.

        lift_alerts can take a radius; what matters is that the stand-down
        path passes the named region's extent rather than leaving the floor
        to cover a province four times its size.
        """
        text = (pathlib.Path(__file__).resolve().parent.parent
                / "backend" / "tracker.py").read_text(encoding="utf-8")
        block = text[text.index("def _record("):]
        block = block[:block.index("\n    _counter += 1\n    ident =")]
        assert "max(LIFT_WITHIN_KM" in block
        assert 'placed.get("area_km")' in block

    def test_a_lift_touches_nothing_but_warnings(self):
        # A drone reported five minutes ago is still a drone, and a warning
        # being called off says nothing about it.
        tracker._events.append({
            "id": "drone", "kind": "drone", "lat": 50.0, "lon": 36.0,
            "seen": time.time(), "place": "somewhere", "count": 1,
        })
        self.warn(50.0, 36.0, "warning")
        assert tracker.lift_alerts(50.0, 36.0, 500.0) == 1
        assert [e["id"] for e in tracker._events] == ["drone"]


class TestARegionIsAskedForByItsOwnName:
    """Why Russia had no provinces on it.

    The channel covering the Russian side posts in English -- "Belgorod
    region" -- and the alias table exists precisely because OpenStreetMap
    does not hold it under that name. The table resolved the English spelling
    correctly and then the code handed that same English spelling back to the
    gazetteer, which asked OpenStreetMap a question it had no answer to. The
    boundary never arrived, so a warning over Belgorod was a triangle on a
    point while Ukraine's came out of NEPTUN's file properly shaped.
    """

    SHAPE = {"type": "Polygon",
             "coordinates": [[[36, 50], [37, 50], [37, 51], [36, 51], [36, 50]]]}

    def wired(self, monkeypatch, text, shapes=None):
        asked = []

        def queued(name, countries, urgent=False):
            asked.append(name)
            return True

        def outline(name, countries=""):
            asked.append(name)
            return (shapes or {}).get(name)

        # Nothing shipped: this class is about the name a boundary is FETCHED
        # under, and the shipped file now answers Belgorod before any fetch
        # happens. The question it asks is still live for every region east
        # of what that file covers.
        monkeypatch.setattr(tracker.neighbours, "provinces", dict)
        monkeypatch.setattr(tracker.neighbours, "shape_for", lambda name: None)
        monkeypatch.setattr(tracker.gazetteer, "improve_later", queued)
        monkeypatch.setattr(tracker.gazetteer, "outline", outline)
        item = reports.read(text)
        assert item, text
        return asked, tracker.place_event(item, "ru,ua")

    def test_the_russian_name_is_what_is_asked_for(self, monkeypatch):
        asked, _ = self.wired(monkeypatch, "Air raid alert in Belgorod region")
        assert asked, "no outline was asked for at all"
        assert set(asked) == {"Белгородская область"}, asked

    def test_for_every_region_that_channel_names(self, monkeypatch):
        for said, want in (
            ("UAV threat in Voronezh region", "Воронежская область"),
            ("Air alert in Kursk region", "Курская область"),
            ("Missile danger in Rostov region", "Ростовская область"),
            ("Air alert in Krasnodar Krai", "Краснодарский край"),
            ("UAV danger in the Republic of Tatarstan", "Республика Татарстан"),
        ):
            asked, _ = self.wired(monkeypatch, said)
            assert set(asked) == {want}, (said, asked)

    def test_and_the_boundary_reaches_the_event(self, monkeypatch):
        _, out = self.wired(monkeypatch, "Air raid alert in Belgorod region",
                            {"Белгородская область": self.SHAPE})
        assert out["shape"] == self.SHAPE
        assert out["region_scope"] == "covers"

    def test_a_ukrainian_report_still_asks_in_ukrainian(self, monkeypatch):
        asked, _ = self.wired(monkeypatch, "Повітряна тривога у Харківській області")
        assert set(asked) == {"Харківська область"}, asked

    def test_a_warning_jumps_the_queue(self, monkeypatch):
        """Everything else in that queue is already drawn correctly.

        A warning is the only thing drawn AS its region, so it is the only
        one whose look depends on the boundary arriving.
        """
        urgency = []

        monkeypatch.setattr(tracker.gazetteer, "improve_later",
                            lambda name, countries, urgent=False:
                            urgency.append(urgent) or True)
        monkeypatch.setattr(tracker.gazetteer, "outline",
                            lambda name, countries="": None)
        tracker.place_event(reports.read("Air raid alert in Belgorod region"),
                            "ru,ua")
        assert urgency == [True]
        urgency.clear()
        tracker.place_event(reports.read("Drone over Belgorod region"), "ru,ua")
        assert urgency == [False]


class TestAWarningIsItsRegion:
    """A shaded province instead of a triangle on a point.

    Asked for in as many words -- "instead of an icon, just the region
    highlighted either yellow or red depending on the event" -- and it is the
    better drawing either way: the triangle sat on a centroid nobody had
    reported, claiming a point for something that covers a province, while
    the province it covers went unshaded.
    """

    def source(self):
        return (pathlib.Path(__file__).resolve().parent.parent
                / "frontend" / "js" / "tracker.js").read_text(encoding="utf-8")

    def test_a_warning_with_a_boundary_gets_no_marker(self):
        text = self.source()
        assert "const area = hasArea(event) ? areaFor(event) : null;" in text
        assert "const marker = area ? null : L.marker(where, {" in text

    def test_but_one_without_a_boundary_still_draws_something(self):
        """The one failure this layer must not have.

        A warning whose region is unknown has to be visible as SOMETHING. The
        alternative to a triangle there is nothing at all, which is the map
        saying the airspace is quiet.
        """
        text = self.source()
        block = text[text.index("const marker = area ? null : L.marker"):]
        block = block[:block.index("drawn.set(id, made);")]
        assert "marker.addTo(layer);" in block

    def test_the_boundary_arriving_late_replaces_the_icon(self):
        """It usually does arrive late -- it is fetched at somebody else's
        rate limit, behind a mark that is already drawn."""
        text = self.source()
        block = text[text.index("held.marker?.setLatLng(where);"):]
        block = block[:block.index("continue;")]
        assert "if (!held.area && hasArea(event))" in block
        assert "layer.removeLayer(held.marker);" in block
        assert "held.marker = null;" in block

    def test_the_shaded_region_can_be_asked_about(self):
        # It carries the popup now; there is no triangle beside it to do it.
        text = self.source()
        # In areaFor, which is the layer being drawn -- not in some other
        # layer that happens to use the same words.
        block = text[text.index("function areaFor(event)"):]
        block = block[:block.index("\n}")]
        assert "interactive: true," in block, "the region is deaf to the mouse"
        # And the popup bound on the freshly drawn one, not only on the one
        # that replaces an icon later.
        block = text[text.index("const marker = area ? null : L.marker"):]
        block = block[:block.index("drawn.set(id, made);")]
        assert "area.bindPopup(() => popup(event));" in block
        # The pane has to hear the mouse too, or the popup never opens.
        assert "getPane('trackerArea').style.pointerEvents = 'auto'" in text

    def test_a_warning_is_yellow_or_red_and_nothing_else(self):
        text = self.source()
        block = text[text.index("function colourOf"):]
        block = block[:block.index("\n}")]
        # No third colour for a warning whose cause nobody stated.
        assert "WARNING_DEFAULT" in block
        colours = text[text.index("const WARNING_COLOURS"):]
        colours = colours[:colours.index("\nconst WARNING_DEFAULT")]
        assert "'#ffd400'" in colours and "'#ff3b30'" in colours
        default = text[text.index("const WARNING_DEFAULT"):]
        default = default[:default.index(";")]
        assert "#ffd400" in default, "an unstated cause is not the lesser colour"

    def test_red_is_the_missile(self):
        # Which way round matters: red is the fast thing.
        text = self.source()
        line = text[text.index("const WARNING_COLOURS"):]
        line = line[:line.index("\n")]
        assert "missile: '#ff3b30'" in line
        assert "drone: '#ffd400'" in line

    def test_nothing_still_reaches_through_a_marker_that_may_be_gone(self):
        """Half the file assumed every record had one."""
        text = self.source()
        # spotOf is the one place allowed to reach for it, because it is the
        # place that checks first and answers for the ones that have none.
        assert "function spotOf(held)" in text
        start = text.index("function spotOf(held)")
        rest = text[:start] + text[text.index("\n}", start):]
        for reach in ("held.marker.getLatLng()", "held.marker.setOpacity",
                      "one.marker.getElement", "held.marker.openPopup()",
                      "held.marker.setLatLng"):
            assert reach not in rest, reach
        assert "if (held.marker) return held.marker.getLatLng();" in text


class TestPickingWhatThePictureCovers:
    """The Image button used to photograph whatever was on screen.

    Which made taking a picture of somewhere else a matter of panning there,
    taking it, and panning back -- losing the view you were watching, which
    on a busy night is the thing you were actually doing.
    """

    def source(self):
        return (pathlib.Path(__file__).resolve().parent.parent
                / "frontend" / "js" / "tracker.js").read_text(encoding="utf-8")

    def test_the_button_asks_before_it_takes_anything(self):
        text = self.source()
        block = text[text.index("id: 'trackerShot'"):]
        block = block[:block.index("}, 'Image')")]
        assert "onclick: askWhere," in block
        assert "onclick: saveShot" not in block

    def test_the_area_is_an_argument_rather_than_the_screen(self):
        # The ground comes from the caller and falls back to the screen,
        # rather than being the screen and nothing else. Matched loosely on
        # the signature because the options it takes keep growing -- this is
        # about the FIRST argument, and pinning the whole line meant every
        # new option broke a test about something else.
        text = self.source()
        assert re.search(r"async function saveShot\(where, \{", text)
        assert "const view = where ?? map.getBounds();" in text

    def test_what_it_offers(self):
        """The four that get asked for: what I am looking at, one country,
        all of it, or a box I draw."""
        block = self.source()
        block = block[block.index("function askWhere()"):]
        block = block[:block.index("\nfunction closeWhere")]
        assert "'This view'" in block
        assert "Object.entries(AIRSPACE)" in block, "the countries are not offered"
        assert "'Everything'" in block
        assert "'Draw an area…'" in block

    def test_the_countries_come_from_the_one_table(self):
        # A second list of countries here is one to forget to update.
        text = self.source()
        assert "const AIRSPACE = {" in text
        block = text[text.index("function askWhere()"):]
        block = block[:block.index("\nfunction closeWhere")]
        assert "'Ukraine'" not in block and "'Russia'" not in block

    def test_everything_means_every_mark_not_every_country(self):
        block = self.source()
        block = block[block.index("function everything()"):]
        block = block[:block.index("\n}")]
        assert "for (const held of drawn.values())" in block
        # Including the shaded provinces, which reach past their centroids.
        assert "held.area?.getBounds?.()" in block
        # And it never hands back an empty box.
        assert "map.getBounds()" in block

    def test_drawing_a_box_takes_the_map_off_the_mouse(self):
        """Otherwise the first pull pans the map instead of drawing, which
        reads as the mode not having started."""
        block = self.source()
        block = block[block.index("function drawArea()"):]
        assert "map.dragging.disable();" in block
        assert "map.dragging.enable();" in block
        assert "map.boxZoom.disable();" in block

    def test_and_can_be_left_without_taking_anything(self):
        block = self.source()
        block = block[block.index("function drawArea()"):]
        assert "'Escape'" in block
        assert "map.off('mousedown', begin);" in block, "the handlers leak"
        assert "pane.style.cursor = '';" in block

    def test_a_click_is_not_an_area(self):
        # Taking the whole view on a stray click would be a picture nobody
        # asked for, saved to their photos.
        block = self.source()
        block = block[block.index("function done(e)"):]
        block = block[:block.index("\n  }")]
        assert "span < 20" in block
        assert "return;" in block


class TestAskingForOneCountry:
    """"When I ask for Russia, it should be this region with all the alerts."

    The country was a fixed box and Russia's runs to the Urals, so choosing
    it put two warnings near Kursk in the corner of three thousand kilometres
    of empty ground. It fits what is actually there now.
    """

    def source(self, name="frontend/js/tracker.js"):
        return (pathlib.Path(__file__).resolve().parent.parent
                / name).read_text(encoding="utf-8")

    def test_the_backend_says_which_border_is_whose(self):
        """The page cannot work it out and this is the only place that can.

        Asserted through the answer rather than through the source text,
        which is what it used to read: the spelling of one call is not the
        behaviour, and every rename broke this test while every real change
        went through it.
        """
        tracker.reset()
        neptun.forget()
        gaz.forget()
        try:
            # NEPTUN's file IS Ukraine's provinces, so everything in it is
            # Ukrainian by definition rather than by guess.
            neptun.remember_shapes({"сумська область": {
                "type": "Polygon", "coordinates": [
                    [[33.0, 50.0], [34.0, 50.0], [34.0, 51.0],
                     [33.0, 50.0]]]}})
            # And what the gazetteer learned on its own is "elsewhere" rather
            # than "ru": it will hand back a Kazakh oblast just as readily.
            gaz.remember("Омская область", "ru", {
                "name": "Омская область", "lat": 55.0, "lon": 73.4,
                "category": "boundary", "bbox": (54.0, 56.0, 72.0, 75.0),
                "shape": {"type": "Polygon", "coordinates": [
                    [[80.0, 55.0], [81.0, 55.0], [81.0, 56.0],
                     [80.0, 55.0]]]}})
            shipped = set(neighbours.provinces())
            whose = {o["name"]: o["in"] for o in tracker.outlines()
                     if o["level"] != "country" and o["name"] not in shipped}
            assert whose == {"сумська область": "ua",
                             "омская область": "elsewhere"}
        finally:
            tracker.reset()
            neptun.forget()
            gaz.forget()

    def test_a_picture_of_a_country_is_told_which_country(self):
        """Wiring, and read off the source because that is what it is.

        picked() and belongsTo() are both checked properly in
        tests/picture.test.mjs. What cannot be checked there is that the
        button actually HANDS the country over -- saveShot needs a map, a
        Leaflet bounds and a canvas. Without this the two would agree about
        the rule and disagree about whether it is applied, which is exactly
        the state this fixed: the fit filtered by country and the picture
        did not.
        """
        text = self.source()
        block = text[text.index("function askWhere()"):]
        block = block[:block.index("\nfunction ")]
        assert "saveShot(areaOf(key)?.bounds, { only: key })" in block
        # And the other three answers name no country, because they are not
        # about one: this view, everything, and a box somebody drew.
        assert "saveShot(null, { exactly: true })" in block
        assert "saveShot(everything())" in block

    def test_the_borders_are_loaded_before_the_marks_are_chosen(self):
        """Ordering, and it decides whether the filter works at all.

        belongsTo asks inUkraine; inUkraine answers null until the provinces
        arrive; belongsTo reads null as "cannot tell, keep it". Fetched after
        the loop -- where it was -- a picture of one country would quietly
        contain both, and nothing would look broken.
        """
        text = self.source()
        block = text[text.index("async function saveShot("):]
        block = block[:block.index("\n}")]
        assert block.index("await regionOutlines()") < block.index("for (const held")

    def test_the_view_fits_the_marks_rather_than_the_box(self):
        block = self.source()
        block = block[block.index("function areaOf(which)"):]
        block = block[:block.index("\n}")]
        assert "belongsTo(held, which, box)" in block
        assert "L.latLngBounds(points)" in block
        # Including the shaded provinces, which reach past their centroids.
        assert "held.area?.getBounds?.()" in block

    def test_an_empty_country_still_answers(self):
        # Flying to an empty rectangle is at least an answer to where the
        # country is, and the panel says that is what happened.
        block = self.source()
        block = block[block.index("function areaOf(which)"):]
        block = block[:block.index("\n}")]
        assert "return { bounds: box, found: 0 };" in block
        said = self.source()
        said = said[said.index("async function showAirspace"):]
        said = said[:said.index("\n}")]
        assert "if (!got.found)" in said
        assert "toast(" in said

    def test_the_picture_fits_the_same_way(self):
        # Otherwise the map and the picture disagree about what "Russia"
        # means, which is worse than either answer on its own.
        block = self.source()
        block = block[block.index("function askWhere()"):]
        block = block[:block.index("\nfunction closeWhere")]
        assert "areaOf(key)?.bounds" in block
        assert "await regionOutlines();" in block

    def test_not_knowing_the_border_does_not_empty_the_map(self):
        # Until the outlines arrive every mark is in the box's country, which
        # is what the view did before any of this.
        block = self.source()
        block = block[block.index("function belongsTo"):]
        block = block[:block.index("\n}")]
        assert "if (ua === null) return true;" in block


class TestNeptunIsTheSourceForUkraine:
    """A channel reading that lands inside Ukraine is not drawn.

    This module has said so at the top since the other four channels were
    removed -- "two paths to the same facts with only the worse one able to
    put a mark in the wrong province" -- and then went on reading the one
    remaining channel into Ukraine anyway.

    What it removes is real and was on screen: a jet drone over Sumy, drawn
    in a colour NEPTUN's vocabulary cannot produce -- their types are uav,
    recon, missile, ballistic, kab and mig31k, and none of them is a jet
    drone -- sitting beside NEPTUN's own tracks of the same raid, which did
    not include it.
    """

    # A square standing in for Ukraine, around Sumy and well clear of
    # Belgorod: the two are a hundred kilometres apart.
    UKRAINE = {"type": "Polygon", "coordinates": [
        [[33.0, 49.5], [35.5, 49.5], [35.5, 51.5], [33.0, 51.5], [33.0, 49.5]]]}

    def wired(self, monkeypatch, shapes):
        monkeypatch.setattr(tracker.neptun, "shapes", lambda: shapes)
        monkeypatch.setattr(tracker.neptun, "_rings", None, raising=False)
        monkeypatch.setattr(tracker.neptun, "_rings_from", None, raising=False)
        tracker.reset()

    def post(self, lat, lon, kind="jet_drone", by="rules"):
        item = {"kind": kind, "place": "somewhere", "by": by}
        message = {"id": "t1", "channel": "lpr1_treugolnik",
                   "region": "Luhansk and Russia", "date": time.time(),
                   "text": "a report"}
        monkey = lambda i, c, lookup=None: {   # noqa: E731
            "placed": True, "lat": lat, "lon": lon, "kind": kind,
            "summary": "a report", "place": "somewhere", "count": 1,
            "region_scope": None, "area_km": 5.0,
        }
        return item, message, monkey

    def test_a_channel_mark_inside_ukraine_is_not_drawn(self, monkeypatch):
        self.wired(monkeypatch, {"ua": self.UKRAINE})
        item, message, place = self.post(50.9, 34.8)     # Sumy
        monkeypatch.setattr(tracker, "place_event", place)
        assert tracker._record(item, message, "ru,ua") is False
        assert tracker._events == []
        assert tracker._shadowed == 1

    def test_but_one_in_russia_is(self, monkeypatch):
        self.wired(monkeypatch, {"ua": self.UKRAINE})
        item, message, place = self.post(50.6, 36.6)     # Belgorod
        monkeypatch.setattr(tracker, "place_event", place)
        assert tracker._record(item, message, "ru,ua") is True
        assert len(tracker._events) == 1
        assert tracker._shadowed == 0

    def test_with_no_border_the_channel_is_all_there_is(self, monkeypatch):
        """None is not False.

        With no boundary file there is no NEPTUN behind it either, so the
        channel is the only source there is and dropping its reports would
        empty the map to prefer a source that is not answering.
        """
        self.wired(monkeypatch, {})
        item, message, place = self.post(50.9, 34.8)
        monkeypatch.setattr(tracker, "place_event", place)
        assert tracker._record(item, message, "ru,ua") is True
        assert len(tracker._events) == 1

    def test_a_report_with_no_position_is_untouched(self, monkeypatch):
        # It is listed as unplaced, which is a different thing from shadowed.
        self.wired(monkeypatch, {"ua": self.UKRAINE})
        item = {"kind": "drone", "place": None, "by": "rules"}
        message = {"id": "t2", "channel": "lpr1_treugolnik",
                   "region": "Luhansk and Russia", "date": time.time(),
                   "text": "a report"}
        monkeypatch.setattr(tracker, "place_event",
                            lambda i, c, lookup=None: {
                                "placed": False, "lat": None, "lon": None,
                                "kind": "drone", "summary": "", "count": 1,
                                "why_unplaced": "no place named",
                            })
        # _record returns False for anything unplaced -- "an alert always, a
        # track only if it placed" -- so what matters is that the report is
        # still in the stream and was not counted as shadowed.
        tracker._record(item, message, "ru,ua")
        assert len(tracker._alerts) == 1
        assert tracker._shadowed == 0

    def test_the_count_is_said_out_loud(self, monkeypatch):
        # "Read, and not drawn, because a better source has Ukraine" is a
        # different fact from "not read", and a thin channel row with no
        # explanation reads as the channel being broken.
        self.wired(monkeypatch, {"ua": self.UKRAINE})
        item, message, place = self.post(50.9, 34.8)
        monkeypatch.setattr(tracker, "place_event", place)
        tracker._record(item, message, "ru,ua")
        assert tracker.current()["shadowed"] == 1

    def test_neptuns_own_tracks_are_never_shadowed(self, monkeypatch):
        # They are the source being deferred to; deferring them to
        # themselves would empty Ukraine entirely.
        self.wired(monkeypatch, {"ua": self.UKRAINE})
        item, message, place = self.post(50.9, 34.8, by="neptun")
        monkeypatch.setattr(tracker, "place_event", place)
        assert tracker._record(item, message, "ru,ua") is True


class TestAWarningAtATownCoversItsRegion:
    """"If a town or area has an alert in that region, highlight the whole
    region."

    A warning reported at a town used to be a triangle on one street corner
    of a province -- which is what the Russian side looked like beside
    Ukraine's filled oblasts, because NEPTUN key their alerts to regions and
    a Telegram post names whatever the poster named.
    """

    KRAI = {"type": "Polygon", "coordinates": [
        [[37.0, 43.0], [41.5, 43.0], [41.5, 46.5], [37.0, 46.5], [37.0, 43.0]]]}

    def wired(self, monkeypatch, *, held=(), reverse=None):
        # Nothing shipped. These tests are about the two fallbacks -- a
        # boundary already held, then the gazetteer -- and the shipped file
        # now answers before both, so it is taken away to reach them. That
        # the file DOES answer first is its own test below.
        monkeypatch.setattr(tracker.neighbours, "provinces", dict)
        monkeypatch.setattr(tracker.neighbours, "shape_for", lambda name: None)
        monkeypatch.setattr(tracker.gazetteer, "outlines", lambda: list(held))
        monkeypatch.setattr(tracker.gazetteer, "region_at",
                            lambda lat, lon, countries="": reverse)
        monkeypatch.setattr(tracker.gazetteer, "improve_later",
                            lambda name, countries, urgent=False: True)
        monkeypatch.setattr(tracker.gazetteer, "outline",
                            lambda name, countries="": self.KRAI)

    def alert(self, monkeypatch, lat=43.60, lon=39.73, kind="alert"):
        # Sochi: a town, not a region.
        monkeypatch.setattr(tracker, "_look", lambda *a, **k: {
            "lat": lat, "lon": lon, "name": "Sochi", "kind": "town",
            "category": "place", "bbox": [lat - 0.09, lat + 0.09,
                                          lon - 0.09, lon + 0.09],
            "shape": None,
        })
        return tracker.place_event(
            {"kind": kind, "place": "Sochi", "count": 1}, "ru")

    def test_a_town_warning_is_drawn_as_its_region(self, monkeypatch):
        self.wired(monkeypatch, reverse="Краснодарский край")
        out = self.alert(monkeypatch)
        assert out["region_scope"] == "covers"
        assert out["region_wide"] is True
        assert out["shape"] == self.KRAI

    def test_a_boundary_already_held_is_used_without_asking_anybody(
            self, monkeypatch):
        """Exact and free.

        Once a province has been drawn over once, every later warning inside
        it is decided by the real border rather than by a reverse lookup.
        """
        asked = []
        self.wired(monkeypatch, held=[("Краснодарский край", self.KRAI)])
        monkeypatch.setattr(tracker.gazetteer, "region_at",
                            lambda lat, lon, countries="":
                            asked.append(1) or "somewhere else")
        out = self.alert(monkeypatch)
        assert out["shape"] == self.KRAI
        assert asked == [], "the gazetteer was asked despite holding the border"

    def test_the_town_stays_the_place(self, monkeypatch):
        # "Air alert in Sochi" is what was reported and is more use than the
        # krai's name; the region is recorded beside it.
        self.wired(monkeypatch, reverse="Краснодарский край")
        out = self.alert(monkeypatch)
        assert out["place_match"] == "Sochi"
        assert out["region_over"] == "Краснодарский край"

    def test_a_drone_over_a_town_is_still_at_the_town(self, monkeypatch):
        """The distinction this layer has spent a long time getting right.

        A drone over Sochi is AT Sochi. Shading the krai for it would say a
        warning covers ground nobody mentioned.
        """
        self.wired(monkeypatch, reverse="Краснодарский край")
        out = self.alert(monkeypatch, kind="drone")
        assert out["region_scope"] is None
        assert out["shape"] is None

    def test_a_point_outside_every_border_held_falls_through(self, monkeypatch):
        # The held boundary must actually contain the point, or a warning in
        # one province would be drawn over another.
        self.wired(monkeypatch, held=[("Краснодарский край", self.KRAI)],
                   reverse=None)
        out = self.alert(monkeypatch, lat=60.0, lon=100.0)
        assert out["region_scope"] is None

    def test_nothing_known_leaves_it_as_a_point(self, monkeypatch):
        # A triangle at the town is a worse drawing than a shaded province
        # and a far better one than a province picked by guesswork.
        self.wired(monkeypatch, reverse=None)
        out = self.alert(monkeypatch)
        assert out["region_scope"] is None
        assert out["shape"] is None

    def test_the_gazetteer_being_down_is_not_fatal(self, monkeypatch):
        self.wired(monkeypatch)
        monkeypatch.setattr(tracker.gazetteer, "region_at",
                            lambda *a, **k: (_ for _ in ()).throw(
                                tracker.gazetteer.GazetteerError("down")))
        out = self.alert(monkeypatch)
        assert out["placed"] is True
        assert out["region_scope"] is None


class TestAWarningThatNamesARegionIsShadedToo:
    """"Some areas of Russia get the region warnings while others dont."

    Two paths, two answers, on one map. A warning reported at a TOWN went
    through the promotion above and was shaded at once. A warning that NAMED
    a region -- "Air raid alert in Kursk region" -- already had region_scope
    set by the match, so the promotion skipped it and it sat waiting on the
    gazetteer's background queue, which is rate-limited and gives up.

    The question the promotion asks is whether there is an OUTLINE to draw,
    not whether a region was named.
    """

    OBLAST = {"type": "Polygon", "coordinates": [
        [[34.0, 50.5], [39.0, 50.5], [39.0, 52.5], [34.0, 52.5],
         [34.0, 50.5]]]}

    def named(self, monkeypatch, *, held=(), reverse=None, kind="alert"):
        # As above: the shipped file answers first, and these are about what
        # happens when it has not.
        monkeypatch.setattr(tracker.neighbours, "provinces", dict)
        monkeypatch.setattr(tracker.neighbours, "shape_for", lambda name: None)
        monkeypatch.setattr(tracker.gazetteer, "outlines", lambda: list(held))
        monkeypatch.setattr(tracker.gazetteer, "region_at",
                            lambda lat, lon, countries="": reverse)
        monkeypatch.setattr(tracker.gazetteer, "improve_later",
                            lambda name, countries, urgent=False: True)
        monkeypatch.setattr(tracker.gazetteer, "outline",
                            lambda name, countries="": self.OBLAST)
        # The match IS a region -- and has no boundary yet, which is the
        # whole of the bug.
        monkeypatch.setattr(tracker, "_look", lambda *a, **k: {
            "lat": 51.7, "lon": 36.2, "name": "Kursk region",
            "kind": "state", "category": "boundary",
            "bbox": [50.5, 52.5, 34.0, 39.0], "shape": None,
        })
        monkeypatch.setattr(tracker.neptun, "shape_for", lambda name: None)
        return tracker.place_event(
            {"kind": kind, "place": "Kursk region", "count": 1}, "ru")

    def test_a_named_region_with_no_outline_is_shaded(self, monkeypatch):
        out = self.named(monkeypatch, reverse="Курская область")
        assert out["shape"] == self.OBLAST
        assert out["region_wide"] is True

    def test_it_is_answered_by_a_border_already_held(self, monkeypatch):
        """The centroid of a region is inside that region.

        So the first test the promotion makes -- a boundary this app already
        holds -- answers a named region for nothing, without a lookup.
        """
        asked = []
        out = self.named(monkeypatch,
                         held=[("Курская область", self.OBLAST)])
        monkeypatch.setattr(tracker.gazetteer, "region_at",
                            lambda *a, **k: asked.append(1))
        assert out["shape"] == self.OBLAST
        assert asked == []

    def test_an_outline_already_matched_is_not_replaced(self, monkeypatch):
        """The promotion only runs when there is nothing to draw.

        A region whose own boundary arrived with the match keeps it; asking
        the gazetteer again could hand back a neighbour.
        """
        mine = {"type": "Polygon", "coordinates": [
            [[35.0, 51.0], [36.0, 51.0], [36.0, 52.0], [35.0, 52.0],
             [35.0, 51.0]]]}
        monkeypatch.setattr(tracker.gazetteer, "outlines",
                            lambda: [("Somewhere else", self.OBLAST)])
        monkeypatch.setattr(tracker.gazetteer, "improve_later",
                            lambda name, countries, urgent=False: True)
        monkeypatch.setattr(tracker, "_look", lambda *a, **k: {
            "lat": 51.7, "lon": 36.2, "name": "Kursk region",
            "kind": "state", "category": "boundary",
            "bbox": [50.5, 52.5, 34.0, 39.0], "shape": mine,
        })
        monkeypatch.setattr(tracker.neptun, "shape_for", lambda name: None)
        out = tracker.place_event(
            {"kind": "alert", "place": "Kursk region", "count": 1}, "ru")
        assert out["shape"] == mine
        assert out.get("region_over") is None

    def test_a_drone_over_a_region_is_not_shaded_by_this(self, monkeypatch):
        # Same distinction as for towns: only warnings cover their region.
        out = self.named(monkeypatch, reverse="Курская область",
                         kind="drone")
        assert out["region_wide"] is False
        assert out["shape"] is None


class TestThePictureTakesTheGroundYouAskedFor:
    """"When you press this view, it takes it from that view and ignores
    whether there's a drone there or not."

    The crop was pulling in around the marks whatever the area came from, so
    asking for the view got a picture of one corner of it -- and asking for a
    quiet border got "nothing in that area" and no picture at all.
    """

    def source(self):
        return (pathlib.Path(__file__).resolve().parent.parent
                / "frontend" / "js" / "tracker.js").read_text(encoding="utf-8")

    def test_this_view_asks_for_the_view_exactly(self):
        block = self.source()
        block = block[block.index("function askWhere()"):]
        block = block[:block.index("\nfunction closeWhere")]
        assert "saveShot(null, { exactly: true })" in block

    def test_a_drawn_box_is_taken_exactly_too(self):
        block = self.source()
        block = block[block.index("function done(e)"):]
        assert "saveShot(box, { exactly: true })" in block

    def test_a_country_is_still_fitted_to_what_is_over_it(self):
        # "Russia" means the part of Russia something is happening in, not
        # three thousand kilometres of empty ground.
        block = self.source()
        block = block[block.index("function askWhere()"):]
        block = block[:block.index("\nfunction closeWhere")]
        assert "saveShot(areaOf(key)?.bounds" in block
        # And NOT as ground taken exactly: a country is fitted to what is
        # over it, and "exactly" would hand back the rectangle instead.
        assert "areaOf(key)?.bounds, { exactly" not in block

    def test_an_empty_chosen_area_still_makes_a_picture(self):
        """"Nothing over this border tonight" is worth sending.

        It used to refuse outright, which reads as the button being broken
        rather than as the sky being quiet.
        """
        block = self.source()
        block = block[block.index("async function saveShot"):]
        assert "if (!marks.length && !warnings.length && !exactly) {" in block

    def test_and_says_so_rather_than_saying_nothing(self):
        block = self.source()
        block = block[block.index("async function saveShot"):]
        assert "'an empty sky'" in block

    def test_the_flag_reaches_the_drawing(self):
        block = self.source()
        block = block[block.index("async function saveShot"):]
        assert "      exactly," in block
