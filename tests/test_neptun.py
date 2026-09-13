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
        assert neptun.claim_turn() is True
        began = time.time()
        assert neptun.claim_turn() is False
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
        assert neptun.claim_turn() is True
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
