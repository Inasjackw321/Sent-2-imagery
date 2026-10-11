"""The live satellite panel, made faster: what the backend half does about it.

Three things were slowing it down, and two of them are here.

The catalogue -- the document that says which frame is newest -- was held for
three hours. A satellite that publishes every ten minutes was offered a
"newest" frame up to three hours old, and the panel's own refresh could not
move it on, because every refresh got the same held document back. It is now
renewed in the background once it is ten minutes old, and nobody waits for
the renewal.

And every tile was a bare requests.get: a new connection and a new TLS
handshake per tile, to another country, for a picture a few tens of kilobytes
long. Tiles now share a pooled connection, and two asks for the same tile at
the same moment -- which is exactly what "newest first" produces, since the
newest frame is the one already on screen -- go upstream once.

Nothing here touches the network.
"""

from __future__ import annotations

import threading
import time

import pytest

from backend import copernicus, mtg


class Answer:
    def __init__(self, body=b"PNGDATA", kind="image/png", code=200):
        self.content = body
        self.status_code = code
        self.headers = {"Content-Type": kind}
        self.ok = code == 200


@pytest.fixture(autouse=True)
def _blank():
    copernicus.forget_tiles()
    yield
    copernicus.forget_tiles()


# ── Identical tiles in flight go upstream once ────────────────────


class TestOneFetchPerTile:
    def test_two_asks_at_once_are_one_trip(self, monkeypatch):
        trips = []
        gate = threading.Event()

        def slow(url, params=None, **kw):
            trips.append(params)
            gate.wait(2)
            return Answer()

        monkeypatch.setattr(copernicus._session, "get", slow)
        got = []
        ask = {"layers": "x", "bbox": "1,2,3,4", "time": "t"}
        threads = [threading.Thread(target=lambda: got.append(copernicus.tile(ask)))
                   for _ in range(4)]
        for thread in threads:
            thread.start()
        # Let all four reach the point of asking before the first answers.
        time.sleep(0.2)
        gate.set()
        for thread in threads:
            thread.join(3)

        assert len(trips) == 1, f"{len(trips)} trips upstream for one tile"
        assert len(got) == 4
        assert all(body == b"PNGDATA" for body, _kind, _hit in got)

    def test_the_ones_that_waited_say_they_were_served_from_the_cache(self, monkeypatch):
        gate = threading.Event()

        def slow(url, params=None, **kw):
            gate.wait(2)
            return Answer()

        monkeypatch.setattr(copernicus._session, "get", slow)
        got = []
        ask = {"layers": "x", "bbox": "1,2,3,4"}
        threads = [threading.Thread(target=lambda: got.append(copernicus.tile(ask)))
                   for _ in range(3)]
        for thread in threads:
            thread.start()
        time.sleep(0.2)
        gate.set()
        for thread in threads:
            thread.join(3)
        assert sorted(hit for _b, _k, hit in got) == [False, True, True]

    def test_different_tiles_do_not_wait_for_each_other(self, monkeypatch):
        """Single-flight is per tile. A slow tile holding up the other thirty
        in its frame would be slower than no coalescing at all."""
        slow_gate = threading.Event()

        def fetch(url, params=None, **kw):
            if params["bbox"] == "slow":
                slow_gate.wait(2)
            return Answer(body=params["bbox"].encode())

        monkeypatch.setattr(copernicus._session, "get", fetch)
        held = threading.Thread(
            target=lambda: copernicus.tile({"layers": "x", "bbox": "slow"}))
        held.start()
        time.sleep(0.1)
        started = time.time()
        body, _kind, _hit = copernicus.tile({"layers": "x", "bbox": "fast"})
        assert body == b"fast"
        assert time.time() - started < 0.5
        slow_gate.set()
        held.join(3)

    def test_nothing_is_left_marked_in_flight_after_a_success(self, monkeypatch):
        monkeypatch.setattr(copernicus._session, "get", lambda *a, **k: Answer())
        copernicus.tile({"layers": "x", "bbox": "1"})
        assert copernicus._inflight == {}

    def test_or_after_a_failure(self, monkeypatch):
        """A key left behind would make every later ask for that tile wait
        INFLIGHT_WAIT seconds for a fetch that is never coming."""
        def refuse(*a, **k):
            raise copernicus.requests.RequestException("no")

        monkeypatch.setattr(copernicus._session, "get", refuse)
        with pytest.raises(copernicus.CopernicusError):
            copernicus.tile({"layers": "x", "bbox": "1"})
        assert copernicus._inflight == {}

    def test_a_first_fetch_that_failed_does_not_fail_the_second_by_proxy(self, monkeypatch):
        """The failure may have been the first asker's alone -- a refusal,
        an error page -- so the one that waited asks for itself rather than
        inheriting it."""
        trips = []
        gate = threading.Event()

        def first_bad_then_good(url, params=None, **kw):
            trips.append(1)
            if len(trips) == 1:
                gate.wait(2)
                return Answer(code=500)
            return Answer()

        monkeypatch.setattr(copernicus._session, "get", first_bad_then_good)
        ask = {"layers": "x", "bbox": "1"}
        out = {}

        def first():
            try:
                copernicus.tile(ask)
            except copernicus.CopernicusError as exc:
                out["first"] = str(exc)

        a = threading.Thread(target=first)
        a.start()
        time.sleep(0.1)
        b = threading.Thread(target=lambda: out.setdefault("second", copernicus.tile(ask)))
        b.start()
        time.sleep(0.1)
        gate.set()
        a.join(3)
        b.join(3)
        assert "500" in out["first"]
        assert out["second"][0] == b"PNGDATA"
        assert len(trips) == 2

    def test_a_host_known_to_be_down_is_not_asked_again_by_the_waiter(self, monkeypatch):
        trips = []
        gate = threading.Event()

        def unreachable(url, params=None, **kw):
            trips.append(1)
            gate.wait(2)
            raise copernicus.requests.RequestException("Read timed out")

        monkeypatch.setattr(copernicus._session, "get", unreachable)
        ask = {"layers": "x", "bbox": "1"}
        errors = []

        def go():
            try:
                copernicus.tile(ask)
            except copernicus.CopernicusError as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=go) for _ in range(3)]
        for thread in threads:
            thread.start()
        time.sleep(0.2)
        gate.set()
        for thread in threads:
            thread.join(3)
        assert len(errors) == 3
        assert len(trips) == 1, "the waiters went upstream to a host already known to be down"


class TestOneConnectionPool:
    def test_tiles_go_through_the_pooled_session(self, monkeypatch):
        """Not requests.get, which is a fresh connection and handshake each
        time. Stubbing the old path must not be enough to serve a tile."""
        def forbidden(*a, **k):
            raise AssertionError("a tile went out over a fresh connection")

        monkeypatch.setattr(copernicus.requests, "get", forbidden)
        monkeypatch.setattr(copernicus._session, "get", lambda *a, **k: Answer())
        assert copernicus.tile({"layers": "x", "bbox": "1"})[0] == b"PNGDATA"

    def test_the_pool_is_wide_enough_for_two_layers_at_once(self):
        """Six connections per origin from the browser, two layers -- the one
        on screen and the one fetching ahead. A pool of one would serialise
        all of it behind a single socket."""
        adapter = copernicus._session.get_adapter("https://view.eumetsat.int/")
        assert adapter._pool_maxsize >= 12

    def test_the_session_says_who_it_is(self):
        assert copernicus._session.headers["User-Agent"] == copernicus.config.USER_AGENT


# ── The catalogue is renewed behind the panel, not in front of it ──


@pytest.fixture
def catalogue(monkeypatch):
    """A held catalogue of a chosen age, and a count of downloads."""
    fetched = []

    class Reply:
        ok = True
        status_code = 200
        text = "<new/>"

    def get(*a, **k):
        fetched.append(1)
        return Reply()

    monkeypatch.setattr(mtg.requests, "get", get)

    def hold(age_seconds, text="<old/>"):
        mtg._xml = text
        mtg._xml_at = time.time() - age_seconds
        mtg._xml_renewing = False

    yield hold, fetched
    mtg._xml = None
    mtg._xml_at = 0.0
    mtg._xml_renewing = False


def _settle():
    for _ in range(50):
        if not mtg._xml_renewing:
            return
        time.sleep(0.02)


class TestTheCatalogue:
    def test_a_fresh_one_is_answered_without_asking(self, catalogue):
        hold, fetched = catalogue
        hold(60)
        assert mtg.capabilities() == "<old/>"
        _settle()
        assert fetched == []

    def test_an_old_one_is_still_answered_at_once(self, catalogue):
        """Nobody waits for a multi-megabyte download to open the panel."""
        hold, _fetched = catalogue
        hold(mtg.CAPABILITIES_FRESH_SECONDS + 5)
        assert mtg.capabilities() == "<old/>"

    def test_and_a_new_one_is_fetched_behind_it(self, catalogue):
        """Which is the whole point: the newest frame was up to three hours
        behind a satellite that publishes every ten minutes, and the panel's
        refresh could not move it on."""
        hold, fetched = catalogue
        hold(mtg.CAPABILITIES_FRESH_SECONDS + 5)
        mtg.capabilities()
        _settle()
        assert fetched == [1]
        assert mtg.capabilities() == "<new/>"

    def test_only_one_renewal_at_a_time(self, catalogue, monkeypatch):
        hold, fetched = catalogue
        gate = threading.Event()

        class Reply:
            ok = True
            status_code = 200
            text = "<new/>"

        def slow(*a, **k):
            fetched.append(1)
            gate.wait(2)
            return Reply()

        monkeypatch.setattr(mtg.requests, "get", slow)
        hold(mtg.CAPABILITIES_FRESH_SECONDS + 5)
        for _ in range(5):
            mtg.capabilities()
        gate.set()
        _settle()
        assert fetched == [1]

    def test_a_renewal_that_fails_keeps_what_was_held(self, catalogue, monkeypatch):
        hold, _fetched = catalogue

        def down(*a, **k):
            raise mtg.requests.RequestException("no")

        monkeypatch.setattr(mtg.requests, "get", down)
        hold(mtg.CAPABILITIES_FRESH_SECONDS + 5)
        mtg.capabilities()
        _settle()
        assert mtg.capabilities() == "<old/>"

    def test_and_is_free_to_be_tried_again(self, catalogue, monkeypatch):
        hold, _fetched = catalogue

        def down(*a, **k):
            raise mtg.requests.RequestException("no")

        monkeypatch.setattr(mtg.requests, "get", down)
        hold(mtg.CAPABILITIES_FRESH_SECONDS + 5)
        mtg.capabilities()
        _settle()
        assert mtg._xml_renewing is False

    def test_nothing_held_is_waited_for(self, catalogue):
        _hold, fetched = catalogue
        mtg._xml = None
        assert mtg.capabilities() == "<new/>"
        assert fetched == [1]

    def test_one_past_its_hard_limit_is_waited_for_too(self, catalogue):
        """Answering a day-old catalogue straight away would offer frames
        from yesterday as the newest."""
        hold, fetched = catalogue
        hold(mtg.CACHE_SECONDS + 5)
        assert mtg.capabilities() == "<new/>"
        assert fetched == [1]

    def test_asking_for_a_refresh_still_waits_for_one(self, catalogue):
        hold, fetched = catalogue
        hold(60)
        assert mtg.capabilities(refresh=True) == "<new/>"
        assert fetched == [1]

    def test_the_fresh_window_is_about_one_frame_of_meteosat(self):
        assert mtg.CAPABILITIES_FRESH_SECONDS <= 15 * 60


class TestAnEmptyTime:
    """The page now always sends a time, so a tile's address is the same
    however the layer asking for it was built. When there is no time to give
    it is empty -- and an empty TIME forwarded to a strict WMS is answered
    with an exception, not with the default frame."""

    def test_is_not_forwarded(self):
        assert "time" not in copernicus.tile_params({"layers": "x", "time": ""})

    def test_a_real_one_still_is(self):
        assert copernicus.tile_params({"layers": "x", "time": "t"})["time"] == "t"

    def test_and_an_empty_styles_still_goes_through(self):
        """WMS 1.3.0 requires STYLES even when it is empty. Dropping every
        empty parameter would have broken every tile to fix one."""
        assert copernicus.tile_params({"layers": "x", "styles": ""})["styles"] == ""

    def test_with_or_without_it_is_one_tile_in_the_cache(self):
        assert copernicus.tile_key(copernicus.tile_params({"layers": "x", "time": ""})) \
            == copernicus.tile_key(copernicus.tile_params({"layers": "x"}))
