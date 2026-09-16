"""NOTAMs -- the notices that say which airspace is shut, and when.

A NOTAM is how a state tells pilots that something about its airspace has
changed: a corridor closed for firing practice, a whole FIR shut to civil
traffic, a drone operation at a named point. They are the formal record of
where aircraft may not go, and they sit naturally beside this app's other
layer: one shows what somebody said is flying, the other shows what the
authorities have closed.

This module does not fetch them
-------------------------------

It used to, three different ways, and all three are why this file was
rewritten.

The FAA's documented API wants a client id and secret. Their NOTAM Search
endpoint answered every request with a 403. DINS is a web page that can
change shape at any time. Every worldwide NOTAM service that is actually
dependable -- Cirium, Laminar, Notamify, AvDelphi, the RapidAPI ones --
wants a key or a contract. There is no keyless live source; that is a fact
about the industry rather than a gap in this app.

So the notice comes from the person instead. They open a NOTAM source in
their own browser -- where it works, because a browser with a real session
is exactly what those pages are built for -- copy the text, and paste it
here. This module reads it.

That trade is worth being explicit about. What is lost is automatic
refresh. What is gained is a layer that cannot 403, cannot rate-limit,
cannot expire a key, needs no registration, and works with the text from ANY
source -- DINS, an AIP, a briefing pack, a European AIS, a message from
somebody else -- because it reads the ICAO format rather than one service's
JSON.

What it is careful about
------------------------

  The coordinates. A NOTAM's Q-line carries its position as a compact
  "4915N02330E" -- degrees and minutes, no separator, hemisphere as a letter
  -- and its radius in NAUTICAL MILES. Reading those as decimal degrees and
  kilometres puts a closure in the wrong country at a fifth of its size.

  The times. A NOTAM is only true between its start and end, and "PERM" is a
  real end value meaning permanent. One that has expired is not drawn, and
  one that has not started yet is listed rather than drawn.

  Saying nothing rather than guessing. A notice whose position cannot be read
  is counted and listed, not placed -- the same rule the air tracker follows
  for a report it cannot geocode.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time
from typing import Any

# One nautical mile. The Q-line radius is in these, and reading it as
# kilometres shrinks every closure to a bit over half its size.
NM_KM = 1.852

# The radius a point NOTAM with no stated one is drawn at. The FAA's own
# default for a Q-line with no radius is 5 NM, which is what this is.
DEFAULT_NM = 5.0

# A ceiling, because a NOTAM covering a whole FIR carries a radius of hundreds
# of miles and drawing it as a circle would cover half of Europe in one wash.
# Past this it is listed and marked as area-wide rather than drawn as a disc
# pretending to be a boundary.
MOST_KM = 400.0

# How much text one paste may carry, and how many notices may come out of it.
# A FIR's whole active set is a few hundred; these are generous enough for
# that and mean enough that a pasted book does not become a map of a thousand
# circles nobody can read.
MOST_CHARS = 400_000
MOST_NOTAMS = 600


class NotamError(RuntimeError):
    pass


# "4915N02330E" and "491530N0233045E": degrees, minutes, optionally seconds,
# with the hemisphere as a trailing letter and no separators anywhere.
QCOORD = re.compile(
    r"^(\d{2})(\d{2})(\d{2})?([NS])(\d{3})(\d{2})(\d{2})?([EW])$", re.I)

# The other spelling, one field at a time: "49-15-00.000N".
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


def _american(text: str) -> dt.datetime | None:
    """"01/15/2026 0000", which is how the FAA's search page writes a date."""
    for shape in ("%m/%d/%Y %H%M", "%m/%d/%Y %H:%M", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(text, shape).replace(
                tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    return None


def _moment(value: Any) -> float | None:
    """A NOTAM time as epoch seconds, or None.

    None covers two different things and they want the same answer here.
    "PERM" and "UFN" are real end values meaning the notice does not expire,
    and in_force() reads no-end as "still true" rather than as "ended long
    ago". Nonsense is also None, because a time this cannot read is a time it
    must not act on.
    """
    text = _text(value, 40)
    if not text:
        return None
    try:
        when = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        when = _american(text)
        if when is None:
            return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return when.timestamp()


def _stamp(digits: str) -> float | None:
    """"2601150000" -- the ten-digit form the B) and C) lines carry."""
    try:
        return dt.datetime.strptime(digits, "%y%m%d%H%M").replace(
            tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# The raw ICAO form, which is what somebody pastes
# ---------------------------------------------------------------------------
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
ESCAPES = (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
           ("&#39;", "'"), ("&nbsp;", " "))


def untag(html: str) -> str:
    """Strip tags and undo the escaping, for text copied out of a web page.

    Copying from a browser usually gives plain text, but copying a page's
    SOURCE, or saving it and opening the file, gives markup -- and a paste box
    that turned that into nothing would look broken for a reason nobody could
    see. Tags are stripped rather than parsed: what is wanted is the text
    between them, and a real HTML parse would be a dependency and an attack
    surface for no gain.
    """
    text = TAGS.sub("\n", html)
    for code, char in ESCAPES:
        text = text.replace(code, char)
    return text


def read_raw(text: str, where: str | None = None) -> dict[str, Any] | None:
    """One raw ICAO NOTAM as something this map can draw, or None."""
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

    # The position only ever off the Q-line. A coordinate-shaped run of digits
    # in the E) text is a bearing or a boundary list, and reading one as the
    # position puts the closure somewhere nobody filed it.
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


def read_record(raw: Any) -> dict[str, Any] | None:
    """One JSON record, in any of the shapes the services hand out.

    Kept because somebody who DOES have a key, or who saved a response from
    one of the paid services, should be able to paste that too. It is the
    same question -- what is closed and where -- asked in JSON.
    """
    if not isinstance(raw, dict):
        return None
    props = raw.get("properties")
    props = props if isinstance(props, dict) else {}
    core = props.get("coreNOTAMData")
    core = core if isinstance(core, dict) else {}
    notam = core.get("notam")
    notam = notam if isinstance(notam, dict) else raw

    ident = _text(notam.get("number") or notam.get("notamNumber")
                  or notam.get("notam_id") or notam.get("id"), 40)
    body = _text(notam.get("text") or notam.get("body")
                 or notam.get("icaoMessage") or notam.get("traditionalMessage")
                 or notam.get("raw"), 2000)
    if not ident or not body:
        return None

    # A record may carry the whole raw NOTAM in one of its fields, and that
    # is a better source for the position than the record's own -- it is what
    # the state actually filed. Tried first, and fallen back on.
    whole = notam.get("raw") or notam.get("icaoMessage")
    if isinstance(whole, str) and RAW_QLINE.search(whole):
        from_raw = read_raw(whole, _text(notam.get("location"), 12))
        if from_raw and from_raw["placed"]:
            return from_raw

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

    nm = _number(notam.get("radius"))
    km = (nm if nm and nm > 0 else DEFAULT_NM) * NM_KM
    return {
        "id": ident,
        "location": _text(notam.get("location") or notam.get("icaoLocation")
                          or notam.get("facilityDesignator"), 12),
        "text": body,
        "lat": lat,
        "lon": lon,
        "radius_km": round(km, 2),
        "wide": km > MOST_KM,
        "from": _moment(notam.get("effectiveStart") or notam.get("startDate")
                        or notam.get("effective")),
        "to": _moment(notam.get("effectiveEnd") or notam.get("endDate")
                      or notam.get("expiration")),
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


def split_raw(text: str) -> list[str]:
    """One pasted blob as separate notices.

    Split on the notice number at the start of one, which is the only marker
    every source shares. Blank lines are not it: DINS puts each notice in its
    own block, an AIP runs them together, and a briefing pack wraps them at
    seventy columns with blank lines inside a single notice.
    """
    hits = list(RAW_ID.finditer(text))
    if not hits:
        return []
    # Where each notice starts. A number that appears INSIDE a notice -- a
    # replacement referring to the one it replaces, "NOTAMR A1233/26" -- is
    # not the start of anything, so only the first number on a line counts.
    starts = []
    for hit in hits:
        line_began = text.rfind("\n", 0, hit.start()) + 1
        before = text[line_began:hit.start()].strip()
        # Nothing before it on the line, or only the word that introduces it.
        if before and not re.fullmatch(r"(?i:notam[rnc]?|\d+\.?|[-*•])?", before):
            continue
        if starts and hit.start() <= starts[-1]:
            continue
        starts.append(hit.start())
    if not starts:
        starts = [hits[0].start()]
    starts.append(len(text))
    return [text[starts[i]:starts[i + 1]] for i in range(len(starts) - 1)]


def read_text(raw: str) -> dict[str, Any]:
    """Everything in one pasted blob, in the shape the map draws.

    Takes whatever was pasted: raw ICAO notices, a JSON response from a
    service, a saved web page. Nothing is fetched and nothing is kept.
    """
    text = str(raw or "")
    if len(text) > MOST_CHARS:
        raise NotamError(
            f"that is {len(text):,} characters; paste up to {MOST_CHARS:,}")
    if not text.strip():
        raise NotamError("nothing was pasted")

    found: list[dict[str, Any]] = []
    how = "raw ICAO text"

    # JSON first, because a JSON payload is unambiguous and cheap to rule out.
    shaped = _read_json(text)
    if shaped is not None:
        found = [n for n in (read_record(r) for r in shaped) if n]
        how = "JSON records"

    if not found:
        if "<" in text and ">" in text:
            text = untag(text)
            how = "text from a web page"
        found = [n for n in (read_raw(part) for part in split_raw(text)) if n]

    if not found:
        raise NotamError(
            "no NOTAMs could be read out of that. This reads the raw ICAO "
            "form -- the one with the Q) and E) lines in it -- so paste the "
            "notices themselves rather than a summary of them.")

    # One notice can be filed against two adjacent regions, so the same number
    # arrives twice. Kept once, and the first reading wins.
    seen: dict[str, dict[str, Any]] = {}
    for one in found[:MOST_NOTAMS]:
        seen.setdefault(one["id"], one)

    now = time.time()
    live = [n for n in seen.values() if in_force(n, now)]
    later = [n for n in seen.values()
             if not in_force(n, now) and (n.get("from") or 0) > now]
    return {
        "notams": [n for n in live if n["placed"] and not n["wide"]],
        # Real, in force, and not drawable as a circle: no position, or a
        # radius so wide the circle would be a lie about a boundary. Listed,
        # because a layer that silently dropped them would be saying the sky
        # is open.
        "unplaced": [n for n in live if not n["placed"]],
        "wide": [n for n in live if n["placed"] and n["wide"]],
        "count": len(live),
        "read": len(seen),
        # Expired ones are dropped without comment; ones that have not started
        # are worth a number, because "nothing is closed yet" is a different
        # fact from "nothing was pasted".
        "later": len(later),
        "capped": len(found) > MOST_NOTAMS,
        "how": how,
    }


def _read_json(text: str) -> list[Any] | None:
    """The records in a JSON payload, or None if it is not JSON at all."""
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        got = json.loads(stripped)
    except ValueError:
        return None
    if isinstance(got, list):
        return got
    if isinstance(got, dict):
        for key in ("items", "notamList", "notams", "data", "results"):
            inner = got.get(key)
            if isinstance(inner, list):
                return inner
        return [got]
    return None


# Invented notices for the build with no network and nothing pasted.
#
# In the raw form, read by the same read_text() the live path uses, so the
# offline build exercises the reader rather than a copy of it. The text says
# plainly that they are invented: a convincing fake airspace closure is among
# the worst things this app could put on a screen.
DEMO = """
A0001/26 NOTAMN
Q) UKBV/QRTCA/IV/BO/W/000/999/5020N03030E030
A) UKBV B) 2601010000 C) PERM
E) DEMO - INVENTED NOTICE. AIRSPACE CLOSED TO ALL CIVIL TRAFFIC.
NOT A REAL NOTAM.

A0002/26 NOTAMN
Q) UKLV/QRTCA/IV/BO/W/000/999/4950N02400E012
A) UKLV B) 2601010000 C) PERM
E) DEMO - INVENTED NOTICE. TEMPORARY RESERVED AREA ACTIVE SFC-FL200.
NOT A REAL NOTAM.

A0003/26 NOTAMN
Q) UKOV/QRTCA/IV/BO/W/000/999/4630N03045E240
A) UKOV B) 2601010000 C) PERM
E) DEMO - INVENTED NOTICE. FIR-WIDE RESTRICTION, LISTED RATHER THAN DRAWN
AS A CIRCLE. NOT A REAL NOTAM.

A0004/26 NOTAMN
Q) UKFV/QRTCA/IV/BO/W/000/999/
A) UKFV B) 2601010000 C) PERM
E) DEMO - INVENTED NOTICE. NO POSITION THIS CAN READ. NOT A REAL NOTAM.

A0005/26 NOTAMN
Q) UKBV/QRTCA/IV/BO/W/000/999/5100N03100E020
A) UKBV B) 2001010000 C) 2001020000
E) DEMO - INVENTED NOTICE, ALREADY EXPIRED. NOT A REAL NOTAM.
"""


def demo() -> dict[str, Any]:
    """The invented set, through the real reader."""
    return {**read_text(DEMO), "how": "demo - invented notices"}
