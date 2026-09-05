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
    return {"kind": "drone", "place": "Nikopol", "toward": None,
            "count": 1, "summary": "Drone over Nikopol", **over}


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
        assert got["projected_km"] == pytest.approx(133.3, abs=1)

    def test_a_report_with_no_course_does_not_move(self):
        now = time.time()
        event = {"kind": "drone", "heading": None, "seen": now - 900,
                 "origin_lat": 50.0, "origin_lon": 30.0}
        got = osint.project(event, now)
        assert (got["lat"], got["lon"]) == (50.0, 30.0)
        assert got["projected"] is False

    def test_a_ballistic_track_is_not_extrapolated(self):
        # It has a course but no useful constant speed: a marker sliding at a
        # made-up figure would look like it was being followed.
        now = time.time()
        event = {"kind": "ballistic", "heading": 90.0, "seen": now - 300,
                 "origin_lat": 50.0, "origin_lon": 30.0}
        assert osint.project(event, now)["projected"] is False

    def test_every_kind_has_a_speed_or_is_deliberately_not_airborne(self):
        for kind in osint.KINDS:
            assert kind in osint.SPEEDS or kind in osint.NOT_AIRBORNE, kind
        for look in osint.KINDS.values():
            assert look["colour"].startswith("#") and look["label"] and look["rank"]


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

    def test_polling_without_a_key_says_so_rather_than_calling_out(self):
        osint.set_key(None)
        with pytest.raises(osint.OsintError, match="No OpenRouter key"):
            osint.poll()


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
        osint._demo_epoch = 0.0
        try:
            osint.demo()
            osint._demo_epoch -= osint.DEMO_CYCLE - 1
            assert osint.demo()["events"] == []
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
