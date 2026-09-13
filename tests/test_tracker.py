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
import threading
import math
import time

import pytest

from backend import ollama, places, reports, tracker


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

    def test_a_strike_is_given_no_course_however_the_sentence_reads(self):
        # A strike happened where it happened. "Вибух у Києві, БпЛА курсом на
        # Львів" is two facts, and carrying the drone's bearing onto the
        # explosion would draw an arrow for something on the ground.
        got = tracker._clean({"kind": "explosion", "place": "Kyivia",
                              "course": "N", "toward": "Lviv", "count": 1,
                              "summary": "boom", "region": None})
        assert got["course"] is None and got["toward"] is None
        read = reports.read("Вибухи у Харкові курсом на північ")
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

    def test_a_strike_stays_far_longer_than_anything_in_flight(self):
        # The whole point of the per-kind clock: where a strike happened is
        # still true six hours later, and a report of a drone crossing an
        # oblast is not.
        assert tracker.KEEP["explosion"] >= 6 * tracker.KEEP["drone"]
        for kind in ("drone", "jet_drone", "missile", "aircraft"):
            assert tracker.KEEP[kind] < tracker.KEEP["explosion"], kind

    def test_a_strike_outranks_everything_in_the_stream(self):
        assert tracker.KINDS["explosion"]["rank"] == max(
            look["rank"] for look in tracker.KINDS.values())

    def test_only_the_things_that_do_not_fly_stand_still(self):
        for name in tracker.NOT_AIRBORNE:
            assert tracker.MOTION[name] == "still", name
        assert set(tracker.NOT_AIRBORNE) == {"explosion", "alert"}

    def test_the_kinds_are_the_differences_worth_drawing(self):
        # Eight. Written out rather than derived, because which distinctions
        # this map makes is a decision somebody took and not whatever happens
        # to be in the table.
        #
        # "bomb" joined when NEPTUN's feed did. A KAB is released from an
        # aircraft near the line and glides tens of kilometres; folding it
        # into "missile" would draw a hundreds-of-kilometres weapon where a
        # tens-of-kilometres one was reported, which is the wrong answer to
        # "how long have I got".
        assert set(tracker.KINDS) == {
            "drone", "jet_drone", "missile", "bomb", "aircraft",
            "explosion", "alert", "unknown"}

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
            assert folded in tracker.KINDS, f"{text!r}: {raw} -> {folded}"
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

    def test_a_strike_does_not_travel_however_the_report_reads(self):
        # "Explosions in Kherson, drones heading for Mykolaiv" is one message.
        # The strike is where it is; only the airborne thing has a course.
        got = tracker.place_event(
            one(kind="explosion", place="Nikopolia", toward="Khersonia"),
            "ua", lookup=gazetteer)
        assert got["placed"] is True
        assert got["heading"] is None

    def test_an_air_alert_does_not_travel_either(self):
        got = tracker.place_event(
            one(kind="alert", place="Nikopolia", toward="Khersonia"), "ua", lookup=gazetteer)
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
        for kind in ("drone", "jet_drone", "missile", "aircraft", "unknown"):
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

    def test_a_strike_reported_across_a_region_does_shade_it(self):
        # "вибухи на Київщині" says explosions somewhere in the oblast and
        # does not say where. The region is the honest extent of that.
        got = tracker.place_event(one(kind="explosion", place="Kyivia oblast"),
                                "ua", lookup=self.looks_up())
        assert got["region_wide"] is True

    def test_a_region_with_no_outline_falls_back_to_a_circle(self):
        got = tracker.place_event(one(kind="alert", place="Kyivia oblast"), "ua",
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
        import pathlib
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
        for kind in ("missile", "aircraft", "explosion", "alert"):
            assert tracker.KINDS[kind]["colour"] != drone, kind

    def test_a_missile_is_not_drawn_like_a_drone(self):
        """The one distinction nobody may have to guess at.

        Colour alone was carrying it and was not carrying it well enough: a
        drone is #ff3b30 and a cruise missile #ff6a3b, which is 58 apart out
        of a possible 765 -- fine side by side in a key, not fine across a
        map. So missiles get a narrower arrow as well.

        Found by the test below when the silhouettes came out, which is the
        whole reason that test exists: removing the shapes moved the entire
        burden onto the palette, and the palette was not ready for it.
        """
        text = self.source()
        assert "const SLIM =" in text
        assert "SLIM_KINDS" in text
        block = text[text.index("const SLIM_KINDS"):]
        named = block[:block.index("]")]
        for kind in ("missile",):
            assert kind in named, kind
        for kind in ("drone", "jet_drone"):
            assert kind not in named, f"{kind} is not a missile"

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

        # Everything drawn as a wide arrow, and everything drawn as a slim one.
        wide = ("drone", "aircraft", "unknown")
        slim = ("missile",)
        for family in (wide, slim):
            for i, one in enumerate(family):
                for other in family[i + 1:]:
                    assert apart(one, other) >= 60, \
                        f"{one} and {other} share a shape and are too close"
        # And a drone against a missile, which is the pairing that matters
        # most: different shape AND a usable colour gap.
        assert apart("drone", "missile") >= 60

    def test_the_kinds_drawn_by_behaviour_are_still_drawn_that_way(self):
        # Strikes burst, warnings are a triangle over their area. Those read
        # by what they are rather than by a direction, and neither is an
        # arrow: an arrow on a strike would be pointing somewhere for no
        # reason.
        for name in ("explosion", "alert"):
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
        assert {r["channel"] for r in rows} == {c["name"] for c in tracker.CHANNELS}
        # All four states a channel can be in, so each renders at least once.
        assert any(r["problem"] for r in rows)
        assert any(r["posts"] > 0 and r["fresh"] == 0 and not r["problem"]
                   for r in rows)
        assert any(r["read"] and not r["placed"] for r in rows)
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
        # number stop meaning "reports the gazetteer could place".
        reported = [e for e in got["events"] if not e.get("derived")]
        assert got["reports"]["placed"] >= len(reported)
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

    def test_the_demo_has_pictures_on_its_strikes(self):
        # A feature whose only demonstration needs a network is a feature
        # nobody checks.
        strikes = [e for e in tracker.demo()["events"] if e["kind"] == "explosion"]
        assert strikes
        for strike in strikes:
            assert strike["photos"], strike["place"]

    def test_the_demo_pictures_are_obviously_not_photographs(self):
        # The single worst thing in this app to get wrong would be a
        # convincing invented picture of a strike.
        for event in tracker.demo()["events"]:
            for shot in event.get("photos") or []:
                assert shot.startswith("data:image/svg+xml"), shot
                assert "demo" in shot


class TestHowLongAStrikeStays:
    """A day, not an evening."""

    def test_a_strike_is_held_for_at_least_a_day(self):
        assert tracker.KEEP["explosion"] >= 24 * 60

    def test_and_a_little_more_than_a_day(self):
        # Exactly twenty-four hours means a strike reported at nine in the
        # morning disappears at nine the next morning, while somebody is
        # looking at it and just as they go to compare it with today.
        assert tracker.KEEP["explosion"] > 24 * 60

    def test_a_strike_from_yesterday_evening_is_still_drawn(self):
        now = time.time()
        strike = {"kind": "explosion", "seen": now - 20 * 3600}
        assert tracker._alive(strike, now)

    def test_a_strike_from_two_days_ago_is_not(self):
        now = time.time()
        assert not tracker._alive({"kind": "explosion", "seen": now - 48 * 3600}, now)

    def test_the_report_outlives_its_own_marker_never_the_other_way(self):
        # A marker on the map with no row in the panel to explain it is worse
        # than either problem alone.
        for kind in tracker.KINDS:
            held = max(tracker.ALERT_MINUTES, tracker.keep_minutes(kind))
            assert held >= tracker.keep_minutes(kind), kind

    def test_the_cap_has_room_for_a_day_of_them(self):
        # The cap drops the oldest, so one sized for an evening would quietly
        # stop being a day for anybody having a bad week -- and the map would
        # look complete while missing the beginning of it.
        assert tracker.MAX_EVENTS >= 1000
        assert tracker.MAX_ALERTS >= 500

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

    def test_a_strike_is_given_no_course_at_all(self):
        # Replaces the recon case, which no longer exists. The rule is about
        # motion rather than about the kind: something that is not travelling
        # has no course, and a strike is a place rather than a direction.
        got = tracker.place_event(
            {"kind": "explosion", "place": "Nikopolia", "course": 90.0,
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

    def test_and_shorter_than_a_strike(self):
        assert tracker.KEEP["alert"] < tracker.KEEP["explosion"]

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
        # In a thread pool a fetch raising something unexpected surfaces at the
        # point the result is collected, and it must not take the other three
        # with it.
        def mixed(channel):
            if channel == "kpszsu":
                raise RuntimeError("something nobody thought of")
            if channel == "war_monitor":
                raise tracker.TrackerError("war_monitor answered 500")
            return []

        tracker.reset()
        monkeypatch.setattr(tracker, "_fetch_channel", mixed)
        got = tracker.poll()
        rows = {r["channel"]: r for r in got["sources"]}
        assert rows["kpszsu"]["problem"]
        assert "500" in rows["war_monitor"]["problem"]
        # The two that worked are still accounted for, not missing.
        assert rows["eRadarrua"]["problem"] is None
        assert rows["lpr1_treugolnik"]["problem"] is None

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
                                "Вибухи у Харкові"])
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
                             {"eRadarrua": "Шахед над Нікополем"}, {"eRadarrua": 5})
        assert rows["eRadarrua"]["posts"] == 1
        assert rows["eRadarrua"]["fresh"] == 1

    def test_a_post_older_than_the_window_is_seen_but_not_fresh(self, monkeypatch):
        # The exact case that read as "none readable". One post, read from the
        # page perfectly well, simply too old to be news.
        #
        # Ninety minutes no longer qualifies -- the window is the longest keep
        # time now, so a post is only "not news" once nothing it could become
        # would still be drawn. Aged past that.
        stale = tracker.LOOKBACK_MINUTES + 60
        rows = self.read_one(monkeypatch,
                             {"eRadarrua": "Шахед над Нікополем"},
                             {"eRadarrua": stale})
        assert rows["eRadarrua"]["posts"] == 1
        assert rows["eRadarrua"]["fresh"] == 0
        assert rows["eRadarrua"]["problem"] is None, \
            "nothing went wrong, so nothing should be reported as a problem"

    def test_the_tally_carries_the_four_numbers_the_panel_needs(self, monkeypatch):
        # posts, fresh, read, placed -- in the order the reading happens, so
        # the panel can say where it stopped.
        rows = self.read_one(monkeypatch, {"kpszsu": "Вибухи у Харкові"})
        for row in rows.values():
            assert set(row) >= {"posts", "fresh", "read", "placed", "problem"}

    def test_the_demo_shows_a_channel_with_posts_but_nothing_new(self):
        """The state all four channels were in on a live start.

        And the state the demo could not produce, which is why the wording
        for it was never looked at -- it said "20 posts, none readable", which
        claims the app cannot read the channel. Third time a drawing has gone
        unexamined because the offline build could not reach it, so it is
        pinned here.
        """
        rows = tracker.demo()["sources"]
        quiet = [r for r in rows
                 if r["posts"] > 0 and r["fresh"] == 0 and not r["problem"]]
        assert quiet, [(r["channel"], r["posts"], r["fresh"]) for r in rows]

    def test_the_demo_still_shows_the_other_three_states(self):
        rows = tracker.demo()["sources"]
        assert any(r["problem"] for r in rows), "unreachable"
        assert any(r["fresh"] and not r["placed"] for r in rows), "none placeable"
        assert any(r["placed"] for r in rows), "working"


class TestWhatItGrabsOnStartUp:
    """Twenty minutes, from a cold start.

    Worth pinning because it is easy to believe it is broken: a channel that
    has not posted for half an hour gives a cold start nothing at all, which
    looks identical to a reader that cannot read.
    """

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

    def strikes_at(self, monkeypatch, *ages):
        """The same, but reporting strikes -- which are held for a day.

        A drone is held for twenty minutes, so a test written with drones
        cannot tell "not read" from "read and expired", and would pass with
        the read window set to anything at all.
        """
        tracker.reset()
        now = dt.datetime.now(dt.timezone.utc)
        monkeypatch.setattr(tracker, "_fetch_channel", lambda channel: [
            {"id": f"{channel}/{i}", "channel": channel,
             "when": (now - dt.timedelta(minutes=old)).isoformat(),
             "text": "Вибух у Кременчуці", "photos": [], "link": None}
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
        # Tested with a strike rather than a drone, because a drone an hour
        # old is read and then immediately expired by its own keep time --
        # which would make this pass whatever the window was.
        inside = self.strikes_at(monkeypatch, tracker.LOOKBACK_MINUTES - 5)
        outside = self.strikes_at(monkeypatch, tracker.LOOKBACK_MINUTES + 5)
        assert inside["count"] == len(tracker.CHANNELS)
        assert outside["count"] == 0

    def test_a_strike_from_hours_ago_is_on_the_map_from_a_cold_start(self):
        """The failure this whole change was about.

        Strikes are held for twenty-five hours. With a twenty-minute read
        window a freshly opened app could only ever show one from the last
        twenty minutes, so the other twenty-four hours and forty minutes of
        the feature were unreachable on every start -- which is every start,
        for a page somebody opens to see what has happened.
        """
        assert tracker.LOOKBACK_MINUTES >= tracker.KEEP["explosion"]

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
             "text": "Вибух у Кременчуці", "photos": [], "link": None}
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
        # @radarrussiia joined the original four when the Russian side's
        # warnings were asked for. It is the only one that posts in English,
        # and reading it needed both the English region spellings in
        # backend/places.py and a reader that stops calling "Lipetsk Oblast
        # Drone Alert" a drone.
        assert names == {"eRadarrua", "kpszsu", "war_monitor",
                         "lpr1_treugolnik", "radarrussiia"}

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
        tracker._record(one(kind="explosion", place="Київ", summary="Boom"),
                        {"id": "c/3", "channel": "c"}, "ua")
        tracker._record(one(kind="drone", place="Київ", summary="Drone"),
                        {"id": "c/4", "channel": "c"}, "ua")

        tracker._record(one(kind=tracker.LIFTED, place="Київ",
                            summary="All clear"),
                        {"id": "c/5", "channel": "c"}, "ua")

        left = tracker.current()["events"]
        kinds = sorted(e["kind"] for e in left)
        assert kinds == ["alert", "drone", "explosion"]
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
        assert len(got) == 1
        assert got[0]["kind"] == "alert"
        assert got[0]["cause"] == "drone"

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
        # A drone in the air IS a drone; a cause there would say the same
        # thing twice, and on a strike it would colour the crater by whatever
        # made it.
        for text in ("Шахед над Нікополем", "Вибухи у Харкові"):
            got = self.one(text)["events"]
            assert got[0].get("cause") is None, text

    def test_the_colours_a_cause_can_name_are_ones_the_map_has(self):
        # The page looks the cause up in the same kinds table it colours marks
        # from, so a cause naming a kind that is not there would fall back to
        # amber silently.
        for cause in ("drone", "missile"):
            assert cause in tracker.KINDS, cause


class TestTheRussianRadarChannelIsRead:
    def test_it_is_one_of_the_channels(self):
        named = [c["name"] for c in tracker.CHANNELS]
        assert "radarrussiia" in named

    def test_it_is_asked_for_russia_first(self):
        # A gazetteer asked for a Russian region with Ukraine first answers
        # with somewhere else confidently.
        row = next(c for c in tracker.CHANNELS if c["name"] == "radarrussiia")
        assert row["countries"].split(",")[0] == "ru"

    def test_an_hour_of_its_warnings_lands_on_the_map(self, monkeypatch):
        """What "alerts from the last hour on starting the app" needs.

        Two things have to hold together: the read window must reach back far
        enough to find them, and a warning must be kept long enough to still
        be drawn once found. At a twenty-minute window and a sixty-minute
        keep, a warning declared fifty minutes ago -- still running -- was
        never read at all.
        """
        tracker.reset()
        now = dt.datetime.now(dt.timezone.utc)
        posts = [
            ("Lipetsk Oblast Drone Alert", 5),
            ("Voronezh Oblast Missile Alert", 25),
            ("Kursk Oblast Drone Alert", 50),
            ("Bryansk Oblast Drone Alert", 85),
        ]
        monkeypatch.setattr(tracker, "CHANNELS", (
            {"name": "radarrussiia", "region": "Russia", "countries": "ru,ua"},))
        monkeypatch.setattr(tracker, "_fetch_channel", lambda channel: [
            {"id": f"{channel}/{i}", "channel": channel,
             "when": (now - dt.timedelta(minutes=old)).isoformat(),
             "text": text, "photos": [], "link": None}
            for i, (text, old) in enumerate(posts)])
        got = tracker.poll()
        # The three inside ninety minutes. The eighty-five-minute one is read
        # and kept; the ninety-minute keep is what decides the edge.
        drawn = {e["place"]: e for e in got["events"]}
        assert "Lipetsk oblast" in drawn
        assert "Voronezh oblast" in drawn
        assert "Kursk oblast" in drawn
        assert drawn["Voronezh oblast"]["cause"] == "missile"
        assert drawn["Kursk oblast"]["cause"] == "drone"

    def test_a_stand_down_takes_its_regions_warnings_down(self, monkeypatch):
        # The built-in table answers for these, so no network is reached --
        # but pinned explicitly rather than relied on, because a test that
        # quietly starts making requests is a test that starts being slow and
        # then starts being flaky.
        monkeypatch.setattr(tracker.gazetteer, "find",
                            lambda name, countries="": places.lookup(name))
        tracker.reset()
        for i, text in enumerate([
            "Republic of Tatarstan Drone Alert",
            "Republic of Bashkortostan Drone Alert",
        ]):
            for plain in reports.read_all(text):
                plain["kind"] = tracker.fold_kind(plain["kind"])
                tracker._record(tracker._clean({**plain, "id": f"c/{i}"}),
                                {"id": f"c/{i}", "channel": "radarrussiia"}, "ru")
        assert len(tracker.current()["events"]) == 2

        # One post, both regions -- which is the shape that channel writes.
        for plain in reports.read_all(
                "Republic of Tatarstan, Republic of Bashkortostan – UAV alert cleared."):
            plain["kind"] = tracker.fold_kind(plain["kind"])
            tracker._record(tracker._clean({**plain, "id": "c/9"}),
                            {"id": "c/9", "channel": "radarrussiia"}, "ru")
        assert tracker.current()["events"] == []


class TestAWarningWhereThereAreThingsInTheAir:
    """The only mark on this map that nobody reported.

    Asked for: drones in a region should mean a warning over that region. That
    is a real convenience and a real hazard -- every other mark here exists
    because somebody said so, and this one does not. So it is derived rather
    than invented, it never displaces a declared warning, and it says which it
    is.
    """

    def raise_some(self, text, channel_region="Ukraine"):
        tracker.reset()
        for plain in reports.read_all(text):
            plain["kind"] = tracker.fold_kind(plain["kind"])
            tracker._record(tracker._clean({**plain, "id": "c/1"}),
                            {"id": "c/1", "channel": "x",
                             "region": channel_region}, "ua")
        return tracker.current()["events"]

    def digest(self):
        return ("🛸 Сумщина: 🛩 БпЛА в р-ні н.п. Путивль та Глухів рухаються "
                "західним курсом; 🛸 Рівненщина: 🛩 БпЛА в р-ні н.п. Корець "
                "рухаються західним курсом.")

    def test_a_region_with_drones_in_it_gets_a_warning(self, monkeypatch):
        monkeypatch.setattr(tracker.gazetteer, "find",
                            lambda name, countries="": places.lookup(name))
        got = self.raise_some(self.digest())
        made = [e for e in got if e.get("derived")]
        assert {e["place_match"] for e in made} == {
            "Сумська область", "Рівненська область"}
        assert all(e["kind"] == "alert" for e in made)

    def test_it_says_it_was_derived_and_from_how_many(self, monkeypatch):
        monkeypatch.setattr(tracker.gazetteer, "find",
                            lambda name, countries="": places.lookup(name))
        made = [e for e in self.raise_some(self.digest()) if e.get("derived")]
        sumy = next(e for e in made if e["place_match"] == "Сумська область")
        assert sumy["by"] == "derived"
        assert sumy["from_marks"] == 2
        assert "2 objects" in sumy["summary"]

    def test_the_oblast_survives_the_channels_own_region(self, monkeypatch):
        """The bug that made this impossible to do honestly.

        An event's "region" is the channel's beat -- "Ukraine", "Luhansk and
        Russia" -- and it was overwriting the oblast the READER found, so a
        drone from a digest section headed "Сумщина" reached the map knowing
        only which channel it came from. A warning is declared over an oblast,
        so the oblast has to survive.
        """
        monkeypatch.setattr(tracker.gazetteer, "find",
                            lambda name, countries="": places.lookup(name))
        got = self.raise_some(self.digest())
        towns = [e for e in got if e["kind"] == "drone"]
        assert {e["oblast"] for e in towns} == {
            "Сумська область", "Рівненська область"}
        assert {e["region"] for e in towns} == {"Ukraine"}

    def test_a_declared_warning_is_never_replaced_by_a_derived_one(
            self, monkeypatch):
        """The one that matters, and the first version of it proved nothing.

        It used "Шахед над Сумщиною" as the drone, whose PLACE is the oblast
        and whose oblast field is therefore empty -- so no warning could be
        derived from it whatever the code did, and the test passed with the
        "already declared" check deleted. The drone has to be somewhere INSIDE
        the region for the two to collide at all.
        """
        monkeypatch.setattr(tracker.gazetteer, "find",
                            lambda name, countries="": places.lookup(name))
        tracker.reset()
        for i, text in enumerate(["Повітряна тривога у Сумській області",
                                  "Сумщина: Шахед над Охтиркою"]):
            for plain in reports.read_all(text):
                plain["kind"] = tracker.fold_kind(plain["kind"])
                tracker._record(tracker._clean({**plain, "id": f"c/{i}"}),
                                {"id": f"c/{i}", "channel": "x",
                                 "region": "Ukraine"}, "ua")
        got = tracker.current()["events"]
        # The drone is in Sumy oblast, so without the check a derived warning
        # for Sumy oblast would be drawn on top of the declared one.
        drones = [e for e in got if e["kind"] == "drone"]
        assert drones and drones[0]["oblast"] == "Сумська область"

        warnings = [e for e in got if e["kind"] == "alert"]
        assert len(warnings) == 1, [(e.get("by"), e["place"]) for e in warnings]
        assert not warnings[0].get("derived")
        assert warnings[0]["by"] != "derived"

    def test_a_region_with_nothing_in_it_gets_nothing(self):
        assert tracker.derived_alerts([]) == []
        assert tracker.derived_alerts([
            {"kind": "explosion", "oblast": "Сумська область", "placed": True,
             "seen": 0.0}]) == []

    def test_an_unplaced_mark_raises_nothing(self):
        # It could not be put anywhere, so there is no evidence anything is
        # over that region -- only that a report named it.
        assert tracker.derived_alerts([
            {"kind": "drone", "oblast": "Сумська область", "placed": False,
             "seen": 0.0}]) == []

    def test_one_missile_makes_it_a_missile_warning(self):
        # Which is drawn red rather than yellow. A province with nine drones
        # and one missile in it is a missile problem.
        made = tracker.derived_alerts([
            {"kind": "drone", "oblast": "Сумська область", "placed": True,
             "seen": 0.0},
            {"kind": "missile", "oblast": "Сумська область", "placed": True,
             "seen": 0.0}])
        assert len(made) == 1 and made[0]["cause"] == "missile"

    def test_it_costs_no_lookup(self, monkeypatch):
        # Computed on a render path, so a region the built-in table does not
        # know is skipped rather than fetched. A warning nobody declared is
        # not worth a second of Nominatim's rate limit.
        monkeypatch.setattr(tracker.gazetteer, "find", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("derived_alerts must not reach the gazetteer")))
        assert tracker.derived_alerts([
            {"kind": "drone", "oblast": "Nowhere Anyone Knows", "placed": True,
             "seen": 0.0}]) == []

    def test_it_disappears_with_the_marks_it_came_from(self, monkeypatch):
        # Not stored, so there is no second expiry clock to drift from the
        # first. When the last drone in a province ages out, so does this.
        monkeypatch.setattr(tracker.gazetteer, "find",
                            lambda name, countries="": places.lookup(name))
        self.raise_some(self.digest())
        assert any(e.get("derived") for e in tracker.current()["events"])
        with tracker._lock:
            tracker._events.clear()
        assert tracker.current()["events"] == []


class TestARegionWhoseDronesAreReportedOverItDirectly:
    """The commonest shape in the feed, and the one that raised no warning.

    "БпЛА над Житомирщиною" places a mark on Zhytomyr oblast, and read() then
    clears the region field because repeating the place as the region says
    nothing. So the mark knew no oblast -- and a province with drones reported
    straight over it, the clearest case there is, was the one case
    derived_alerts() could not see.
    """

    def raise_over(self, monkeypatch, *texts):
        monkeypatch.setattr(tracker.gazetteer, "find",
                            lambda name, countries="": places.lookup(name))
        tracker.reset()
        for i, text in enumerate(texts):
            for plain in reports.read_all(text):
                plain["kind"] = tracker.fold_kind(plain["kind"])
                tracker._record(tracker._clean({**plain, "id": f"c/{i}"}),
                                {"id": f"c/{i}", "channel": "x",
                                 "region": "Ukraine"}, "ua")
        return tracker.current()["events"]

    def test_a_drone_over_an_oblast_raises_a_warning_for_it(self, monkeypatch):
        got = self.raise_over(monkeypatch, "БпЛА над Житомирщиною")
        made = [e for e in got if e.get("derived")]
        assert [e["place_match"] for e in made] == ["Житомирська область"]

    def test_it_works_for_every_way_an_oblast_is_written(self, monkeypatch):
        got = self.raise_over(monkeypatch,
                              "БпЛА над Житомирщиною",
                              "Шахед над Львівською областю",
                              "БпЛА над Воронежской областью")
        made = {e["place_match"] for e in got if e.get("derived")}
        assert made == {"Житомирська область", "Львівська область",
                        "Воронежская область"}

    def test_a_town_still_raises_one_for_the_region_it_is_in(self, monkeypatch):
        # The other half, which already worked: the oblast comes from the
        # report rather than from the mark's own place.
        got = self.raise_over(monkeypatch, "Сумщина: Шахед над Охтиркою")
        made = [e for e in got if e.get("derived")]
        assert [e["place_match"] for e in made] == ["Сумська область"]

    def test_a_strike_on_an_oblast_raises_nothing(self, monkeypatch):
        # Only things in flight put a region under threat. A strike is a fact
        # about a place that has already happened.
        got = self.raise_over(monkeypatch, "Вибухи на Житомирщині")
        assert not [e for e in got if e.get("derived")]

    def test_a_declared_warning_over_that_oblast_still_wins(self, monkeypatch):
        got = self.raise_over(monkeypatch,
                              "Повітряна тривога у Житомирській області",
                              "БпЛА над Житомирщиною")
        warnings = [e for e in got if e["kind"] == "alert"]
        assert len(warnings) == 1, [(e.get("by"), e["place"]) for e in warnings]
        assert not warnings[0].get("derived")


class TestTheDemoShowsTheDerivedWarnings:
    """Or they go unexamined, which is how this one reached a screenshot.

    The demo builds its events directly rather than through _record, so the
    oblast the reader found was being dropped on the way in -- and the offline
    build showed no derived warning at all while the live path drew them. That
    is the fourth time a drawing has gone unlooked-at because the build with
    no network could not reach the state.
    """

    def test_the_demo_draws_some(self):
        made = [e for e in tracker.demo()["events"] if e.get("derived")]
        assert made, "the offline build cannot show a derived warning"

    def test_they_come_from_the_same_function_as_the_live_ones(self):
        made = [e for e in tracker.demo()["events"] if e.get("derived")]
        assert all(e["by"] == "derived" for e in made)
        assert all(e["kind"] == "alert" for e in made)
        assert all(e["from_marks"] >= 1 for e in made)

    def test_the_demos_marks_carry_the_oblast_the_reader_found(self):
        # The thing that was dropped. Without it derived_alerts() sees nothing
        # to group, and the demo silently disagrees with the live path.
        towns = [e for e in tracker.demo()["events"]
                 if e["kind"] == "drone" and e.get("oblast")]
        assert towns, "no demo mark knows which oblast it is in"


class TestADerivedWarningExplainsItself:
    """A mark with nothing in the panel about it is worse than no mark.

    That is the rule this panel exists for, and a derived warning is the one a
    reader is most likely to want explained -- because nobody reported it. It
    was going onto the map with no row at all, which the "every drawn event
    has a row" invariant caught.
    """

    def made(self):
        got = tracker.demo()
        return ([e for e in got["events"] if e.get("derived")],
                [a for a in got["alerts"] if a.get("derived")])

    def test_every_derived_mark_has_a_row(self):
        marks, rows = self.made()
        assert marks
        assert {e["id"] for e in marks} == {a["id"] for a in rows}

    def test_the_row_says_it_was_not_reported(self):
        _, rows = self.made()
        assert rows
        assert all(a["by"] == "derived" for a in rows)
        assert all("Not reported" in a["text"] for a in rows)

    def test_the_row_says_what_it_came_from(self):
        _, rows = self.made()
        assert any("report(s) placed inside" in a["text"] for a in rows)

    def test_it_claims_to_cover_a_region_only_when_it_can_draw_one(self):
        # Every other path sets region_wide from whether there is a shape.
        # Claiming it without one leaves a mark that says it covers a province
        # and has no province to draw.
        marks, _ = self.made()
        for event in marks:
            if event["region_wide"]:
                assert event["shape"], event["place"]

    def test_it_is_projected_like_any_other_event(self):
        # They were appended raw, so every field project() adds was missing
        # and the page read undefined for each of them.
        marks, _ = self.made()
        for event in marks:
            assert "age_minutes" in event, event["id"]
            assert "region_wide" in event


class TestTakingAMarkOffByHand:
    """Somebody watching this knows things the feed does not.

    A drone was shot down and the channel has not said so; a warning is stale;
    a report was plainly a duplicate. Until now the only answer was to wait
    out the keep time, which for a strike is twenty-five hours.

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
                                           ("explosion", "Харків"),
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

    def test_a_strike_can_be_taken_off(self, monkeypatch):
        got = self.some(monkeypatch)
        strike = next(e for e in got["events"] if e["kind"] == "explosion")
        tracker.dismiss(strike["id"])
        assert not [e for e in tracker.current()["events"]
                    if e["kind"] == "explosion"]

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

    def test_a_derived_warning_can_be_cancelled_and_stays_cancelled(
            self, monkeypatch):
        # These are rebuilt from scratch on every read, so nothing but the
        # dismissal itself can keep one off.
        monkeypatch.setattr(tracker.gazetteer, "find",
                            lambda name, countries="": places.lookup(name))
        tracker.reset()
        for plain in reports.read_all("БпЛА над Житомирщиною"):
            plain["kind"] = tracker.fold_kind(plain["kind"])
            tracker._record(tracker._clean({**plain, "id": "c/1"}),
                            {"id": "c/1", "channel": "x",
                             "region": "Ukraine"}, "ua")
        made = [e for e in tracker.current()["events"] if e.get("derived")]
        assert made
        tracker.dismiss(made[0]["id"])
        assert not [e for e in tracker.current()["events"] if e.get("derived")]
        assert not [e for e in tracker.current()["events"] if e.get("derived")]

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
        # A strike in a town is a point in it. A rectangle round one would
        # claim the damage followed the municipal border.
        monkeypatch.setattr(tracker.gazetteer, "find",
                            lambda name, countries="": places.lookup(name))
        tracker.reset()
        item = tracker._clean({
            "kind": "explosion", "place": "Харків", "id": "e/1", "count": 1,
            "summary": "boom", "region": None, "toward": None,
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
