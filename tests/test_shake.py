"""The Raspberry Shakes: what is drawn when the station index answers, and
what is drawn when it does not.

Nothing here touches the network. The interesting behaviour is all in the
falling back: a Shake's real position comes from Raspberry Shake's own station
service, and when that cannot be reached the pin has to land on the town
instead and the panel has to say which of the two it is showing. Presenting a
town centre as an instrument's position is the one failure mode that looks
exactly like success.
"""

from __future__ import annotations

import pytest
import requests

from backend import seismic, shake


@pytest.fixture(autouse=True)
def _blank():
    shake.forget()
    yield
    shake.forget()


def _answer(text: str, *, ok: bool = True, status: int = 200):
    class Reply:
        pass

    reply = Reply()
    reply.ok = ok
    reply.status_code = status
    reply.text = text
    return lambda *a, **k: reply


def _refuses(*_a, **_k):
    raise requests.RequestException("connection refused")


REAL = """#Network|Station|Latitude|Longitude|Elevation|SiteName|StartTime|EndTime
AM|RD834|47.8501|35.1201|85.0|Zaporizhzhia Shake|2022-01-01T00:00:00|
AM|R2DB7|49.9700|36.2500|140.0|Kharkiv Shake|2021-06-01T00:00:00|
"""


# ── The stations asked for ─────────────────────────────────────


ASKED_FOR = ["RD834", "R2DB7", "S29F5", "SE569",
             "S5D35", "R1F39", "S994C", "R85A6"]


def test_every_one_asked_for_is_there():
    got = shake.stations(get=_refuses)
    assert [s["station"] for s in got["stations"]] == ASKED_FOR


def test_every_one_is_on_the_am_network():
    assert all(s["network"] == "AM" for s in shake.stations(get=_refuses)["stations"])


def test_every_one_carries_a_place_name():
    assert all(s["place"] for s in shake.stations(get=_refuses)["stations"])


def test_the_places_are_the_ones_asked_for():
    places = {s["station"]: s["place"] for s in shake.stations(get=_refuses)["stations"]}
    assert places == {
        "RD834": "Zaporizhzhia", "R2DB7": "Kharkiv",
        "S29F5": "Khrystynivka", "SE569": "Rivne",
        "S5D35": "Sillam\u00e4e, Estonia",
        "R1F39": "Dubai, United Arab Emirates",
        "S994C": "Dubai, United Arab Emirates",
        "R85A6": "Abu Dhabi, United Arab Emirates"}


def test_no_station_code_is_listed_twice():
    """Two entries for one code would be two pins on one instrument and two
    windows with the same id."""
    assert len(ASKED_FOR) == len(set(ASKED_FOR))


def test_every_position_is_a_real_one():
    for s in shake.stations(get=_refuses)["stations"]:
        assert -90.0 <= s["lat"] <= 90.0, s
        assert -180.0 <= s["lon"] <= 180.0, s
        assert (s["lat"], s["lon"]) != (0.0, 0.0), s


def test_each_one_is_in_the_country_its_name_says():
    """A digit lost out of a coordinate puts a Shake in the sea, and nothing
    on the map would say so -- the pin is a pin."""
    boxes = {
        "Zaporizhzhia": (44.0, 53.0, 22.0, 41.0),
        "Kharkiv": (44.0, 53.0, 22.0, 41.0),
        "Khrystynivka": (44.0, 53.0, 22.0, 41.0),
        "Rivne": (44.0, 53.0, 22.0, 41.0),
        "Sillam\u00e4e, Estonia": (57.5, 60.0, 21.5, 28.3),
        "Dubai, United Arab Emirates": (24.7, 25.4, 55.0, 55.8),
        "Abu Dhabi, United Arab Emirates": (24.0, 24.7, 54.0, 55.0),
    }
    for s in shake.stations(get=_refuses)["stations"]:
        south, north, west, east = boxes[s["place"]]
        assert south < s["lat"] < north, s
        assert west < s["lon"] < east, s


def test_the_channel_is_the_vertical_geophone():
    assert all(s["channel"] == "EHZ" for s in shake.stations(get=_refuses)["stations"])


def test_the_location_code_is_zero_zero():
    assert all(s["loc"] == "00" for s in shake.stations(get=_refuses)["stations"])


def test_each_links_to_raspberry_shakes_own_viewer():
    for s in shake.stations(get=_refuses)["stations"]:
        assert s["view"] == (f"https://dataview.raspberryshake.org/#/AM/"
                             f"{s['station']}/00/EHZ")


# ── Where they are, and whose figure that is ───────────────────


def test_a_reachable_index_gives_the_instruments_own_position():
    got = shake.stations(get=_answer(REAL))
    one = next(s for s in got["stations"] if s["station"] == "RD834")
    assert (one["lat"], one["lon"]) == (47.8501, 35.1201)
    assert one["placed"] == "station"


def test_a_station_the_index_does_not_know_keeps_its_town():
    got = shake.stations(get=_answer(REAL))
    one = next(s for s in got["stations"] if s["station"] == "SE569")
    assert (one["lat"], one["lon"]) == (50.6199, 26.2516)
    assert one["placed"] == "town"


def test_an_unreachable_index_leaves_every_pin_on_what_was_published():
    got = shake.stations(get=_refuses)
    assert all(s["placed"] in ("town", "given") for s in got["stations"])
    assert not any(s["placed"] == "station" for s in got["stations"])


def test_a_station_that_came_with_coordinates_is_not_called_a_town():
    """It is the position published for the instrument, which is a better
    claim than the middle of a city and should not be marked as a worse one."""
    got = shake.stations(get=_refuses)
    one = next(s for s in got["stations"] if s["station"] == "R1F39")
    assert one["placed"] == "given"


def test_a_station_that_came_with_only_a_name_still_says_town():
    got = shake.stations(get=_refuses)
    one = next(s for s in got["stations"] if s["station"] == "RD834")
    assert one["placed"] == "town"


def test_the_index_still_outranks_a_published_position():
    given = ("#h\n"
             "AM|R1F39|25.3000|55.4000|12.0|Dubai Shake|2021-01-01T00:00:00|\n")
    one = next(s for s in shake.stations(get=_answer(given))["stations"]
               if s["station"] == "R1F39")
    assert (one["lat"], one["lon"]) == (25.3, 55.4)
    assert one["placed"] == "station"


def test_an_unreachable_index_is_said_rather_than_raised():
    got = shake.stations(get=_refuses)
    assert "could not be reached" in got["trouble"]
    assert "connection refused" in got["trouble"]


def test_a_timeout_is_said_in_a_few_words_rather_than_a_stack_of_them():
    """The real exception is the connection pool, the query string and a
    nested cause. It goes in a panel nine pixels tall."""
    got = shake.stations(get=lambda *a, **k: (_ for _ in ()).throw(
        requests.ConnectionError(
            "HTTPSConnectionPool(host='data.raspberryshake.org', port=443): "
            "Max retries exceeded with url: /fdsnws/station/1/query?net=AM&"
            "sta=RD834%2CR2DB7 (Caused by ProxyError('Unable to connect to "
            "proxy', OSError('Tunnel connection failed: 403 Forbidden')))")))
    assert "Tunnel connection failed" in got["trouble"]
    assert "HTTPSConnectionPool" not in got["trouble"]
    assert len(got["trouble"]) < 140


def test_a_reason_with_nothing_recognisable_in_it_is_merely_trimmed():
    got = shake.stations(get=lambda *a, **k: (_ for _ in ()).throw(
        requests.RequestException("something nobody has seen before " * 8)))
    assert got["trouble"].endswith("…")
    assert len(got["trouble"]) < 200


def test_a_short_reason_is_left_alone():
    got = shake.stations(get=lambda *a, **k: (_ for _ in ()).throw(
        requests.RequestException("no route to host")))
    assert got["trouble"].endswith("no route to host")


def test_a_refusing_index_says_what_it_answered():
    got = shake.stations(get=_answer("", ok=False, status=503))
    assert "503" in got["trouble"]


def test_an_index_that_lists_none_of_them_says_so():
    got = shake.stations(get=_answer("#Network|Station|Latitude\n"))
    assert "listed none" in got["trouble"]


def test_a_reachable_index_leaves_no_complaint_behind():
    assert shake.stations(get=_answer(REAL))["trouble"] == ""


def test_elevation_comes_through_when_the_index_has_it():
    got = shake.stations(get=_answer(REAL))
    one = next(s for s in got["stations"] if s["station"] == "R2DB7")
    assert one["elevation_m"] == 140


def test_elevation_is_absent_rather_than_zero_when_unknown():
    got = shake.stations(get=_refuses)
    assert all(s["elevation_m"] is None for s in got["stations"])


# ── Reading the FDSN text ──────────────────────────────────────


def test_read_places_keys_on_the_station_code():
    assert set(shake.read_places(REAL)) == {"RD834", "R2DB7"}


def test_read_places_ignores_the_header():
    assert "Station" not in shake.read_places(REAL)


def test_read_places_ignores_blank_lines():
    assert len(shake.read_places(REAL + "\n\n   \n")) == 2


def test_read_places_skips_a_line_with_too_few_fields():
    assert shake.read_places("AM|RD834|47.0|35.0\n") == {}


def test_read_places_skips_a_position_that_is_not_a_number():
    assert shake.read_places("AM|RD834|north|35.0|85|Shake|\n") == {}


def test_read_places_skips_the_null_island():
    """0,0 is what a Shake with no position set reports, and it is at sea."""
    assert shake.read_places("AM|RD834|0.0|0.0|0|Shake|\n") == {}


def test_read_places_keeps_a_station_genuinely_on_the_equator():
    got = shake.read_places("AM|RD834|0.0|35.0|0|Shake|\n")
    assert got["RD834"]["lon"] == 35.0


def test_read_places_survives_an_empty_reply():
    assert shake.read_places("") == {}


def test_read_places_survives_none():
    assert shake.read_places(None) == {}


def test_read_places_keeps_the_site_name():
    assert shake.read_places(REAL)["RD834"]["name"] == "Zaporizhzhia Shake"


def test_read_places_tolerates_padding_around_the_fields():
    got = shake.read_places(" AM | RD834 | 47.5 | 35.5 | 90 | Shake |\n")
    assert got["RD834"]["lat"] == 47.5


def test_read_places_rounds_a_fractional_elevation():
    got = shake.read_places("AM|RD834|47.5|35.5|85.4|Shake|\n")
    assert got["RD834"]["elevation_m"] == 85


def test_read_places_leaves_an_unreadable_elevation_absent():
    got = shake.read_places("AM|RD834|47.5|35.5||Shake|\n")
    assert got["RD834"]["elevation_m"] is None


# ── Holding on to an answer ────────────────────────────────────


def test_a_fetched_position_is_not_fetched_twice():
    calls = []

    def counting(*a, **k):
        calls.append(1)
        return _answer(REAL)()

    shake.stations(get=counting)
    shake.stations(get=counting)
    assert len(calls) == 1


def test_a_refusal_is_not_held_on_to():
    """An unreachable index is worth trying again; a position is not."""
    calls = []

    def counting(*a, **k):
        calls.append(1)
        raise requests.RequestException("down")

    shake.stations(get=counting)
    shake.stations(get=counting)
    assert len(calls) == 2


def test_a_refresh_asks_again():
    calls = []

    def counting(*a, **k):
        calls.append(1)
        return _answer(REAL)()

    shake.stations(get=counting)
    shake.stations(refresh=True, get=counting)
    assert len(calls) == 2


def test_forgetting_drops_what_was_fetched():
    shake.stations(get=_answer(REAL))
    shake.forget()
    got = shake.stations(get=_refuses)
    assert not any(s["placed"] == "station" for s in got["stations"])


def test_a_later_refusal_keeps_the_positions_already_found():
    shake.stations(get=_answer(REAL))
    shake.forget()
    shake.stations(get=_answer(REAL))
    got = shake.stations(refresh=True, get=_refuses)
    one = next(s for s in got["stations"] if s["station"] == "RD834")
    assert one["placed"] == "station"


def test_the_index_is_asked_for_exactly_these_stations():
    seen = {}

    def watching(url, **kwargs):
        seen.update(kwargs.get("params") or {})
        return _answer(REAL)()

    shake.stations(get=watching)
    assert seen["net"] == "AM"
    assert set(seen["sta"].split(",")) == set(ASKED_FOR)


def test_the_index_is_asked_in_the_text_format():
    seen = {}

    def watching(url, **kwargs):
        seen.update(kwargs.get("params") or {})
        return _answer(REAL)()

    shake.stations(get=watching)
    assert seen["format"] == "text"


def test_the_index_asked_is_raspberry_shakes_own():
    seen = []

    def watching(url, **kwargs):
        seen.append(url)
        return _answer(REAL)()

    shake.stations(get=watching)
    assert seen == [shake.STATION_URL]
    assert "raspberryshake.org" in seen[0]


def test_the_index_is_not_waited_on_forever():
    seen = {}

    def watching(url, **kwargs):
        seen.update(kwargs)
        return _answer(REAL)()

    shake.stations(get=watching)
    assert 0 < seen["timeout"] <= 30


# ── Telling them apart from the federated stations ─────────────


def test_am_is_ours():
    assert shake.ours("AM")


def test_a_lowercase_am_is_still_ours():
    assert shake.ours("am")


def test_another_network_is_not_ours():
    assert not shake.ours("IU")


def test_a_network_that_merely_contains_am_is_not_ours():
    assert not shake.ours("AMX")


def test_no_network_at_all_is_not_ours():
    assert not shake.ours("")
    assert not shake.ours(None)


def test_a_shake_recording_is_only_asked_of_raspberry_shake():
    """The federated nodes have never archived AM, so asking them is six
    timeouts for a guaranteed nothing."""
    assert seismic.centres_for("AM") == [(shake.ATTRIBUTION, shake.DATA_URL)]


def test_the_shake_archive_is_raspberry_shakes_own_dataselect():
    (_, url), = seismic.centres_for("AM")
    assert "raspberryshake.org" in url
    assert url.endswith("/fdsnws/dataselect/1/query")


def test_an_ordinary_network_still_walks_every_data_centre():
    assert seismic.centres_for("IU") == list(seismic.DATA_CENTRES)


def test_no_federated_centre_is_asked_for_a_shake():
    urls = [u for _, u in seismic.centres_for("AM")]
    assert not any(u in urls for _, u in seismic.DATA_CENTRES)


def test_the_choice_of_archive_does_not_mutate_the_list_of_centres():
    before = list(seismic.DATA_CENTRES)
    seismic.centres_for("IU").append(("Nowhere", "https://example.invalid"))
    assert seismic.DATA_CENTRES == before


# ── The panel's own wording ────────────────────────────────────


def test_the_panel_is_told_these_are_not_research_instruments():
    said = shake.stations(get=_refuses)["about"].lower()
    assert "not research instruments" in said


def test_the_panel_credits_raspberry_shake():
    assert "Raspberry Shake" in shake.stations(get=_refuses)["attribution"]


def test_the_demo_panel_holds_the_same_stations():
    got = shake.demo_stations()
    assert [s["station"] for s in got["stations"]] == ASKED_FOR


def test_the_demo_panel_says_it_is_a_demo():
    assert shake.demo_stations()["demo"] is True


def test_the_demo_panel_complains_about_nothing():
    """Offline is the expected state there, not a fault worth reporting."""
    assert shake.demo_stations()["trouble"] == ""


def test_the_demo_panel_reaches_no_network(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("the demo panel asked the network")

    monkeypatch.setattr(requests, "get", forbidden)
    assert shake.demo_stations()["stations"]


# ── What else is in view ───────────────────────────────────────
#
# The named stations above are a decision: somebody is watching those places.
# This half is a question -- what else of this network is under the rectangle
# on screen -- and it is the half that answers "is there anything at all
# listening on that coast", which is not a question a fixed list can answer.


BOX = (55.0, 22.0, 60.0, 27.0)

CHANNELS_SAID = """#Network|Station|Location|Channel|Latitude|Longitude|\
Elevation|Depth|Azimuth|Dip|SensorDescription|Scale|ScaleFreq|ScaleUnits|\
SampleRate|StartTime|EndTime
AM|R7B2C|00|EHZ|23.5880|58.3829|20.0|0|0|-90|Raspberry Shake|1|1|M/S|100|2021-02-03T00:00:00|
AM|R7B2C|00|HDF|23.5880|58.3829|20.0|0|0|-90|Raspberry Boom|1|1|PA|100|2021-02-03T00:00:00|
AM|RB00M|00|HDF|24.4000|56.7000|300.0|0|0|-90|Raspberry Boom|1|1|PA|100|2023-01-01T00:00:00|
"""

NAMES_SAID = """#Network|Station|Latitude|Longitude|Elevation|SiteName|StartTime|EndTime
AM|R7B2C|23.5880|58.3829|20.0|Muscat rooftop|2021-02-03T00:00:00|
AM|RB00M|24.4000|56.7000|300.0||2023-01-01T00:00:00|
"""


def _index(channels: str = CHANNELS_SAID, names: str = NAMES_SAID,
           *, asked: list | None = None):
    """A stand-in for the AM station index that answers both levels."""
    def get(url, params=None, timeout=None, headers=None):
        if asked is not None:
            asked.append(dict(params or {}))
        return _answer(channels if (params or {}).get("level") == "channel"
                       else names)()
    return get


class TestWhatIsInThisRectangle:
    def test_the_index_is_asked_about_the_box_it_was_given(self):
        asked: list = []
        shake.nearby(BOX, get=_index(asked=asked))
        first = asked[0]
        assert first["net"] == "AM"
        assert (first["minlongitude"], first["minlatitude"]) == (55.0, 22.0)
        assert (first["maxlongitude"], first["maxlatitude"]) == (60.0, 27.0)

    def test_and_only_for_channels_this_app_can_plot(self):
        asked: list = []
        shake.nearby(BOX, get=_index(asked=asked))
        assert asked[0]["level"] == "channel"
        assert set(asked[0]["cha"].split(",")) == set(shake.CHANNELS)

    def test_a_station_is_one_entry_however_many_channels_it_has(self):
        got = shake.nearby(BOX, get=_index())
        # In distance order from the middle of the rectangle, which is what
        # the panel shows and what a cap would keep.
        assert [s["station"] for s in got["stations"]] == ["RB00M", "R7B2C"]

    def _by_code(self, got):
        return {s["station"]: s for s in got["stations"]}

    def test_a_shake_that_also_hears_the_air_is_still_a_seismograph(self):
        """Its trace button should draw ground motion: that is what a
        seismo-acoustic station is mostly for, and the microphone is said
        beside it rather than instead of it."""
        one = self._by_code(shake.nearby(BOX, get=_index()))["R7B2C"]
        assert one["kind"] == "seismograph"
        assert one["channel"] == "EHZ"
        assert one["hears_air"] is True

    def test_a_boom_on_its_own_is_a_microphone(self):
        """And must not be listed as a seismograph. What crosses the red line
        on one of these is a pressure wave in the air -- a blast, a sonic
        boom, thunder -- and the ground may not have moved at all."""
        two = self._by_code(shake.nearby(BOX, get=_index()))["RB00M"]
        assert two["kind"] == "microphone"
        assert two["channel"] == shake.MICROPHONE

    def test_what_its_owner_called_it_is_used(self):
        got = self._by_code(shake.nearby(BOX, get=_index()))
        assert got["R7B2C"]["place"] == "Muscat rooftop"

    def test_and_a_station_with_no_name_gets_its_position(self):
        """Not the nearest town. This app has no offline gazetteer, and
        guessing a city from a coordinate is how a Shake in Seeb ends up
        labelled Muscat and then quoted as though somebody had checked."""
        two = self._by_code(shake.nearby(BOX, get=_index()))["RB00M"]
        assert two["named"] is False
        assert "24.4" in two["place"] and "56.7" in two["place"]

    def test_a_name_that_cannot_be_fetched_costs_only_the_name(self):
        def get(url, params=None, timeout=None, headers=None):
            if (params or {}).get("level") == "channel":
                return _answer(CHANNELS_SAID)()
            raise requests.RequestException("no")

        got = shake.nearby(BOX, get=get)
        assert sorted(s["station"] for s in got["stations"]) == ["R7B2C", "RB00M"]

    def test_an_index_that_will_not_answer_is_said_rather_than_raised(self):
        got = shake.nearby(BOX, get=_refuses)
        assert got["stations"] == [] and got["count"] == 0
        assert "could not be reached" in got["trouble"]

    def test_nothing_here_is_an_answer_not_a_fault(self):
        """204 is the index saying the rectangle is empty. Reported as a
        failure, it would read as a broken panel over ground that genuinely
        has no instruments on it."""
        got = shake.nearby(BOX, get=_answer("", status=204))
        assert got["count"] == 0 and got["trouble"] == ""

    def test_and_it_is_asked_for_in_the_query(self):
        """Rather than 404, which would have to be told apart from a genuine
        one -- a wrong path, a service that has moved -- and cannot be."""
        asked: list = []
        shake.nearby(BOX, get=_index(asked=asked))
        assert asked[0]["nodata"] == "204"

    def test_a_refusal_is_said_with_its_status(self):
        got = shake.nearby(BOX, get=_answer("", ok=False, status=413))
        assert "413" in got["trouble"]

    def test_the_same_rectangle_is_not_asked_about_twice(self):
        asked: list = []
        get = _index(asked=asked)
        shake.nearby(BOX, get=get)
        rounds = len(asked)
        shake.nearby(BOX, get=get)
        assert len(asked) == rounds

    def test_and_a_different_one_is(self):
        asked: list = []
        get = _index(asked=asked)
        shake.nearby(BOX, get=get)
        shake.nearby((0.0, 0.0, 1.0, 1.0), get=get)
        assert len(asked) > 2

    def test_the_nearest_to_the_middle_come_first(self):
        """Not the alphabet. A capped answer that kept whichever codes sorted
        early would be a list of instruments somewhere else in the view, and
        the panel calls it "nearest shown"."""
        rows = "\n".join(
            f"AM|S{i:04d}|00|EHZ|{22.0 + i * 0.4:.4f}|55.2|10|0|0|-90|S|1|1|M/S|100|2020-01-01T00:00:00|"
            for i in range(12))
        got = shake.nearby(BOX, get=_index(channels=rows, names=""))
        middles = [abs(s["lat"] - 24.5) for s in got["stations"]]
        assert middles == sorted(middles)

    def test_too_many_are_capped_and_the_cap_is_admitted(self, monkeypatch):
        monkeypatch.setattr(shake, "NEARBY_LIMIT", 3)
        rows = "\n".join(
            f"AM|S{i:04d}|00|EHZ|{22.5 + i * 0.1:.4f}|56.0|10|0|0|-90|S|1|1|M/S|100|2020-01-01T00:00:00|"
            for i in range(9))
        got = shake.nearby(BOX, get=_index(channels=rows, names=""))
        assert len(got["stations"]) == 3
        assert got["count"] == 9 and got["capped"] is True

    def test_only_this_network_is_kept(self):
        """The rectangle is asked of Raspberry Shake's own index, but the
        answer decides what gets drawn in the Shake colour, and a research
        station drawn as a hobby instrument is the mix-up this whole layer
        exists to prevent."""
        rows = ("AM|GOOD1|00|EHZ|23.0|57.0|10|0|0|-90|S|1|1|M/S|100|2020-01-01T00:00:00|\n"
                "IU|BIG01|00|BHZ|23.1|57.1|10|0|0|-90|S|1|1|M/S|100|2020-01-01T00:00:00|")
        got = shake.nearby(BOX, get=_index(channels=rows, names=""))
        assert [s["station"] for s in got["stations"]] == ["GOOD1"]

    def test_a_retired_instrument_is_left_out(self):
        """It has no live trace to draw, and a dot that plots nothing is
        worse than no dot: it reads as an instrument that has gone quiet."""
        rows = ("AM|GONE1|00|EHZ|23.0|57.0|10|0|0|-90|S|1|1|M/S|100|"
                "2015-01-01T00:00:00|2019-06-01T00:00:00")
        assert shake.nearby(BOX, get=_index(channels=rows, names=""))["count"] == 0

    def test_a_station_with_no_position_set_is_left_out(self):
        """0,0 is in the Atlantic. Better no dot than a dot off Ghana."""
        rows = ("AM|NULL1|00|EHZ|0.0|0.0|10|0|0|-90|S|1|1|M/S|100|"
                "2020-01-01T00:00:00|")
        assert shake.nearby(BOX, get=_index(channels=rows, names=""))["count"] == 0


class TestStillRunning:
    def test_no_end_time_means_it_is_still_going(self):
        assert shake.still_running("") is True

    def test_an_end_time_in_the_future_does_too(self):
        assert shake.still_running("2099-01-01T00:00:00") is True

    def test_and_one_in_the_past_does_not(self):
        assert shake.still_running("2019-06-01T00:00:00") is False

    def test_a_date_that_cannot_be_read_keeps_the_station(self):
        """A station is not written off over a date this could not parse."""
        assert shake.still_running("whenever") is True


class TestTheTwoHalvesTogether:
    def test_the_named_ones_are_there_whatever_the_rectangle_says(self):
        """They were asked for by name because somebody is watching those
        places. A list that emptied itself when the map moved would be right
        only by accident."""
        got = shake.stations(get=_index(), box=BOX)
        codes = [s["station"] for s in got["stations"]]
        assert codes[:len(ASKED_FOR)] == ASKED_FOR

    def test_and_what_is_in_view_comes_after_them(self):
        got = shake.stations(get=_index(), box=BOX)
        assert sorted(s["station"] for s in got["stations"][len(ASKED_FOR):]) \
            == ["R7B2C", "RB00M"]

    def test_a_named_station_found_again_is_still_one_dot(self):
        rows = ("AM|R1F39|00|EHZ|25.2361|55.3570|10|0|0|-90|S|1|1|M/S|100|"
                "2020-01-01T00:00:00|")
        got = shake.stations(get=_index(channels=rows, names=""), box=BOX)
        codes = [s["station"] for s in got["stations"]]
        assert codes.count("R1F39") == 1

    def test_the_named_ones_are_marked_as_asked_for(self):
        got = shake.stations(get=_index(), box=BOX)
        asked = {s["station"] for s in got["stations"] if s["asked_for"]}
        assert asked == set(ASKED_FOR)

    def test_how_many_are_in_view_is_counted_separately(self):
        """Eight named stations elsewhere in the world plus two here is not
        "ten in view", and the panel says the second number."""
        got = shake.stations(get=_index(), box=BOX)
        assert got["in_view"] == 2 and got["searched"] is True

    def test_the_microphones_are_counted(self):
        assert shake.stations(get=_index(), box=BOX)["microphones"] == 1

    def test_without_a_rectangle_nothing_is_searched(self):
        got = shake.stations(get=_index())
        assert got["searched"] is False and got["in_view"] == 0
        assert [s["station"] for s in got["stations"]] == ASKED_FOR

    def test_one_trouble_line_rather_than_the_same_fault_twice(self):
        """Both halves ask the same index, so when it is down they fail
        together -- and saying it twice reads as two separate faults."""
        got = shake.stations(get=_refuses, box=BOX)
        assert got["trouble"].count("could not be reached") == 1

    def test_the_demo_panel_finds_some_in_the_rectangle(self):
        """Offline is where this panel gets looked at without a network, and
        a search that always came back empty there cannot be told apart from
        a search that is broken."""
        got = shake.demo_stations(BOX)
        assert got["in_view"] >= 1 and got["searched"] is True
        assert got["microphones"] >= 1
        for s in got["stations"][len(ASKED_FOR):]:
            assert 55.0 <= s["lon"] <= 60.0 and 22.0 <= s["lat"] <= 27.0

    def test_the_demo_panel_still_reaches_no_network(self, monkeypatch):
        def forbidden(*a, **k):
            raise AssertionError("the demo panel asked the network")

        monkeypatch.setattr(requests, "get", forbidden)
        assert shake.demo_stations(BOX)["stations"]


class TestTheEndpoint:
    """The route, because the rectangle has to survive the trip.

    A direct call to stations() cannot tell you whether the route remembered
    to pass the box through, and a route that quietly dropped it would look
    exactly like a network with nothing in view.
    """

    def _no_network(self, monkeypatch):
        monkeypatch.setattr(shake, "places",
                            lambda refresh=False, get=None: ({}, ""))
        seen: list = []

        def nearby(box, refresh=False, get=None):
            seen.append(box)
            return {"stations": [], "count": 0, "capped": False, "trouble": ""}

        monkeypatch.setattr(shake, "nearby", nearby)
        return seen

    def test_a_rectangle_reaches_the_search(self, client, monkeypatch):
        seen = self._no_network(monkeypatch)
        got = client.get("/api/shake", params={
            "west": 55, "south": 22, "east": 60, "north": 27})
        assert got.status_code == 200
        assert seen == [(55.0, 22.0, 60.0, 27.0)]
        assert got.json()["searched"] is True

    def test_and_no_rectangle_asks_nothing_about_one(self, client, monkeypatch):
        seen = self._no_network(monkeypatch)
        got = client.get("/api/shake")
        assert got.status_code == 200 and seen == []
        assert got.json()["searched"] is False

    def test_half_a_rectangle_is_not_a_rectangle(self, client, monkeypatch):
        """Three corners is a bug in the caller, and guessing the fourth
        would answer about ground nobody asked about."""
        seen = self._no_network(monkeypatch)
        got = client.get("/api/shake", params={"west": 55, "south": 22, "east": 60})
        assert got.status_code == 200 and seen == []

    def test_a_corner_off_the_globe_is_refused(self, client, monkeypatch):
        self._no_network(monkeypatch)
        got = client.get("/api/shake", params={
            "west": 55, "south": 22, "east": 60, "north": 200})
        assert got.status_code == 422
