"""Internet connectivity from RIPE Atlas probes.

What is worth pinning down is the reading of each probe -- which of the four
things it is saying -- and the arithmetic behind "how much of this country
went dark", because both are easy to get subtly wrong in ways that look
plausible on a map. Nothing here touches the network.
"""

from __future__ import annotations

import datetime as dt

import pytest
import requests

from backend import config, connectivity as net

NOW = dt.datetime(2026, 10, 17, 12, 0, tzinfo=dt.timezone.utc)
SIX_HOURS = dt.timedelta(hours=6)
BOX = (22.0, 44.0, 40.0, 52.5)


@pytest.fixture(autouse=True)
def _blank():
    net.forget()
    yield
    net.forget()


def probe(pid=1, status=1, ago=dt.timedelta(days=30), lon=30.5, lat=50.4,
          country="ua", **extra):
    out = {
        "id": pid,
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "status": {"id": status, "name": "x",
                   "since": (NOW - ago).isoformat().replace("+00:00", "Z")},
        "country_code": country,
        "asn_v4": 15895,
        "description": "a probe",
    }
    out.update(extra)
    return out


# ── Which of the four things a probe is saying ─────────────────


class TestClassify:
    def test_online_for_a_long_time_is_up(self):
        assert net.classify(1, NOW - dt.timedelta(days=9), SIX_HOURS, NOW) == "up"

    def test_online_since_inside_the_window_is_back(self):
        assert net.classify(1, NOW - dt.timedelta(hours=2), SIX_HOURS, NOW) == "back"

    def test_offline_since_inside_the_window_is_down(self):
        """The thing this layer exists to show."""
        assert net.classify(2, NOW - dt.timedelta(minutes=30), SIX_HOURS, NOW) == "down"

    def test_offline_since_before_the_window_is_gone_not_down(self):
        """A probe that went dark yesterday did not go dark in the last six
        hours. Drawn red, it would be read as part of whatever is happening
        now."""
        assert net.classify(2, NOW - dt.timedelta(hours=20), SIX_HOURS, NOW) == "gone"

    def test_the_window_edge_counts_as_inside(self):
        assert net.classify(2, NOW - SIX_HOURS, SIX_HOURS, NOW) == "down"
        assert net.classify(1, NOW - SIX_HOURS, SIX_HOURS, NOW) == "back"

    def test_offline_for_more_than_a_week_is_dead_not_dropped(self):
        """Not news, so not red -- but still a probe that is there, so still
        on the map. Dropping it made the map look like data was missing."""
        assert net.classify(2, NOW - dt.timedelta(days=8), SIX_HOURS, NOW) == "dead"
        assert net.classify(2, NOW - dt.timedelta(days=6), SIX_HOURS, NOW) == "gone"

    def test_never_connected_and_abandoned_are_drawn_too(self):
        """Every probe, whatever its status. Abandoned, written off, and a
        status this does not recognise are all "inactive" on the map."""
        assert net.classify(0, NOW, SIX_HOURS, NOW) == "never"
        assert net.classify(3, NOW, SIX_HOURS, NOW) == "inactive"
        assert net.classify(4, NOW, SIX_HOURS, NOW) == "inactive"
        assert net.classify(None, NOW, SIX_HOURS, NOW) == "inactive"

    def test_every_state_is_counted(self):
        assert set(net.summary([])) >= set(net.ALL_STATES)

    def test_a_status_with_no_time_is_not_promoted_to_news(self):
        """Without a time there is no telling whether it changed in the
        window, so it is drawn as the quiet version of itself."""
        assert net.classify(1, None, SIX_HOURS, NOW) == "up"
        assert net.classify(2, None, SIX_HOURS, NOW) == "gone"

    def test_a_longer_window_catches_more(self):
        when = NOW - dt.timedelta(hours=10)
        assert net.classify(2, when, dt.timedelta(hours=6), NOW) == "gone"
        assert net.classify(2, when, dt.timedelta(hours=24), NOW) == "down"


# ── Reading the fields ─────────────────────────────────────────


class TestFields:
    def test_geojson_is_longitude_then_latitude(self):
        """The order GeoJSON uses, and the order it is easiest to get backwards.
        Swapped, Kyiv lands in the Arabian Sea."""
        assert net.position_of({"geometry": {"coordinates": [30.5, 50.4]}}) == (50.4, 30.5)

    def test_no_position_is_no_dot(self):
        assert net.position_of({}) is None
        assert net.position_of({"geometry": None}) is None
        assert net.position_of({"geometry": {"coordinates": [1]}}) is None
        assert net.position_of({"geometry": {"coordinates": ["a", "b"]}}) is None

    def test_null_island_is_no_dot(self):
        assert net.position_of({"geometry": {"coordinates": [0, 0]}}) is None

    def test_a_position_off_the_globe_is_no_dot(self):
        assert net.position_of({"geometry": {"coordinates": [200, 10]}}) is None

    def test_since_from_the_status_object(self):
        got = net.since_of({"status": {"id": 1, "since": "2026-10-17T10:00:00Z"}})
        assert got == dt.datetime(2026, 10, 17, 10, tzinfo=dt.timezone.utc)

    def test_since_from_unix_seconds_when_that_is_all_there_is(self):
        stamp = int(dt.datetime(2026, 10, 17, 10, tzinfo=dt.timezone.utc).timestamp())
        assert net.since_of({"status": 1, "status_since": stamp}) == \
            dt.datetime(2026, 10, 17, 10, tzinfo=dt.timezone.utc)

    def test_no_since_is_none_not_now(self):
        assert net.since_of({"status": {"id": 1}}) is None
        assert net.since_of({"status": {"id": 1, "since": "garbage"}}) is None

    def test_status_from_either_shape(self):
        assert net.status_of({"status": {"id": 2}}) == 2
        assert net.status_of({"status": 1}) == 1
        assert net.status_of({}) is None

    def test_an_owners_description_is_cut_to_one_line(self):
        long = probe(description="  lots   of\n\nspace " + "x" * 200)
        got = net.read_probe(long, SIX_HOURS, NOW)
        assert "\n" not in got["description"]
        assert "  " not in got["description"]
        assert len(got["description"]) <= net.DESCRIPTION_LIMIT

    def test_country_is_upper_case(self):
        assert net.read_probe(probe(country="pl"), SIX_HOURS, NOW)["country"] == "PL"


# ── How much of a region went dark ─────────────────────────────


def states(**counts):
    out = []
    for state, n in counts.items():
        out += [{"state": state, "country": "UA"}] * n
    return out


class TestRegions:
    def test_the_share_is_out_of_those_that_were_up(self):
        """Ten up, two down, five already offline since last week: two of
        twelve went down, not two of seventeen. The ones already gone could
        not go down today."""
        got = net.by_country(states(up=10, down=2, gone=5))[0]
        assert got["share_down"] == round(2 / 12, 3)

    def test_and_ones_that_came_back_were_up_too(self):
        got = net.by_country(states(up=6, down=2, back=2))[0]
        assert got["share_down"] == round(2 / 10, 3)

    def test_most_outages_sort_first_not_highest_share(self):
        """One probe and one outage is 100 % and means nothing; forty probes
        and twelve outages is 30 % and means a great deal."""
        rows = ([{"state": "down", "country": "AA"}]
                + [{"state": "up", "country": "BB"}] * 28
                + [{"state": "down", "country": "BB"}] * 12)
        got = net.by_country(rows)
        assert [r["country"] for r in got] == ["BB", "AA"]

    def test_a_probe_with_no_country_is_still_counted(self):
        got = net.by_country([{"state": "down", "country": None}])
        assert got[0]["country"] == "??"

    def test_nothing_up_is_zero_not_a_division_by_zero(self):
        assert net.by_country(states(gone=3))[0]["share_down"] == 0.0
        assert net.summary([])["share_down"] == 0.0

    def test_the_list_is_short(self):
        rows = [{"state": "down", "country": f"C{i}"} for i in range(20)]
        assert len(net.by_country(rows)) == 8


# ── Asking RIPE Atlas ──────────────────────────────────────────


class Reply:
    def __init__(self, body=None, status=200):
        self._body = body if body is not None else {"results": [], "next": None}
        self.status_code = status
        self.ok = 200 <= status < 300

    def json(self):
        return self._body


def answering(*pages, first_status=200):
    """A stand-in for the probes endpoint that serves these pages in turn."""
    asked = []

    def get(url, params=None, timeout=None):
        asked.append((url, dict(params or {})))
        if len(asked) == 1 and first_status != 200:
            return Reply(status=first_status)
        page = pages[min(len(asked) - (1 if first_status == 200 else 2), len(pages) - 1)]
        return Reply(page)
    return get, asked


class TestAsking:
    def test_the_rectangle_is_asked_for_with_the_published_filters(self):
        get, asked = answering({"results": [], "next": None})
        net.probes(BOX, get=get, now=NOW)
        params = asked[0][1]
        assert params["latitude__gte"] == 44.0 and params["latitude__lte"] == 52.5
        assert params["longitude__gte"] == 22.0 and params["longitude__lte"] == 40.0
        # No status filter: every probe is asked for, whatever its status.
        assert not any(k.startswith("status") for k in params)
        assert params["page_size"] == 500

    def test_every_page_is_followed(self):
        pages = [{"results": [probe(pid=i)], "next": f"https://next/{i}"} for i in range(3)]
        pages.append({"results": [probe(pid=9)], "next": None})
        get, asked = answering(*pages)
        got = net.probes(BOX, get=get, now=NOW)
        assert got["count"] == 4
        assert asked[1][0] == "https://next/0"
        assert got["capped"] is False

    def test_but_only_so_many_and_then_it_says_so(self, monkeypatch):
        monkeypatch.setattr(net, "MOST_PAGES", 2)
        pages = [{"results": [probe(pid=i)], "next": "https://next"} for i in range(5)]
        get, asked = answering(*pages)
        got = net.probes(BOX, get=get, now=NOW)
        assert len(asked) == 2
        assert got["capped"] is True

    def test_a_later_page_that_fails_keeps_the_rest_and_says_partial(self):
        calls = []

        def get(url, params=None, timeout=None):
            calls.append(url)
            if len(calls) == 1:
                return Reply({"results": [probe(pid=1)], "next": "https://next"})
            raise requests.RequestException("dropped")

        got = net.probes(BOX, get=get, now=NOW)
        assert got["count"] == 1
        assert got["capped"] is True

    def test_a_probe_of_every_status_comes_back(self):
        """The census: connected, offline, long gone, abandoned, never
        connected -- all on the map, and only a probe with no position left
        off, because there is nowhere to draw it."""
        raw = [probe(pid=1, status=1), probe(pid=2, status=2, ago=dt.timedelta(minutes=5)),
               probe(pid=3, status=2, ago=dt.timedelta(days=40)), probe(pid=4, status=3),
               probe(pid=5, status=0), probe(pid=6, status=1, lon=0, lat=0)]
        get, _ = answering({"results": raw, "next": None})
        got = net.probes(BOX, get=get, now=NOW)
        assert sorted(p["id"] for p in got["probes"]) == [1, 2, 3, 4, 5]
        assert {p["state"] for p in got["probes"]} == {"up", "down", "dead", "inactive", "never"}

    def test_ripes_own_word_for_a_status_is_kept(self):
        raw = probe(pid=4, status=3)
        raw["status"]["name"] = "Written Off"
        got = net.read_probe(raw, SIX_HOURS, NOW)
        assert got["state"] == "inactive" and got["status_name"] == "Written Off"

    def test_any_other_refusal_is_said(self):
        get, _ = answering(first_status=503)
        with pytest.raises(net.ConnectivityError, match="503"):
            net.probes(BOX, get=get, now=NOW)

    def test_an_unreachable_service_is_said(self):
        def down(*a, **k):
            raise requests.RequestException("no route")

        with pytest.raises(net.ConnectivityError, match="could not be reached"):
            net.probes(BOX, get=down, now=NOW)

    def test_not_json_is_said(self):
        class Bad(Reply):
            def json(self):
                raise ValueError("html")

        with pytest.raises(net.ConnectivityError, match="not JSON"):
            net.probes(BOX, get=lambda *a, **k: Bad(), now=NOW)

    def test_the_same_rectangle_is_not_asked_twice_in_a_row(self, monkeypatch):
        calls = []
        monkeypatch.setattr(net._session, "get",
                            lambda *a, **k: (calls.append(1), Reply())[1])
        net.probes(BOX)
        net.probes(BOX)
        assert len(calls) == 1

    def test_but_a_new_window_reuses_the_probes_rather_than_asking(self, monkeypatch):
        """The window only changes how the same probes are read."""
        calls = []
        monkeypatch.setattr(net._session, "get",
                            lambda *a, **k: (calls.append(1), Reply())[1])
        net.probes(BOX, hours=1)
        net.probes(BOX, hours=24)
        assert len(calls) == 1

    def test_an_unknown_window_falls_back_to_the_default(self):
        get, _ = answering({"results": [], "next": None})
        assert net.probes(BOX, hours=5, get=get, now=NOW)["hours"] == net.DEFAULT_WINDOW


class TestTheAnswer:
    def test_changes_are_never_the_ones_a_cap_drops(self, monkeypatch):
        """A continental view holds thousands of steady probes. If the cap
        kept the nearest, the three that just went down at the edge of the
        view would vanish -- and they are the whole point."""
        monkeypatch.setattr(net, "MOST_RETURNED", 3)
        raw = [probe(pid=i, lon=31.0, lat=48.0) for i in range(10)]
        raw.append(probe(pid=99, status=2, ago=dt.timedelta(minutes=5), lon=22.1, lat=44.1))
        got = net.shape(raw, 6, BOX, now=NOW)
        assert got["probes"][0]["id"] == 99
        assert got["capped"] is True
        # And the summary is of everything, not of what was drawn.
        assert got["summary"]["up"] == 10

    def test_it_says_when_it_was_asked(self):
        got = net.shape([], 6, BOX, now=NOW)
        assert got["checked"] == "2026-10-17T12:00:00Z"
        assert got["attribution"] == net.ATTRIBUTION


class TestDemo:
    def test_has_a_dark_patch(self):
        """The pattern this layer exists to show. An offline build that never
        shows one cannot be checked by eye."""
        got = net.demo(BOX, now=NOW)
        assert got["summary"]["down"] >= 5
        assert got["demo"] is True

    def test_stays_inside_the_rectangle(self):
        for p in net.demo(BOX, now=NOW)["probes"]:
            assert BOX[0] <= p["lon"] <= BOX[2] and BOX[1] <= p["lat"] <= BOX[3]

    def test_reaches_no_network(self, monkeypatch):
        def forbidden(*a, **k):
            raise AssertionError("the demo asked the network")

        monkeypatch.setattr(net._session, "get", forbidden)
        monkeypatch.setattr(requests, "get", forbidden)
        assert net.demo(BOX)["probes"]


# ── The route ──────────────────────────────────────────────────


class TestTheRoute:
    def test_the_rectangle_and_window_reach_the_module(self, client, monkeypatch):
        seen = {}

        def fake(box, hours):
            seen.update(box=box, hours=hours)
            return {"probes": [], "count": 0}

        monkeypatch.setattr(config, "DEMO_MODE", False)
        monkeypatch.setattr(net, "probes", fake)
        got = client.get("/api/connectivity", params={
            "west": 22, "south": 44, "east": 40, "north": 52.5, "hours": 24})
        assert got.status_code == 200
        assert seen == {"box": (22.0, 44.0, 40.0, 52.5), "hours": 24}

    def test_a_window_past_a_day_is_refused(self, client):
        got = client.get("/api/connectivity", params={
            "west": 22, "south": 44, "east": 40, "north": 52.5, "hours": 48})
        assert got.status_code == 422

    def test_a_failure_is_an_error_status_not_an_empty_map(self, client, monkeypatch):
        def broken(box, hours):
            raise net.ConnectivityError("RIPE Atlas answered 503")

        monkeypatch.setattr(config, "DEMO_MODE", False)
        monkeypatch.setattr(net, "probes", broken)
        got = client.get("/api/connectivity", params={
            "west": 22, "south": 44, "east": 40, "north": 52.5})
        assert got.status_code >= 500
        assert "503" in got.text

    def test_the_panel_is_told_its_windows(self, client):
        said = client.get("/api/config").json()["connectivity"]
        assert set(said["windows"]) == {"1", "6", "24"}

    def test_the_self_test_checks_ripe_atlas_too(self):
        import inspect

        from backend import app
        assert '"RIPE Atlas"' in inspect.getsource(app.selftest)


# ── One probe over time: the graph ─────────────────────────────

T0 = NOW.timestamp()


class TestReadingRipeResults:
    def test_connects_and_disconnects_in_time_order(self):
        got = net.read_events([
            {"timestamp": T0 - 60, "event": "connect"},
            {"timestamp": T0 - 600, "event": "disconnect"},
            {"timestamp": T0 - 30, "event": "something else"},
            {"event": "connect"},  # no time: cannot be placed
        ])
        assert got == [(T0 - 600, "down"), (T0 - 60, "up")]

    def test_a_ping_round_is_its_median_not_its_mean(self):
        """One slow reply out of three would drag a mean up; the median is
        what the connection was doing."""
        got = net.read_pings([{"timestamp": T0, "sent": 3, "rcvd": 3,
                               "result": [{"rtt": 10}, {"rtt": 11}, {"rtt": 300}]}])
        assert got[0]["rtt"] == 11

    def test_a_round_with_nothing_back_has_no_round_trip_not_zero(self):
        """Zero would draw the worst moment as the best."""
        got = net.read_pings([{"timestamp": T0, "sent": 3, "rcvd": 0, "avg": -1,
                               "result": [{"x": "*"}, {"x": "*"}, {"x": "*"}]}])
        assert got[0]["rtt"] is None
        assert got[0]["lost"] == 1.0

    def test_partial_loss_is_counted(self):
        got = net.read_pings([{"timestamp": T0, "sent": 3, "rcvd": 2,
                               "result": [{"rtt": 9}, {"x": "*"}, {"rtt": 11}]}])
        assert got[0]["rtt"] == 10
        assert got[0]["lost"] == pytest.approx(1 / 3)

    def test_an_average_is_used_only_when_there_are_no_replies_listed(self):
        got = net.read_pings([{"timestamp": T0, "sent": 3, "rcvd": 3, "avg": 14.5}])
        assert got[0]["rtt"] == 14.5


class TestTimeline:
    def test_a_connect_alone_is_not_an_outage_before_it(self):
        """Probes reconnect to RIPE's controllers while already online, and a
        new probe connects for the first time. Read as "offline until then",
        every such probe was an outage from the start of the window to its
        first connect, and an area's line climbed all week."""
        got = net.timeline([(T0 - 100, "up")], "up", T0 - 1000, T0)
        assert got == [(T0 - 1000, T0 - 100, "unknown"), (T0 - 100, T0, "up")]

    def test_a_disconnect_alone_means_it_was_online_until_then(self):
        got = net.timeline([(T0 - 100, "down")], "down", T0 - 1000, T0)
        assert got == [(T0 - 1000, T0 - 100, "up"), (T0 - 100, T0, "down")]

    def test_what_it_was_doing_before_the_window_decides_the_start(self):
        """An outage that began before the window and ended inside it really
        was an outage at the start -- the log from before the window says so."""
        got = net.timeline([(T0 - 1500, "down"), (T0 - 100, "up")], "up", T0 - 1000, T0)
        assert got == [(T0 - 1000, T0 - 100, "down"), (T0 - 100, T0, "up")]

    def test_no_events_is_all_window_whatever_it_is_now(self):
        assert net.timeline([], "up", T0 - 1000, T0) == [(T0 - 1000, T0, "up")]
        assert net.timeline([], None, T0 - 1000, T0) == [(T0 - 1000, T0, "unknown")]

    def test_an_outage_and_a_recovery(self):
        got = net.timeline([(T0 - 600, "down"), (T0 - 300, "up")], "up", T0 - 1000, T0)
        assert [s for _a, _b, s in got] == ["up", "down", "up"]
        assert got[1] == (T0 - 600, T0 - 300, "down")

    def test_only_the_last_event_before_the_window_counts(self):
        got = net.timeline([(T0 - 5000, "down"), (T0 - 3000, "up")], "down", T0 - 1000, T0)
        assert got == [(T0 - 1000, T0, "up")]

    def test_events_after_the_window_are_ignored(self):
        got = net.timeline([(T0 + 50, "down")], "up", T0 - 1000, T0)
        assert got == [(T0 - 1000, T0, "up")]

    def test_the_stretches_cover_the_window_exactly(self):
        got = net.timeline([(T0 - 900, "down"), (T0 - 500, "up"), (T0 - 200, "down")],
                           "down", T0 - 1000, T0)
        assert got[0][0] == T0 - 1000 and got[-1][1] == T0
        for (a, b, _s), (c, _d, _t) in zip(got, got[1:]):
            assert b == c


def ripe(probe_record=None, connections=(), pings=(), ping_status=200):
    """A stand-in for the three things a graph asks RIPE for."""
    asked = []

    def get(url, params=None, timeout=None):
        asked.append((url, dict(params or {})))
        if "/probes/" in url:
            return Reply(probe_record or {"status": {"id": 1}, "asn_v4": 1, "country_code": "ua"})
        if "/7000/" in url:
            return Reply(list(connections))
        if "/1001/" in url:
            return Reply(list(pings), status=ping_status)
        raise AssertionError(url)
    return get, asked


class TestHistory:
    def test_asks_for_this_probe_and_this_window_only(self):
        get, asked = ripe()
        net.history(4242, 24, get=get, now=NOW)
        results = [p for u, p in asked if "/results/" in u]
        assert {p["probe_ids"] for p in results} == {4242}
        # From a few days before the window, so what it was doing when the
        # window opened is known rather than guessed.
        lookback = int(net.LOOKBACK.total_seconds())
        conn = [p for u, p in asked if "/7000/" in u]
        assert all(p["stop"] - p["start"] == 24 * 3600 + lookback for p in conn)
        assert any("/7000/" in u for u, _ in asked) and any("/1001/" in u for u, _ in asked)

    def test_an_unknown_span_falls_back_to_a_day(self):
        get, _ = ripe()
        assert net.history(1, 5, get=get, now=NOW)["hours"] == net.DEFAULT_HISTORY

    def test_pings_that_will_not_come_cost_the_pings_not_the_graph(self):
        get, _ = ripe(connections=[{"timestamp": T0 - 600, "event": "disconnect"}],
                      ping_status=503)
        got = net.history(1, 24, get=get, now=NOW)
        assert got["pings"] == []
        assert "503" in got["ping_trouble"]
        assert got["segments"][-1][2] == "down"

    def test_but_a_connection_log_that_will_not_come_is_an_error(self):
        def get(url, params=None, timeout=None):
            if "/7000/" in url:
                return Reply(status=502)
            return Reply({"status": {"id": 1}})

        with pytest.raises(net.ConnectivityError, match="502"):
            net.history(1, 24, get=get, now=NOW)

    def test_a_probe_record_that_will_not_come_costs_only_the_label(self):
        def get(url, params=None, timeout=None):
            if "/probes/" in url:
                raise requests.RequestException("no")
            return Reply([])

        got = net.history(1, 24, get=get, now=NOW)
        assert got["asn"] is None
        assert got["segments"] == [(T0 - 24 * 3600, T0, "unknown")]

    def test_what_it_is_doing_now_fills_a_quiet_window(self):
        get, _ = ripe(probe_record={"status": {"id": 2}})
        got = net.history(1, 24, get=get, now=NOW)
        assert got["segments"] == [(T0 - 24 * 3600, T0, "down")]

    def test_the_same_probe_is_not_asked_twice_in_a_minute(self, monkeypatch):
        calls = []

        def get(url, params=None, timeout=None):
            calls.append(url)
            return Reply([] if "/results/" in url else {"status": {"id": 1}})

        monkeypatch.setattr(net._session, "get", get)
        net._history_held.clear()
        net.history(7, 24)
        net.history(7, 24)
        assert len(calls) == 3
        net._history_held.clear()


class TestTheGraph:
    def _history(self, **over):
        h = net.demo_history(1000078, 24, "down", "2026-10-17T11:00:00Z", now=NOW)
        h.update(over)
        return h

    def test_is_a_png_the_size_of_a_seismograph(self):
        from PIL import Image
        import io
        png = net.plot(self._history())
        image = Image.open(io.BytesIO(png))
        assert image.format == "PNG"
        assert image.size == net.PLOT_SIZE

    def test_the_offline_stretch_is_red_in_the_band(self):
        from PIL import Image
        import io
        image = Image.open(io.BytesIO(net.plot(self._history()))).convert("RGB")
        width, _h = image.size
        # The last few pixels of the band are the outage at the end.
        assert image.getpixel((width - 15, 46)) == net._DOWN
        assert image.getpixel((60, 46)) == net._UP

    def test_and_shaded_through_the_round_trip_below(self):
        """So a gap in the line lines up with the reason for it."""
        from PIL import Image
        import io
        image = Image.open(io.BytesIO(net.plot(self._history()))).convert("RGB")
        width, height = image.size
        r, g, b = image.getpixel((width - 15, height // 2))
        assert r > g and r > b

    def test_no_pings_is_said_on_the_graph(self):
        png = net.plot(self._history(pings=[], ping_trouble=""))
        assert png[:8] == b"\x89PNG\r\n\x1a\n"

    def test_a_graph_with_one_ping_does_not_divide_by_zero(self):
        net.plot(self._history(pings=[{"t": T0 - 60, "rtt": 0.0, "sent": 3,
                                       "rcvd": 3, "lost": 0.0}]))

    def test_demo_history_agrees_with_the_dot(self):
        """A probe drawn red whose graph said it had been online all day
        would look exactly like a bug."""
        down = net.demo_history(5, 24, "down", "2026-10-17T11:00:00Z", now=NOW)
        assert down["segments"][-1][2] == "down"
        back = net.demo_history(5, 24, "back", "2026-10-17T11:00:00Z", now=NOW)
        assert [s for _a, _b, s in back["segments"]][-2:] == ["down", "up"]
        up = net.demo_history(5, 24, "up", None, now=NOW)
        assert {s for _a, _b, s in up["segments"]} == {"up"}

    def test_no_pings_while_offline_in_the_demo(self):
        h = net.demo_history(5, 24, "down", "2026-10-17T11:00:00Z", now=NOW)
        assert all(p["t"] < T0 - 3600 for p in h["pings"])


class TestTheGraphRoute:
    def test_draws_one_probe(self, client, monkeypatch):
        seen = {}

        def fake(pid, hours):
            seen.update(id=pid, hours=hours)
            return net.demo_history(pid, hours, now=NOW)

        monkeypatch.setattr(config, "DEMO_MODE", False)
        monkeypatch.setattr(net, "history", fake)
        got = client.get("/api/connectivity/probe.png", params={"id": 4242, "hours": 168})
        assert got.status_code == 200
        assert got.headers["content-type"] == "image/png"
        assert got.headers["cache-control"] == "no-store"
        assert seen == {"id": 4242, "hours": 168}

    def test_a_failure_is_said_in_words_not_as_a_broken_image(self, client, monkeypatch):
        def broken(pid, hours):
            raise net.ConnectivityError("RIPE Atlas answered 502 for measurement 7000")

        monkeypatch.setattr(config, "DEMO_MODE", False)
        monkeypatch.setattr(net, "history", broken)
        got = client.get("/api/connectivity/probe.png", params={"id": 1})
        assert got.status_code >= 500
        assert "measurement 7000" in got.json()["detail"]

    def test_a_probe_id_is_a_number(self, client):
        assert client.get("/api/connectivity/probe.png",
                          params={"id": "../etc"}).status_code == 422
        assert client.get("/api/connectivity/probe.png",
                          params={"id": 0}).status_code == 422

    def test_the_demo_hints_are_only_read_by_the_demo(self, client, monkeypatch):
        """state and since come from the browser. The real graph must be
        what RIPE says, never what the page told it to draw."""
        seen = {}
        ready = net.demo_history(3, 24, now=NOW)

        def fake(pid, hours):
            seen["called"] = True
            return ready

        monkeypatch.setattr(config, "DEMO_MODE", False)
        monkeypatch.setattr(net, "history", fake)
        monkeypatch.setattr(net, "demo_history",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("demo used")))
        got = client.get("/api/connectivity/probe.png",
                         params={"id": 3, "state": "down", "since": "2026-10-17T11:00:00Z"})
        assert got.status_code == 200 and seen["called"]

    def test_the_panel_is_told_its_graph_spans(self, client):
        said = client.get("/api/config").json()["connectivity"]
        # One chooser serves both kinds of graph, so they offer the same spans.
        assert set(said["history"]) == {"24", "48", "168"}
        assert set(said["area_history"]) == set(said["history"])


# ── A whole area over time ─────────────────────────────────────

AREA = (28.0, 50.0, 29.5, 51.0)
START = NOW - dt.timedelta(hours=48)


class TestWhichProbesCount:
    def test_online_now_counts(self):
        got = net.area_members([probe(pid=1, status=1)], START, AREA)
        assert [m["id"] for m in got] == [1]

    def test_gone_offline_inside_the_window_counts(self):
        """It was online when the window began, and its going is the story."""
        got = net.area_members([probe(pid=2, status=2, ago=dt.timedelta(hours=3))],
                               START, AREA)
        assert [m["state"] for m in got] == ["down"]

    def test_already_offline_before_the_window_does_not(self):
        """It would sit at zero all the way across and drag the line down for
        something that did not happen in the window."""
        got = net.area_members([probe(pid=3, status=2, ago=dt.timedelta(days=3))],
                               START, AREA)
        assert got == []

    def test_offline_with_no_time_does_not(self):
        raw = probe(pid=4, status=2)
        raw["status"] = {"id": 2}
        assert net.area_members([raw], START, AREA) == []

    def test_abandoned_and_never_connected_do_not(self):
        assert net.area_members([probe(status=3), probe(status=0)], START, AREA) == []

    def test_nearest_the_middle_first(self):
        raw = [probe(pid=1, lon=28.0, lat=50.0), probe(pid=2, lon=28.75, lat=50.5)]
        assert [m["id"] for m in net.area_members(raw, START, AREA)] == [2, 1]


class TestTheLine:
    def test_counts_how_many_were_up_at_each_step(self):
        s, e = 0.0, 600.0
        timelines = [[(0.0, 600.0, "up")],
                     [(0.0, 300.0, "up"), (300.0, 600.0, "down")]]
        got = net.online_series(timelines, s, e, 100.0)
        assert [n for _t, n, _c in got] == [2, 2, 2, 1, 1, 1]
        # Both in service throughout: one online, one offline, both counted.
        assert [c for _t, _n, c in got] == [2] * 6

    def test_a_probe_not_yet_in_service_is_not_counted_either_way(self):
        timelines = [[(0.0, 600.0, "up")],
                     [(0.0, 300.0, "unknown"), (300.0, 600.0, "up")]]
        got = net.online_series(timelines, 0.0, 600.0, 100.0)
        assert [(n, c) for _t, n, c in got] == [(1, 1)] * 3 + [(2, 2)] * 3

    def test_read_at_the_middle_of_each_step(self):
        """An outage starting a minute into a step belongs to that step."""
        timelines = [[(0.0, 110.0, "up"), (110.0, 400.0, "down")]]
        got = net.online_series(timelines, 0.0, 400.0, 100.0)
        assert got[0][1] == 1 and got[1][1] == 0

    def test_the_last_step_reaches_the_end(self):
        got = net.online_series([[(0.0, 250.0, "up")]], 0.0, 250.0, 100.0)
        assert [t for t, _n, _c in got] == [0.0, 100.0, 200.0]
        assert got[-1][1] == 1

    def test_each_step_is_a_share_of_those_in_service_then(self):
        """Not of every probe by the end of the window. Probes arriving over
        the week -- new ones, or ones seen only from a reconnect -- were the
        offline ones at the start, and the line climbed all week."""
        got = net.summarise_area([(0, 40, 40), (1, 45, 45), (2, 50, 50)], members=50)
        assert got["shares"] == [1.0, 1.0, 1.0]
        assert got["low"] == 1.0

    def test_normalised_to_the_best_moment(self):
        """Volunteers' connections are never all up together; a line that
        never reached the top would read as a region permanently degraded."""
        got = net.summarise_area([(0, 49, 50), (1, 49, 50), (2, 35, 50), (3, 42, 50)],
                                 members=50)
        assert got["now"] == pytest.approx(42 / 49)
        assert got["low"] == pytest.approx(35 / 49)
        assert got["low_at"] == 2
        assert got["online_now"] == 42 and got["in_service_now"] == 50

    def test_a_step_with_nobody_in_service_is_a_gap_not_a_zero(self):
        got = net.summarise_area([(0, 0, 0), (1, 5, 5)], members=5)
        assert got["shares"] == [None, 1.0]
        assert got["low"] == 1.0

    def test_nothing_online_ever_is_zero_not_a_division_by_zero(self):
        got = net.summarise_area([(0, 0, 3), (1, 0, 3)], members=3)
        assert got["now"] == 0.0 and got["low"] == 0.0

    def test_no_steps_at_all(self):
        got = net.summarise_area([], members=0)
        assert got["now"] is None and got["low"] is None


def area_ripe(raw, events_by_probe, asked=None):
    """The probe list, and connection logs for any comma-separated ids."""
    def get(url, params=None, timeout=None):
        if asked is not None:
            asked.append((url, dict(params or {})))
        if "/7000/" in url:
            ids = [int(i) for i in str(params["probe_ids"]).split(",")]
            return Reply([row for i in ids for row in events_by_probe.get(i, [])])
        return Reply({"results": raw, "next": None})
    return get


class TestAreaHistory:
    def test_an_outage_in_part_of_the_area_is_a_dip_in_the_line(self):
        raw = [probe(pid=i, status=1, lon=28.5 + i * 0.01, lat=50.5) for i in range(10)]
        cut = (NOW - dt.timedelta(hours=5)).timestamp()
        back = (NOW - dt.timedelta(hours=3)).timestamp()
        events = {i: [{"prb_id": i, "timestamp": cut, "event": "disconnect"},
                      {"prb_id": i, "timestamp": back, "event": "connect"}]
                  for i in range(4)}
        got = net.area_history(AREA, 24, get=area_ripe(raw, events), now=NOW)
        assert got["members"] == 10
        assert got["low"] == pytest.approx(0.6)
        assert got["now"] == pytest.approx(1.0)
        assert NOW.timestamp() - 5 * 3600 <= got["low_at"] <= NOW.timestamp() - 3 * 3600

    def test_logs_are_asked_for_in_batches(self, monkeypatch):
        monkeypatch.setattr(net, "IDS_PER_ASK", 4)
        raw = [probe(pid=i, lon=28.5, lat=50.5) for i in range(10)]
        asked = []
        net.area_history(AREA, 24, get=area_ripe(raw, {}, asked), now=NOW)
        batches = [p["probe_ids"] for u, p in asked if "/7000/" in u]
        assert len(batches) == 3
        assert sorted(int(i) for b in batches for i in b.split(",")) == list(range(10))

    def test_a_crowded_box_is_sampled_and_says_so(self, monkeypatch):
        monkeypatch.setattr(net, "MOST_AREA_PROBES", 5)
        raw = [probe(pid=i, lon=28.5, lat=50.5) for i in range(12)]
        got = net.area_history(AREA, 24, get=area_ripe(raw, {}), now=NOW)
        assert got["members"] == 5 and got["in_box"] == 12 and got["sampled"] is True

    def test_an_unknown_span_falls_back(self):
        got = net.area_history(AREA, 5, get=area_ripe([probe()], {}), now=NOW)
        assert got["hours"] == net.DEFAULT_AREA_HISTORY

    def test_the_steps_fit_the_span(self):
        for hours in (24, 48, 168):
            got = net.area_history(AREA, hours, get=area_ripe([probe()], {}), now=NOW)
            assert len(got["series"]) == hours * 60 // net.AREA_STEP_MINUTES[hours]

    def test_a_small_box_is_named_after_its_region(self):
        got = net.area_history(AREA, 24, get=area_ripe([probe()], {}), now=NOW,
                               namer=lambda lat, lon: "Zhytomyr Oblast")
        assert got["name"] == "Zhytomyr Oblast"

    def test_a_gazetteer_that_fails_costs_only_the_name(self):
        def broken(lat, lon):
            raise RuntimeError("rate limited")

        got = net.area_history(AREA, 24, get=area_ripe([probe(country="ua")], {}),
                               now=NOW, namer=broken)
        assert got["name"] == "UA"

    def test_a_big_box_is_named_after_its_countries_not_one_region(self):
        """The middle of a box over three countries is in one region, and
        naming the box after it would be naming it wrong."""
        big = (14.0, 44.0, 40.0, 56.0)
        raw = [probe(pid=1, country="pl", lon=20, lat=52), probe(pid=2, country="ua"),
               probe(pid=3, country="ua", lon=31, lat=49)]
        got = net.area_history(big, 24, get=area_ripe(raw, {}), now=NOW,
                               namer=lambda lat, lon: "Somewhere Oblast")
        assert got["name"] == "UA · PL"


def has_colour(image, colour) -> bool:
    """Whether any pixel is exactly this colour. Through numpy rather than
    getdata, which Pillow is retiring."""
    import numpy as np
    return bool((np.asarray(image.convert("RGB")) == np.array(colour)).all(axis=-1).any())


class TestTheAreaPicture:
    def _found(self, **over):
        found = net.demo_area(AREA, 48, now=NOW)
        found.update(over)
        return found

    def test_is_a_big_png(self):
        from PIL import Image
        import io
        image = Image.open(io.BytesIO(net.plot_area(self._found())))
        assert image.size == net.AREA_PLOT_SIZE

    def test_carries_the_mark(self):
        """The mark is drawn, so it cannot be read back as text -- but the
        corner one is solid ink on the background, and is there to be found."""
        from PIL import Image
        import io
        image = Image.open(io.BytesIO(net.plot_area(self._found()))).convert("RGB")
        corner = image.crop((net.AREA_PLOT_SIZE[0] - 260, 55, net.AREA_PLOT_SIZE[0] - 60, 100))
        assert has_colour(corner, net._AREA_INK)
        assert net.WATERMARK == "@Kaldockhi"

    def test_trouble_is_red_and_calm_is_teal(self):
        assert net.area_colour(1.0) == net._AREA_GOOD
        assert net.area_colour(0.9) == net._AREA_WOBBLE
        assert net.area_colour(0.6) == net._AREA_BAD
        assert net.area_colour(net.AREA_GOOD) == net._AREA_GOOD
        assert net.area_colour(net.AREA_TROUBLE) == net._AREA_WOBBLE

    def test_the_outage_is_drawn_red(self):
        from PIL import Image
        import io
        image = Image.open(io.BytesIO(net.plot_area(self._found()))).convert("RGB")
        # The demo's outage is in the last few hours: the right-hand end of
        # the chart has the trouble colour in it, the left-hand end does not.
        assert has_colour(image.crop((1300, 250, 1470, 780)), net._AREA_BAD)
        assert not has_colour(image.crop((80, 250, 400, 780)), net._AREA_BAD)

    def test_survives_an_area_with_no_steps(self):
        net.plot_area(self._found(shares=[], series=[], now=None, low=None, low_at=None))

    def test_survives_a_name_in_cyrillic(self):
        net.plot_area(self._found(name="Житомирська область"))


class TestTheAreaRoute:
    PARAMS = {"west": 28, "south": 50, "east": 29.5, "north": 51, "hours": 48}

    def test_draws_the_box_and_sends_the_figures_with_it(self, client, monkeypatch):
        seen = {}

        def fake(box, hours, namer=None, **kw):
            seen.update(box=box, hours=hours, namer=namer)
            found = net.demo_area(box, hours, now=NOW)
            found["name"] = "Житомирська область"
            return found

        monkeypatch.setattr(config, "DEMO_MODE", False)
        monkeypatch.setattr(net, "area_history", fake)
        got = client.get("/api/connectivity/area.png", params=self.PARAMS)
        assert got.status_code == 200 and got.headers["content-type"] == "image/png"
        assert seen["box"] == (28.0, 50.0, 29.5, 51.0) and seen["hours"] == 48
        assert seen["namer"] is not None
        assert got.headers["x-probes"] == "60"
        assert got.headers["x-now"].isdigit()
        # Header values are Latin-1: a Cyrillic name has to be encoded.
        from urllib.parse import unquote
        assert unquote(got.headers["x-name"]) == "Житомирська область"

    def test_an_empty_box_is_said_in_words(self, client, monkeypatch):
        def empty(box, hours, namer=None, **kw):
            found = net.demo_area(box, hours, now=NOW)
            found.update(members=0)
            return found

        monkeypatch.setattr(config, "DEMO_MODE", False)
        monkeypatch.setattr(net, "area_history", empty)
        got = client.get("/api/connectivity/area.png", params=self.PARAMS)
        assert got.status_code == 404
        assert "larger area" in got.json()["detail"]

    def test_a_box_drawn_inside_out_is_refused(self, client):
        got = client.get("/api/connectivity/area.png",
                         params={**self.PARAMS, "west": 30, "east": 29})
        assert got.status_code == 400

    def test_ripe_failing_is_an_error_status(self, client, monkeypatch):
        def broken(box, hours, namer=None, **kw):
            raise net.ConnectivityError("RIPE Atlas answered 503 for measurement 7000")

        monkeypatch.setattr(config, "DEMO_MODE", False)
        monkeypatch.setattr(net, "area_history", broken)
        got = client.get("/api/connectivity/area.png", params=self.PARAMS)
        assert got.status_code >= 500 and "503" in got.json()["detail"]


# ── Collecting all of it ───────────────────────────────────────


class TestEveryPage:
    def test_the_rest_of_the_pages_are_worked_out_from_the_first(self):
        got = net.page_urls(
            "https://atlas.ripe.net/api/v2/probes/?latitude__gte=44&page=2&page_size=500",
            count=1700, per_page=500)
        assert len(got) == 3
        assert [u.split("page=")[1].split("&")[0] for u in got] == ["2", "3", "4"]
        # Every other filter carried over exactly.
        assert all("latitude__gte=44" in u and "page_size=500" in u for u in got)

    def test_not_when_the_answer_did_not_say_how_many(self):
        assert net.page_urls("https://x/?page=2", count=None, per_page=500) is None

    def test_not_when_the_pages_are_not_numbered(self):
        """A cursor rather than a page number: walked instead."""
        assert net.page_urls("https://x/?cursor=abc", count=900, per_page=500) is None

    def test_numbered_pages_are_all_fetched_and_all_kept(self):
        def get(url, params=None, timeout=None):
            if params is not None:          # the first ask
                return Reply({"count": 1250, "next": "https://ripe/?page=2&f=1",
                              "results": [probe(pid=i) for i in range(500)]})
            n = int(url.split("page=")[1].split("&")[0])
            size = 500 if n == 2 else 250
            return Reply({"results": [probe(pid=n * 1000 + i) for i in range(size)]})

        got = net.probes(BOX, get=get, now=NOW)
        assert got["count"] == 1250
        assert got["capped"] is False

    def test_they_are_asked_for_together(self, monkeypatch):
        """Eighty pages in a queue is over a minute."""
        import threading
        import time as clock
        running, most = [0], [0]
        lock = threading.Lock()

        def get(url, params=None, timeout=None):
            if params is not None:
                return Reply({"count": 3000, "next": "https://ripe/?page=2",
                              "results": [probe(pid=1)] * 500})
            with lock:
                running[0] += 1
                most[0] = max(most[0], running[0])
            clock.sleep(0.05)
            with lock:
                running[0] -= 1
            return Reply({"results": []})

        net.probes(BOX, get=get, now=NOW)
        assert most[0] > 1

    def test_a_page_that_fails_is_admitted(self):
        def get(url, params=None, timeout=None):
            if params is not None:
                return Reply({"count": 1500, "next": "https://ripe/?page=2",
                              "results": [probe(pid=i) for i in range(500)]})
            if "page=3" in url:
                return Reply(status=502)
            return Reply({"results": [probe(pid=9000 + i) for i in range(500)]})

        got = net.probes(BOX, get=get, now=NOW)
        assert got["count"] == 1000 and got["capped"] is True

    def test_the_whole_fleet_fits(self):
        """A little over forty thousand probes, in every status. Eight pages
        used to be the limit."""
        assert net.MOST_PAGES * net.PAGE_SIZE >= 45000
        assert net.MOST_RETURNED >= 45000


# ── A shape clicked out corner by corner ───────────────────────

SQUARE = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
# An L: the notch at the top right is outside it.
ELL = [(0.0, 0.0), (10.0, 0.0), (10.0, 4.0), (4.0, 4.0), (4.0, 10.0), (0.0, 10.0)]


class TestShapes:
    def test_inside_and_outside(self):
        assert net.inside(5, 5, SQUARE)
        assert not net.inside(15, 5, SQUARE)
        assert not net.inside(5, -1, SQUARE)

    def test_a_concave_shape(self):
        """The reason to click corners rather than drag a box: the notch of
        an L is in the box around it, and not in the L."""
        assert net.inside(2, 8, ELL)
        assert net.inside(8, 2, ELL)
        assert not net.inside(8, 8, ELL)

    def test_corners_are_read_from_the_query(self):
        got = net.read_corners("28.1,50.2;29.5,50.0;29.0,51.3")
        assert got == [(28.1, 50.2), (29.5, 50.0), (29.0, 51.3)]

    def test_a_closed_ring_is_not_a_repeated_corner(self):
        assert len(net.read_corners("0,0;1,0;1,1;0,0")) == 3

    def test_too_few_corners_is_not_an_area(self):
        with pytest.raises(ValueError, match="3 to"):
            net.read_corners("0,0;1,1")

    def test_too_many_is_refused(self):
        many = ";".join(f"{i * 0.01},{i * 0.01}" for i in range(net.MOST_CORNERS + 1))
        with pytest.raises(ValueError):
            net.read_corners(many)

    def test_a_corner_off_the_globe_is_refused(self):
        with pytest.raises(ValueError, match="not a place"):
            net.read_corners("0,0;1,0;200,1")

    def test_nonsense_is_refused(self):
        for said in ("a,b;c,d;e,f", "1;2;3", "nan,nan;1,1;2,2"):
            with pytest.raises(ValueError):
                net.read_corners(said)

    def test_the_box_around_a_shape(self):
        assert net.corners_box(ELL) == (0.0, 0.0, 10.0, 10.0)


class TestAreaByShape:
    def test_only_the_probes_inside_the_shape_are_counted(self):
        ell = [(28.0, 50.0), (29.5, 50.0), (29.5, 50.4), (28.6, 50.4), (28.6, 51.0), (28.0, 51.0)]
        raw = [probe(pid=1, lon=28.2, lat=50.8),   # in the upright of the L
               probe(pid=2, lon=29.2, lat=50.2),   # in the foot
               probe(pid=3, lon=29.2, lat=50.8)]   # in the notch: in the box, not the L
        got = net.area_history(AREA, 24, get=area_ripe(raw, {}), now=NOW, corners=ell)
        assert got["members"] == 2
        assert got["corners"] is not None

    def test_the_box_asked_about_is_the_shapes_box(self):
        asked = []
        shape = [(28.0, 50.0), (29.0, 50.5), (28.5, 51.0)]
        net.area_history((0, 0, 1, 1), 24, get=area_ripe([], {}, asked), now=NOW,
                         corners=shape)
        first = asked[0][1]
        assert (first["longitude__gte"], first["longitude__lte"]) == (28.0, 29.0)
        assert (first["latitude__gte"], first["latitude__lte"]) == (50.0, 51.0)

    def test_what_was_left_out_is_counted_and_said(self):
        """A graph of 47 probes over an area holding 80 reads as data that was
        not collected -- unless it says what the other 33 were."""
        raw = [probe(pid=1, status=1), probe(pid=2, status=2, ago=dt.timedelta(days=3)),
               probe(pid=3, status=3), probe(pid=4, status=3), probe(pid=5, status=0)]
        got = net.area_history(AREA, 24, get=area_ripe(raw, {}), now=NOW)
        assert got["left_out"] == {"offline_before": 1, "inactive": 2, "never": 1}

    def test_and_the_picture_says_it(self):
        from PIL import Image
        import io
        found = net.demo_area(AREA, 48, now=NOW)
        assert sum(found["left_out"].values()) > 0
        Image.open(io.BytesIO(net.plot_area(found)))

    def test_batches_are_asked_for_together(self, monkeypatch):
        import threading
        import time as clock
        monkeypatch.setattr(net, "IDS_PER_ASK", 2)
        running, most = [0], [0]
        lock = threading.Lock()
        raw = [probe(pid=i, lon=28.5, lat=50.5) for i in range(10)]

        def get(url, params=None, timeout=None):
            if "/7000/" in url:
                with lock:
                    running[0] += 1
                    most[0] = max(most[0], running[0])
                clock.sleep(0.05)
                with lock:
                    running[0] -= 1
                return Reply([])
            return Reply({"results": raw, "next": None})

        net.area_history(AREA, 24, get=get, now=NOW)
        assert most[0] > 1

    def test_a_batch_that_fails_fails_the_graph(self):
        """Rather than drawing its probes as if nothing happened to them."""
        raw = [probe(pid=i, lon=28.5, lat=50.5) for i in range(3)]

        def get(url, params=None, timeout=None):
            if "/7000/" in url:
                return Reply(status=502)
            return Reply({"results": raw, "next": None})

        with pytest.raises(net.ConnectivityError, match="502"):
            net.area_history(AREA, 24, get=get, now=NOW)

    def test_a_crowded_area_holds_far_more_than_it_did(self):
        assert net.MOST_AREA_PROBES >= 2000

    def test_the_demo_keeps_to_the_shape(self):
        shape = [(28.0, 50.0), (29.5, 50.0), (28.0, 51.0)]
        got = net.demo_area((0, 0, 0, 0), 24, now=NOW, corners=shape)
        assert 0 < got["members"] < 90


class TestTheShapeRoute:
    def test_a_shape_reaches_the_module(self, client, monkeypatch):
        seen = {}

        def fake(box, hours, namer=None, corners=None):
            seen.update(box=box, corners=corners)
            return net.demo_area(box, hours, now=NOW, corners=corners)

        monkeypatch.setattr(config, "DEMO_MODE", False)
        monkeypatch.setattr(net, "area_history", fake)
        got = client.get("/api/connectivity/area.png", params={
            "west": 0, "south": 0, "east": 0, "north": 0, "hours": 24,
            "poly": "28.0,50.0;29.5,50.0;29.0,51.0;28.2,51.2"})
        assert got.status_code == 200
        assert seen["corners"] == [(28.0, 50.0), (29.5, 50.0), (29.0, 51.0), (28.2, 51.2)]
        # The box is the shape's, whatever edges came with it.
        assert seen["box"] == (28.0, 50.0, 29.5, 51.2)

    def test_a_shape_that_cannot_be_read_is_said(self, client):
        got = client.get("/api/connectivity/area.png", params={
            "west": 0, "south": 0, "east": 1, "north": 1, "poly": "1,1;2,2"})
        assert got.status_code == 400
        assert "corners" in got.json()["detail"]


class TestNoClimbFromArithmetic:
    """The Gelderland graph: a smooth climb from 92 % a week ago to 100 % now,
    "lowest" on the first day, no dip anywhere. That shape was the arithmetic,
    not the network. Probes that logged a routine reconnect partway through
    the week were counted as offline from the start of the window until it."""

    def _week(self, events):
        raw = [probe(pid=i, status=1, lon=28.5, lat=50.5) for i in range(100)]
        return net.area_history(AREA, 168, get=area_ripe(raw, events), now=NOW)

    def test_routine_reconnects_through_the_week_are_a_flat_line(self):
        # Half the probes log one connect, spread across the week, with no
        # disconnect before it: a reconnect while already online.
        events = {i: [{"prb_id": i, "event": "connect",
                       "timestamp": (NOW - dt.timedelta(hours=160 - 3 * i)).timestamp()}]
                  for i in range(50)}
        got = self._week(events)
        assert got["low"] == pytest.approx(1.0)
        assert min(s for s in got["shares"] if s is not None) == pytest.approx(1.0)

    def test_a_real_outage_that_began_before_the_window_still_shows(self):
        """The log is read from before the window, so a probe that went
        offline the day before and came back inside it is offline at the start."""
        events = {i: [{"prb_id": i, "event": "disconnect",
                       "timestamp": (NOW - dt.timedelta(hours=190)).timestamp()},
                      {"prb_id": i, "event": "connect",
                       "timestamp": (NOW - dt.timedelta(hours=150)).timestamp()}]
                  for i in range(30)}
        got = self._week(events)
        assert got["shares"][0] == pytest.approx(0.7)
        assert got["now"] == pytest.approx(1.0)

    def test_a_probe_that_went_offline_keeps_its_time_online_before(self):
        """Asked for directly: the ones that are offline now still count as
        online for every hour they were online."""
        raw = [probe(pid=i, status=1, lon=28.5, lat=50.5) for i in range(9)]
        raw.append(probe(pid=99, status=2, ago=dt.timedelta(hours=10), lon=28.5, lat=50.5))
        events = {99: [{"prb_id": 99, "event": "disconnect",
                        "timestamp": (NOW - dt.timedelta(hours=10)).timestamp()}]}
        got = net.area_history(AREA, 168, get=area_ripe(raw, events), now=NOW)
        assert got["members"] == 10
        assert got["shares"][0] == pytest.approx(1.0)       # all ten online then
        assert got["now"] == pytest.approx(0.9)              # nine of ten now

    def test_the_log_is_read_from_before_the_window(self):
        asked = []
        raw = [probe(pid=1, lon=28.5, lat=50.5)]
        net.area_history(AREA, 24, get=area_ripe(raw, {}, asked), now=NOW)
        conn = [p for u, p in asked if "/7000/" in u][0]
        assert conn["start"] == int((NOW - dt.timedelta(hours=24) - net.LOOKBACK).timestamp())
