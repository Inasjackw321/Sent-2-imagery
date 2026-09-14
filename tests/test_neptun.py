"""Tests for reading NEPTUN's feed.

Written against the payloads in their developer documentation verbatim, which
is the only thing available to check against: this environment cannot reach
neptun.in.ua, so nothing here has been run against the live service. That is
worth saying plainly rather than leaving to be discovered -- these tests hold
that the reader matches the documented contract, not that the contract matches
what the server sends today.

Most of what is asserted is about NOT overstating things. Their documentation
asks for three specific restraints and every one of them is a way for a map to
lie confidently, so each gets its own test.
"""

from __future__ import annotations

import datetime as dt
import time

import pytest

from backend import neptun, places, tracker

# Their example, copied from the developer page.
ONE_THREAT = {
    "id": "trk_1a2b",
    "type": "uav",
    "title": "Шахед",
    "region": "Одеська область",
    "district": "Одеський район",
    "locality": "Чорноморськ",
    "lat": 46.30, "lon": 30.65,
    "heading": 42,
    "confidenceLevel": "high",
    "sourceCount": 3,
    "count": 2,
    "updatedAt": "2026-07-09T12:34:50.000Z",
    "status": "active",
    "explanationShort": "БпЛА курсом на Чорноморськ",
    "velocity": {"bearingDeg": 42, "speedKmh": 150},
    "confirmedAt": "2026-07-09T12:34:20.000Z",
    "uncertaintyKm": 4,
    "positionQuality": "confirmed",
    "advisory": False,
    "areaOnly": False,
}

SNAPSHOT = {"serverTime": "2026-07-09T12:34:56.000Z", "threats": [ONE_THREAT]}

DECLARED = {
    "raions": [{"key": "odeska:odeskyi", "name": "Одеський район",
                "oblast": "Одеська область", "since": "2026-07-09T12:00:00Z"}],
    "oblasts": [{"key": "krymska", "name": "АР Крим", "oblast": "",
                 "since": "2026-07-09T11:00:00Z"}],
}


def area_only(**over):
    """Their areaOnly case: a region named, and no point."""
    return {"id": "trk_area", "type": "missile", "region": "Одеська область",
            "lat": 46.6, "lon": 30.2, "status": "active", "areaOnly": True,
            "positionQuality": "approx", **over}


class TestReadingOneTrack:
    def test_their_example_comes_out_whole(self):
        got = neptun.read_threat(ONE_THREAT)
        assert got["kind"] == "drone"
        assert (got["lat"], got["lon"]) == (46.30, 30.65)
        assert got["heading"] == 42.0
        assert got["count"] == 2
        assert got["place"] == "Чорноморськ"
        assert got["region"] == "Одеська область"
        assert got["id"] == "trk_1a2b"

    def test_the_finest_place_named_is_the_one_used(self):
        # locality beats district beats region. A track over Chornomorsk is a
        # mark on Chornomorsk, not one in the middle of Odesa oblast.
        assert neptun.read_threat(ONE_THREAT)["place"] == "Чорноморськ"
        no_town = {**ONE_THREAT, "locality": None}
        assert neptun.read_threat(no_town)["place"] == "Одеський район"
        neither = {**ONE_THREAT, "locality": None, "district": None}
        assert neptun.read_threat(neither)["place"] == "Одеська область"

    def test_a_course_is_taken_from_velocity_when_heading_is_absent(self):
        moving = {**ONE_THREAT, "heading": None}
        assert neptun.read_threat(moving)["heading"] == 42.0

    def test_a_track_with_no_course_anywhere_has_none(self):
        # Their heading is explicitly nullable. An arrow here would point
        # north and mean it.
        still = {**ONE_THREAT, "heading": None, "velocity": None}
        assert neptun.read_threat(still)["heading"] is None

    def test_a_resolved_track_is_not_drawn(self):
        # They have said it has ended. Drawing it would say something is in
        # the air that they have said is not.
        assert neptun.read_threat({**ONE_THREAT, "status": "resolved"}) is None

    def test_a_stale_track_still_is(self):
        # "stale" is a track that has not been updated lately, not one that
        # has ended -- and dropping those would empty the map every lull.
        assert neptun.read_threat({**ONE_THREAT, "status": "stale"}) is not None

    def test_a_track_with_no_usable_position_is_not_drawn(self):
        for broken in ({**ONE_THREAT, "lat": None},
                       {**ONE_THREAT, "lon": "nonsense"},
                       {**ONE_THREAT, "lat": 999},
                       {**ONE_THREAT, "lon": -400}):
            assert neptun.read_threat(broken) is None

    def test_rubbish_is_refused_rather_than_guessed_at(self):
        for junk in (None, "a string", 42, [], {}):
            assert neptun.read_threat(junk) is None


class TestAreaOnlyIsNotAPlace:
    """"There is no dot." Their words, and the sharpest thing in their docs.

    lat/lon on such a track is the CENTROID of a province -- a point nobody
    reported, in the middle of an area. They ask that it not be drawn as a
    place, without extrapolation, without a course and without a distance,
    "otherwise you will show a person a made-up number as a fact".

    Every clause of that gets a test, because each one is a different way for
    a map to say something confident and untrue.
    """

    def test_it_is_marked_as_an_area(self):
        assert neptun.read_threat(area_only())["area_only"] is True
        assert neptun.read_threat(ONE_THREAT)["area_only"] is False

    def test_it_carries_no_course_even_when_one_is_in_the_payload(self):
        # Their contract says heading is absent for these. Stripped here
        # anyway rather than trusted, because the cost of being wrong is an
        # arrow claiming a direction for a mark that has no position.
        got = neptun.read_threat(area_only(heading=90,
                                           velocity={"bearingDeg": 90,
                                                     "speedKmh": 800}))
        assert got["heading"] is None

    def test_the_place_it_names_is_the_region(self):
        got = neptun.read_threat(area_only())
        assert got["place"] == "Одеська область"
        assert got["region"] == "Одеська область"

    def test_a_finer_name_in_the_payload_is_not_used_for_one(self):
        # Their contract says locality and district are absent here. If one
        # arrives anyway it is not evidence of a place: the position is still
        # a centroid, and using the name would put a province's mark on a town.
        got = neptun.read_threat(area_only(locality="Чорноморськ"))
        assert got["place"] == "Одеська область"

    def test_the_map_draws_it_as_the_region_and_not_as_a_point(self, monkeypatch):
        monkeypatch.setattr(neptun, "_get", lambda url, **kw: SNAPSHOT
                            if "threats" in url else {"raions": [], "oblasts": []})
        tracker.reset()
        monkeypatch.setattr(neptun, "threats",
                            lambda: [neptun.read_threat(area_only())])
        monkeypatch.setattr(neptun, "alerts", lambda: [])
        tracker.take_neptun()
        got = tracker.current()["events"][0]
        assert got["region_scope"] == "located"
        assert got["heading"] is None and got["course"] is None
        assert got["dest_km"] is None

    def test_and_never_a_distance_to_anywhere(self, monkeypatch):
        # "how many kilometres to me" is the number they single out. Nothing
        # here computes one for any NEPTUN track, area or not.
        monkeypatch.setattr(neptun, "threats",
                            lambda: [neptun.read_threat(ONE_THREAT),
                                     neptun.read_threat(area_only())])
        monkeypatch.setattr(neptun, "alerts", lambda: [])
        tracker.reset()
        tracker.take_neptun()
        for event in tracker.current()["events"]:
            assert event["dest_km"] is None, event["id"]
            assert event["toward"] is None


class TestAdvisoryIsNotAnAlarm:
    """"Surveillance, not a signal to hide."

    A MiG-31K taking off is worth recording and is not a reason to take
    cover. Drawing it like one teaches people to ignore the signal that
    matters, which is their argument and it is a good one.
    """

    def test_it_is_marked(self):
        got = neptun.read_threat({**ONE_THREAT, "type": "mig31k",
                                  "advisory": True})
        assert got["advisory"] is True
        assert got["kind"] == "aircraft"

    def test_an_absent_field_means_it_is_not_advisory(self):
        # Their docs: the field can be missing and equals false.
        bare = {k: v for k, v in ONE_THREAT.items() if k != "advisory"}
        assert neptun.read_threat(bare)["advisory"] is False

    def test_it_reaches_the_map_marked(self, monkeypatch):
        monkeypatch.setattr(neptun, "threats", lambda: [
            neptun.read_threat({**ONE_THREAT, "type": "mig31k",
                                "advisory": True})])
        monkeypatch.setattr(neptun, "alerts", lambda: [])
        tracker.reset()
        tracker.take_neptun()
        assert tracker.current()["events"][0]["advisory"] is True


class TestTheirTypesOntoThisMapsKinds:
    def test_every_type_they_document_lands_somewhere_real(self):
        for theirs in ("uav", "recon", "missile", "ballistic", "kab",
                       "mig31k", "unknown"):
            ours = neptun.KINDS[theirs]
            assert ours in tracker.KINDS, f"{theirs} -> {ours}"

    def test_a_guided_bomb_is_not_a_missile(self):
        # A KAB is released near the line and glides tens of kilometres.
        # Folding it into "missile" would draw a hundreds-of-kilometres
        # weapon where a tens-of-kilometres one was reported, which is the
        # wrong answer to "how long have I got".
        assert neptun.KINDS["kab"] == "bomb"
        assert neptun.KINDS["kab"] != neptun.KINDS["missile"]

    def test_a_type_they_add_later_is_not_invented(self):
        # A kind this map does not have must not become a confident mark of
        # the wrong sort.
        got = neptun.read_threat({**ONE_THREAT, "type": "something_new"})
        assert got["kind"] == "unknown"


class TestTheAlerts:
    def test_their_example_comes_out_whole(self):
        got = neptun.read_alerts(DECLARED)
        assert [a["name"] for a in got] == ["АР Крим", "Одеський район"]
        assert got[0]["scope"] == "oblast"
        assert got[1]["scope"] == "raion"

    def test_oblasts_come_first(self):
        # Where a province and one of its raions are both under alert, the
        # province is the bigger statement and the one worth drawing.
        got = neptun.read_alerts(DECLARED)
        assert got[0]["scope"] == "oblast"

    def test_the_key_is_kept(self):
        # It indexes into their boundary files, which is how a warning gets a
        # real outline without a geocoding request per province.
        assert neptun.read_alerts(DECLARED)[0]["key"] == "krymska"

    def test_rubbish_is_skipped_rather_than_drawn(self):
        got = neptun.read_alerts({"oblasts": ["not an object", {}, None,
                                              {"name": "Сумська область"}],
                                  "raions": "not a list"})
        assert [a["name"] for a in got] == ["Сумська область"]

    def test_nothing_at_all_is_no_alerts(self):
        assert neptun.read_alerts(None) == []
        assert neptun.read_alerts({}) == []


class TestBoundaries:
    GEOJSON = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature",
             "properties": {"key": "sumska", "name": "Сумська область"},
             "geometry": {"type": "Polygon",
                          "coordinates": [[[33, 50], [35, 50], [35, 52], [33, 50]]]}},
            {"type": "Feature", "properties": {"key": "broken"},
             "geometry": None},
        ],
    }

    def test_it_indexes_by_key_and_by_name(self):
        got = neptun.index_shapes(self.GEOJSON)
        assert "sumska" in got
        assert "сумська область" in got

    def test_a_feature_with_no_geometry_is_skipped(self):
        assert "broken" not in neptun.index_shapes(self.GEOJSON)

    def test_rubbish_is_no_shapes(self):
        assert neptun.index_shapes(None) == {}
        assert neptun.index_shapes({"features": "nope"}) == {}

    def test_a_missing_boundary_is_not_an_error(self, monkeypatch):
        # It degrades to what the app did without this module at all: the
        # warning is drawn from its extent instead. An exception here would
        # take a poll down over a decoration.
        monkeypatch.setattr(neptun, "_get", lambda url, **kw: (_ for _ in ()).throw(
            neptun.NeptunError("down")))
        neptun.forget()
        assert neptun.shapes() == {}
        assert neptun.shape_for("Сумська область") is None


class TestBeingPolite:
    """Their one operational request: no more than a poll every five seconds.

    Honoured with a longer floor, and by SKIPPING rather than waiting -- the
    difference matters. A blocking rate limit inside a poll loop holds up four
    channel fetches for a snapshot that is CDN-cached anyway.
    """

    def test_the_floor_is_at_least_what_they_ask_for(self):
        assert neptun.MIN_INTERVAL >= 5.0

    def test_a_second_call_inside_the_interval_is_skipped_not_delayed(self):
        neptun.forget()
        assert neptun.claim_turn(neptun.THREATS) is True
        began = time.time()
        assert neptun.claim_turn(neptun.THREATS) is False
        assert time.time() - began < 0.5, "it waited instead of skipping"

    def test_a_skip_is_its_own_kind_of_answer(self):
        # So the caller can tell "too soon, keep what you have" from "it is
        # down, say so" -- which are different things to show a reader.
        assert issubclass(neptun.TooSoon, neptun.NeptunError)

    def test_a_skipped_poll_keeps_the_marks_it_already_had(self, monkeypatch):
        monkeypatch.setattr(neptun, "threats",
                            lambda: [neptun.read_threat(ONE_THREAT)])
        monkeypatch.setattr(neptun, "alerts", lambda: [])
        tracker.reset()
        neptun.forget()
        tracker.take_neptun()
        before = len(tracker.current()["events"])
        assert before == 1

        # Now with the interval genuinely just used, so the next ask is too
        # soon. Checked on _get itself rather than through threats(), which
        # this test has already replaced with a stub -- going through the stub
        # would have tested the stub.
        assert neptun.claim_turn(neptun.THREATS) is True
        # The real _get, which the suite otherwise stubs out so no test
        # reaches the network. The pacing being checked lives inside it, and
        # checking it through the stub would be checking the stub.
        with pytest.raises(neptun.TooSoon):
            neptun.unstubbed_get(neptun.THREATS)
        # And the marks that were already there are still there: a skip is not
        # a failure and must not empty anything.
        assert len(tracker.current()["events"]) == before


class TestAttribution:
    """Their only condition of use, and it is not decoration.

    "The API is free and open. The only requirement is a visible link to
    NEPTUN next to the map/data."
    """

    def test_the_link_is_carried_with_the_data(self):
        assert neptun.ATTRIBUTION["url"].startswith("https://neptun.in.ua")
        assert "NEPTUN" in neptun.ATTRIBUTION["text"]

    def test_the_feed_hands_it_to_the_page(self):
        assert tracker.current()["attribution"]["url"] == neptun.ATTRIBUTION["url"]

    def test_the_demo_carries_it_too(self):
        # The offline build shows NEPTUN-shaped marks, so it shows the credit.
        assert tracker.demo()["attribution"]["url"] == neptun.ATTRIBUTION["url"]

    def test_the_safety_caveat_is_carried_as_well(self):
        # Theirs: an aggregator, not an official alert system. A map that
        # people might use to decide whether to take cover has to say so.
        said = neptun.ATTRIBUTION["caveat"].lower()
        assert "not an official" in said
        assert "official alerts" in said


class TestOnTheMap:
    def setup_method(self):
        tracker.reset()

    def teardown_method(self):
        tracker.reset()

    def feed(self, monkeypatch, tracks=(), declared=()):
        monkeypatch.setattr(neptun, "threats", lambda: list(tracks))
        monkeypatch.setattr(neptun, "alerts", lambda: list(declared))
        tracker.take_neptun()
        return tracker.current()

    def test_a_track_arrives_already_placed(self, monkeypatch):
        # The whole point of a second source: nothing here goes near the text
        # reader or the gazetteer, so it cannot fail the way they fail.
        monkeypatch.setattr(tracker.gazetteer, "find", lambda *a, **k: (
            _ for _ in ()).throw(AssertionError("must not geocode")))
        got = self.feed(monkeypatch, [neptun.read_threat(ONE_THREAT)])
        assert len(got["events"]) == 1
        assert got["events"][0]["placed"] is True
        assert (got["events"][0]["lat"], got["events"][0]["lon"]) == (46.30, 30.65)

    def test_a_moved_track_replaces_its_own_mark(self, monkeypatch):
        """Their ids are stable across updates, and their snapshot is whole.

        Both matter. Without the id a moving track leaves a trail of stale
        marks; without treating the snapshot as the whole state, a track that
        has ended stays on the map forever.
        """
        self.feed(monkeypatch, [neptun.read_threat(ONE_THREAT)])
        moved = neptun.read_threat({**ONE_THREAT, "lat": 47.0, "lon": 31.0})
        got = self.feed(monkeypatch, [moved])
        assert len(got["events"]) == 1
        assert got["events"][0]["lat"] == 47.0

    def test_a_track_that_has_gone_from_the_snapshot_goes_from_the_map(
            self, monkeypatch):
        self.feed(monkeypatch, [neptun.read_threat(ONE_THREAT)])
        assert self.feed(monkeypatch, [])["events"] == []

    def test_it_does_not_take_the_channels_marks_with_it(self, monkeypatch):
        # Two sources sharing one list. Clearing by source rather than
        # wholesale, or a NEPTUN refresh would wipe the channel reports.
        monkeypatch.setattr(tracker.gazetteer, "find",
                            lambda name, countries="": places.lookup(name))
        item = tracker._clean({"kind": "drone", "place": "Суми", "id": "p/1",
                               "count": 1, "summary": "from a channel",
                               "region": None, "toward": None, "course": None,
                               "cause": None})
        tracker._record(item, {"id": "p/1", "channel": "x",
                               "region": "Ukraine"}, "ua")
        got = self.feed(monkeypatch, [neptun.read_threat(ONE_THREAT)])
        assert len(got["events"]) == 2
        self.feed(monkeypatch, [])
        left = tracker.current()["events"]
        assert [e["place"] for e in left] == ["Суми"]

    def test_an_official_alert_is_drawn_over_its_region(self, monkeypatch):
        got = self.feed(monkeypatch, [], neptun.read_alerts(
            {"oblasts": [{"key": "sumska", "name": "Сумська область"}],
             "raions": []}))
        drawn = [e for e in got["events"] if e["kind"] == "alert"]
        assert len(drawn) == 1
        assert drawn[0]["place"] == "Сумська область"

    def test_an_alert_for_a_region_this_map_cannot_place_is_still_listed(
            self, monkeypatch):
        # Visible rather than silently dropped, which is the same bargain the
        # gazetteer makes everywhere else in this app.
        got = self.feed(monkeypatch, [], neptun.read_alerts(
            {"oblasts": [{"key": "nowhere", "name": "Зззз область"}],
             "raions": []}))
        assert not [e for e in got["events"] if e["kind"] == "alert"]
        row = [a for a in got["alerts"] if a["place"] == "Зззз область"]
        assert row and row[0]["placed"] is False
        assert "not a region this map knows" in row[0]["why_unplaced"]

    def test_its_marks_can_be_taken_off_by_hand_like_any_other(self, monkeypatch):
        got = self.feed(monkeypatch, [neptun.read_threat(ONE_THREAT)])
        tracker.dismiss(got["events"][0]["id"])
        # And stay off across a refresh, which for this source means a whole
        # new snapshot rather than a re-read of one post.
        again = self.feed(monkeypatch, [neptun.read_threat(ONE_THREAT)])
        assert again["events"] == []

    def test_the_panel_gets_a_row_for_the_feed(self, monkeypatch):
        monkeypatch.setattr(neptun, "threats",
                            lambda: [neptun.read_threat(ONE_THREAT)])
        monkeypatch.setattr(neptun, "alerts", lambda: [])
        monkeypatch.setattr(tracker, "_fetch_channel", lambda channel: [])
        tracker.poll()
        rows = {s["channel"]: s for s in tracker.current()["sources"]}
        assert tracker.NEPTUN_SOURCE in rows
        assert rows[tracker.NEPTUN_SOURCE]["placed"] == 1

    def test_the_feed_being_down_does_not_take_the_poll_with_it(
            self, monkeypatch):
        """The reason a second source must not be able to break the first.

        The channels are fine; if a failure here emptied the map or raised,
        adding NEPTUN would have made the app less reliable rather than more.
        """
        monkeypatch.setattr(neptun, "threats", lambda: (_ for _ in ()).throw(
            neptun.NeptunError("NEPTUN answered 503")))
        monkeypatch.setattr(tracker, "_fetch_channel", lambda channel: [])
        got = tracker.poll()
        assert "503" in got["state"]
        rows = {s["channel"]: s for s in got["sources"]}
        assert rows[tracker.NEPTUN_SOURCE]["problem"]

    def test_a_failure_does_not_clear_what_was_already_drawn(self, monkeypatch):
        # Clearing first and then failing would empty the map on a blip: the
        # other source is fine and the screen goes blank anyway.
        self.feed(monkeypatch, [neptun.read_threat(ONE_THREAT)])
        monkeypatch.setattr(neptun, "threats", lambda: (_ for _ in ()).throw(
            neptun.NeptunError("down")))
        monkeypatch.setattr(tracker, "_fetch_channel", lambda channel: [])
        tracker.poll()
        assert len(tracker.current()["events"]) == 1


class TestItFitsTheVocabularyItJoined:
    """A second source has to speak the map's language, not its own.

    Each of these was got wrong first time, and each failed the same way: a
    value this map does not use falls through to the wrong drawing rather than
    to an error, so it looks fine and says something untrue.
    """

    def test_a_stated_course_draws_a_solid_arrow(self, monkeypatch):
        # course_from is a closed vocabulary and a fifth value falls through
        # to the HOLLOW arrow -- which means "this app inferred it from the
        # neighbours". Marking somebody else's stated course that way is the
        # wrong claim about where the information came from.
        monkeypatch.setattr(neptun, "threats",
                            lambda: [neptun.read_threat(ONE_THREAT)])
        monkeypatch.setattr(neptun, "alerts", lambda: [])
        tracker.reset()
        tracker.take_neptun()
        got = tracker.current()["events"][0]
        assert got["course_from"] == "stated"
        assert got["course_from"] in ("stated", "destination", "group")

    def test_every_mark_has_a_size(self, monkeypatch):
        # area_km is what sizes a mark, and every other path guarantees one.
        # A None here left a mark with no size at all.
        monkeypatch.setattr(neptun, "threats", lambda: [
            neptun.read_threat(ONE_THREAT),
            neptun.read_threat(area_only()),
            neptun.read_threat({**ONE_THREAT, "id": "t2", "uncertaintyKm": None}),
        ])
        monkeypatch.setattr(neptun, "alerts", lambda: [])
        tracker.reset()
        tracker.take_neptun()
        for event in tracker.current()["events"]:
            assert event["area_km"] and event["area_km"] > 0, event["id"]

    def test_their_uncertainty_is_used_where_they_give_one(self, monkeypatch):
        # A better number than any default: it is how far out they think the
        # position is.
        monkeypatch.setattr(neptun, "threats", lambda: [
            neptun.read_threat({**ONE_THREAT, "uncertaintyKm": 25})])
        monkeypatch.setattr(neptun, "alerts", lambda: [])
        tracker.reset()
        tracker.take_neptun()
        assert tracker.current()["events"][0]["area_km"] == 25

    def test_its_kinds_are_ones_the_map_draws(self, monkeypatch):
        monkeypatch.setattr(neptun, "threats", lambda: [
            neptun.read_threat({**ONE_THREAT, "id": f"t{i}", "type": theirs})
            for i, theirs in enumerate(neptun.KINDS)])
        monkeypatch.setattr(neptun, "alerts", lambda: [])
        tracker.reset()
        tracker.take_neptun()
        for event in tracker.current()["events"]:
            assert event["kind"] in tracker.KINDS, event["kind"]


class TestTheDemoShowsTheFeed:
    """Or the states it is easiest to draw wrongly go unexamined.

    An advisory drawn as an alarm and a region centroid drawn as a place are
    both invisible in a payload and obvious on a screen -- if the screen can
    be made to show them without a network. This is the sixth state the
    offline build has had to be taught.
    """

    def test_the_demo_has_neptun_marks(self):
        got = [e for e in tracker.demo()["events"] if e.get("by") == "neptun"]
        assert got

    def test_it_shows_an_advisory_and_an_area_only_one(self):
        got = [e for e in tracker.demo()["events"] if e.get("by") == "neptun"]
        assert any(e.get("advisory") for e in got), "no advisory to look at"
        assert any(e.get("area_only") for e in got), "no areaOnly to look at"

    def test_it_shows_a_guided_bomb(self):
        # The kind that joined with this feed, and the one with no other way
        # of reaching the offline build.
        kinds = {e["kind"] for e in tracker.demo()["events"]}
        assert "bomb" in kinds

    def test_they_age_like_everything_else(self):
        # They were appended without the expiry check every other demo mark
        # goes through, so they sat there through the whole cycle while the
        # rest came and went -- which made the one thing the demo exists to
        # show false for a quarter of what it drew.
        tracker._demo_epoch = 0.0
        try:
            tracker.demo()
            tracker._demo_epoch -= tracker.DEMO_CYCLE - 1
            late = [e for e in tracker.demo()["events"]
                    if e.get("by") == "neptun"
                    and e["kind"] not in tracker.NOT_AIRBORNE]
            assert late == [], [e["kind"] for e in late]
        finally:
            tracker._demo_epoch = 0.0

    def test_a_row_is_not_left_behind_by_an_expired_mark(self):
        # The panel's rule everywhere else: no row for a mark that is gone,
        # and no mark without a row.
        got = tracker.demo()
        drawn = {e["id"] for e in got["events"] if e.get("by") == "neptun"}
        rows = {a["id"] for a in got["alerts"] if a.get("by") == "neptun"}
        assert rows == drawn


class TestTheTrail:
    """Where a track has BEEN, which is not where it is going.

    Every point is a position their feed gave at a time it gave it. Nothing is
    interpolated between them and nothing is extended past the last one --
    which is the whole difference between a trail and a predicted path, and
    this app only has grounds to draw the first.
    """

    def setup_method(self):
        tracker.reset()

    def teardown_method(self):
        tracker.reset()

    def fly(self, monkeypatch, *lons, ident="t1"):
        for lon in lons:
            track = neptun.read_threat({**ONE_THREAT, "id": ident, "lon": lon})
            monkeypatch.setattr(neptun, "threats", lambda t=track: [t])
            monkeypatch.setattr(neptun, "alerts", lambda: [])
            tracker.take_neptun()
        return tracker.current()["events"][0]

    def test_it_collects_the_positions_that_were_reported(self, monkeypatch):
        got = self.fly(monkeypatch, 30.0, 30.5, 31.0)
        assert [round(p[1], 1) for p in got["trail"]] == [30.0, 30.5, 31.0]

    def test_the_same_position_twice_is_not_two_points(self, monkeypatch):
        # Their snapshot is polled far more often than a track actually moves,
        # so without this a stationary track collects twenty identical points
        # and draws nothing at all.
        got = self.fly(monkeypatch, 30.0, 30.0, 30.0, 30.5)
        assert len(got["trail"]) == 2

    def test_it_is_bounded(self, monkeypatch):
        got = self.fly(monkeypatch, *[30.0 + i * 0.2 for i in range(40)])
        assert len(got["trail"]) <= tracker.TRAIL_POINTS

    def test_an_area_only_track_gets_none(self, monkeypatch):
        """Its positions are province centroids, not places.

        A line joining two of those would be a flight between two
        middles-of-nowhere, drawn as though something had flown it.
        """
        monkeypatch.setattr(neptun, "threats",
                            lambda: [neptun.read_threat(area_only())])
        monkeypatch.setattr(neptun, "alerts", lambda: [])
        tracker.take_neptun()
        tracker.take_neptun()
        assert tracker.current()["events"][0]["trail"] == []

    def test_a_track_that_ends_takes_its_trail_with_it(self, monkeypatch):
        # Otherwise a line hangs on the map with nothing at the end of it.
        self.fly(monkeypatch, 30.0, 30.5)
        assert tracker._trails
        monkeypatch.setattr(neptun, "threats", lambda: [])
        monkeypatch.setattr(neptun, "alerts", lambda: [])
        tracker.take_neptun()
        assert not tracker._trails

    def test_two_tracks_keep_their_own(self, monkeypatch):
        first = neptun.read_threat({**ONE_THREAT, "id": "a", "lon": 30.0})
        second = neptun.read_threat({**ONE_THREAT, "id": "b", "lon": 35.0})
        monkeypatch.setattr(neptun, "threats", lambda: [first, second])
        monkeypatch.setattr(neptun, "alerts", lambda: [])
        tracker.take_neptun()
        moved = neptun.read_threat({**ONE_THREAT, "id": "a", "lon": 30.5})
        monkeypatch.setattr(neptun, "threats", lambda: [moved, second])
        tracker.take_neptun()
        # Keyed by id: both tracks are over the same town in their example,
        # so keying by place collapsed them and the test compared one trail
        # with itself.
        trails = {e["id"]: e["trail"] for e in tracker.current()["events"]}
        assert len(trails) == 2
        assert sorted(len(v) for v in trails.values()) == [1, 2]

    def test_the_demo_has_one_to_look_at(self):
        # Walked backwards along the track's own stated course, which is the
        # one place in this app that makes up a position -- and it is the
        # demo, whose contract is that its reports are invented.
        got = [e for e in tracker.demo()["events"]
               if e.get("by") == "neptun" and len(e.get("trail") or []) > 1]
        assert got, "the offline build cannot show a trail"

    def test_the_live_path_never_invents_one(self, monkeypatch):
        # ahead() exists for the demo. A trail on a real track is only ever
        # the positions the feed gave, so one poll is one point.
        monkeypatch.setattr(tracker, "ahead", lambda *a, **k: (
            _ for _ in ()).throw(AssertionError("the live path must not")))
        got = self.fly(monkeypatch, 30.0)
        assert len(got["trail"]) == 1


class TestTheirSpeed:
    def test_it_is_carried_and_credited(self, monkeypatch):
        # This map refuses to print a speed it worked out from "this is a
        # Shahed and Shaheds do about 180" -- that dresses an assumption up as
        # telemetry. A speed THEY report is a different claim.
        monkeypatch.setattr(neptun, "threats",
                            lambda: [neptun.read_threat(ONE_THREAT)])
        monkeypatch.setattr(neptun, "alerts", lambda: [])
        tracker.reset()
        tracker.take_neptun()
        assert tracker.current()["events"][0]["speed_kmh"] == 150.0

    def test_a_track_with_no_speed_claims_none(self):
        bare = {k: v for k, v in ONE_THREAT.items() if k != "velocity"}
        assert neptun.read_threat(bare)["speed_kmh"] is None


class TestCatchingUpOnTheLastHalfHour:
    """Their snapshot is the state NOW and has no history in it.

    Open the app at four in the morning and it shows what is in the air at
    four in the morning and nothing about the hour before. Their message feed
    carries times, so it is read once on a cold start for the recent past --
    through the same reader the Telegram channels go through, because it is
    the same kind of thing.
    """

    def setup_method(self):
        tracker.reset()

    def teardown_method(self):
        tracker.reset()

    def said(self, monkeypatch, *posts):
        import datetime as dt
        now = dt.datetime.now(dt.timezone.utc)
        monkeypatch.setattr(neptun, "_get", lambda url, **kw: {"messages": [
            {"channel": "kpszsu", "text": text,
             "date": (now - dt.timedelta(minutes=old)).isoformat()}
            for text, old in posts]})
        monkeypatch.setattr(tracker.gazetteer, "find",
                            lambda name, countries="": places.lookup(name))
        return tracker.catch_up()

    def test_recent_messages_become_marks(self, monkeypatch):
        made = self.said(monkeypatch, ("Вибухи у Харкові", 5),
                         ("Шахед над Нікополем курсом на північ", 10))
        assert made == 2
        assert len(tracker.current()["events"]) == 2

    def test_older_ones_are_left_alone(self, monkeypatch):
        stale = tracker.CATCH_UP_MINUTES + 20
        assert self.said(monkeypatch, ("Вибухи у Харкові", stale)) == 0

    def test_the_window_is_about_half_an_hour(self):
        assert 15 <= tracker.CATCH_UP_MINUTES <= 60

    def test_a_message_is_read_once(self, monkeypatch):
        self.said(monkeypatch, ("Вибухи у Харкові", 5))
        before = len(tracker.current()["events"])
        tracker.catch_up()
        assert len(tracker.current()["events"]) == before

    def test_it_is_credited_to_the_channel_that_wrote_it(self, monkeypatch):
        # Their feed carries the channel's own name. Crediting the aggregator
        # would lose which channel actually said it.
        self.said(monkeypatch, ("Вибухи у Харкові", 5))
        assert tracker.current()["alerts"][0]["channel"] == "kpszsu"

    def test_a_message_feed_that_is_down_is_not_a_failure(self, monkeypatch):
        # It is a convenience on start and the live sources are what matter.
        monkeypatch.setattr(neptun, "_get", lambda url, **kw: (
            _ for _ in ()).throw(neptun.NeptunError("down")))
        monkeypatch.setattr(tracker, "_fetch_channel", lambda channel: [])
        got = tracker.poll()
        assert got["events"] == []

    def test_rubbish_in_the_feed_is_skipped(self, monkeypatch):
        monkeypatch.setattr(neptun, "_get", lambda url, **kw: {"messages": [
            "not an object", {}, {"text": "no date"}, None]})
        assert tracker.catch_up() == 0


class TestWarningsCoverRealRegions:
    """A bounding box is a rectangle, and no province is shaped like one.

    NEPTUN publish the oblast and raion boundaries their own alert keys index
    into. Used for EVERY warning, not only their own: the shape of Sumy oblast
    does not depend on who mentioned it.
    """

    SHAPE = {"type": "Polygon",
             "coordinates": [[[33, 50], [35, 50], [35, 52], [33, 52], [33, 50]]]}

    def setup_method(self):
        tracker.reset()

    def teardown_method(self):
        tracker.reset()
        neptun.forget()

    def test_a_channel_read_warning_gets_their_outline(self, monkeypatch):
        monkeypatch.setattr(neptun, "shape_for",
                            lambda name: self.SHAPE if name and "Сумська" in str(name)
                            else None)
        monkeypatch.setattr(tracker.gazetteer, "find",
                            lambda name, countries="": places.lookup(name))
        item = tracker._clean({"kind": "alert", "place": "Сумська область",
                               "id": "c/1", "count": 1, "summary": "alert",
                               "region": None, "toward": None, "course": None,
                               "cause": "drone"})
        tracker._record(item, {"id": "c/1", "channel": "kpszsu",
                               "region": "Ukraine"}, "ua")
        got = tracker.current()["events"][0]
        assert got["shape"] == self.SHAPE
        assert got["region_wide"] is True

    def test_raion_boundaries_are_indexed_too(self, monkeypatch):
        # Their alert feed reports both raions and oblasts, so a warning for
        # either has to be drawable.
        asked = []
        monkeypatch.setattr(neptun, "_get", lambda url, **kw: (
            asked.append(url),
            {"features": [{"type": "Feature",
                           "properties": {"key": "k", "name": "n"},
                           "geometry": TestWarningsCoverRealRegions.SHAPE}]})[1])
        neptun.forget()
        neptun.shapes()
        assert any("oblasts" in url for url in asked)
        assert any("raions" in url for url in asked)

    def test_a_missing_raion_file_still_leaves_the_provinces(self, monkeypatch):
        def maybe(url, **kw):
            if "raions" in url:
                raise neptun.NeptunError("no raions today")
            return {"features": [{"type": "Feature",
                                  "properties": {"name": "Сумська область"},
                                  "geometry": TestWarningsCoverRealRegions.SHAPE}]}
        monkeypatch.setattr(neptun, "_get", maybe)
        neptun.forget()
        assert neptun.shape_for("Сумська область") == self.SHAPE




class TestEveryWarningIsARegion:
    """A warning is drawn as a region or it is not drawn as an area at all.

    It used to fall back to the region's EXTENT as a dotted rectangle, and a
    screen full of those is what "the map looks wrong" meant: ten dashed boxes
    in a country made of jagged borders, none of them the shape of anything,
    each covering ground the warning does not cover and missing ground it
    does. A rectangle is not a cautious version of a province -- it is a
    different and wrong claim about where a warning applies.
    """

    def test_a_warning_with_a_boundary_is_drawn_as_one(self):
        drawn = [e for e in tracker.demo()["events"]
                 if e["kind"] == "alert" and e.get("region_wide")]
        assert drawn
        assert all(e["shape"] for e in drawn)

    def test_no_warning_is_drawn_from_a_bounding_box(self):
        # The property, stated about every warning rather than sampled: if it
        # claims to cover a region it has the region's shape, and if it has no
        # shape it makes no claim about area.
        for event in tracker.demo()["events"]:
            if event["kind"] != "alert":
                continue
            assert bool(event.get("region_wide")) == bool(event.get("shape")), (
                event["place"])

    def test_neptuns_own_alerts_arrive_with_theirs(self, monkeypatch):
        shape = {"type": "Polygon",
                 "coordinates": [[[33, 50], [35, 50], [35, 52], [33, 50]]]}
        monkeypatch.setattr(neptun, "shape_for", lambda name: shape)
        monkeypatch.setattr(neptun, "threats", lambda: [])
        monkeypatch.setattr(neptun, "alerts", lambda: neptun.read_alerts(
            {"oblasts": [{"key": "sumska", "name": "Сумська область"}],
             "raions": []}))
        tracker.reset()
        tracker.take_neptun()
        got = tracker.current()["events"][0]
        assert got["shape"] == shape
        assert got["region_wide"] is True


class TestNothingRaisesAWarningByItself:
    """The feature that put a warning wherever there were drones is gone.

    It was asked for and then asked to be removed, and the removal is the
    better call: it was the only mark on this map that nobody reported. Every
    warning now comes from something that actually declared one -- NEPTUN's
    official alert feed, or a channel post saying so.
    """

    def test_no_event_claims_to_be_derived(self):
        assert not [e for e in tracker.demo()["events"] if e.get("derived")]

    def test_the_machinery_is_gone_rather_than_switched_off(self):
        # Left in and disabled, it would be dead code that looks live.
        assert not hasattr(tracker, "derived_alerts")
        assert not hasattr(tracker, "derived_row")
        assert not hasattr(tracker, "RAISES_A_WARNING")

    def test_drones_over_a_region_raise_nothing(self, monkeypatch):
        monkeypatch.setattr(tracker.gazetteer, "find",
                            lambda name, countries="": places.lookup(name))
        tracker.reset()
        for i, text in enumerate(["БпЛА над Житомирщиною",
                                  "Шахед над Львівською областю"]):
            for plain in tracker.reports.read_all(text):
                plain["kind"] = tracker.fold_kind(plain["kind"])
                tracker._record(tracker._clean({**plain, "id": f"c/{i}"}),
                                {"id": f"c/{i}", "channel": "x",
                                 "region": "Ukraine"}, "ua")
        got = tracker.current()["events"]
        assert len(got) == 2
        assert not [e for e in got if e["kind"] == "alert"]


class TestTheSourcesItReadsNow:
    def test_neptun_covers_ukraine(self):
        assert tracker.NEPTUN_SOURCE == "neptun.in.ua"

    def test_one_channel_is_left_and_it_is_the_russian_one(self):
        # NEPTUN carries the Ukrainian channels' reports already read and
        # already placed. What it does not cover is the Russian side, which
        # is the one thing this app would otherwise lose.
        assert [c["name"] for c in tracker.CHANNELS] == ["lpr1_treugolnik"]

    def test_it_looks_in_russia_first(self):
        # Its place names are in Russia and the occupied east. Ukraine first
        # would have a gazetteer answer a Donbas town with the pre-war
        # administrative name of somewhere else.
        assert tracker.CHANNELS[0]["countries"].split(",")[0] == "ru"

    def test_the_feed_has_a_row_in_the_panel(self):
        # "I cannot see anything from NEPTUN" has to be answerable from the
        # panel rather than by guessing, the same way it is for a channel.
        rows = {r["channel"] for r in tracker.demo()["sources"]}
        assert tracker.NEPTUN_SOURCE in rows


class TestTheDeadReckoningAnchor:
    """`confirmedAt` — the moment a position was last confirmed.

    It is what NEPTUN's own predict() reckons from: the position at
    confirmedAt, carried along velocity.bearingDeg at velocity.speedKmh for
    however long it has been since. Without it a mark can only sit still
    between snapshots and then jump.
    """

    def test_it_is_read_as_a_moment(self):
        got = neptun.read_threat(ONE_THREAT)
        # 2026-07-09T12:34:20Z, their example.
        assert got["confirmed_at"] == pytest.approx(
            dt.datetime(2026, 7, 9, 12, 34, 20,
                        tzinfo=dt.timezone.utc).timestamp())

    def test_a_stamp_with_no_offset_is_read_as_utc(self, monkeypatch):
        # This runs on a server whose timezone is an accident of deployment.
        # Reading their UTC as local time would put the anchor hours out and
        # fling the mark across the country.
        #
        # Run with the machine somewhere else, which is the whole point: the
        # test is worthless on a UTC box because a naive stamp read as local
        # time comes out right there by accident, and that is exactly the
        # accident a deployment elsewhere does not get.
        monkeypatch.setenv("TZ", "Asia/Tokyo")
        time.tzset()
        try:
            naive = {**ONE_THREAT, "confirmedAt": "2026-07-09T12:34:20"}
            assert neptun.read_threat(naive)["confirmed_at"] == pytest.approx(
                dt.datetime(2026, 7, 9, 12, 34, 20,
                            tzinfo=dt.timezone.utc).timestamp())
        finally:
            monkeypatch.undo()
            time.tzset()

    def test_nonsense_is_no_anchor_rather_than_a_crash(self):
        for bad in ("", None, "yesterday", 12345, {"at": 1}):
            assert neptun.read_threat(
                {**ONE_THREAT, "confirmedAt": bad})["confirmed_at"] is None

    def test_updated_at_is_not_used_as_the_anchor(self):
        # updatedAt moves for reasons that have nothing to do with the thing
        # having moved. Falling back to it would restart the reckoning every
        # time a record was touched.
        no_anchor = dict(ONE_THREAT)
        no_anchor.pop("confirmedAt")
        got = neptun.read_threat(no_anchor)
        assert got["confirmed_at"] is None
        assert got["updated_at"] == ONE_THREAT["updatedAt"]

    def test_an_area_only_track_never_gets_one(self):
        # "Without extrapolation." Their rule for these, kept by the shape of
        # the data rather than by a condition somebody can forget: no anchor
        # means nothing downstream can carry it anywhere.
        stamped = area_only(confirmedAt=ONE_THREAT["confirmedAt"],
                            velocity={"bearingDeg": 90, "speedKmh": 700})
        assert neptun.read_threat(stamped)["confirmed_at"] is None


class TestMarksThatMove:
    """What the map is told about carrying a mark along.

    The position sent is always the reported one. What goes with it is
    `drift_minutes`: how long ago that position was confirmed. The map adds
    its own elapsed time to that and does the arithmetic, so the two clocks
    never have to agree.
    """

    def _event(self, **over):
        track = neptun.read_threat({**ONE_THREAT, **over})
        tracker.reset()
        tracker._record_neptun(track, time.time())
        return next(e for e in tracker._events if e.get("by") == "neptun")

    def test_a_moving_track_says_how_long_since_it_was_confirmed(self):
        now = time.time()
        event = self._event()
        event["confirmed_at"] = now - 180
        assert tracker.project(event, now)["drift_minutes"] == pytest.approx(3.0)

    def test_the_position_sent_is_still_the_reported_one(self):
        # Nothing is moved here. The anchor is sent so the MAP can move it,
        # which is what keeps a mark gliding between polls instead of jumping.
        now = time.time()
        event = self._event()
        event["confirmed_at"] = now - 300
        out = tracker.project(event, now)
        assert (out["lat"], out["lon"]) == (event["origin_lat"],
                                            event["origin_lon"])
        assert out["projected"] is False

    def test_the_reckoning_stops_after_the_cap(self):
        # An assumption that nothing turned decays. A track nobody has
        # confirmed for an hour must not still be flying across the country.
        now = time.time()
        event = self._event()
        event["confirmed_at"] = now - 3600
        assert (tracker.project(event, now)["drift_minutes"]
                == tracker.MOST_DRIFT_MINUTES)

    def test_a_track_with_no_speed_does_not_move(self):
        # The removed version of this feature flew things at a speed looked
        # up from a table of what the type typically does. Without a speed
        # from the source there is nothing honest to carry it at.
        event = self._event(velocity=None)
        assert tracker.project(event, time.time())["drift_minutes"] is None

    def test_a_track_standing_still_does_not_move(self):
        # A speed of zero is a speed, and it is the one that means "this is
        # not going anywhere". Carrying it along anyway would be the map
        # contradicting its own source.
        event = self._event(velocity={"bearingDeg": 42, "speedKmh": 0})
        assert tracker.project(event, time.time())["drift_minutes"] is None

    def test_a_track_with_no_course_does_not_move(self):
        event = self._event(heading=None, velocity={"speedKmh": 150})
        assert tracker.project(event, time.time())["drift_minutes"] is None

    def test_an_area_only_track_does_not_move(self):
        track = neptun.read_threat(area_only())
        tracker.reset()
        tracker._record_neptun(track, time.time())
        event = next(e for e in tracker._events if e.get("by") == "neptun")
        assert tracker.project(event, time.time())["drift_minutes"] is None

    def test_a_warning_does_not_move(self):
        # It is a statement about a region, not an object with a course.
        tracker.reset()
        tracker._record_neptun_alert(
            {"name": "Одеська область", "key": "odeska", "oblast": None},
            "NP-alert-odeska", time.time())
        event = next(e for e in tracker._events if e["kind"] == "alert")
        assert tracker.project(event, time.time())["drift_minutes"] is None

    def test_the_cap_goes_out_with_the_feed(self):
        # The map keeps reckoning between polls and has to stop where this
        # stops. One number, sent, rather than two that can drift apart.
        assert tracker.demo()["drift_cap_minutes"] == tracker.MOST_DRIFT_MINUTES

    def test_the_demo_has_something_that_actually_moves(self):
        # Three drawing bugs reached a screenshot because the offline build
        # could not reach the case they were in. A demo where nothing drifts
        # cannot show whether drifting works.
        moving = [e for e in tracker.demo()["events"]
                  if e.get("drift_minutes") is not None]
        assert moving, "the demo draws nothing that moves"
        assert all(e["speed_kmh"] > 0 and e["heading"] is not None
                   for e in moving)

    def test_the_demo_anchors_are_fresh_rather_than_pinned_at_the_cap(self):
        # A stamp written into the table would be hours stale the second time
        # anybody opened the demo, and every mark would sit at the cap.
        moving = [e for e in tracker.demo()["events"]
                  if e.get("drift_minutes") is not None]
        assert all(e["drift_minutes"] < tracker.MOST_DRIFT_MINUTES
                   for e in moving)


class TestEverySourceTheyAggregate:
    """Their message feed, which is every channel they watch.

    /api/v1/threats is what somebody has already turned into a track.
    /api/v1/messages is what their sources actually said, which is a great
    deal more of the night, and it is the widest source this app has.

    It used to be read on the first poll of a process and never again, on the
    reasoning that a cold start is the only time there is a gap to fill. That
    was wrong. After thirty seconds of a run the only NEPTUN input left was
    the snapshot, so every other source they aggregate stopped contributing
    and the map went as sparse as the snapshot happened to be.
    """

    def setup_method(self):
        tracker.reset()

    def teardown_method(self):
        tracker.reset()

    def posts(self, n):
        """n distinct reports their feed might carry, newest first."""
        when = dt.datetime.now(dt.timezone.utc).isoformat().replace(
            "+00:00", "Z")
        return [{"id": f"np-msg/{i}", "channel": "napramok",
                 "when": when, "text": f"Шахед на Полтавщині, курс на захід {i}",
                 "photos": [], "link": neptun.BASE} for i in range(n)]

    def poll_with(self, monkeypatch, messages):
        monkeypatch.setattr(neptun, "messages", lambda: messages)
        monkeypatch.setattr(neptun, "threats", lambda: [])
        monkeypatch.setattr(neptun, "alerts", lambda: [])
        monkeypatch.setattr(tracker, "_fetch_channel", lambda channel: [])
        return tracker.poll()

    def test_their_messages_are_read_on_every_poll(self, monkeypatch):
        calls = []
        real = tracker.catch_up
        monkeypatch.setattr(tracker, "catch_up",
                            lambda: (calls.append(1), real())[1])
        self.poll_with(monkeypatch, self.posts(1))
        self.poll_with(monkeypatch, self.posts(1))
        self.poll_with(monkeypatch, self.posts(1))
        assert len(calls) == 3, "their other sources stopped after the first poll"

    def test_a_report_that_arrives_later_still_gets_on_the_map(self, monkeypatch):
        # The whole point of reading them again: a post their feed did not
        # have on the first poll is a mark on the fourth.
        first = self.poll_with(monkeypatch, self.posts(1))
        was = len(first["events"])
        later = self.poll_with(monkeypatch, self.posts(3))
        assert len(later["events"]) > was

    def test_a_post_read_once_is_not_drawn_twice(self, monkeypatch):
        # Re-reading the same feed every thirty seconds is only safe because
        # the post ids are remembered. Without that this would pile up a
        # duplicate mark a poll for as long as the post stayed in the window.
        self.poll_with(monkeypatch, self.posts(2))
        settled = len(tracker.current()["events"])
        self.poll_with(monkeypatch, self.posts(2))
        self.poll_with(monkeypatch, self.posts(2))
        assert len(tracker.current()["events"]) == settled

    def test_the_feed_being_down_does_not_stop_the_snapshot(self, monkeypatch):
        # Prose on top of the snapshot. It failing is not the panel's
        # business; the snapshot failing is.
        monkeypatch.setattr(neptun, "messages", lambda: (_ for _ in ()).throw(
            neptun.NeptunError("messages 503")))
        monkeypatch.setattr(neptun, "threats",
                            lambda: [neptun.read_threat(ONE_THREAT)])
        monkeypatch.setattr(neptun, "alerts", lambda: [])
        monkeypatch.setattr(tracker, "_fetch_channel", lambda channel: [])
        got = tracker.poll()
        assert [e for e in got["events"] if e.get("by") == "neptun"]
        assert "503" not in got["state"]


class TestOneEndpointDoesNotStarveAnother:
    """The bug that emptied the map of everything except warnings.

    A poll reads three of their endpoints one after another: the messages,
    the threat snapshot, and the alerts. The politeness gate was one slot for
    the whole module, so the first of the three took it and the other two
    were refused as "asked again too soon" -- every poll, forever.

    What that looked like on screen was a province shaded for an air alert
    with a single mark inside it, beside NEPTUN's own map showing ten. The
    warnings were arriving (their boundary file is not paced) and the
    snapshot the warnings were about was never fetched at all.

    Their condition is one REST poll every five seconds, which is about not
    hammering a resource. Three different resources read once each per
    thirty-second poll is nowhere near it.
    """

    def setup_method(self):
        neptun.forget()

    def teardown_method(self):
        neptun.forget()

    def test_one_poll_reaches_all_three_endpoints(self, monkeypatch):
        asked = []

        def watched(url, **kw):
            asked.append(url)
            return {"messages": [], "threats": [], "oblasts": [], "raions": []}

        monkeypatch.setattr(neptun, "_get", watched)
        # Through the paced gate rather than around it: _get is stubbed for
        # the fetch, and claim_turn is the thing under test, so each call
        # takes its own turn first the way the real _get does.
        for url in (neptun.MESSAGES, neptun.THREATS, neptun.ALERTS):
            assert neptun.claim_turn(url) is True, f"{url} was refused"

    def test_a_turn_taken_for_one_is_not_taken_for_the_others(self):
        assert neptun.claim_turn(neptun.MESSAGES) is True
        assert neptun.claim_turn(neptun.THREATS) is True
        assert neptun.claim_turn(neptun.ALERTS) is True

    def test_each_endpoint_still_has_its_own_floor(self):
        # Per endpoint is not "no limit". Asking the same one twice in a row
        # is still refused, which is the politeness this exists for.
        assert neptun.claim_turn(neptun.THREATS) is True
        assert neptun.claim_turn(neptun.THREATS) is False

    def test_the_floor_is_inside_what_they_ask_for(self):
        # They ask for no more than one poll every five seconds.
        assert neptun.MIN_INTERVAL >= 5.0

    def test_the_snapshot_actually_arrives_through_a_whole_poll(self, monkeypatch):
        """End to end: messages first, and the tracks still get drawn.

        The order matters and is the order poll() uses. With one shared slot
        this drew nothing from the snapshot at all.
        """
        tracker.reset()
        payloads = {
            neptun.MESSAGES: {"messages": []},
            neptun.THREATS: {"threats": [ONE_THREAT]},
            neptun.ALERTS: DECLARED,
        }

        def served(url, *, paced=True):
            # The real gate, on the real urls -- that is the whole point.
            # The boundary files are fetched unpaced and are not part of it.
            if paced and not neptun.claim_turn(url):
                raise neptun.TooSoon("asked again before the interval was up")
            return payloads.get(url, {"features": []})

        monkeypatch.setattr(neptun, "_get", served)
        monkeypatch.setattr(tracker, "_fetch_channel", lambda channel: [])
        got = tracker.poll()
        tracks = [e for e in got["events"]
                  if e.get("by") == "neptun" and e["kind"] != "alert"]
        assert tracks, "the threat snapshot never reached the map"
        tracker.reset()


class TestOneRefusalDoesNotLoseTheOther:
    """Tracks and warnings are two endpoints and arrive on their own terms.

    Letting either refusal abort the whole read meant they could only ever
    arrive together, so whichever was asked for second lost every time.
    """

    def setup_method(self):
        tracker.reset()
        neptun.forget()

    def teardown_method(self):
        tracker.reset()
        neptun.forget()

    def test_the_warnings_survive_a_poll_where_the_tracks_were_not_due(
            self, monkeypatch):
        monkeypatch.setattr(neptun, "threats",
                            lambda: [neptun.read_threat(ONE_THREAT)])
        monkeypatch.setattr(neptun, "alerts",
                            lambda: neptun.read_alerts(DECLARED))
        tracker.take_neptun()
        warnings = {e["id"] for e in tracker.current()["events"]
                    if e["kind"] == "alert"}
        assert warnings

        was = {e["id"] for e in tracker.current()["events"]
               if e.get("by") == "neptun" and e["kind"] != "alert"}
        assert was

        monkeypatch.setattr(neptun, "threats", lambda: (_ for _ in ()).throw(
            neptun.TooSoon("not due")))
        tracker.take_neptun()
        assert {e["id"] for e in tracker.current()["events"]
                if e["kind"] == "alert"} == warnings
        # And the tracks themselves are still there. They were not re-read,
        # which is not the same as having ended -- clearing them because the
        # warnings came back would blank the map every other poll.
        assert {e["id"] for e in tracker.current()["events"]
                if e.get("by") == "neptun" and e["kind"] != "alert"} == was

    def test_and_the_tracks_survive_a_poll_where_the_warnings_were_not(
            self, monkeypatch):
        monkeypatch.setattr(neptun, "threats",
                            lambda: [neptun.read_threat(ONE_THREAT)])
        monkeypatch.setattr(neptun, "alerts",
                            lambda: neptun.read_alerts(DECLARED))
        tracker.take_neptun()
        tracks = [e for e in tracker.current()["events"]
                  if e.get("by") == "neptun" and e["kind"] != "alert"]
        assert tracks

        monkeypatch.setattr(neptun, "alerts", lambda: (_ for _ in ()).throw(
            neptun.TooSoon("not due")))
        drawn, raised = tracker.take_neptun()
        assert drawn == len(tracks)
        assert [e for e in tracker.current()["events"]
                if e.get("by") == "neptun" and e["kind"] != "alert"]

    def test_a_trail_is_not_thrown_away_when_the_snapshot_was_not_due(
            self, monkeypatch):
        # forget_trails() treats "not in the snapshot" as "this track has
        # ended". With no snapshot at all that would end every track there is.
        monkeypatch.setattr(neptun, "threats",
                            lambda: [neptun.read_threat(ONE_THREAT)])
        monkeypatch.setattr(neptun, "alerts", lambda: [])
        tracker.take_neptun()
        tracker.take_neptun()
        kept = len(tracker._trails)
        assert kept

        monkeypatch.setattr(neptun, "threats", lambda: (_ for _ in ()).throw(
            neptun.TooSoon("not due")))
        monkeypatch.setattr(neptun, "alerts",
                            lambda: neptun.read_alerts(DECLARED))
        tracker.take_neptun()
        assert len(tracker._trails) == kept

    def test_both_refused_is_still_a_skip(self, monkeypatch):
        # Neither was due: nothing is cleared, nothing is drawn, and the
        # caller's own TooSoon branch keeps the map as it stands.
        monkeypatch.setattr(neptun, "threats", lambda: (_ for _ in ()).throw(
            neptun.TooSoon("not due")))
        monkeypatch.setattr(neptun, "alerts", lambda: (_ for _ in ()).throw(
            neptun.TooSoon("not due")))
        with pytest.raises(neptun.TooSoon):
            tracker.take_neptun()
