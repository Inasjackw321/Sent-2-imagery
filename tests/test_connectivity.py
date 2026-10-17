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

    def test_offline_for_more_than_a_week_is_left_off(self):
        """A probe nobody has plugged back in is not news."""
        assert net.classify(2, NOW - dt.timedelta(days=8), SIX_HOURS, NOW) is None
        assert net.classify(2, NOW - dt.timedelta(days=6), SIX_HOURS, NOW) == "gone"

    def test_never_connected_and_abandoned_say_nothing(self):
        assert net.classify(0, NOW, SIX_HOURS, NOW) is None
        assert net.classify(3, NOW, SIX_HOURS, NOW) is None
        assert net.classify(None, NOW, SIX_HOURS, NOW) is None

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
        assert params["status__in"] == "1,2"
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

    def test_a_refused_status_filter_is_asked_again_without_it(self):
        """The status filter is the one parameter not taken from the published
        examples. If this API version refuses it, the answer is the same
        filtered here -- only longer."""
        get, asked = answering({"results": [probe(status=3), probe(pid=2)], "next": None},
                               first_status=400)
        got = net.probes(BOX, get=get, now=NOW)
        assert "status__in" in asked[0][1]
        assert "status__in" not in asked[1][1]
        # And the abandoned one it no longer filtered out is filtered here.
        assert got["count"] == 1

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
