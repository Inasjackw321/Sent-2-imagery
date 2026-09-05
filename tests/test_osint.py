"""Tests for the air-threat layer.

This feature has two ways of being wrong, and only one of them is loud.

The loud one is a crash: Telegram changes its markup, or the model returns
prose where JSON was asked for, and nothing appears. That is a nuisance.

The quiet one is a marker in a place nobody reported. A language model asked
for coordinates will always produce coordinates -- 0,0 when it has no idea,
a plausible-looking pair when it is guessing from context -- and drawn on a map
next to real reports that is indistinguishable from evidence. So most of what
is tested here is refusal: what `_clean` throws away, and why.

The third thing tested is the arithmetic that carries a marker forward, because
it is the one part of this that claims to know something nobody said.
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


class TestReadingTheChannel:
    def test_each_post_keeps_its_own_id_and_time(self):
        got = osint.parse_preview(PAGE, "eRadarrua")
        assert [p["id"] for p in got] == ["eRadarrua/12345", "eRadarrua/12346"]
        assert got[0]["when"] == "2026-09-01T11:52:00+00:00"
        assert got[0]["channel"] == "eRadarrua"

    def test_the_text_comes_out_as_text(self):
        # Entities decoded, tags gone, line breaks kept as breaks -- the model
        # is being asked to read this, and markup is noise it pays for.
        body = osint.parse_preview(PAGE, "eRadarrua")[0]["text"]
        assert "Nikopol" in body and "Marhanets" in body
        assert "<" not in body and "&#" not in body

    def test_a_post_does_not_swallow_the_next_one(self):
        # The failure this guards: one greedy match across the whole page,
        # giving the first post's id the last post's text.
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
            {"kind": "drone", "lat": 47.6, "lon": 34.4, "heading": 200,
             "place": "Nikopol", "count": 3, "text": "x"}]}))
        assert len(got) == 1
        assert got[0]["kind"] == "drone" and got[0]["count"] == 3

    def test_a_code_fence_is_survived(self):
        got = osint.read_events(
            '```json\n{"events": [{"kind": "drone", "lat": 47.6, "lon": 34.4}]}\n```')
        assert len(got) == 1

    def test_a_sentence_before_the_json_is_survived(self):
        got = osint.read_events(
            'Here are the events:\n{"events": [{"kind": "cruise", "lat": 46.4, "lon": 32.0}]}')
        assert got[0]["kind"] == "cruise"

    def test_a_bare_list_is_survived(self):
        got = osint.read_events('[{"kind": "drone", "lat": 47.6, "lon": 34.4}]')
        assert len(got) == 1

    def test_json_that_is_not_json_is_refused_clearly(self):
        with pytest.raises(osint.OsintError, match="did not return JSON"):
            osint.read_events("I could not find any events in these messages.")

    def test_an_empty_answer_is_no_events_not_an_error(self):
        assert osint.read_events('{"events": []}') == []


class TestWhatIsRefused:
    """The quiet failure. A marker is a claim; these are the ones not made."""

    def test_no_position_means_no_marker(self):
        assert osint.read_events(
            '{"events": [{"kind": "drone", "place": "somewhere in the east"}]}') == []

    def test_null_island_is_where_a_model_puts_a_shrug(self):
        assert osint.read_events(
            '{"events": [{"kind": "drone", "lat": 0, "lon": 0}]}') == []

    def test_a_position_off_the_globe_is_refused(self):
        assert osint.read_events(
            '{"events": [{"kind": "drone", "lat": 91, "lon": 34}]}') == []
        assert osint.read_events(
            '{"events": [{"kind": "drone", "lat": 47, "lon": 200}]}') == []

    def test_coordinates_as_words_are_refused(self):
        # "lat": "47.6 north" is a thing models do, and float() on it would
        # either throw at request time or, worse, half-work.
        assert osint.read_events(
            '{"events": [{"kind": "drone", "lat": "47.6", "lon": "34.4"}]}') == []

    def test_a_kind_nobody_offered_becomes_unknown_rather_than_a_gap(self):
        got = osint.read_events(
            '{"events": [{"kind": "hypersonic doom ray", "lat": 47.6, "lon": 34.4}]}')
        assert got[0]["kind"] == "unknown"
        assert got[0]["kind"] in osint.KINDS

    def test_a_heading_is_a_compass_bearing_or_nothing(self):
        wrapped = osint.read_events(
            '{"events": [{"kind": "drone", "lat": 47.6, "lon": 34.4, "heading": 380}]}')
        assert wrapped[0]["heading"] == 20
        for junk in ('"north-east"', "null", "NaN"):
            got = osint.read_events(
                '{"events": [{"kind": "drone", "lat": 47.6, "lon": 34.4, '
                f'"heading": {junk}}}]}}'.replace("NaN", "1e999"))
            assert got[0]["heading"] is None or 0 <= got[0]["heading"] < 360

    def test_rubbish_among_good_events_does_not_take_them_with_it(self):
        got = osint.read_events(json.dumps({"events": [
            "not a dict",
            {"kind": "drone", "lat": 0, "lon": 0},
            {"kind": "drone", "lat": 47.6, "lon": 34.4},
        ]}))
        assert len(got) == 1


class TestCarryingAMarkerForward:
    def test_due_north_changes_only_the_latitude(self):
        lat, lon = osint.advance(50.0, 30.0, 0, 111.195)
        assert lat == pytest.approx(51.0, abs=0.01)
        assert lon == pytest.approx(30.0, abs=1e-9)

    def test_due_east_at_latitude_is_not_the_flat_earth_answer(self):
        # 111 km east at 50 N is about 1.56 degrees of longitude, not 1.0.
        # A flat-earth advance would give 1.0 and put a Shahed in the wrong
        # oblast within the hour, which is the bug this test exists for.
        _, lon = osint.advance(50.0, 30.0, 90, 111.195)
        assert lon == pytest.approx(31.556, abs=0.01)

    def test_going_nowhere_stays_put(self):
        assert osint.advance(50.0, 30.0, 90, 0) == (50.0, 30.0)

    def test_crossing_the_date_line_comes_out_the_other_side(self):
        _, lon = osint.advance(0.0, 179.5, 90, 200)
        assert -180 <= lon <= 180
        assert lon < 0

    def test_a_projected_event_keeps_the_position_that_was_reported(self):
        now = time.time()
        event = {"kind": "cruise", "heading": 90.0, "seen": now - 600,
                 "origin_lat": 50.0, "origin_lon": 30.0, "lat": 50.0, "lon": 30.0}
        got = osint.project(event, now)
        assert got["projected"] is True
        assert got["origin_lat"] == 50.0 and got["origin_lon"] == 30.0
        # Ten minutes of a cruise missile is about 133 km, which is east of
        # where it was reported and nowhere near it.
        assert got["lon"] > 31.5
        assert got["projected_km"] == pytest.approx(133.3, abs=1)

    def test_a_report_with_no_heading_does_not_move(self):
        now = time.time()
        event = {"kind": "drone", "heading": None, "seen": now - 900,
                 "origin_lat": 50.0, "origin_lon": 30.0, "lat": 50.0, "lon": 30.0}
        got = osint.project(event, now)
        assert (got["lat"], got["lon"]) == (50.0, 30.0)
        assert got["projected"] is False

    def test_a_ballistic_track_is_not_extrapolated(self):
        # It has a heading, but no useful constant speed: a marker sliding at
        # a made-up figure would be worse than one that stays where it was
        # reported, because it would look like it was being followed.
        now = time.time()
        event = {"kind": "ballistic", "heading": 90.0, "seen": now - 300,
                 "origin_lat": 50.0, "origin_lon": 30.0, "lat": 50.0, "lon": 30.0}
        got = osint.project(event, now)
        assert (got["lat"], got["lon"]) == (50.0, 30.0)
        assert got["projected"] is False

    def test_every_kind_has_a_speed_and_a_look(self):
        assert set(osint.SPEEDS) == set(osint.KINDS)
        for look in osint.KINDS.values():
            assert look["colour"].startswith("#") and look["label"]


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
        assert set(got) >= {"events", "count", "state", "keep_minutes", "speeds",
                            "kinds", "channels", "model", "keyed"}
        for event in got["events"]:
            assert set(event) >= {"id", "kind", "lat", "lon", "origin_lat",
                                  "origin_lon", "heading", "seen"}
            assert event["kind"] in osint.KINDS
            assert math.isfinite(event["lat"]) and math.isfinite(event["lon"])

    def test_the_demo_shows_both_a_moving_and_a_still_marker(self):
        # Otherwise the build with no network exercises one drawing path and
        # hides whatever the other one does.
        kinds = {e["heading"] is None for e in osint.demo()["events"]}
        assert kinds == {True, False}

    def test_the_ids_are_the_form_the_labels_expect(self):
        for event in osint.demo()["events"]:
            assert event["id"].startswith("AO#")
