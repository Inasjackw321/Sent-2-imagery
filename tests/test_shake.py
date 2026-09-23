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
