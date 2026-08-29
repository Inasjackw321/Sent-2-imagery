"""Turning Telegram messages into pins, and refusing to when it cannot.

Neither Ollama nor Telegram is reachable from here, so the model and the
gazetteer are both stubbed. That is the right level anyway: what matters is
not whether a 1.5B model reads Ukrainian -- it does, and no test here could
prove it -- but what this code does with whatever the model says, including
when the model is absent, unsure, or wrong.

The rule the tests exist to hold: a pin is a claim about a location, and one
is never drawn from anything less than a place name a gazetteer recognised.
"""

from __future__ import annotations

import pytest
import requests

from backend import alerts, llm


@pytest.fixture(autouse=True)
def _clean():
    alerts.forget()
    alerts._places.clear()
    yield
    alerts.forget()
    alerts._places.clear()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Nothing reaches out by accident."""
    def refuse(*a, **kw):
        raise AssertionError("unstubbed network call")
    monkeypatch.setattr(alerts._session, "get", refuse)
    monkeypatch.setattr(llm._session, "get", refuse)
    monkeypatch.setattr(llm._session, "post", refuse)
    # available() is cached; keep tests from inheriting each other's answer.
    llm._probe.update(at=0.0, ok=False, detail="stubbed", models=[])


def _model(monkeypatch, answer):
    monkeypatch.setattr(llm, "read", lambda text, **kw: answer)


def _gazetteer(monkeypatch, table):
    def fake_locate(place):
        return table.get((place or "").strip().lower())
    monkeypatch.setattr(alerts, "locate", fake_locate)


KYIV = {"lat": 50.45, "lon": 30.52, "matched": "Kyiv, Ukraine", "scale": "city"}


# ── What gets a pin ────────────────────────────────────────────


def test_a_message_with_a_known_place_is_plotted(monkeypatch):
    _model(monkeypatch, {"kind": "drone", "place": "Kyiv", "confident": True, "by": "model"})
    _gazetteer(monkeypatch, {"kyiv": KYIV})
    out = alerts.add({"id": "c:1", "text": "Група БпЛА курсом на Київ"})
    assert out["kind"] == "drone"
    assert (out["lat"], out["lon"]) == (50.45, 30.52)
    assert out["read_by"] == "model"


def test_a_message_with_no_place_is_kept_but_never_plotted(monkeypatch):
    """The rule this whole module exists to hold.

    A pin is a claim about a location. There is no honest way to invent one,
    so an alert with nowhere named is listed and left off the map.
    """
    _model(monkeypatch, {"kind": "explosion", "place": "", "confident": True, "by": "model"})
    _gazetteer(monkeypatch, {})
    out = alerts.add({"id": "c:2", "text": "Вибухи"})
    assert out["kind"] == "explosion"
    assert out["lat"] is None and out["lon"] is None
    assert alerts.held()["count"] == 1
    assert alerts.held()["plotted"] == 0


def test_a_place_the_gazetteer_does_not_know_is_not_plotted(monkeypatch):
    """A model can return a place that does not exist. It must not become a
    pin on the strength of having been said confidently."""
    _model(monkeypatch, {"kind": "missile", "place": "Nowhereograd", "confident": True, "by": "model"})
    _gazetteer(monkeypatch, {"kyiv": KYIV})
    out = alerts.add({"id": "c:3", "text": "..."})
    assert out["lat"] is None
    assert out["place"] == "Nowhereograd"


def test_only_placed_alerts_can_be_asked_for(monkeypatch):
    _model(monkeypatch, {"kind": "drone", "place": "Kyiv", "confident": True, "by": "model"})
    _gazetteer(monkeypatch, {"kyiv": KYIV})
    alerts.add({"id": "c:4", "text": "a"})
    _model(monkeypatch, {"kind": "drone", "place": "", "confident": True, "by": "model"})
    alerts.add({"id": "c:5", "text": "b"})
    assert alerts.held()["count"] == 2
    assert alerts.held(placed_only=True)["count"] == 1


# ── Reading without a model ────────────────────────────────────


def test_without_a_model_the_word_list_still_reads_the_kind(monkeypatch):
    monkeypatch.setattr(llm, "read", lambda text, **kw: None)
    _gazetteer(monkeypatch, {})
    out = alerts.add({"id": "c:6", "text": "Шахед над містом"})
    assert out["kind"] == "drone"
    assert out["read_by"] == "words"
    assert out["confident"] is False


def test_the_word_list_never_offers_a_place(monkeypatch):
    """Pulling a town out of free text with string matching works for the
    cases you thought of and invents nonsense for the rest."""
    monkeypatch.setattr(llm, "read", lambda text, **kw: None)
    _gazetteer(monkeypatch, {"kyiv": KYIV, "київ": KYIV})
    out = alerts.add({"id": "c:7", "text": "Вибух у Києві"})
    assert out["kind"] == "explosion"
    assert out["place"] == ""
    assert out["lat"] is None


@pytest.mark.parametrize("text, kind", [
    ("Шахеди курсом на південь", "drone"),
    ("Ракетна небезпека", "missile"),
    ("Потужні вибухи", "explosion"),
    ("Працює ППО", "air_defence"),
    ("Обстріл прикордоння", "artillery"),
    ("Зліт МіГ-31К", "aircraft"),
    ("Повітряна тривога", "alert"),
    ("Доброго ранку", "other"),
])
def test_the_word_list_covers_the_ordinary_phrasings(text, kind):
    assert alerts.by_words(text)["kind"] == kind


def test_a_downed_drone_is_air_defence_not_a_drone():
    """Both words are in the message, so order decides, and the more specific
    phrase has to win or every intercept is filed as an incoming drone."""
    assert alerts.by_words("Збито БпЛА над областю")["kind"] == "air_defence"


def test_a_model_that_reads_nothing_useful_falls_through_to_words(monkeypatch):
    """A model answering "other" with no place has told you nothing, and the
    word list may still recognise the message."""
    _model(monkeypatch, {"kind": "other", "place": "", "confident": False, "by": "model"})
    _gazetteer(monkeypatch, {})
    out = alerts.add({"id": "c:8", "text": "Ракета на Харків"})
    assert out["kind"] == "missile"
    assert out["read_by"] == "words"


# ── Keeping them ───────────────────────────────────────────────


def test_the_same_message_is_not_kept_twice(monkeypatch):
    _model(monkeypatch, {"kind": "drone", "place": "Kyiv", "confident": True, "by": "model"})
    _gazetteer(monkeypatch, {"kyiv": KYIV})
    alerts.add({"id": "c:9", "text": "a"})
    assert alerts.known("c:9")
    assert not alerts.known("c:10")


def test_the_list_does_not_grow_for_ever(monkeypatch):
    """A channel in the middle of a mass attack posts continuously, and an
    unbounded list in memory is a leak with a long fuse."""
    monkeypatch.setattr(alerts, "MAX_ALERTS", 5)
    _model(monkeypatch, {"kind": "drone", "place": "", "confident": True, "by": "model"})
    _gazetteer(monkeypatch, {})
    for i in range(12):
        alerts.add({"id": f"c:{i}", "text": "x"})
    assert alerts.held()["count"] == 5
    # Newest first, so the survivors are the recent ones.
    assert alerts.held()["alerts"][0]["id"] == "c:11"


def test_alerts_can_be_filtered_by_kind(monkeypatch):
    _gazetteer(monkeypatch, {})
    for i, kind in enumerate(["drone", "missile", "drone"]):
        _model(monkeypatch, {"kind": kind, "place": "", "confident": True, "by": "model"})
        alerts.add({"id": f"k:{i}", "text": "x"})
    assert alerts.held(["drone"])["count"] == 2
    assert alerts.held(["missile"])["count"] == 1


# ── Geocoding ──────────────────────────────────────────────────


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self._payload


def test_a_place_is_looked_up_once_and_then_remembered(monkeypatch):
    """These channels name the same dozen towns all day, and the gazetteer is
    free and asks for a second between requests."""
    asked = []

    def fake_get(url, params=None, timeout=None):
        asked.append(params["q"])
        return FakeResponse([{"lat": "50.45", "lon": "30.52",
                              "display_name": "Kyiv", "addresstype": "city"}])
    monkeypatch.setattr(alerts._session, "get", fake_get)
    monkeypatch.setattr(alerts, "GEOCODE_GAP", 0)

    first = alerts.locate("Kyiv")
    second = alerts.locate("kyiv")     # same place, different case
    assert first == second
    assert len(asked) == 1


def test_a_place_the_gazetteer_rejects_is_remembered_as_unknown(monkeypatch):
    """Otherwise a name that is not a place is looked up again on every
    message that repeats it, for as long as the channel keeps saying it."""
    asked = []

    def fake_get(url, params=None, timeout=None):
        asked.append(params["q"])
        return FakeResponse([])
    monkeypatch.setattr(alerts._session, "get", fake_get)
    monkeypatch.setattr(alerts, "GEOCODE_GAP", 0)

    assert alerts.locate("Nowhereograd") is None
    assert alerts.locate("Nowhereograd") is None
    assert len(asked) == 1


def test_a_gazetteer_failure_is_not_a_position(monkeypatch):
    def boom(url, params=None, timeout=None):
        raise requests.ConnectionError("no route")
    monkeypatch.setattr(alerts._session, "get", boom)
    monkeypatch.setattr(alerts, "GEOCODE_GAP", 0)
    assert alerts.locate("Kyiv") is None


def test_a_name_too_short_to_mean_anything_is_not_looked_up(monkeypatch):
    # The session is refused by the autouse fixture, so reaching the network
    # here would fail the test outright.
    assert alerts.locate("") is None
    assert alerts.locate("x") is None


# ── The model client ───────────────────────────────────────────


def test_the_model_answer_is_constrained_to_known_kinds(monkeypatch):
    """Ollama enforces the schema, but a model that returns something outside
    the enum must not become a pin type nothing knows how to draw."""
    class Reply:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"response": '{"kind": "banana", "place": "Kyiv", "confident": true}'}

    monkeypatch.setattr(llm._session, "post", lambda *a, **kw: Reply())
    assert llm.read("x")["kind"] == "other"


def test_an_absent_model_is_not_an_error(monkeypatch):
    def boom(*a, **kw):
        raise requests.ConnectionError("nothing listening")
    monkeypatch.setattr(llm._session, "post", boom)
    assert llm.read("x") is None


def test_the_model_is_asked_for_json_not_prose(monkeypatch):
    """Constrained decoding is what makes a 1.5B model usable here: there is
    no prose to parse and no retry loop when it answers in a paragraph."""
    sent = {}

    class Reply:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"response": '{"kind": "drone", "place": "Kyiv", "confident": true}'}

    def fake_post(url, json=None, timeout=None):
        sent.update(json)
        return Reply()

    monkeypatch.setattr(llm._session, "post", fake_post)
    llm.read("Шахед")
    assert sent["format"] == llm.SCHEMA
    assert sent["stream"] is False
    assert sent["options"]["temperature"] == 0.0


def test_a_missing_model_is_reported_with_what_to_run(monkeypatch):
    """Ollama running without the model pulled is the likeliest half-working
    state, and "no model" would send someone to debug the wrong thing."""
    class Tags:
        def raise_for_status(self):
            pass

        def json(self):
            return {"models": [{"name": "llama3.2:3b"}]}

    monkeypatch.setattr(llm._session, "get", lambda *a, **kw: Tags())
    out = llm.available(force=True)
    assert out["ok"] is False
    assert "ollama pull" in out["detail"]


def test_an_installed_model_reports_ready(monkeypatch):
    class Tags:
        def raise_for_status(self):
            pass

        def json(self):
            return {"models": [{"name": llm.MODEL}]}

    monkeypatch.setattr(llm._session, "get", lambda *a, **kw: Tags())
    assert llm.available(force=True)["ok"] is True


# ── Demo mode ──────────────────────────────────────────────────


def test_demo_alerts_are_all_placed_and_marked_synthetic():
    out = alerts.demo()
    assert out["demo"] is True
    assert out["plotted"] == out["count"]
    assert all(a["lat"] is not None for a in out["alerts"])
    assert all(a["read_by"] == "demo" for a in out["alerts"])


def test_demo_alerts_are_spread_over_time():
    """A live feed arrives over hours. A dozen simultaneous events would
    misrepresent what the layer normally looks like."""
    stamps = {a["at"] for a in alerts.demo()["alerts"]}
    assert len(stamps) > 1
