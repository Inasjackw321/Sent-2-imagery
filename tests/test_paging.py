"""Reaching past the first page of a catalogue.

A STAC search answers one page at a time and says where the next one is.
Asking once and stopping caps the archive at whatever fits in a single
response, however wide a date range was asked for -- so a search over ten
years came back with the most recent handful and looked like the satellite
had only just launched.
"""

from __future__ import annotations

import datetime as dt

import pytest

from backend import stac


class _Catalogue:
    """A stand-in STAC API that pages, and counts how often it is asked."""

    def __init__(self, total, page=100, style="post"):
        self.total = total
        self.page = page
        self.style = style
        self.calls = 0

    def _items(self, offset):
        day = dt.date(2024, 6, 1)
        out = []
        for i in range(offset, min(offset + self.page, self.total)):
            when = day - dt.timedelta(days=i * 5)
            out.append({
                "id": f"S2_{i}",
                "collection": "sentinel-2-l2a",
                "properties": {"datetime": f"{when.isoformat()}T10:30:00Z",
                               "eo:cloud_cover": 5.0},
                "assets": {"red": {"href": f"https://example/{i}/red.tif"},
                           "green": {"href": f"https://example/{i}/green.tif"},
                           "blue": {"href": f"https://example/{i}/blue.tif"}},
            })
        return out

    def answer(self, offset):
        self.calls += 1
        nxt = offset + self.page
        links = []
        if nxt < self.total:
            if self.style == "post":
                links = [{"rel": "next", "method": "POST",
                          "href": "https://example/search",
                          "body": {"__offset": nxt}}]
            else:
                links = [{"rel": "next", "href": f"https://example/search?offset={nxt}"}]
        return {"type": "FeatureCollection", "numberMatched": self.total,
                "features": self._items(offset), "links": links}


@pytest.fixture
def catalogue(monkeypatch):
    made = {}

    def build(total, page=100, style="post"):
        cat = _Catalogue(total, page, style)
        made["cat"] = cat

        class Reply:
            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        def post(url, json=None, timeout=None):
            return Reply(cat.answer(int((json or {}).get("__offset", 0))))

        def get(url, timeout=None):
            offset = int(url.split("offset=")[1]) if "offset=" in url else 0
            return Reply(cat.answer(offset))

        monkeypatch.setattr(stac._session, "post", post)
        monkeypatch.setattr(stac._session, "get", get)
        return cat

    return build


AOI = {"type": "Polygon", "coordinates": [[[0, 51], [0.1, 51], [0.1, 51.1], [0, 51.1], [0, 51]]]}


def test_a_search_walks_past_the_first_page(catalogue):
    """250 scenes over three pages, not 100 over one."""
    cat = catalogue(total=400)
    found, matched = stac._search_source(
        stac.config.SOURCES["earth-search"], stac.config.SATELLITES["sentinel-2"],
        AOI, "2015-01-01", "2024-06-01", 100.0, 250)

    assert len(found) == 250
    assert matched == 400
    assert cat.calls == 3, "should have followed two next links"


def test_paging_stops_once_it_has_enough(catalogue):
    """No point fetching a page nobody asked for."""
    cat = catalogue(total=1000)
    found, _ = stac._search_source(
        stac.config.SOURCES["earth-search"], stac.config.SATELLITES["sentinel-2"],
        AOI, "2015-01-01", "2024-06-01", 100.0, 120)

    assert len(found) == 120
    assert cat.calls == 2


def test_a_get_style_next_link_is_followed_too(catalogue):
    """Both paging conventions are in the wild."""
    cat = catalogue(total=250, style="get")
    found, _ = stac._search_source(
        stac.config.SOURCES["earth-search"], stac.config.SATELLITES["sentinel-2"],
        AOI, "2015-01-01", "2024-06-01", 100.0, 250)

    assert len(found) == 250
    assert cat.calls == 3


def test_the_page_budget_is_not_infinite(catalogue):
    """A search over a whole country must not run away with itself."""
    cat = catalogue(total=100000)
    found, _ = stac._search_source(
        stac.config.SOURCES["earth-search"], stac.config.SATELLITES["sentinel-2"],
        AOI, "2015-01-01", "2024-06-01", 100.0, stac.MAX_LIMIT * 10)

    assert cat.calls <= stac.MAX_PAGES
    assert len(found) <= stac.PAGE_SIZE * stac.MAX_PAGES


def test_the_search_asks_for_newest_first(catalogue):
    """Without an explicit sort the order across pages is the catalogue's whim.

    Taking the first page of an unordered result is not the same as taking the
    newest scenes, and the difference only shows once there is more than one
    page.
    """
    sent = {}
    cat = catalogue(total=50)
    original = stac._session.post

    def spy(url, json=None, timeout=None):
        sent.update(json or {})
        return original(url, json=json, timeout=timeout)

    stac._session.post = spy
    try:
        stac._search_source(
            stac.config.SOURCES["earth-search"], stac.config.SATELLITES["sentinel-2"],
            AOI, "2015-01-01", "2024-06-01", 100.0, 50)
    finally:
        stac._session.post = original

    assert sent["sortby"] == [{"field": "properties.datetime", "direction": "desc"}]
    assert cat.calls == 1


def test_the_offline_catalogue_reaches_back_years_too():
    """The demo used to stop at sixty however far back it was asked to go."""
    from backend.geo import normalise_aoi

    found = stac.search_scenes(
        normalise_aoi({"bbox": [-0.2, 51.4, 0.0, 51.6]}),
        "2016-01-01", "2024-06-01", max_cloud=100, limit=700, demo=True,
        satellites=["sentinel-2"])["scenes"]

    # Sentinel-2 comes round every five days here, so seven hundred passes is
    # most of a decade -- and the old cap of sixty was four months of it.
    assert len(found) > 500, f"only {len(found)} synthetic passes over eight years"
    assert min(s["date"] for s in found) < "2017-06-01"
    assert len(found) > 60 * 5, "the sixty-scene cap is gone"
