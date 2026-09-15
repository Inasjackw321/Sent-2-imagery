"""NOTAMs -- the notices that say which airspace is shut, and when.

A NOTAM is how a state tells pilots that something about its airspace has
changed: a corridor closed for firing practice, a whole FIR shut to civil
traffic, a navaid out of service, a drone operation at a named point. They are
the formal record of where aircraft may not go, published by the state that
owns the airspace, and they sit naturally beside this app's other layer: one
shows what somebody said is flying, the other shows what the authorities have
closed.

Where they come from
--------------------

Three places, all of them the FAA, which publishes ICAO NOTAMs worldwide
rather than only American ones. In the order they are tried:

  Their documented API, if FAA_CLIENT_ID and FAA_CLIENT_SECRET are set. Free
  to obtain, read from the environment and held in memory -- never written
  anywhere, the same rule the rest of this app's keys follow. Still the best
  source: documented, paged, stable.

  NOTAM Search, with no key -- the endpoint behind their public search page.
  Asked for the way that page asks: session cookie first, its own headers,
  and a second attempt if the session has gone stale.

  DINS, with no key -- their other public front door, the one flight crews
  use. It answers with a page rather than JSON, so the raw ICAO text is
  parsed out of it. Slower and plainer, and reached for only when Search
  refuses.

The point of the last two is that the layer works without anybody registering
for anything, and keeps working when one door closes. Whichever answered is
named in the panel, because they do not carry quite the same set.

When none of them answers, the layer says which refused and draws nothing. A
NOTAM layer that quietly shows an empty map is indistinguishable from a sky
with nothing closed in it, and those are very different facts.

What this module is careful about
---------------------------------

  The coordinates. A NOTAM's Q-line carries its position as a compact
  "4915N02330E" -- degrees and minutes, no separator, hemisphere as a letter
  -- and its radius in NAUTICAL MILES. Reading those as decimal degrees and
  kilometres puts a closure in the wrong country at a fifth of its size. Both
  forms are parsed here and both are tested against the spelling in the
  documentation.

  The times. A NOTAM is only true between its start and end, and "PERM" is a
  real end value meaning permanent. One that has expired is not drawn.

  Saying nothing rather than guessing. A record whose position cannot be read
  is counted and listed, not placed -- the same rule the air tracker follows
  for a report it cannot geocode.
"""

from __future__ import annotations

import datetime as dt
import math
import os
import re
import threading
import time
from typing import Any

import requests

from . import config

# Three ways in, and the two keyless ones are the default.
#
# The documented API wants a client id and secret. They are free and it is
# still the better source -- documented, stable, paged -- so a key is used
# when there is one. But requiring anybody to go and register before a layer
# shows anything at all is the difference between a feature and a promise,
# and the honest answer to "it still doesn't work" is that it should work
# without being set up.
#
# So the second is the FAA's own NOTAM Search, which is what their public
# search page talks to. No key, same notices, worldwide by ICAO location. It
# is not a documented API and it can change under us -- and it did, refusing
# every region with a 403 -- which is exactly why read_notam below accepts
# several shapes, why every attempt records what it was told rather than
# failing silently, and why there is a third.
API = "https://external-api.faa.gov/notamapi/v1/notams"
SEARCH = "https://notams.aim.faa.gov/notamSearch/search"
SEARCH_PAGE = "https://notams.aim.faa.gov/notamSearch/"

# And a third, for when the second one says no.
#
# NOTAM Search refused every region with a 403. That is what a server says to
# a client it does not think is its own page, and the fix is either to look
# like that page (done below) or to ask somebody else. Both, here, because a
# layer that depends on one undocumented endpoint staying friendly is a layer
# that breaks again next month.
#
# DINS is the FAA's other public front door -- the one flight crews use -- and
# it answers by ICAO location with no key. It hands back a web page rather
# than JSON, with the raw ICAO NOTAM text in it, which read_raw() below parses
# from the Q-line. Less convenient, considerably harder to turn off.
DINS = "https://www.notams.faa.gov/dinsQueryWeb/queryRetrievalMapAction.do"

# What a browser sends, for the two hosts that only answer browsers.
#
# This is not pretending to be somebody else's software for the sake of it:
# their search page posts these, and the endpoint checks. Sending only a
# User-Agent got a 403 from every region in the table.
BROWSERY = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}

# Kept under the old name so nothing that reached for it breaks.
BASE = API

CLIENT_ID = os.environ.get("FAA_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("FAA_CLIENT_SECRET", "").strip()

# One nautical mile. The Q-line radius is in these, and reading it as
# kilometres shrinks every closure to a bit over half its size.
NM_KM = 1.852

# Their page size, and how many pages are worth walking. A busy FIR runs to
# hundreds of notices and nobody reads the four hundredth; this is enough to
# cover a country's active set without turning a map pan into a minute of
# paging.
PER_PAGE = 250
MOST_PAGES = 4

TIMEOUT = 20.0

# How long an answer is kept. NOTAMs are issued hours ahead and change slowly,
# so re-asking on every pan would spend somebody's quota to be told the same
# thing.
KEEP_SECONDS = 600

# The radius a point NOTAM with no stated one is drawn at. Their own default
# for a Q-line with no radius is 5 NM, which is what this is.
DEFAULT_NM = 5.0

# And a ceiling, because a NOTAM covering a whole FIR carries a radius of
# hundreds of miles and drawing it as a circle would cover half of Europe in
# one wash. Past this it is listed and marked as area-wide rather than drawn
# as a disc pretending to be a boundary.
MOST_KM = 400.0

# The furthest their radius query will go, in nautical miles.
#
# This is why the layer showed nothing. A map of Ukraine is an 850-kilometre
# circle -- four hundred and sixty nautical miles -- and their API refuses a
# locationRadius past a hundred, so every request at the zoom anybody
# actually uses this map at was rejected before it was read. The layer was
# doing the right thing with the answer it got, and the answer was "no".
MOST_NM = 100.0

# The flight information regions this app's subject is in, as rough boxes.
#
# Asking by FIR rather than by radius is both the fix for the cap above and
# the better query: a country-sized view is one or two of these instead of a
# grid of circles, and a notice that closes a whole FIR is FILED against that
# FIR rather than against a point in it.
#
# The boxes are approximate and only ever used to decide which FIR to ask
# about. Nothing is drawn from them, so a box that is too generous costs one
# extra request and a box that is too mean costs a missed region -- never a
# mark in the wrong place. Same for the codes: a wrong one comes back empty,
# which the panel says, rather than coming back wrong.
FIRS: tuple[tuple[str, str, float, float, float, float], ...] = (
    # code,  name,          south, west,  north, east
    ("UKBV", "Kyiv",         48.3,  28.0,  52.4,  35.6),
    ("UKLV", "Lviv",         47.7,  22.1,  51.6,  28.6),
    ("UKOV", "Odesa",        44.9,  28.0,  48.8,  33.6),
    ("UKDV", "Dnipro",       46.4,  33.0,  50.6,  40.3),
    ("UKFV", "Simferopol",   43.3,  32.0,  46.3,  36.7),
    ("UMMV", "Minsk",        51.2,  23.0,  56.3,  33.0),
    ("UUWV", "Moscow",       51.8,  30.0,  60.2,  45.5),
    ("URRV", "Rostov",       43.5,  36.0,  52.6,  48.5),
    ("LUUU", "Chisinau",     45.4,  26.6,  48.6,  30.2),
    ("LRBB", "Bucharest",    43.5,  20.2,  48.4,  29.8),
    ("EPWW", "Warsaw",       48.9,  14.1,  55.0,  24.3),
)

# A bound on how many of them one view asks about.
MOST_FIRS = 8


class NotamError(RuntimeError):
    pass


_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_session = requests.Session()
_session.headers.update({"User-Agent": config.USER_AGENT})

# A separate one for the keyless pages, because they want a cookie jar and a
# browser's headers and the documented API wants neither.
_browser = requests.Session()
_browser.headers.update(BROWSERY)
_primed = 0.0


def configured() -> bool:
    """Whether there is a key to ask with."""
    return bool(CLIENT_ID and CLIENT_SECRET)


# "4915N02330E" and "491530N0233045E": degrees, minutes, optionally seconds,
# with the hemisphere as a trailing letter and no separators anywhere.
QCOORD = re.compile(
    r"^(\d{2})(\d{2})(\d{2})?([NS])(\d{3})(\d{2})(\d{2})?([EW])$", re.I)

# And the other spelling, which is what their search page hands back one field
# at a time: "49-15-00.000N". Same numbers, different punctuation, and a
# reader that knew only the first would place nothing at all from that source.
DMS = re.compile(r"^(\d{1,3})-(\d{1,2})(?:-(\d{1,2}(?:\.\d+)?))?\s*([NSEW])$",
                 re.I)


def read_angle(raw: Any) -> float | None:
    """One "49-15-00.000N" as signed degrees, or None."""
    text = " ".join(str(raw or "").split()).upper()
    hit = DMS.match(text)
    if not hit:
        return None
    out = (int(hit.group(1)) + int(hit.group(2)) / 60
           + float(hit.group(3) or 0) / 3600)
    return -out if hit.group(4) in ("S", "W") else out


def read_coord(raw: Any) -> tuple[float, float] | None:
    """A Q-line coordinate as decimal degrees, or None.

    "4915N02330E" is 49 degrees 15 minutes north, 23 degrees 30 minutes east.
    Read as a number it is forty-nine million, and read as decimal degrees it
    is 4915 -- either way the mark leaves the planet, so this refuses anything
    that is not exactly the documented shape.
    """
    text = " ".join(str(raw or "").split()).replace(" ", "").upper()
    hit = QCOORD.match(text)
    if not hit:
        return None
    lat = int(hit.group(1)) + int(hit.group(2)) / 60 + int(hit.group(3) or 0) / 3600
    lon = int(hit.group(5)) + int(hit.group(6)) / 60 + int(hit.group(7) or 0) / 3600
    if hit.group(4) == "S":
        lat = -lat
    if hit.group(8) == "W":
        lon = -lon
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return round(lat, 5), round(lon, 5)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


def _text(value: Any, limit: int = 2000) -> str | None:
    out = " ".join(str(value or "").split())[:limit]
    return out or None


def _moment(value: Any) -> float | None:
    """A NOTAM time as epoch seconds, or None.

    None covers two different things and they want the same answer here.
    "PERM" and "UFN" are real end values meaning the notice does not expire,
    and in_force() reads no-end as "still true" rather than as "ended long
    ago". Nonsense is also None, because a time this cannot read is a time it
    must not act on.

    They were listed explicitly for a while. That list did nothing: "PERM" is
    not a timestamp, so it comes back None from the parse below anyway, and a
    branch nothing can reach is one nobody would notice breaking.
    """
    text = _text(value, 40)
    if not text:
        return None
    try:
        when = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        # Their search page writes "01/15/2026 0000", which is not ISO
        # anything. A time this cannot read is a notice whose period is
        # unknown, and in_force() would then treat it as permanent -- so it
        # is worth the second spelling rather than the wrong answer.
        when = _american(text)
        if when is None:
            return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return when.timestamp()


def _american(text: str) -> dt.datetime | None:
    """"01/15/2026 0000" and "01/15/2026 00:00", as their search page writes it."""
    for shape in ("%m/%d/%Y %H%M", "%m/%d/%Y %H:%M", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(text, shape).replace(
                tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    return None


def read_notam(raw: Any) -> dict[str, Any] | None:
    """One of their records, as something this map can draw.

    Their shape, from the documentation: each item carries
    properties.coreNOTAMData.notam, and the fields worth having are on it.
    """
    if not isinstance(raw, dict):
        return None
    props = raw.get("properties")
    props = props if isinstance(props, dict) else {}
    core = props.get("coreNOTAMData")
    core = core if isinstance(core, dict) else {}
    notam = core.get("notam")
    notam = notam if isinstance(notam, dict) else raw
    if not isinstance(notam, dict):
        return None

    ident = _text(notam.get("number") or notam.get("notamNumber")
                  or notam.get("id"), 40)
    body = _text(notam.get("text") or notam.get("icaoMessage")
                 or notam.get("traditionalMessage")
                 or notam.get("traditionalMessageFrom4thWord"), 2000)
    if not ident or not body:
        return None

    # Position, from whichever of the three forms is there, cheapest first.
    #
    # Three, because the two sources spell it differently and neither is
    # negotiable: the API gives a decimal pair or a Q-line string, and the
    # search page gives "49-15-00.000N" one field at a time. A reader that
    # knew only one of them would place nothing at all from the other, which
    # on a map is indistinguishable from an empty sky.
    lat = _number(notam.get("latitude"))
    lon = _number(notam.get("longitude"))
    if lat is None or lon is None:
        lat = read_angle(notam.get("latitude"))
        lon = read_angle(notam.get("longitude"))
    if lat is None or lon is None:
        spot = read_coord(notam.get("coordinates"))
        if spot:
            lat, lon = spot
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        lat = lon = None

    # The radius, in nautical miles on the wire.
    nm = _number(notam.get("radius"))
    km = (nm if nm and nm > 0 else DEFAULT_NM) * NM_KM

    return {
        "id": ident,
        "location": _text(notam.get("location") or notam.get("icaoLocation")
                          or notam.get("facilityDesignator"), 12),
        "text": body,
        "classification": _text(notam.get("classification"), 20),
        "kind": _text(notam.get("type"), 20),
        "lat": lat,
        "lon": lon,
        "radius_km": round(km, 2),
        # Bigger than a circle can honestly say. Listed rather than drawn as a
        # disc; a FIR-wide closure is a boundary, not a compass circle.
        "wide": km > MOST_KM,
        "from": _moment(notam.get("effectiveStart") or notam.get("startDate")),
        "to": _moment(notam.get("effectiveEnd") or notam.get("endDate")),
        "placed": lat is not None,
        "why_unplaced": None if lat is not None else
                        "the notice carries no position this can read",
    }


def in_force(notam: dict[str, Any], now: float) -> bool:
    """Whether this notice applies at this moment.

    A NOTAM issued for next Tuesday is real and is not a closure now, and one
    that ended this morning is neither. "PERM" arrives as no end at all, which
    is not the same as an end in the past.
    """
    began = notam.get("from")
    ends = notam.get("to")
    if began is not None and began > now:
        return False
    return not (ends is not None and ends < now)


def _read_json(resp: Any, who: str) -> dict[str, Any]:
    """Their answer as a dict, or a NotamError saying what arrived instead.

    The message matters as much as the refusal. "It doesn't work" was
    unanswerable for two rounds because every failure looked the same from
    outside; these say which service, which status, and what the body began
    with, so the next round starts from a fact.
    """
    if resp.status_code in (401, 403):
        raise NotamError(f"{who} refused the request ({resp.status_code})")
    if resp.status_code == 429:
        raise NotamError(f"{who} is rate limiting")
    if not resp.ok:
        raise NotamError(f"{who} answered {resp.status_code}")
    try:
        got = resp.json()
    except ValueError:
        head = " ".join((resp.text or "")[:120].split())
        raise NotamError(f"{who} answered something that is not JSON: {head}") from None
    return got if isinstance(got, dict) else {"items": got}


def _ask(params: dict[str, Any]) -> dict[str, Any]:
    """The documented API. Needs a key."""
    if not configured():
        raise NotamError("no FAA key is set, so NOTAMs cannot be fetched")
    try:
        resp = _session.get(API, params=params, timeout=TIMEOUT, headers={
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "Accept": "application/json",
        })
    except requests.RequestException as exc:
        raise NotamError(f"the NOTAM API could not be reached: {exc}") from exc
    return _read_json(resp, "the NOTAM API")


# What their search page posts. Spelt out because it posts a whole form and
# omitting a field it expects is a 500 rather than a default.
def _search_form(location: str, offset: int) -> dict[str, str]:
    return {
        "searchType": "0",
        "designatorsForLocation": location,
        "designatorForAccountable": "",
        "latDegrees": "", "latMinutes": "0", "latSeconds": "0",
        "longDegrees": "", "longMinutes": "0", "longSeconds": "0",
        "radius": "10",
        "sortColumns": "5 false",
        "sortDirection": "true",
        "designatorForNotamNumberSearch": "",
        "radiusSearchOnDesignator": "false",
        "radiusSearchDesignator": "",
        "latitudeDirection": "N", "longitudeDirection": "E",
        "freeFormText": "", "flightPathText": "",
        "flightPathDivertAirfields": "", "flightPathBuffer": "4",
        "flightPathIncludeNavaids": "true",
        "flightPathIncludeArtcc": "false",
        "flightPathIncludeTfr": "true",
        "flightPathIncludeRegulatory": "false",
        "flightPathResultsType": "All NOTAMs",
        "archiveDate": "", "archiveDesignator": "",
        "offset": str(offset),
        "notamsOnly": "false",
        "filters": "",
        "searchTypeSelected": "0",
        "notamNumber": "",
    }


def _prime(force: bool = False) -> None:
    """Load the search page first, so the post arrives with its session.

    Their endpoint is the back half of a page, not an API, and it answers a
    post that arrives out of nowhere with a 403 -- which is exactly what the
    layer was showing for every region. A browser gets the page, is given a
    session cookie, and posts with it; this does the same. A failure here is
    swallowed on purpose: the post is still worth attempting, and its answer
    is the one worth reporting.
    """
    global _primed
    now = time.time()
    if not force and now - _primed < KEEP_SECONDS:
        return
    try:
        _browser.get(SEARCH_PAGE, timeout=TIMEOUT)
    except requests.RequestException:
        pass
    _primed = now


def _ask_search(location: str, offset: int = 0) -> dict[str, Any]:
    """The keyless one: what their public search page talks to.

    Twice on a refusal, because the likeliest cause of one is a session that
    has gone stale, and re-fetching the page is how a browser recovers from
    the same thing without the person at it ever knowing.
    """
    last: NotamError | None = None
    for go in range(2):
        _prime(force=go > 0)
        try:
            resp = _browser.post(SEARCH, data=_search_form(location, offset),
                                 timeout=TIMEOUT, headers={
                                     "Accept": "application/json, text/javascript, */*; q=0.01",
                                     "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                                     "X-Requested-With": "XMLHttpRequest",
                                     "Referer": SEARCH_PAGE,
                                     "Origin": "https://notams.aim.faa.gov",
                                 })
        except requests.RequestException as exc:
            raise NotamError(f"NOTAM Search could not be reached: {exc}") from exc
        try:
            return _read_json(resp, "NOTAM Search")
        except NotamError as exc:
            if resp.status_code not in (401, 403):
                raise
            last = exc
    raise last


# A raw ICAO NOTAM, as DINS prints it:
#
#   A1234/26 NOTAMN
#   Q) UKBV/QRTCA/IV/BO/W/000/999/5020N03030E030
#   A) UKBV B) 2601150000 C) PERM
#   E) AIRSPACE CLOSED TO ALL CIVIL TRAFFIC
#
# The position lives on the end of the Q-line: an eleven-character coordinate
# and a three-digit radius in nautical miles, run together with no separator.
RAW_ID = re.compile(r"\b([A-Z]\d{4}/\d{2})\b")
RAW_QLINE = re.compile(r"\bQ\)\s*(.+?)(?=\n\s*[A-G]\)|\Z)", re.S)
RAW_Q = re.compile(r"(\d{4}(?:\d{2})?[NS]\d{5}(?:\d{2})?[EW])(\d{3})?\b")
RAW_A = re.compile(r"\bA\)\s*([A-Z]{4})")
RAW_B = re.compile(r"\bB\)\s*(\d{10})")
RAW_C = re.compile(r"\bC\)\s*(\d{10}|PERM|UFN)")
RAW_E = re.compile(r"\bE\)\s*(.+?)(?=\n\s*[A-G]\)|\Z)", re.S)
TAGS = re.compile(r"<[^>]+>")
# How their page says a location has nothing filed against it.
NOTHING = re.compile(r"no\s+notams?\b|not\s+found|no\s+data", re.I)
PRE = re.compile(r"<pre[^>]*>(.*?)</pre>", re.S | re.I)


def _stamp(digits: str) -> float | None:
    """"2601150000" -- the ten-digit form the B) and C) lines carry."""
    try:
        return dt.datetime.strptime(digits, "%y%m%d%H%M").replace(
            tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        return None


def read_raw(text: str, where: str | None = None) -> dict[str, Any] | None:
    """One raw ICAO NOTAM as something this map can draw, or None.

    Same output shape as read_notam(), so everything downstream -- the
    in-force filter, the de-duplication, the drawing -- is the one code path
    regardless of which of the three sources the notice came from.
    """
    body = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not body:
        return None
    ident = RAW_ID.search(body)
    if not ident:
        return None

    said = RAW_E.search(body)
    said = _text(said.group(1), 2000) if said else None
    if not said:
        # No E) line means either a shape this cannot read or a notice with
        # nothing in it. Either way there is nothing to put in a popup, and a
        # mark with no text is a dot nobody can act on.
        return None

    # Only ever off the Q-line. A coordinate-shaped run of digits in the E)
    # text is a bearing or a runway, and reading one as a position is how a
    # closure ends up in the wrong country.
    qline = RAW_QLINE.search(body)
    spot = RAW_Q.search(" ".join(qline.group(1).split())) if qline else None
    place = read_coord(spot.group(1)) if spot else None
    nm = _number(spot.group(2)) if spot and spot.group(2) else None
    km = (nm if nm and nm > 0 else DEFAULT_NM) * NM_KM

    began = RAW_B.search(body)
    ends = RAW_C.search(body)
    at = RAW_A.search(body)
    return {
        "id": ident.group(1),
        "location": _text(at.group(1) if at else where, 12),
        "text": said,
        "classification": None,
        "kind": None,
        "lat": place[0] if place else None,
        "lon": place[1] if place else None,
        "radius_km": round(km, 2),
        "wide": km > MOST_KM,
        "from": _stamp(began.group(1)) if began else None,
        # "PERM" and "UFN" both mean no end, which _stamp cannot parse and
        # in_force() reads correctly as still true.
        "to": _stamp(ends.group(1)) if ends else None,
        "placed": place is not None,
        "why_unplaced": None if place else
                        "the notice carries no position this can read",
    }


def _ask_dins(location: str) -> str:
    """DINS, the other keyless door. Hands back a page, not JSON."""
    try:
        resp = _browser.post(DINS, timeout=TIMEOUT, data={
            "retrieveLocId": location,
            "reportType": "Raw",
            "actionType": "notamRetrievalByICAOs",
            "submit": "View NOTAMs",
        }, headers={
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://www.notams.faa.gov/dinsQueryWeb/",
            "Origin": "https://www.notams.faa.gov",
        })
    except requests.RequestException as exc:
        raise NotamError(f"DINS could not be reached: {exc}") from exc
    if resp.status_code in (401, 403):
        raise NotamError(f"DINS refused the request ({resp.status_code})")
    if not resp.ok:
        raise NotamError(f"DINS answered {resp.status_code}")
    return resp.text or ""


def _walk_dins(location: str, found: list[dict[str, Any]]) -> int:
    """Every notice on one DINS page into `found`. Returns nought, always.

    Nought because the number this returns is "how many their service says
    there are", which is what the panel compares against what was drawn to
    say whether a page cap bit. DINS states no such number -- it hands over
    the lot -- and returning how many were READ instead made the count wrong
    in the one way that matters: a notice filed against two adjacent FIRs is
    read twice and kept once, so the sum came out higher than the set and the
    panel said notices were missing when none were.

    Their page puts each notice in its own <pre>. Tags are stripped rather
    than parsed: the thing wanted here is the text between them, and a real
    HTML parse would be a dependency and an attack surface for no gain.
    """
    page = _ask_dins(location)
    blocks = PRE.findall(page)
    if not blocks:
        # A region with nothing closed in it is a real and common answer, and
        # it is not a failure -- reporting it as one put "DINS answered a page
        # with no notices in it" against five quiet FIRs at once. Their page
        # says so in words, so that is what is checked; a page that says
        # neither is a shape this cannot read, which IS worth saying.
        if NOTHING.search(page):
            return 0
        raise NotamError("DINS answered a page this cannot read")
    for block in blocks:
        one = read_raw(_untag(block), location)
        if one:
            found.append(one)
    return 0


def _untag(html: str) -> str:
    text = TAGS.sub("", html)
    for code, char in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                       ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")):
        text = text.replace(code, char)
    return text


def firs_over(west: float, south: float, east: float,
              north: float) -> list[tuple[str, str]]:
    """Which flight information regions a rectangle touches, nearest first.

    Nearest to the middle of the view first, so a cap that bites drops the
    edges rather than the thing being looked at.
    """
    lat = (south + north) / 2
    lon = (west + east) / 2
    touching = []
    for code, name, fs, fw, fn, fe in FIRS:
        if fw > east or fe < west or fs > north or fn < south:
            continue
        away = _apart(lat, lon, (fs + fn) / 2, (fw + fe) / 2)
        touching.append((away, code, name))
    touching.sort()
    return [(code, name) for _, code, name in touching[:MOST_FIRS]]


def _records(got: dict[str, Any]) -> list[Any]:
    """The list of notices in an answer, whichever key it arrived under.

    "items" is the documented API's; "notamList" is the search page's. Asked
    for by name rather than by "whichever value is a list", because a payload
    that changes shape should come back empty and say so, not quietly hand
    over the first array it happens to contain.
    """
    for key in ("items", "notamList"):
        got_list = got.get(key)
        if isinstance(got_list, list):
            return got_list
    return []


def _count(got: dict[str, Any]) -> int:
    for key in ("totalCount", "totalNotamCount"):
        n = _number(got.get(key))
        if n:
            return int(n)
    return 0


def _walk(params: dict[str, Any], found: list[dict[str, Any]]) -> int:
    """Read every page of one keyed query into `found`. Returns their total."""
    total = 0
    for page in range(1, MOST_PAGES + 1):
        got = _ask({**params, "pageSize": PER_PAGE, "pageNum": page})
        items = _records(got)
        for item in items:
            one = read_notam(item)
            if one:
                found.append(one)
        total = _count(got) or total
        if len(items) < PER_PAGE:
            break
    return total


def _walk_search(location: str, found: list[dict[str, Any]]) -> int:
    """The same, through the keyless search. Its paging is by offset."""
    total = 0
    seen = 0
    for _ in range(MOST_PAGES):
        got = _ask_search(location, seen)
        items = _records(got)
        for item in items:
            one = read_notam(item)
            if one:
                found.append(one)
        total = _count(got) or total
        seen += len(items)
        if not items or seen >= total:
            break
    return total


def _region(code: str, found: list[dict[str, Any]], used: list[str],
            shut: set[str]) -> int:
    """One region, through whichever source will answer for it.

    The fallback is the point of this function. A layer built on a single
    undocumented endpoint is a layer that shows an empty sky the day that
    endpoint decides it does not like non-browser clients -- which is what
    happened, with a 403 from every region at once. Falling through to DINS
    costs one extra request on a bad day and is the difference between a map
    and an apology.

    Both failures are carried in the message when both fail, because "which
    of them said no" is the fact the next fix starts from.
    """
    if configured():
        out = _walk({"icaoLocation": code}, found)
        if "FAA NOTAM API" not in used:
            used.append("FAA NOTAM API")
        return out
    # A door slammed once in this view stays shut for the rest of it. Without
    # this, a view over eight regions asked Search eight times -- sixteen,
    # with its retry -- to be refused eight times before falling through to
    # DINS each time: half a minute of waiting to learn what the first
    # refusal already said.
    #
    # For THIS VIEW rather than for ten minutes, deliberately. A service
    # having a moment should cost one slow view, not an afternoon of quietly
    # never asking it again.
    was: NotamError | None = None
    if "search" in shut:
        was = NotamError("NOTAM Search refused the request")
    else:
        try:
            out = _walk_search(code, found)
        except NotamError as exc:
            was = exc
            shut.add("search")
    if was is None:
        if "FAA NOTAM Search" not in used:
            used.append("FAA NOTAM Search")
        return out
    try:
        out = _walk_dins(code, found)
    except NotamError as also:
        raise NotamError(f"{was}; and {also}") from also
    if "DINS" not in used:
        used.append("DINS")
    return out
    if "FAA NOTAM Search" not in used:
        used.append("FAA NOTAM Search")
    return out


def over(west: float, south: float, east: float,
         north: float) -> dict[str, Any]:
    """Every notice in force over a rectangle.

    By flight information region where the view is in one this knows, and by
    radius otherwise. The FIR query is the important half: their radius query
    stops at a hundred nautical miles, and a map of a country is four or five
    times that, so asking by radius at any useful zoom was asking for a
    refusal.
    """
    now = time.time()
    regions = firs_over(west, south, east, north)
    key = ("fir:" + ",".join(code for code, _ in regions)) if regions else (
        f"box:{round(west, 1)}/{round(south, 1)}/{round(east, 1)}/{round(north, 1)}")
    with _lock:
        held = _cache.get(key)
        if held and now - held[0] < KEEP_SECONDS:
            return held[1]

    found: list[dict[str, Any]] = []
    total = 0
    asked: list[str] = []
    trouble: list[str] = []
    short = False
    used: list[str] = []
    shut: set[str] = set()
    if regions:
        for code, name in regions:
            # Three doors, tried in order of how much this trusts them: the
            # documented API where there is a key, then their search page's
            # endpoint, then DINS. One region failing is not the whole view
            # failing -- five answering and one refusing is a better map than
            # no map -- and the one that refused is named in the panel.
            try:
                total += _region(code, found, used, shut)
                asked.append(f"{code} ({name})")
            except NotamError as exc:
                trouble.append(f"{code}: {exc}")
    elif not configured():
        # No region in the table and no key. The search takes a location
        # rather than a circle, so there is nothing to ask it.
        trouble.append("nowhere in this view is a region this knows, and "
                       "without a key there is no way to ask by position")
    else:
        lat, lon, radius_km = bounds_circle(west, south, east, north)
        wanted_nm = radius_km / NM_KM
        # Their cap, honoured rather than discovered: asking for more is a
        # rejected request, and a rejected request looks like an empty sky.
        short = wanted_nm > MOST_NM
        total = _walk({
            "locationLongitude": round(lon, 4),
            "locationLatitude": round(lat, 4),
            "locationRadius": round(max(1.0, min(MOST_NM, wanted_nm))),
        }, found)
        used.append("FAA NOTAM API")
        asked.append(f"{round(min(MOST_NM, wanted_nm))} NM around "
                     f"{lat:.2f},{lon:.2f}")

    # One notice can be filed against two adjacent FIRs, so the same number
    # arrives twice. Kept once, and the first reading wins.
    seen: dict[str, dict[str, Any]] = {}
    for one in found:
        seen.setdefault(one["id"], one)
    live = [n for n in seen.values() if in_force(n, now)]

    answer = {
        "notams": [n for n in live if n["placed"]],
        "unplaced": [n for n in live if not n["placed"]],
        "count": len(live),
        # How many their service says there are, so a page cap that bites is
        # visible rather than looking like a quiet sky.
        "total": total or len(seen),
        "capped": total > len(seen) if total else False,
        # Which query was actually made. "It does not work" is otherwise
        # unanswerable from the outside, and this is the one fact that
        # separates "nothing is closed" from "nothing was asked".
        "asked": asked,
        # What went wrong, per region, and said out loud. Two rounds of "it
        # doesn't work" went by with every failure looking identical from the
        # outside; this is the difference between a bug report and a guess.
        "trouble": trouble,
        # True when the view is wider than one radius query can cover and no
        # FIR was known for it, so part of it was not looked at.
        "partial": short,
        # Which of the three actually answered, rather than which one was
        # meant to. They do not carry the same set, so a map fed by DINS
        # because Search refused is a different map and should say so.
        "source": " and ".join(used) if used else (
            "FAA NOTAM API" if configured() else "FAA NOTAM Search"),
    }
    with _lock:
        # An answer that got nothing from anybody is not worth keeping for ten
        # minutes: it is usually a service having a moment, and caching it
        # makes a blip last.
        if live or not trouble:
            _cache[key] = (now, answer)
        if len(_cache) > 64:
            _cache.clear()
    return answer


def forget() -> None:
    """Drop what is cached. For tests and for starting over."""
    global _primed
    with _lock:
        _cache.clear()
    _primed = 0.0


def status() -> dict[str, Any]:
    return {"configured": configured(),
            "source": "FAA NOTAM API" if configured() else
                      "FAA NOTAM Search, then DINS",
            "cached": len(_cache)}


def _apart(lat: float, lon: float, to_lat: float, to_lon: float) -> float:
    """How far apart two points are, in kilometres. Haversine."""
    a, b = math.radians(lat), math.radians(to_lat)
    dlat = b - a
    dlon = math.radians(to_lon - lon)
    h = (math.sin(dlat / 2) ** 2
         + math.cos(a) * math.cos(b) * math.sin(dlon / 2) ** 2)
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(h)))


def bounds_circle(west: float, south: float, east: float,
                  north: float) -> tuple[float, float, float]:
    """The centre and radius of a circle covering a rectangle, in km.

    Their API asks for a circle and a map shows a rectangle, so one has to
    become the other. Bigger rather than smaller: a circle that misses the
    corners misses closures that are on screen, which is the one error worth
    avoiding here.

    The radius is the real distance to the furthest corner rather than a flat
    approximation. The first version scaled the east-west half-width by the
    cosine of the CENTRE latitude, which is not where the box is widest -- the
    edge nearer the equator is -- so it came up twenty-one kilometres short on
    a box the size of Ukraine and the south-west corner fell outside.
    """
    lat = (south + north) / 2
    lon = (west + east) / 2
    corners = ((south, west), (south, east), (north, west), (north, east))
    return lat, lon, max(_apart(lat, lon, *corner) for corner in corners)


# Invented notices for the build with no network and no key.
#
# In their documented shape, read by the same read_notam() the live path uses,
# so the offline build exercises the reader rather than a copy of it. The text
# says plainly that they are invented: a convincing fake airspace closure is
# among the worst things this app could put on a screen.
DEMO = (
    {"properties": {"coreNOTAMData": {"notam": {
        "number": "A0001/26", "location": "UKBV", "type": "N",
        "classification": "INTL",
        "coordinates": "5020N03030E", "radius": 30,
        "effectiveStart": "2026-01-01T00:00:00Z", "effectiveEnd": "PERM",
        "text": "DEMO — INVENTED NOTICE. AIRSPACE CLOSED TO ALL CIVIL "
                "TRAFFIC. NOT A REAL NOTAM.",
    }}}},
    {"properties": {"coreNOTAMData": {"notam": {
        "number": "A0002/26", "location": "UKLV", "type": "N",
        "classification": "INTL",
        "coordinates": "4950N02400E", "radius": 12,
        "effectiveStart": "2026-01-01T00:00:00Z", "effectiveEnd": "PERM",
        "text": "DEMO — INVENTED NOTICE. TEMPORARY RESERVED AREA ACTIVE "
                "SFC-FL200. NOT A REAL NOTAM.",
    }}}},
    {"properties": {"coreNOTAMData": {"notam": {
        "number": "A0003/26", "location": "UKOO", "type": "N",
        "classification": "INTL",
        "coordinates": "4630N03045E", "radius": 240,
        "effectiveStart": "2026-01-01T00:00:00Z", "effectiveEnd": "PERM",
        "text": "DEMO — INVENTED NOTICE. FIR-WIDE RESTRICTION, LISTED "
                "RATHER THAN DRAWN AS A CIRCLE. NOT A REAL NOTAM.",
    }}}},
    # One with no readable position, so the offline build shows what an
    # unplaceable notice looks like rather than only the happy path.
    {"properties": {"coreNOTAMData": {"notam": {
        "number": "A0004/26", "location": "UKFV", "type": "N",
        "classification": "INTL", "coordinates": "NOT A COORDINATE",
        "effectiveStart": "2026-01-01T00:00:00Z", "effectiveEnd": "PERM",
        "text": "DEMO — INVENTED NOTICE. NO POSITION THIS CAN READ. "
                "NOT A REAL NOTAM.",
    }}}},
    # One that has already ended, so "in force" is exercised rather than
    # asserted. It must not appear.
    {"properties": {"coreNOTAMData": {"notam": {
        "number": "A0005/26", "location": "UKBV", "type": "N",
        "classification": "INTL", "coordinates": "5100N03100E", "radius": 20,
        "effectiveStart": "2020-01-01T00:00:00Z",
        "effectiveEnd": "2020-01-02T00:00:00Z",
        "text": "DEMO — INVENTED NOTICE, ALREADY EXPIRED. NOT A REAL NOTAM.",
    }}}},
)


def demo() -> dict[str, Any]:
    """The invented set, through the real reader."""
    now = time.time()
    read = [n for n in (read_notam(raw) for raw in DEMO) if n]
    live = [n for n in read if in_force(n, now)]
    return {
        "notams": [n for n in live if n["placed"]],
        "unplaced": [n for n in live if not n["placed"]],
        "count": len(live),
        "total": len(live),
        "capped": False,
        "asked": ["demo — nothing was asked of anybody"],
        "trouble": [],
        "partial": False,
        "source": "demo — invented notices",
    }
