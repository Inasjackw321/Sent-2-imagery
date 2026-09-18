"""Air-threat reports from public Telegram channels, put on the map.

Monitoring channels in Ukraine and Russia post a running commentary of what is
in the air: drones crossing an oblast, missiles on a course, city-wide alerts,
strikes where they land. It is published to warn people, openly and
deliberately, and this reads the same public pages anyone can open in a
browser.

This is the second attempt, and the first one's mistake is worth writing down
because it is not obvious and it looked fine.

The first version asked the language model for coordinates. That is the wrong
job to give a model. Asked to read a report and return a latitude, a model
always returns a latitude: right for a capital city, recalled or interpolated
for anywhere smaller, and occasionally invented outright. Nothing in the number
says which of those happened. The result was markers in the wrong oblast, and
no way to tell them from the correct ones.

So the work is split along the seam it should always have had:

  reading    the channels' public web preview at t.me/s/<name>. No account, no
             API key -- the same HTML a logged-out visitor gets.

  reading    backend/reports.py turns the prose into structure: what kind of
  meaning    thing, the NAME of the place, which way it is going. Regular
             expressions over a grammar these channels genuinely follow, not
             a model -- a model was tried here and was both slower and worse,
             transliterating names the gazetteer holds in Cyrillic and
             flattening the movement digests that carry most of a night.
             One post can be many reports: a digest names a dozen towns per
             oblast and each one gets its own mark.

  placing    a gazetteer turns those names into coordinates, biased to the
             country the channel reports on. If it does not know the place,
             the event is not placed. That is a real outcome and it is kept,
             as text, rather than being quietly dropped.

  drawing    one mark per object, at the place the report named, and it does
             not move. Markers used to be carried along their reported course
             at a typical speed for the kind of thing they were. That is gone:
             see project(). A mark sits where the report put it.

  massing    nearby marks can be gathered into one shape -- "concentrate
             mode" -- because forty separate drone glyphs over one oblast say
             less than one circle labelled forty. Clustered by real distance
             in kilometres, in the backend, so it does not change with zoom
             and can be tested rather than eyeballed.

The other change is that nothing is thrown away for failing to be placeable.
A report that cannot be put on a map is still a report; it goes into the alert
stream with everything else. The old version silently discarded them, so a
patchy night looked identical to a broken feature.
"""

from __future__ import annotations

import datetime as dt
import html
import logging
import math
import re
import threading
import time
from concurrent import futures
from urllib.parse import quote
from typing import Any

import requests

from . import config, gazetteer, neighbours, neptun, places, reports

log = logging.getLogger("sent2.tracker")

# The channels, and what each one is about.
#
# `countries` is the single most valuable thing in this table. A gazetteer
# asked for "Sumy" with no country will happily return a street somewhere
# else; asked for "Sumy" in Ukraine it returns the oblast capital. Two-letter
# ISO codes, as Nominatim wants them.
# The Telegram channels still read, which is now one.
#
# NEPTUN is the source for Ukraine. It carries the same channels' reports --
# and more -- already read, already placed, and with its own alert feed keyed
# to real boundaries, so reading those channels here as well was two paths to
# the same facts with only the worse one able to put a mark in the wrong
# province.
#
# What NEPTUN does not cover is the Russian side, so @lpr1_treugolnik stays:
# reports from Luhansk and Russia, which is the one thing this app would
# otherwise lose. Russia first in its country list, because a gazetteer asked
# for a Donbas town with Ukraine first answers with the pre-war name of
# somewhere else.
#
# The others -- eRadarrua, kpszsu, war_monitor, radarrussiia -- are gone for
# now rather than deleted: they are four lines, and putting one back is
# adding its row here.
CHANNELS = (
    {"name": "lpr1_treugolnik", "region": "Luhansk and Russia",
     "countries": "ru,ua"},
)

# The public web preview. Not the API: this is the page Telegram serves to a
# visitor with no account, and it carries the recent posts as plain HTML.
PREVIEW = "https://t.me/s/{channel}"



# How long a marker stays on the map, when its kind does not say otherwise.
#
# Twenty minutes, and the number is about dead reckoning rather than about
# news: a position extrapolated from one report gets worse every second, and
# after twenty minutes it is a work of fiction. It goes rather than sitting
# there looking authoritative.
#
# Which is why it is only the default. That reasoning applies to something in
# flight and to nothing else -- see `keep` in KINDS below.
KEEP_MINUTES = 20

# How long a report stays in the alert stream. Longer than a track, because
# text does not go stale the way an extrapolated position does -- "a strike was
# reported in Kharkiv an hour ago" is still true an hour later.
ALERT_MINUTES = 90

# How far back to read. Every post the page offers that could still be on the
# map -- which means the longest time any kind is held for, not less.
#
# This was twenty minutes, and it was wrong in a way the old comment here
# argued for at length: it said reading further back would cost "a lot of
# somebody else's bandwidth". It costs none. Telegram's preview page is ONE
# fetch that returns about twenty posts whatever you do, so those posts are
# already downloaded, already parsed, and already in memory when this decides
# whether to look at them. Throwing away the ones older than twenty minutes
# saved nothing and lost most of the feed.
#
# What it looked like from the outside: four channels, "20 posts, nothing
# new", four times over, and an empty map. Every post had arrived. All of them
# were binned before anything read them, because on a quiet half-hour -- or
# any moment more than twenty minutes after the last burst -- nothing on the
# page is inside the window.
#
# Age still matters; it is just applied at the right end. Each kind has its
# own keep time (a drone twenty minutes, a warning ninety) and _expire()
# enforces it against the post's own timestamp. So reading a warning from an
# hour ago puts it on the map where it belongs, and reading a drone from four
# hours ago costs one dict lookup and then nothing.
#
# Derived rather than written down, so it cannot fall behind the keep times.
# It fell from twenty-five hours to ninety minutes when strikes stopped being
# drawn, which is exactly the point of deriving it: the longest-lived kind
# left decides how far back there is any reason to read.
def _lookback_minutes() -> int:
    return max([KEEP_MINUTES, ALERT_MINUTES, *KEEP.values()])

# The floor between two reads of the channels.
#
# Halved, because the two reasons it was fifty-five are both gone: a read is no
# longer blocking anybody's request, and the model call it guarded no longer
# costs anything against a quota. What is left is politeness to Telegram, which
# is why this is thirty and not three -- four channels every thirty seconds is
# about five hundred page fetches an hour, and there is no version of this
# feature that justifies more.
#
# The map still asks every sixty seconds and may be open in several tabs; this
# is what makes that cost one read between them.
MIN_POLL_SECONDS = 30

# What each kind is, what colour it is drawn in, and how it behaves.
#
# The colours follow the published Ukrainian air-situation maps rather than
# being chosen here, because somebody who has looked at one of those already
# knows what a yellow triangle over Chernihiv means. Shahed traffic is yellow
# on them; this makes drones yellow too, and moves what used to be yellow
# (aircraft) somewhere else.
#
# Since the silhouettes went, colour is most of what tells one kind from
# another, so the gaps between these are checked by a test rather than eyed.
#
#   speed   km/h. A rough figure for the type, never a measurement of the
#           object. A Shahed is a propeller aircraft at a couple of hundred; a
#           jet-powered one is three times that; a cruise missile is subsonic
#           and fast; a ballistic one is quicker than anything else here by an
#           order of magnitude and is over in minutes.
#
#   motion  what sort of thing this is, which decides how it is drawn and
#           whether it covers ground:
#             "track"  something in flight, on its way somewhere. Drawn as
#                      itself, pointed along its reported course.
#             "orbit"  something on station, loitering over one place. Drawn
#                      as a ring, because it is not going anywhere.
#             "still"  something that IS a place: a strike, a warning.
#
#           It no longer moves anything. Markers used to be carried along
#           their course between reports, which was honest about being an
#           estimate and was still not worth it -- a map where everything
#           drifts is hard to read, the marks wander off the places the
#           reports named, and a mark sliding across a province looks tracked
#           however carefully the panel says otherwise.
#
#   keep    how many minutes the marker stays, when it differs from the
#           twenty-minute default.
#
#           The default expires things because their POSITION rots, not
#           because the news does. A marker carried along a course for twenty
#           minutes is describing a journey nobody watched, so it goes. But a
#           strike does not move: where it happened is where it happened, and
#           that is as true six hours later as it was at the time. The only
#           reason to take one off the map is that it has stopped being what
#           is going on, which is a much longer clock.
#
#   rank    orders the alert stream. A strike outranks a drone crossing an
#           oblast, which outranks a warning.
KINDS = {
    # Four things in the air, and the differences between them are the ones a
    # reader can actually use.
    #
    # There were seven. "Recon drone" was one, and it went because the
    # distinction it drew was not one this data supports: telling a
    # reconnaissance drone from an attack drone means knowing the airframe,
    # these reports say "БпЛА" most of the time, and a wrong guess between the
    # two changes a marker from "something is coming" to "something is
    # watching" -- which is the most consequential thing on this map to get
    # wrong. Folded into "drone".
    #
    # Cruise and ballistic were two, and they are now one "missile". The
    # difference matters enormously in life and not at all here: both are
    # inbound, both are drawn at the same place, and neither changes what the
    # map can tell you. One kind that is always right beats two that are
    # sometimes swapped.
    # Yellow, red, purple: the three that matter told apart by hue alone, so a
    # glance at a screen full of arrows reads without checking a key. An
    # ordinary drone is the common case and keeps the colour these maps have
    # always used for it; a jet drone is three times the speed and gets red;
    # a missile gets purple.
    "drone":     {"colour": "#ffd400", "label": "Drone",
                  "motion": "track", "rank": 3},
    "jet_drone": {"colour": "#ff3b30", "label": "Jet drone",
                  "motion": "track", "rank": 4},
    # An FPV, and its own kind rather than a drone.
    #
    # Flown by a person on a video link, with a range of a few kilometres
    # rather than a few hundred -- so an FPV mark says the thing that
    # launched it is CLOSE, which is a different fact from a Shahed crossing
    # an oblast and the most useful thing this kind carries.
    #
    # Ranked below a drone because it threatens less ground, and kept for
    # less time: something with a few minutes of flight in it is not still
    # up twenty minutes later, and a stale FPV on the map is a claim about a
    # place nobody is currently being attacked from.
    "fpv":       {"colour": "#ff9ecd", "label": "FPV drone",
                  "motion": "track", "rank": 2, "keep": 8},
    "missile":   {"colour": "#a855f7", "label": "Missile",
                  "motion": "track", "rank": 6},
    # A guided bomb, and its own kind rather than a missile.
    #
    # NEPTUN reports these separately and it is right to. A KAB is released
    # from an aircraft near the line and glides tens of kilometres, so a KAB
    # mark says something different about where the danger is and how long
    # there is: flattening it into "missile" would draw a hundreds-of-
    # kilometres weapon where a tens-of-kilometres one was reported.
    "bomb":      {"colour": "#ff8a3b", "label": "Guided bomb",
                  "motion": "track", "rank": 5},
    # Kept separate, and deliberately. A crewed aircraft is neither a drone nor
    # a missile: "тактична авіація" means aircraft are up, which is a warning
    # about what may follow rather than about something already inbound, and
    # folding it into either would be saying something the report did not.
    "aircraft":  {"colour": "#7dffcf", "label": "Aircraft",
                  "motion": "track", "rank": 3},
    "alert":     {"colour": "#ffb020", "label": "Air alert",
                  "motion": "still", "rank": 1,
                  # An hour and a half. An alert for a city runs about that
                  # long, and on a cold start it is what decides whether the
                  # warnings already in force when you open the app are drawn
                  # at all -- at an hour, one declared seventy minutes ago and
                  # still running showed as nothing.
                  #
                  # Matches ALERT_MINUTES, which is how long the report stays
                  # readable in the stream, so the mark and the line that
                  # explains it now go together instead of the mark leaving
                  # first.
                  "keep": 90},
    "unknown":   {"colour": "#9aa4b2", "label": "Unidentified",
                  "motion": "track", "rank": 2},

}

# Derived, never edited on their own. "motion" no longer drives any movement
# -- nothing moves -- but it still says which kinds are things in flight and
# which are facts about a place, and that difference decides whether a mark can
# join a mass in concentrate mode and how long it stays on the map.
MOTION = {name: look["motion"] for name, look in KINDS.items()}
KEEP = {name: look.get("keep", KEEP_MINUTES) for name, look in KINDS.items()}

# See _lookback_minutes() above for why this is the longest keep time and not
# a window of its own.
LOOKBACK_MINUTES = _lookback_minutes()

# Kinds that are announcements or places rather than things in flight.
NOT_AIRBORNE = tuple(name for name, look in KINDS.items() if look["motion"] == "still")


# How many marks and reports to hold at once.
#
# Generous rather than tuned. A heavy night is hundreds of tracks, the cap
# drops the oldest, and a cap sized for a quiet night would quietly stop being
# a heavy one -- the map would look complete while missing the beginning of
# it. Cheap to leave high: six hundred marks measured a 16.7 ms median frame
# with concentrate mode on, and that is the only thing here worse than
# linear.
MAX_EVENTS = 1200
MAX_ALERTS = 600

_lock = threading.Lock()
# Post ids already read, oldest first -- a dict rather than a set so the
# oldest can be dropped when it gets too big. It grows faster than it used
# to: NEPTUN's message feed is re-read on every poll now, and the whole
# reason that is cheap is this remembering what has been read. Unbounded, it
# would be the one thing in here that only ever grows.
_seen: dict[str, float] = {}
MOST_SEEN = 50_000
_events: list[dict[str, Any]] = []
_alerts: list[dict[str, Any]] = []
_counter = 0
# How many channel readings NEPTUN's own coverage stood in for this run.
# Counted rather than dropped in silence: "read, and not drawn, because a
# better source has Ukraine" is a different fact from "not read".
_shadowed = 0
_state = "not started"
_last_poll = 0.0
# Whether a background read is in flight, so two requests cannot start two.
_polling = False
# What each channel gave on the last read: how many posts it had, how many of
# them were read as events, and how many of those could be placed.
#
# Kept because "I cannot see reports from the other accounts" is otherwise
# unanswerable from either end. A channel can be unreachable, reachable but
# posting nothing, posting things this cannot read, or posting places the
# gazetteer does not know -- four quite different problems that all look like
# an empty map.
_sources: dict[str, dict[str, Any]] = {}
# When OpenRouter may be asked again, and how long the last rest was.


class TrackerError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Reading the channels
# ---------------------------------------------------------------------------

_MESSAGE = re.compile(
    r'data-post="(?P<post>[^"]+)".*?'
    r'<time[^>]*datetime="(?P<when>[^"]+)"',
    re.S)
_TEXT = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(?P<body>.*?)</div>', re.S)
_TAGS = re.compile(r"<[^>]+>")

# The pictures. Telegram's preview does not use <img> for them: a photo is a
# div whose CSS background-image is the file on their CDN, and a video is the
# same thing holding its thumbnail. So this reads the style attribute.
#
# The quoting varies -- single, double, and HTML-escaped double -- so the
# chunk is unescaped before this runs and the quote is optional. Found the hard
# way: the first version took literal quotes only and silently missed every
# photo whose attribute used &quot;, which would have read as the channels
# having stopped posting pictures rather than as this regex being wrong.
_PHOTO = re.compile(
    r"""background-image:\s*url\(\s*['"]?(?P<url>https://[^'")\s]+)""")

# Where a Telegram picture may come from. Checked twice: here, so a rewritten
# page cannot put an arbitrary URL into an event, and again in the proxy that
# fetches it, so a stored event cannot either.
_CDN = re.compile(r"^https://cdn\d+\.cdn-telegram\.org/", re.I)

# How many to keep per post. A strike report carries two or three; a summary
# post can carry ten, and a popup is not a gallery.
MOST_PHOTOS = 4


def parse_preview(page: str, channel: str) -> list[dict[str, Any]]:
    """Pull the posts out of a channel's public preview page.

    Deliberately regex rather than an HTML parser: this reads two attributes
    and a block of text out of a page whose shape has been stable for years,
    and adding a parser dependency to do it would be the larger risk.
    """
    posts: list[dict[str, Any]] = []
    # Each message is one wrapper div; splitting on it keeps a post's own text
    # with its own id rather than letting a greedy match cross into the next.
    for chunk in page.split('class="tgme_widget_message ')[1:]:
        head = _MESSAGE.search(chunk)
        if not head:
            continue
        body = _TEXT.search(chunk)
        if not body:
            continue
        text = _TAGS.sub(" ", body.group("body").replace("<br/>", "\n").replace("<br>", "\n"))
        text = html.unescape(text)
        text = re.sub(r"[ \t]+", " ", text).strip()
        if not text:
            continue
        # Only from Telegram's own CDN. The URL comes out of a page, so it is
        # somebody else's string until it has been checked against a pattern.
        photos = [found.group("url")
                  for found in _PHOTO.finditer(html.unescape(chunk))
                  if _CDN.match(found.group("url"))][:MOST_PHOTOS]
        posts.append({
            "id": head.group("post"),
            "channel": channel,
            "when": head.group("when"),
            "text": text[:1200],
            "photos": photos,
            # The post itself, so a popup can offer the source rather than
            # asking anyone to take its word for it.
            "link": f"https://t.me/{head.group('post')}",
        })
    return posts


def _fetch_channel(channel: str) -> list[dict[str, Any]]:
    try:
        resp = requests.get(PREVIEW.format(channel=channel), timeout=20,
                            headers={"User-Agent": config.USER_AGENT})
    except requests.RequestException as exc:
        raise TrackerError(f"{channel} could not be read: {exc}") from exc
    if not resp.ok:
        raise TrackerError(f"{channel} answered {resp.status_code}")
    return parse_preview(resp.text, channel)


# Things a model offers as a place name when it has not got one. Each of these
# has been seen; none of them is somewhere.
NON_PLACES = {
    "unknown", "unspecified", "n/a", "na", "none", "null", "not specified",
    "not stated", "somewhere", "various", "multiple", "several", "-", "?",
}


def _name(value: Any) -> str | None:
    """A place name, or None if what came back is not one."""
    text = " ".join(str(value or "").split())
    if len(text) < 2 or text.lower().strip(".") in NON_PLACES:
        return None
    # A model told not to give coordinates sometimes gives them anyway, in the
    # place-name field. A gazetteer would then look up the literal string and
    # either miss or match something absurd, so they are refused here.
    if re.fullmatch(r"[-+0-9.,°'\"NSEW\s]+", text):
        return None
    return text[:80]


# The sixteen points of the compass, as degrees. A report saying "курсом на
# північ" has given a course as surely as one naming a town, and the version
# that only understood destinations drew it as a stationary burst -- a thing
# that had landed, which is the opposite of what it said.
COMPASS = {
    "n": 0, "nne": 22.5, "ne": 45, "ene": 67.5,
    "e": 90, "ese": 112.5, "se": 135, "sse": 157.5,
    "s": 180, "ssw": 202.5, "sw": 225, "wsw": 247.5,
    "w": 270, "wnw": 292.5, "nw": 315, "nnw": 337.5,
}

# Written out, in case the model answers in words rather than letters.
COMPASS_WORDS = {
    "north": "n", "north-east": "ne", "northeast": "ne", "east": "e",
    "south-east": "se", "southeast": "se", "south": "s",
    "south-west": "sw", "southwest": "sw", "west": "w",
    "north-west": "nw", "northwest": "nw",
    "north-north-east": "nne", "east-north-east": "ene",
    "east-south-east": "ese", "south-south-east": "sse",
    "south-south-west": "ssw", "west-south-west": "wsw",
    "west-north-west": "wnw", "north-north-west": "nnw",
}


def read_course(value: Any) -> float | None:
    """A compass course as degrees, or None if that is not what this is."""
    text = " ".join(str(value or "").split()).lower().strip(" .")
    if not text:
        return None
    text = COMPASS_WORDS.get(text, text)
    return float(COMPASS[text]) if text in COMPASS else None


# The kinds that were removed, and what they became.
#
# Applied wherever a kind enters rather than by rewriting every regex in
# reports.py and every example in the prompt. Both readers are allowed to go
# on making the finer distinction -- "розвідувальний БпЛА" is genuinely a
# recognisable phrase and "балістика" is genuinely not a cruise missile -- and
# it is collapsed here, in one place, where the decision to collapse it was
# made.
#
# Keeping the readers as they are also means this is reversible: the
# information is still being extracted, and putting a kind back is a line in
# this table rather than a re-derivation.
FOLD = {
    # The reader's name for a guided bomb, onto the map's. NEPTUN send "kab"
    # and this map has drawn a "bomb" since; the reader had no name for one
    # at all until now, and without this line its new name folds to "unknown"
    # and a KAB is a grey ring in the sky.
    "kab": "bomb",
    "recon": "drone",
    "cruise": "missile",
    "ballistic": "missile",
    "helicopter": "aircraft",
}


# Not a thing on the map: a warning being lifted.
#
# "Відбій тривоги" is the all-clear, and it used to be read as an alert --
# "відбій тривоги" contains "тривога", so the broader pattern swallowed it and
# drew a warning at the moment one ended. Worse than a missing feature: the map
# said a province was under alert precisely when it had stopped being.
#
# It is not drawn at all. It takes the warning away.
LIFTED = "all_clear"


# Read, understood, and not drawn.
#
# A strike used to be a star on the map, held for twenty-five hours. It is
# gone, because the position was never good enough to earn a point on a map.
# It is read out of a sentence like "вибухи в Харкові" -- explosions in
# Kharkiv -- which names a city of a million people and sometimes a district,
# and the star went on the city's centroid as though somebody had given a
# grid reference. Beside NEPTUN's map, which draws no strikes at all, it was
# the least trustworthy thing on the screen and it was also the loudest.
#
# Still folded here rather than dropped from the vocabulary, and that is the
# point of this list existing at all. "Вибух" has to keep being recognised AS
# an explosion; a kind the reader no longer has a name for falls through to
# "unknown" and gets drawn as a grey ring in the sky, which would be a worse
# claim than the one just removed.
NOT_DRAWN = ("explosion",)


def fold_kind(kind: Any) -> str:
    """A kind as this app draws it, whatever the reader called it."""
    name = str(kind or "unknown").lower().strip().replace("-", "_")
    name = FOLD.get(name, name)
    if name == LIFTED or name in NOT_DRAWN:
        return name
    return name if name in KINDS else "unknown"


def _clean(item: Any) -> dict[str, Any] | None:
    """One report from the model, checked. Names only -- no positions yet."""
    if not isinstance(item, dict):
        return None

    kind = fold_kind(item.get("kind"))
    # Nothing to draw and nothing to say: see NOT_DRAWN. Dropped here, at the
    # one gate every reading of every source goes through, so there is no
    # second path that could still let one onto the map.
    if kind in NOT_DRAWN:
        return None

    count = item.get("count")
    count = int(count) if isinstance(count, (int, float)) and 1 <= count <= 999 else 1

    place = _name(item.get("place"))
    region = _name(item.get("region"))
    toward = _name(item.get("toward"))
    # "Heading for where it already is" is not a journey, and is usually the
    # model filling a field for the sake of it.
    if toward and place and toward.lower() == place.lower():
        toward = None

    course = read_course(item.get("course"))
    # A thing that is not in flight has no course, whatever the sentence
    # around it happened to mention.
    if kind in NOT_AIRBORNE:
        course, toward = None, None

    summary = " ".join(str(item.get("summary") or "").split())[:160]
    cause = item.get("cause")
    cause = cause if cause in ("drone", "missile") else None
    return {
        "id": str(item.get("id") or "")[:120] or None,
        "kind": kind,
        # What a warning is ABOUT, which is not the same question as what kind
        # of mark this is. "Lipetsk Oblast Drone Alert" is a warning, and the
        # word "Drone" in it says what the warning concerns -- so it is drawn
        # yellow, against red for a missile warning. Only warnings carry one.
        "cause": cause if kind in ("alert", LIFTED) else None,
        "place": place,
        "region": region,
        "toward": toward,
        "course": course,
        "count": count,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Placing
# ---------------------------------------------------------------------------

EARTH_KM = 6371.0088


def bearing(lat: float, lon: float, to_lat: float, to_lon: float) -> float:
    """The compass course from one point to another, in degrees.

    Worked out here rather than asked for, because a report saying "курс на
    Полтаву" has told us the destination and nothing else. Turning that into a
    bearing is spherical trigonometry, which this is good at and a language
    model is not.
    """
    a, b = math.radians(lat), math.radians(to_lat)
    d = math.radians(to_lon - lon)
    y = math.sin(d) * math.cos(b)
    x = math.cos(a) * math.sin(b) - math.sin(a) * math.cos(b) * math.cos(d)
    return math.degrees(math.atan2(y, x)) % 360


def separation(lat: float, lon: float, to_lat: float, to_lon: float) -> float:
    """How far apart two points are, in kilometres. Haversine."""
    a, b = math.radians(lat), math.radians(to_lat)
    dlat = b - a
    dlon = math.radians(to_lon - lon)
    h = (math.sin(dlat / 2) ** 2
         + math.cos(a) * math.cos(b) * math.sin(dlon / 2) ** 2)
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(h)))


# ---------------------------------------------------------------------------
# Concentrate mode
# ---------------------------------------------------------------------------
#
# Forty drone glyphs over one oblast say less than one circle labelled forty.
# On a heavy night the map fills with marks that individually mean "a drone was
# reported here" and collectively mean "this is where the attack is", and the
# second reading is the one you actually want first.
#
# So nearby marks can be gathered into a mass. Three decisions in that, and all
# three are the reason this lives in the backend rather than in the map:
#
#   Distance, not pixels. Clustering by screen distance is what most maps do
#   and it means a cluster changes meaning as you zoom -- the same two marks
#   are "together" at one zoom and apart at the next. Sixty kilometres is
#   sixty kilometres, so a mass means the same thing however you are looking
#   at it, and the number on it can be trusted.
#
#   Transitive, not nearest-centre. A line of drones following a river is one
#   incursion even if its ends are three hundred kilometres apart, so marks
#   join a mass through a chain of neighbours rather than by being close to
#   some centre. This is single-link clustering, and its known weakness --
#   long straggling chains -- is the correct behaviour here, because that is
#   what a corridor of drones is.
#
#   Things in flight only. Alerts are areas already, and a circle drawn over
#   a province that is shaded anyway says nothing the shading did not.

# How close two marks have to be to belong to the same mass. Oblast-scale: far
# enough that a group crossing one province holds together, near enough that
# two separate incursions do not become one.
MASS_WITHIN_KM = 60.0

# How many marks make a mass. Two is a coincidence.
MASS_LEAST = 3


# The masses, and what they were computed from.
#
# Clustering is O(n^2) in the marks on the map, which at the four-hundred cap
# measured 66 ms. Once a minute that is nothing; on every page load, with the
# mode switched off, it is 66 ms of somebody else's request. So it is worked
# out once per change and kept, keyed on exactly which marks are alive --
# which is the thing that changes it, since marks expire between polls as well
# as arriving on them.
_mass_of: tuple[str, ...] = ()
_mass_was: list[dict[str, Any]] = []


def masses_now(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The masses for these marks, recomputed only when the marks change."""
    global _mass_of, _mass_was
    # Keyed on where each mark is as well as which it is. Ids alone would be
    # enough for the live path -- a mark is static, so the same id is always
    # the same place -- but only by luck, and a test that reused ids at new
    # coordinates got a mass computed from the old ones. A cache whose
    # correctness depends on a coincidence elsewhere is not worth the risk of
    # the coincidence ending.
    signature = tuple(
        (str(e.get("id")), e.get("lat"), e.get("lon")) for e in events)
    if signature != _mass_of:
        _mass_was = massed(events)
        _mass_of = signature
    return _mass_was


def mean_bearing(headings: list[float]) -> float | None:
    """The average of several compass bearings.

    Not the arithmetic mean, which is wrong in a way that only shows up
    sometimes: 350 degrees and 10 degrees average to 180 -- due south for two
    things both flying very nearly due north. Averaged as unit vectors
    instead, which is the only answer that does not depend on where the
    numbers happen to wrap.

    None when the headings cancel out, which is the honest answer for a group
    flying in opposite directions: there is no general trajectory, and drawing
    one would be inventing agreement that is not there.
    """
    if not headings:
        return None
    east = sum(math.sin(math.radians(h)) for h in headings)
    north = sum(math.cos(math.radians(h)) for h in headings)
    # How much they agree, from 0 (evenly opposed) to 1 (identical).
    together = math.hypot(east, north) / len(headings)
    if together < 0.25:
        return None
    # Normalised after rounding, not before: a hair under due north comes out
    # of atan2 as -0.03, which % 360 makes 359.97 and round() then makes
    # 360.0 -- a bearing outside the range every consumer of this expects.
    return round(math.degrees(math.atan2(east, north)) % 360, 1) % 360


def borrow_course(events: list[dict[str, Any]],
                  masses: list[dict[str, Any]]) -> int:
    """Give a course to marks in a group that has one and they do not.

    The honest middle of a real problem. Most of these reports state a
    direction -- "курсом на північ", "у напрямку Києва" -- but plenty do not,
    and a drone with no course is drawn as a ring rather than an arrow,
    because an arrow pointing north would be a claim nobody made.

    What can legitimately be said about such a mark is this: several other
    things were reported within sixty kilometres of it, in the same few
    minutes, and those had a course. Things reported together like that are
    usually one group going one way. So the group's trajectory is lent to the
    ones that lack their own, and marked as borrowed -- course_from is
    "group", the popup says so, and the arrow is drawn hollow.

    Only within a KIND, which is the whole of what makes the inference stand
    up. A mass is whatever is within sixty kilometres of whatever else, so a
    guided bomb sitting near a Shahed stream was in that stream's group and
    was given its bearing -- an orange arrow pointing the way the drones were
    going. A KAB is released from an aircraft near the line and glides tens of
    kilometres; a Shahed crosses an oblast at a fifth of the speed on its own
    errand. They are not one group going one way, and the sentence this
    function rests on -- "things reported together like that are usually one
    group" -- is only true of things of the same kind.
    """
    by_id = {str(e.get("id")): e for e in events}
    lent = 0
    for mass in masses:
        members = [by_id[str(i)] for i in mass.get("ids", ()) if str(i) in by_id]
        for kind in {m.get("kind") for m in members}:
            same = [m for m in members if m.get("kind") == kind]
            stated = [m["heading"] for m in same
                      if m.get("heading") is not None
                      and m.get("course_from") != "group"]
            if not stated:
                continue
            course = mean_bearing(stated)
            if course is None:
                continue
            for event in same:
                if event.get("heading") is not None:
                    continue
                event["heading"] = course
                event["course_from"] = "group"
                event["course_from_count"] = len(stated)
                lent += 1
    return lent


def massed(events: list[dict[str, Any]], within_km: float = MASS_WITHIN_KM,
           least: int = MASS_LEAST) -> list[dict[str, Any]]:
    """Group nearby airborne marks into masses.

    Returns one entry per mass, with both a centre and a radius and a bounding
    box, because a circle reads better over a round cluster and a box over a
    corridor -- and which of those a group is cannot be decided here.

    Marks not in any mass are simply absent from the answer. The caller still
    has all of them; this says which of them cluster, not which to draw.
    """
    flying = [e for e in events
              if e.get("kind") not in NOT_AIRBORNE
              and isinstance(e.get("lat"), (int, float))
              and isinstance(e.get("lon"), (int, float))]
    if len(flying) < least:
        return []

    # Union-find over "within within_km of each other".
    parent = list(range(len(flying)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(flying)):
        for j in range(i + 1, len(flying)):
            if separation(flying[i]["lat"], flying[i]["lon"],
                          flying[j]["lat"], flying[j]["lon"]) <= within_km:
                ri, rj = root(i), root(j)
                if ri != rj:
                    parent[ri] = rj

    groups: dict[int, list[dict[str, Any]]] = {}
    for i, event in enumerate(flying):
        groups.setdefault(root(i), []).append(event)

    out: list[dict[str, Any]] = []
    for members in groups.values():
        if len(members) < least:
            continue
        lats = [m["lat"] for m in members]
        lons = [m["lon"] for m in members]
        # The mean is the right centre here rather than a bounding-box middle:
        # it sits where the marks actually are, so a corridor's circle is
        # centred on its densest part instead of on empty ground between the
        # two ends.
        lat = sum(lats) / len(lats)
        lon = sum(lons) / len(lons)
        radius = max(separation(lat, lon, m["lat"], m["lon"]) for m in members)
        tally: dict[str, int] = {}
        for m in members:
            tally[m["kind"]] = tally.get(m["kind"], 0) + 1
        # The general trajectory: the average of whatever courses the members
        # actually have. Only from the ones that have one -- a group of six
        # where two were reported with a course has a trajectory worth
        # drawing, and it is those two's, not a guess on behalf of the other
        # four.
        courses = [m["heading"] for m in members
                   if isinstance(m.get("heading"), (int, float))]
        course = mean_bearing(courses)
        out.append({
            "lat": round(lat, 4),
            "lon": round(lon, 4),
            # A mass of one place would draw a zero-radius circle, which is
            # invisible. Given a floor so it reads as a mark rather than
            # vanishing -- several drones reported at one town is exactly the
            # case this mode is for.
            "radius_km": round(max(radius, 8.0), 1),
            "bbox": [round(min(lons), 4), round(min(lats), 4),
                     round(max(lons), 4), round(max(lats), 4)],
            "count": len(members),
            # The trajectory to draw the mass's arrow along, and how much of
            # the group it was worked out from -- so a mass can say "six
            # tracks, course from two of them" rather than implying all six
            # were reported heading that way.
            "course": course,
            "course_from_count": len(courses),
            "kinds": dict(sorted(tally.items(), key=lambda kv: -kv[1])),
            # What to write on it: the commonest kind, and how many in all.
            "label": f"{len(members)} × {KINDS[max(tally, key=tally.get)]['label'].lower()}"
                     if len(tally) == 1 else f"{len(members)} tracks",
            "ids": [m["id"] for m in members if m.get("id")],
            # The newest report in the mass, so the panel can say how current
            # the whole thing is rather than averaging it.
            "seen": max(m.get("seen", 0) for m in members),
        })
    out.sort(key=lambda m: -m["count"])
    return out


# How wide an alert is drawn when the gazetteer gave no extent to go on, by
# what sort of place it matched. Round numbers, and deliberately modest: an
# area drawn too large claims a warning covers ground nobody mentioned.
AREA_FALLBACK_KM = {
    "administrative": 45.0, "state": 45.0, "region": 45.0, "province": 45.0,
    "county": 25.0, "district": 25.0, "municipality": 15.0,
    "city": 12.0, "town": 6.0, "village": 3.0, "hamlet": 2.0,
    "suburb": 3.0, "neighbourhood": 2.0,
}
AREA_DEFAULT_KM = 8.0
AREA_MAX_KM = 120.0


# What counts as a region rather than a place within one. These are
# Nominatim's own words for an administrative area of some size; a town, a
# village or a suburb is not one however large it happens to be.
REGION_KINDS = ("administrative", "state", "region", "province", "county",
                "district", "governorate", "emirate", "country")


def region_around(lat: float, lon: float,
                  countries: str = "") -> dict[str, Any] | None:
    """The province a point is in, with its outline where that is known.

    Two ways, cheapest first.

    A boundary this app already holds is exact and free: once a province has
    been drawn over once, every later warning inside it is decided by the
    real border rather than by asking anybody. That covers the regions
    warnings actually keep happening in, which is most of them.

    Otherwise the gazetteer is asked which region the point is in, and that
    region's outline is asked for in the background as usual -- so the first
    warning in a new province is drawn from its extent and the second from
    its border.
    """
    if lat is None or lon is None:
        return None
    for name, shape in gazetteer.outlines():
        if _shape_holds(shape, lat, lon):
            known = places.lookup(name) or {}
            return {"name": name, "shape": shape,
                    "bbox": list(known["bbox"]) if known.get("bbox") else None,
                    "area_km": area_km(known) if known else None}
    try:
        name = gazetteer.region_at(lat, lon, countries)
    except gazetteer.GazetteerError:
        return None
    if not name:
        return None
    known = places.lookup(name)
    if known:
        gazetteer.improve_later(known.get("name") or name, countries, urgent=True)
        shape = gazetteer.outline(known.get("name") or name, countries)
        return {"name": known.get("name") or name, "shape": shape,
                "bbox": list(known["bbox"]) if known.get("bbox") else None,
                "area_km": area_km(known)}
    # Not in the table, so the gazetteer is the only source for both its
    # position and its shape. Asked for in the background like any other.
    gazetteer.improve_later(name, countries, urgent=True)
    shape = gazetteer.outline(name, countries)
    return {"name": name, "shape": shape, "bbox": None,
            "area_km": None} if shape else None


def _shape_holds(shape: Any, lat: float, lon: float) -> bool:
    """Whether a GeoJSON polygon holds a point. Ray casting, in degrees."""
    for ring in neptun._walk(shape):
        if len(ring) >= 4 and neptun._ring_holds(ring, lon, lat):
            return True
    return False


def is_region(place: dict[str, Any]) -> bool:
    """Whether a match is an area in its own right rather than a spot in one."""
    if place.get("category") == "boundary":
        return True
    return str(place.get("kind") or "").lower() in REGION_KINDS


def area_km(place: dict[str, Any]) -> float:
    """How far around a place an alert reaches, in kilometres.

    Taken from the extent the gazetteer reported rather than assumed, because
    the difference between the two cases this has to cover is enormous: a
    strike in a village is a couple of kilometres and an air alert over an
    oblast is a hundred. Drawing both the same size would either lose the
    strike in a blob or shrink the oblast to a dot.

    Half the diagonal of the bounding box, which for a roughly square oblast
    is close and for a long thin one is generous. Capped, because a country's
    own bounding box would otherwise shade a continent.
    """
    box = place.get("bbox")
    if box:
        south, north, west, east = box
        across = separation(south, west, north, east)
        if across > 0:
            return round(min(AREA_MAX_KM, max(1.5, across / 2)), 1)
    return AREA_FALLBACK_KM.get(str(place.get("kind") or "").lower(), AREA_DEFAULT_KM)


def _look(lookup, name: str, region: str | None, countries: str,
          kind: str = "", built_in: bool = True):
    """Find a place, using the region the report gave to tell it apart.

    This is the other half of the Kaharlyk failure. "Kaharlyk" on its own is a
    town in Kyiv oblast and also a handful of smaller things elsewhere;
    "Kaharlyk, Kyiv oblast" is one of them. Asking with the region first costs
    a lookup that is almost always cached and removes a whole class of
    confident wrong answers.

    The bare name is still tried afterwards, because the region may be one the
    gazetteer spells differently, and a right town found without it beats no
    town at all.
    """
    # The built-in table first, before anything that costs a request.
    #
    # This was the difference between a poll taking eleven seconds and taking
    # half of one. The region-qualified lookup below is a good idea and it was
    # running FIRST, so "Волинська область" was being asked for as
    # "Волинська область, Ukraine" -- a string the built-in table has never
    # heard of -- and every oblast in the country went to Nominatim at a
    # second apiece despite being three lines away in a dict.
    #
    # Safe to do first precisely because of what is in that table: oblast and
    # major-city names, which are unambiguous. The region qualifier exists to
    # tell two small towns of the same name apart, and no entry here is one.
    # Every de-inflected form is tried against the table too, not just the
    # name as written. These are dict lookups -- the whole sweep costs less
    # than a microsecond -- and each one that hits is a second of Nominatim
    # rate limit not spent. "Кременчуці" is in the table; the spellings the
    # reader derives from it are not, and without this they went to the wire.
    # Only when nobody handed us a gazetteer.
    #
    # An injected lookup means "use this one", and the table was winning over
    # it -- so the demo, which injects a small offline gazetteer, was getting
    # built-in answers with no boundary instead of its own shaped ones, and
    # every test that injected a stub was testing the table instead of the
    # thing it meant to. Nothing is lost on the live path: gazetteer.find()
    # consults the same table as its own first step.
    for attempt in (name, *reports.variants(name)) if built_in else ():
        found = places.lookup(attempt)
        if found:
            if found.get("category") == "boundary":
                # Asked for by the name the TABLE settled on, not by the one
                # the post happened to write.
                #
                # This is why Russia had no provinces on it. The channel
                # covering the Russian side posts in English -- "Belgorod
                # region" -- and the alias table exists precisely because
                # OpenStreetMap does not hold it under that name; it holds
                # "Белгородская область". The table resolved the English
                # spelling correctly and then this handed the English
                # spelling straight back to the gazetteer, which asked
                # OpenStreetMap a question it had no answer to. So the
                # boundary never arrived, and a warning over Belgorod was
                # drawn as a triangle on a point while Ukraine's came out of
                # NEPTUN's file properly shaped.
                canonical = found.get("name") or attempt
                # A warning gets its outline first. It is the only thing here
                # drawn AS the region rather than at a point in it, so it is
                # the only one whose look depends on the boundary arriving.
                gazetteer.improve_later(
                    canonical, countries,
                    urgent=fold_kind(kind) in ("alert", LIFTED))
                # And if it has already arrived, use it. Without this the
                # table's shapeless answer shadowed the boundary forever:
                # the outline was fetched, remembered, and never read, so
                # every region this table knows -- which is every region in
                # Russia -- was a warning with no province under it while
                # Ukraine's came out of NEPTUN's file properly shaped.
                learned = gazetteer.outline(canonical, countries)
                if learned:
                    found = {**found, "shape": learned}
            return found

    if region and region.lower() not in name.lower():
        found = lookup(f"{name}, {region}", countries)
        if found:
            return found
    # The name as written first, then the de-inflected forms. In that order on
    # purpose: what the report actually said is the best thing to ask for, and
    # a guess at its nominative is only worth trying once that has failed.
    for attempt in reports.variants(name):
        found = lookup(attempt, countries)
        if found:
            return found
    return None


def place_event(item: dict[str, Any], countries: str, lookup=None) -> dict[str, Any]:
    """Turn a report's place names into positions, if they are known.

    An unplaceable report is not a failure to be swallowed. It comes back with
    `lat` None and a reason, goes into the alert stream, and is counted -- so
    "the gazetteer does not know these names" is visible as itself rather than
    as an empty map.
    """
    # Resolved here rather than as a default argument. A default is bound
    # once, when the function is defined, so a gazetteer swapped out later --
    # in a test, or for the demo -- was silently ignored and the real one
    # called instead.
    injected = lookup is not None
    lookup = lookup or gazetteer.find

    out = dict(item)
    out["lat"] = out["lon"] = out["heading"] = None
    out["course_from"] = None
    out["dest_lat"] = out["dest_lon"] = out["dest_km"] = None
    out["motion"] = "still"
    out["placed"] = False

    if not item.get("place"):
        out["why_unplaced"] = "the report names no place"
        return out

    try:
        here = _look(lookup, item["place"], item.get("region"), countries,
                     item.get("kind", ""), built_in=not injected)
    except gazetteer.GazetteerError as exc:
        out["why_unplaced"] = str(exc)
        return out
    if not here:
        out["why_unplaced"] = f'"{item["place"]}" is not in the gazetteer'
        return out

    out["lat"], out["lon"] = here["lat"], here["lon"]
    out["place_match"] = here.get("name")
    out["place_kind"] = here.get("kind")
    out["place_category"] = here.get("category")
    out["area_km"] = area_km(here)
    # The real outline, kept only where the report named a REGION. A strike in
    # a town is a point in it, not the whole town, and drawing the municipal
    # boundary round one would claim the damage followed the council's border.
    # A warning over an oblast is genuinely about that oblast, and there the
    # boundary is the honest shape -- a circle over the middle both misses
    # ground the warning covers and covers ground it does not.
    # The outline of the region, where the report named one -- and the reason
    # it is drawn depends on what was reported, because there are two quite
    # different things to say and one shape to say them with:
    #
    #   "covers"   a warning or a strike ACROSS a region. The whole area is
    #              under it, and that is a claim about the ground.
    #
    #   "located"  something in flight, reported only to oblast precision.
    #              The region is not under anything; it is how precisely
    #              anybody knows where the thing is.
    #
    # The second is the one this was missing. A drone reported over an oblast
    # was drawn as a marker on the oblast's centroid, which says its position
    # is known to a few kilometres when the report located it to a couple of
    # hundred. Showing the region says what was actually known.
    # The region's real outline, from whichever source has one.
    #
    # NEPTUN publish the oblast and raion boundaries their own alert keys
    # index into, and they are the answer to "make warnings actually cover
    # regions": a warning drawn from a bounding box is a rectangle over a
    # province, which no province is shaped like. Tried FIRST, because a real
    # boundary beats a learned one and beats an extent, and because it costs
    # nothing -- the file is fetched once and kept.
    #
    # This applies to warnings read from the Telegram channels too, not only
    # to NEPTUN's own. The shape of Sumy oblast does not depend on who
    # mentioned it.
    out["shape"] = None
    if is_region(here):
        out["shape"] = (neptun.shape_for(here.get("name"))
                        or neptun.shape_for(item.get("place"))
                        or here.get("shape"))
    # The region's extent, so a warning whose real boundary has not arrived
    # yet can be drawn as that rectangle rather than as a circle. A disc
    # centred on an oblast is not the shape of any province and reads as a
    # blast radius -- a claim about ground nobody made.
    out["bbox"] = list(here["bbox"]) if is_region(here) and here.get("bbox") else None
    # Whether the report named a REGION, which is a question about the match
    # and not about what this app happens to have fetched.
    #
    # It used to be answered by "did an outline arrive": region_scope was set
    # only when out["shape"] was there. So a report located to an oblast whose
    # boundary had not been fetched yet counted as a point, and was drawn as
    # one -- a mark sitting on the arithmetic centre of a province, which is
    # the "dot icons that make no sense" in the middle of an oblast label.
    #
    # The outline decides whether an AREA can be drawn, and hasArea() asks it
    # separately. It has nothing to do with how precisely the report located
    # anything, and conflating the two made the drawing depend on a fetch.
    out["region_scope"] = (
        "covers" if MOTION.get(item["kind"], "track") == "still" else "located"
    ) if is_region(here) else None

    # A warning reported at a TOWN covers the region that town is in.
    #
    # "Air alert in Sochi" is a warning over Krasnodar Krai. Drawn at the
    # town it was a triangle on one street corner of a province -- which is
    # what the Russian side looked like beside Ukraine's filled oblasts,
    # because NEPTUN key their alerts to regions and a Telegram post names
    # whatever the poster named.
    #
    # Only warnings. A drone over Sochi is AT Sochi; shading the krai for it
    # would say a warning covers ground nobody mentioned, which is the
    # distinction this layer has spent a long time getting right.
    #
    # Asked of any warning with no OUTLINE, rather than of any warning with
    # no region -- which is why some of Russia shaded and the rest did not.
    # A warning that NAMES a region ("Air alert in Kursk region") already had
    # region_scope set, so this never ran for it and it sat waiting on the
    # gazetteer's background queue, which is rate-limited and gives up after
    # four tries. A warning that named a TOWN came through here and was
    # shaded at once. Two paths, two answers, on one map.
    #
    # The point of a region warning IS the region, so the centroid is inside
    # its own province and the very first test below -- a boundary the app
    # already holds -- answers it for nothing.
    if (out["shape"] is None
            and MOTION.get(item["kind"], "track") == "still"):
        wider = region_around(out["lat"], out["lon"], countries)
        if wider:
            out["region_scope"] = "covers"
            out["region_wide"] = True
            out["shape"] = wider.get("shape")
            out["bbox"] = wider.get("bbox") or out["bbox"]
            # The town stays the place: "Air alert in Sochi" is what was
            # reported and is more use than the krai's name. The region is
            # recorded beside it so the popup can say which was shaded.
            out["region_over"] = wider.get("name")
            if wider.get("area_km"):
                out["area_km"] = wider["area_km"]
    # Kept meaning what it always meant: the whole region is under this.
    out["region_wide"] = out["region_scope"] == "covers"
    out["placed"] = True
    out["motion"] = MOTION.get(item["kind"], "track")
    out.pop("why_unplaced", None)

    # Something on station is not going anywhere, so a course would be a
    # claim the report did not make. It circles instead.
    if out["motion"] != "track":
        return out

    # A compass course is a real answer and is used when no destination was
    # named. A destination is better, so it wins where there is one.
    #
    # Where each heading came from is recorded rather than left implicit. The
    # three sources are not equally good -- a bearing computed between two
    # named places is worth more than "north", and both are worth more than a
    # direction borrowed from the group around it -- and a map that draws all
    # three as the same arrow should at least be able to say which it is.
    out["heading"] = item.get("course")
    out["course_from"] = "stated" if out["heading"] is not None else None

    if not item.get("toward"):
        return out
    try:
        there = _look(lookup, item["toward"], item.get("region"), countries)
    except gazetteer.GazetteerError:
        return out
    if not there:
        return out
    km = separation(here["lat"], here["lon"], there["lat"], there["lon"])
    if km < 1:
        # The gazetteer matched both names to the same point. Nothing to
        # travel, and a bearing between two identical points is noise.
        return out
    out["dest_lat"], out["dest_lon"] = there["lat"], there["lon"]
    out["dest_km"] = round(km, 1)
    out["heading"] = bearing(here["lat"], here["lon"], there["lat"], there["lon"])
    out["course_from"] = "destination"
    return out


# ---------------------------------------------------------------------------
# Moving
# ---------------------------------------------------------------------------


# How far past a confirmed position a mark is carried, in minutes.
#
# Dead reckoning is only as good as the assumption that nothing turned, and
# that assumption decays. NEPTUN confirm an active track far more often than
# this, so in normal running the cap is never reached; it is here for the
# track that stops being confirmed -- a drone that went down, a record that
# stalled -- where the alternative is a mark flying serenely across the
# country for an hour on the strength of one old sentence.
#
# Ten minutes at a Shahed's speed is about thirty kilometres, which is a
# wrong-but-honest distance for a mark this map has stopped hearing about.
MOST_DRIFT_MINUTES = 10.0


def project(event: dict[str, Any], now: float) -> dict[str, Any]:
    """An event as it stands now, and how far it has run since it was confirmed.

    The position sent is always the reported one -- what somebody actually
    said, at the place they said it. Nothing is moved here.

    What IS sent, for a track whose source gives a course, a speed and the
    moment the position was last confirmed, is `drift_minutes`: how long ago
    that confirmation was. The map carries the mark along from there and keeps
    carrying it between polls, so a drone crossing an oblast moves instead of
    jumping thirty kilometres every time the feed is refreshed.

    An earlier version of this layer moved marks too and it was removed, for
    reasons that still hold: it flew things along a course at a speed looked
    up from a table of what that kind of aircraft typically does, which is an
    assumption dressed as telemetry. This is not that. The course, the speed
    and the anchor all come from the source, it is the arithmetic their own
    SDK does, and a track that does not carry all three does not move at all.

    Minutes rather than a position, and elapsed rather than absolute, because
    the browser's clock is not this machine's clock. The map adds its own time
    since the feed arrived, so the two never have to agree about what o'clock
    it is -- only about how long a second is.
    """
    out = dict(event)
    out["age_minutes"] = round(max(0.0, (now - event["seen"]) / 60), 1)
    out["lat"], out["lon"] = event["origin_lat"], event["origin_lon"]
    out["projected"] = False
    out["drift_minutes"] = _drift_minutes(event, now)
    return out


def _drift_minutes(event: dict[str, Any], now: float) -> float | None:
    """Minutes since this position was confirmed, or None if it cannot move.

    None is the answer for everything except a moving track that came with all
    three of a course, a speed above zero and an anchor. Those three are the
    whole test, and between them they cover the cases that must never move:

      areaOnly. Their "no extrapolation" rule. neptun.read_threat drops the
      anchor for one, so the rule is kept by the shape of the data rather
      than by a condition here that somebody could later delete.

      A warning. It is a statement about a region, with no course and no
      speed to carry anything at.

      A strike. It has already happened where it happened.

    There was a fourth condition here for a while, refusing anything whose
    motion is "still". Nothing could reach it -- no kind NEPTUN send maps to
    a still motion, and nothing without an anchor gets this far -- and a
    condition that cannot be reached is a condition that cannot be trusted,
    because nothing would notice if it stopped working.
    """
    anchor = event.get("confirmed_at")
    speed = event.get("speed_kmh")
    if not anchor or event.get("heading") is None:
        return None
    if not isinstance(speed, (int, float)) or speed <= 0:
        return None
    return round(min(MOST_DRIFT_MINUTES, max(0.0, (now - anchor) / 60)), 2)


# ---------------------------------------------------------------------------
# Putting it together
# ---------------------------------------------------------------------------


def _when(message: dict[str, Any]) -> float:
    try:
        return dt.datetime.fromisoformat(
            str(message.get("when")).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return time.time()


# The largest picture to pass through. Telegram's preview images are a few
# hundred kilobytes; a ceiling stops a rewritten page, or a changed CDN, from
# being able to stream something enormous through this process.
PHOTO_BYTES = 8 * 1024 * 1024


def fetch_photo(url: str) -> tuple[bytes, str]:
    """One Telegram picture, fetched here rather than by the browser.

    The host is checked against _CDN before anything is opened, and that check
    is the whole security of this function. An endpoint that fetches a URL a
    caller supplies is a way into everything this process can reach that the
    caller cannot -- a metadata service, another container, a database on
    loopback -- so the allowlist is a pattern match against Telegram's own CDN
    and there is no configuration to widen it.

    Redirects are refused rather than followed. A permitted host that answers
    with a redirect elsewhere would otherwise walk straight past the check
    that was the point of it.
    """
    if not isinstance(url, str) or not _CDN.match(url):
        raise TrackerError("that is not a Telegram picture")
    try:
        resp = requests.get(url, timeout=20, stream=True, allow_redirects=False,
                            headers={"User-Agent": config.USER_AGENT})
    except requests.RequestException as exc:
        raise TrackerError(f"the picture could not be fetched: {exc}") from exc
    if resp.is_redirect or resp.is_permanent_redirect:
        raise TrackerError("the picture redirected somewhere else")
    if not resp.ok:
        raise TrackerError(f"Telegram answered {resp.status_code} for that picture")

    kind = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if not kind.startswith("image/"):
        raise TrackerError(f"that is not an image ({kind or 'no type given'})")

    # Read to the ceiling and no further, rather than trusting Content-Length,
    # which is a claim and not a measurement.
    body = bytearray()
    for piece in resp.iter_content(64 * 1024):
        body += piece
        if len(body) > PHOTO_BYTES:
            raise TrackerError("that picture is too large")
    return bytes(body), kind


def photo_paths(urls: Any) -> list[str]:
    """Telegram CDN URLs as paths through this app's own proxy."""
    if not isinstance(urls, list):
        return []
    return [f"/api/tracker/photo?u={quote(url, safe='')}"
            for url in urls[:MOST_PHOTOS]
            if isinstance(url, str) and _CDN.match(url)]


def _record(item: dict[str, Any], message: dict[str, Any],
            countries: str) -> bool:
    """One classified report: an alert always, a track only if it placed."""
    global _counter, _shadowed
    seen = _when(message)
    placed = place_event(item, countries)

    # NEPTUN is the source for Ukraine, so a channel reading that lands
    # inside Ukraine is not drawn.
    #
    # This module has said so at the top since the other four channels were
    # removed -- "two paths to the same facts with only the worse one able to
    # put a mark in the wrong province" -- and then went on reading the one
    # remaining channel into Ukraine anyway. It is kept for the Russian side,
    # which NEPTUN does not cover, and that is the only thing it is kept for.
    #
    # What this removes is real: a jet drone drawn over Sumy from a Telegram
    # post, in a colour NEPTUN's vocabulary cannot even produce -- their
    # types are uav, recon, missile, ballistic, kab and mig31k, and none of
    # them is a jet drone -- sitting beside NEPTUN's own tracks of the same
    # night's raid, which did not include it.
    #
    # Only while NEPTUN is actually answering. covers() returns None when the
    # boundary file has not arrived, and None is not False: with no border to
    # test against, and no NEPTUN behind it, the channel is the only source
    # there is and its reports are drawn.
    if placed["placed"] and item.get("by") != "neptun":
        in_ukraine = neptun.covers(placed["lat"], placed["lon"])
        if in_ukraine:
            _shadowed += 1
            return False

    # An all-clear takes a warning away rather than putting one up.
    #
    # It still goes into the stream -- "the warning over Kyiv oblast was
    # lifted" is worth reading -- but it draws nothing, and it removes the
    # warnings near where it was reported. Without this an alert sat on the
    # map for its full hour after being called off, which is the map saying
    # the opposite of what happened.
    if placed["kind"] == LIFTED:
        took_down = 0
        if placed["placed"]:
            # As far as the thing it named reaches, and never less than the
            # floor. A stand-down for a province calls off that province: if
            # it only reached ninety kilometres from the centroid, the corners
            # of a big region kept a warning nobody had left standing.
            took_down = lift_alerts(
                placed["lat"], placed["lon"],
                max(LIFT_WITHIN_KM, float(placed.get("area_km") or 0.0)))
        _counter += 1
        _alerts.append({
            "id": f"AO{_counter:04d}",
            "source": message.get("id"),
            "by": item.get("by", "model"),
            "kind": "alert",
            "rank": 0,
            "lifts": True,
            # What the warning was about, so a stand-down row is coloured the
            # same way the warning it lifts was. Without it a lifted missile
            # warning and a lifted drone warning read identically.
            "cause": placed.get("cause"),
            "summary": placed["summary"] or "All clear",
            "place": placed.get("place"),
            "placed": placed["placed"],
            "why_unplaced": placed.get("why_unplaced"),
            "channel": message.get("channel"),
            "region": message.get("region"),
            "seen": seen,
            "took_down": took_down,
            "text": message.get("text", "")[:300],
        })
        return False

    _counter += 1
    ident = f"AO{_counter:04d}"
    _alerts.append({
        "id": ident,
        # Which post this came from, so a rules-first reading can be replaced
        # by the model's when it arrives rather than appearing twice.
        "source": message.get("id"),
        "by": item.get("by", "model"),
        "kind": placed["kind"],
        # Drone or missile, for warnings. The row in the panel is coloured by
        # it the same way the mark is.
        "cause": placed.get("cause"),
        "rank": KINDS[placed["kind"]]["rank"],
        "summary": placed["summary"] or (placed.get("place") or "Report"),
        "place": placed.get("place"),
        "placed": placed["placed"],
        "why_unplaced": placed.get("why_unplaced"),
        "channel": message.get("channel"),
        "region": message.get("region"),
        "seen": seen,
        "text": message.get("text", "")[:300],
        # The pictures, as proxy paths rather than CDN URLs. See
        # /api/tracker/photo: the browser never talks to Telegram, which is
        # the same promise the rest of this layer makes, and it is worth more
        # for pictures than for text -- an <img> straight to their CDN would
        # hand them the viewer's address on every popup.
        "photos": photo_paths(message.get("photos")),
        "link": message.get("link"),
    })

    if not placed["placed"]:
        return False
    # A post somebody dismissed stays dismissed when it is read again. Without
    # this the mark returns on the next poll with a new identifier, which from
    # the outside is the dismissal simply not working.
    if str(message.get("id")) in _dismissed:
        return False
    _events.append({
        **placed,
        # The oblast the READER found, kept under its own name.
        #
        # "region" on an event is the channel's beat -- "Ukraine", "Luhansk
        # and Russia" -- and it was overwriting this, so a drone reported in a
        # digest section headed "Сумщина" arrived on the map knowing only that
        # it came from a Ukrainian channel. The oblast is what a warning is
        # declared over, so it has to survive.
        "oblast": placed.get("region"),
        "id": ident,
        "by": item.get("by", "model"),
        "origin_lat": placed["lat"],
        "origin_lon": placed["lon"],
        "seen": seen,
        "channel": message.get("channel"),
        "region": message.get("region"),
        "source": message.get("id"),
        "text": message.get("text", "")[:300],
        # The pictures, as proxy paths rather than CDN URLs. See
        # /api/tracker/photo: the browser never talks to Telegram, which is
        # the same promise the rest of this layer makes, and it is worth more
        # for pictures than for text -- an <img> straight to their CDN would
        # hand them the viewer's address on every popup.
        "photos": photo_paths(message.get("photos")),
        "link": message.get("link"),
    })
    return True


# How near a lifted warning has to be to the one it lifts, in kilometres.
#
# Generous, because an oblast is generous: a warning for Kyiv oblast may have
# been placed at the province centre and the all-clear at its capital, and both
# are the same warning. Matching on the name alone would miss that; matching
# on the whole country would lift warnings elsewhere.
#
# A FLOOR rather than the whole rule, though -- see _record, which widens it
# to the region the stand-down actually names. Ninety kilometres is about
# half a Ukrainian oblast and it was written for those. The Russian side's
# subjects are nothing like that size: Rostov oblast is four hundred
# kilometres across and Bashkortostan is the size of Britain, so a stand-down
# posted for one of those reached a fraction of the province it was calling
# off and the warning stayed up over the rest of it.
LIFT_WITHIN_KM = 90.0


def lift_alerts(lat: float, lon: float, within_km: float = LIFT_WITHIN_KM) -> int:
    """Take down the warnings near a point. Returns how many were lifted.

    Nothing else is touched. A strike does not stop having happened because a
    warning was lifted, and a drone reported five minutes ago is still a drone
    -- only the warning itself is a state that can end.
    """
    lifted = [e for e in _events
              if e["kind"] == "alert"
              and separation(lat, lon, e["lat"], e["lon"]) <= within_km]
    if not lifted:
        return 0
    gone = {e["id"] for e in lifted}
    _events[:] = [e for e in _events if e["id"] not in gone]
    # The reports stay in the stream, marked, because "a warning here was
    # lifted" is worth reading even though there is nothing left to draw.
    for alert in _alerts:
        if alert["id"] in gone:
            alert["lifted"] = True
    return len(lifted)


def forget_source(post_id: Any) -> int:
    """Drop everything that came from one post.

    So the model's reading can replace the rules-read one rather than sit
    beside it. Without this, reading twice draws every mark twice -- which on
    a busy night is the map saying there are forty drones when there are
    twenty, and that is a worse error than a late reading.

    Matched on the post id rather than on the generated AO identifier, because
    the two readings of one post get different identifiers and it is the post
    they have in common.
    """
    if not post_id:
        return 0
    before = len(_events) + len(_alerts)
    _events[:] = [e for e in _events if e.get("source") != post_id]
    _alerts[:] = [a for a in _alerts if a.get("source") != post_id]
    return before - len(_events) - len(_alerts)


# ---------------------------------------------------------------------------
# Dismissing things by hand
# ---------------------------------------------------------------------------
#
# Somebody watching this map knows things it does not. A drone was shot down
# and the channel has not said so yet; a warning is stale; a report was plainly
# a duplicate. Until now the only answer was to wait out the keep time.
#
# What a dismissal is and is not:
#
#   It hides, it does not delete. The report stays in the stream, marked, so
#   the record of what a channel said is not editable from a browser -- this
#   removes a MARK from a MAP, which is a different thing from claiming the
#   report was never made.
#
#   It is remembered by the post it came from, not by the mark's identifier.
#   A mark dismissed and then re-read on the next poll would otherwise come
#   straight back with a new id, which reads as the dismissal not working.
#
#   It is per-process and not per-viewer. Everyone looking at this instance
#   sees the same map, which is the honest behaviour for a shared screen and
#   is the only one this backend can offer without accounts.
_dismissed: set[str] = set()

# A ceiling, so a stuck client cannot grow this without bound.
MAX_DISMISSED = 2000


def dismiss(ident: Any) -> int:
    """Take one mark off the map by hand. Returns how many were removed.

    Accepts either the mark's own identifier or the id of the post it came
    from, because the panel row and the map popup naturally have different
    ones to hand.
    """
    ident = str(ident or "").strip()[:160]
    if not ident:
        return 0
    with _lock:
        sources = {e.get("source") for e in _events if e.get("id") == ident}
        sources |= {a.get("source") for a in _alerts if a.get("id") == ident}
        keys = {ident} | {str(s) for s in sources if s}
        _dismissed.update(keys)
        if len(_dismissed) > MAX_DISMISSED:
            _dismissed.clear()
            _dismissed.update(keys)
        gone = [e for e in _events if _is_dismissed(e)]
        _events[:] = [e for e in _events if not _is_dismissed(e)]
        # The report stays, marked. A mark taken off the map is not a claim
        # that the channel never said it, and the panel row is how somebody
        # notices they dismissed something they should not have.
        for alert in _alerts:
            if alert.get("id") in keys or str(alert.get("source")) in keys:
                alert["dismissed"] = True
    return len(gone)


def _is_dismissed(event: dict[str, Any]) -> bool:
    return (event.get("id") in _dismissed
            or str(event.get("source")) in _dismissed
            # A derived warning is dismissed by its own id, which is stable
            # across polls because it is built from the region name.
            or str(event.get("place_match") or "") in _dismissed)


def hide_dismissed(events: list[dict[str, Any]],
                   alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the marks somebody took off, and mark their rows. Returns events.

    Shared by the live path and the demo, because the demo rebuilds its events
    from a seed on every call and so had no memory of a dismissal at all --
    pressing the button did nothing and looked exactly like the feature being
    broken. Fifth time the offline build has been unable to reach a state.
    """
    for alert in alerts:
        if _is_dismissed(alert):
            alert["dismissed"] = True
    return [e for e in events if not _is_dismissed(e)]


def restore(ident: Any) -> bool:
    """Undo a dismissal. The mark comes back on the next poll that reads it."""
    ident = str(ident or "").strip()[:160]
    with _lock:
        found = ident in _dismissed
        _dismissed.discard(ident)
        for alert in _alerts:
            if alert.get("id") == ident or str(alert.get("source")) == ident:
                alert.pop("dismissed", None)
                _dismissed.discard(str(alert.get("source")))
                _dismissed.discard(str(alert.get("id")))
                found = True
    return found


def dismissed_now() -> int:
    with _lock:
        return len(_dismissed)


def keep_minutes(kind: str) -> int:
    """How long a marker of this kind stays on the map."""
    return KEEP.get(kind, KEEP_MINUTES)


def _alive(event: dict[str, Any], now: float) -> bool:
    return event["seen"] >= now - keep_minutes(event["kind"]) * 60


def _expire(now: float) -> None:
    """Drop what is too old to extrapolate, and what has arrived."""
    _events[:] = [e for e in _events
                  if _alive(e, now)][-MAX_EVENTS:]
    # A report must not leave the list while its marker is still on the map:
    # a burst over a town with nothing in the panel to explain it is worse
    # than either on its own. So the stream holds each kind at least as long
    # as the map does.
    _alerts[:] = [a for a in _alerts
                  if a["seen"] >= now - max(ALERT_MINUTES,
                                            keep_minutes(a["kind"])) * 60][-MAX_ALERTS:]


def reset() -> None:
    """Forget everything read so far. For tests and for starting over."""
    global _counter, _state, _last_poll, _polling, _shadowed
    with _lock:
        _dismissed.clear()
        _seen.clear()
        _events.clear()
        _alerts.clear()
        _counter = 0
        _shadowed = 0
        _state = "not started"
        _last_poll = 0.0


def _remember_sources(seen_now: dict[str, dict[str, Any]]) -> None:
    """Keep what each channel gave, for the panel to show.

    Recorded before anything else can go wrong with the read, because this is
    most needed exactly when nothing is coming through -- and the first
    version updated it only on the path where there WAS something new, so a
    quiet night left the panel with nothing to say about why.

    A channel with nothing new keeps its last counts, so one quiet minute does
    not read as a channel that has gone away.
    """
    with _lock:
        for name, tally in seen_now.items():
            if tally["fresh"] == 0 and name in _sources and not tally["problem"]:
                tally["read"] = _sources[name]["read"]
                tally["placed"] = _sources[name]["placed"]
        _sources.clear()
        _sources.update(seen_now)


# What the second source is called in the panel. Their terms ask for a
# visible link beside the data, and naming the source is the least of that;
# ATTRIBUTION below carries the link itself.
NEPTUN_SOURCE = "neptun.in.ua"

# Whether the one-off catch-up has run this session.


# How far back the catch-up reads on a cold start.
#
# Their threat snapshot is the state NOW: open the app at four in the morning
# and it shows what is in the air at four in the morning and nothing about the
# hour before it. Their message feed carries times, so it can be read for the
# recent past exactly as the channels are -- and that is where a first open
# gets its strikes and its lifted warnings from.
CATCH_UP_MINUTES = 30


def mark_seen(ident: str) -> None:
    """Remember a post as read, and forget the oldest once there are too many.

    The oldest rather than all of them. Clearing the lot would re-read every
    post still inside the catch-up window and draw a second mark for each,
    which is the exact failure this set exists to prevent -- so the cap drops
    the ids that are long past being offered again.
    """
    _seen[ident] = time.time()
    if len(_seen) > MOST_SEEN:
        for old in list(_seen)[:len(_seen) - MOST_SEEN]:
            _seen.pop(old, None)


def catch_up() -> int:
    """Read NEPTUN's message feed -- every channel they aggregate.

    Returns how many reports it produced. Their messages go through the same
    reader the Telegram channels do, because they are the same kind of thing:
    prose from the same channels. What this adds is that they arrive with
    times attached, in one request, from every source they watch rather than
    the one this app can reach directly.

    Run on every poll, not once. It used to run on the first poll of a
    process and never again -- "a cold start is the only time there is a gap
    to fill" -- and that reasoning was wrong. Their snapshot carries the
    tracks somebody has already turned into a track; their messages carry
    everything their sources actually said, which is a great deal more, and
    is the difference between a map with a handful of marks on it and a map
    with the night on it. Cutting it off after the first poll threw away
    every source but two after the first thirty seconds of a run.

    Cheap to repeat: `_seen` holds the post ids, so a post read once is
    skipped for nothing, and the request is the same one either way.
    """
    posts = neptun.messages()
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=CATCH_UP_MINUTES)
    made = 0
    for post in posts:
        if post["id"] in _seen:
            continue
        try:
            when = dt.datetime.fromisoformat(post["when"].replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            continue
        if when < cutoff:
            mark_seen(post["id"])
            continue
        mark_seen(post["id"])
        for plain in reports.read_all(post["text"]):
            plain["kind"] = fold_kind(plain.get("kind"))
            item = _clean({**plain, "id": post["id"]})
            if not item:
                continue
            item["by"] = "rules"
            # Their feed carries the channel's own name, so a report caught up
            # this way is credited to the channel that wrote it rather than to
            # the aggregator that relayed it.
            if _record(item, {**post, "countries": "ua,ru",
                              "region": "Ukraine"}, "ua,ru"):
                made += 1
    return made


# A ceiling on the boundaries handed to the page for drawing its own map.
#
# Twenty-five oblasts, a hundred and change raions, five national borders,
# a hundred and twenty provinces of the neighbours, and whatever has been
# learned from warnings further east -- about two hundred and eighty of them
# with everything arrived.
#
# Raised from 400 for headroom rather than because 400 was being hit: it was
# not, and the arithmetic above is why. It left about a hundred and twenty
# spare, and the only part of that total which grows without bound is the
# learned tail -- one more province every time a warning is reported
# somewhere new. A cap that a long night could reach is a cap that starts
# dropping borders on the night it matters.
#
# Bounded so a runaway index cannot be sent. What it cuts is the tail, and
# the national borders go in first for that reason: under a cap, the last
# thing to go should be the line that says which country the ground is.
MOST_OUTLINES = 600


def outlines() -> list[dict[str, Any]]:
    """Every region boundary this app knows, for drawing a map with no tiles.

    Two sources, because they cover different halves of this layer. NEPTUN
    publish Ukraine's oblasts and raions; Russia's come one at a time from
    the gazetteer as warnings are drawn over them.

    De-duplicated by identity rather than by name, which is the cheap and
    correct thing: neptun.index_shapes files the SAME object under a region's
    key and under each of its names, so a name-based pass would send a
    province's border two or three times.
    """
    want_neighbours()

    out: list[dict[str, Any]] = []
    already: list[Any] = []

    def keep(name: str, shape: Any, where: str, level: str) -> None:
        if not shape or len(out) >= MOST_OUTLINES:
            return
        if any(shape is seen for seen in already):
            return
        already.append(shape)
        # Which country's border this is part of, and which KIND of border.
        #
        # The country, because the page needs it to answer "show me Russia",
        # and this is the only place that knows. Nothing in a report says
        # which country a place is in: the source's own region names the
        # CHANNEL's beat, not the mark's, and Belgorod and Kharkiv sit a
        # hundred kilometres apart inside each other's bounding boxes -- so
        # no box can tell them apart. A real boundary can.
        #
        # The kind, because a national border and a provincial one are not
        # the same fact and must not be the same line. "country" is drawn in
        # red and "region" pale, and which is which is decided here rather
        # than worked out from the shapes: the page tried working it out, by
        # taking the outside edge of the union of a country's provinces, and
        # Ukraine's two nested levels of them came apart in its hands.
        out.append({"name": name, "shape": shape, "in": where,
                    "level": level})

    # The national borders first, so a picture drawn before the provinces
    # have all arrived still has its red lines.
    for code, name, shape in country_outlines():
        keep(name, shape, code, "country")
    # NEPTUN's file IS Ukraine's provinces -- that is what it is a file of --
    # so everything in it is Ukrainian by definition rather than by guess.
    for name, shape in neptun.shapes().items():
        keep(name, shape, "ua", "region")
    # The neighbours' provinces, from the closed lists in neighbours.py.
    # Before the loose ones below, because these are named regions of a named
    # country and the pass below would file the same boundary as "elsewhere"
    # -- and the de-duplication is by identity, so whichever came first won.
    for code, name, shape in neighbour_outlines():
        keep(name, shape, code, "region")
    # And these are learned one at a time as warnings are drawn over them.
    # Whatever is east of the listed part of Russia, and "elsewhere" rather
    # than "ru" because the gazetteer will hand back a Kazakh oblast just as
    # readily and calling that Russia would be a claim this app has no
    # business making.
    #
    # Minus the names the passes above have already placed. The
    # de-duplication is by identity, and a province that answered to BOTH
    # its spellings is two objects holding two slightly different
    # simplifications of one border -- so it came through here as well, drawn
    # a second time and filed as a country nobody had named.
    spoken_for = neighbour_names()
    for name, shape in gazetteer.outlines():
        if _same_name(name) in spoken_for:
            continue
        keep(name, shape, "elsewhere", "region")
    return out


def _same_name(name: str) -> str:
    """A name in the one spelling two of them can be compared in.

    The gazetteer's own normalisation, because that is what its keys are in
    and what outlines() hands back.
    """
    return " ".join(str(name or "").lower().split())


def neighbour_names() -> set[str]:
    """Every spelling this module asks for, comparably.

    Both lists. A country's own boundary is asked for by name like anything
    else, so "Poland" is in the gazetteer's cache alongside its
    voivodeships -- and letting it through the loose pass would draw the
    whole country a second time as an ordinary pale province.
    """
    return {_same_name(one)
            for _code, _name, spellings in neighbours.EVERYTHING
            for one in spellings}


def _found(listed: tuple[tuple[str, str, tuple[str, ...]], ...],
           ) -> list[tuple[str, str, Any]]:
    """The boundaries held for these, as (country, name, shape).

    One entry per entry at most, whichever spelling found it -- see the note
    in neighbours.py about the two. Asking under both names and keeping both
    answers would draw the same ground twice, and the second copy is a
    slightly different simplification of the same border, so the pair reads
    as a shimmer along every line.
    """
    got: list[tuple[str, str, Any]] = []
    for code, name, asked in listed:
        for spelling in asked:
            shape = gazetteer.outline(spelling, code)
            if shape:
                got.append((code, name, shape))
                break
    return got


def country_outlines() -> list[tuple[str, str, Any]]:
    """The national borders held, as (country, name, shape)."""
    return _found(neighbours.COUNTRIES)


def neighbour_outlines() -> list[tuple[str, str, Any]]:
    """The neighbours' provinces held, as (country, name, shape)."""
    return _found(neighbours.REGIONS)


def want_neighbours() -> int:
    """Queue the next missing neighbour boundary. Returns how many were asked.

    One spelling per region per call, and only where no spelling has answered
    yet, so this costs nothing once the set is complete: improve_later refuses
    a name already queued or already known, so the walk simply finds nothing
    to do and stops asking.
    """
    asked = 0
    for code, _name, spellings in neighbours.EVERYTHING:
        if any(gazetteer.outline(one, code) for one in spellings):
            continue
        # Something already in the air for this region. Without this the walk
        # queued the fallback spelling on the next poll, thirty seconds after
        # the first one and long before it had failed -- so every region was
        # asked for twice over and the second answer was thrown away.
        if any(gazetteer.queued(one, code) for one in spellings):
            continue
        for spelling in spellings:
            # Escalation, and the only thing that makes the second spelling
            # worth listing: a name Nominatim has no polygon for is asked
            # four times and then abandoned, and THAT is when the other name
            # is worth trying rather than before.
            if gazetteer.given_up(spelling, code):
                continue
            if gazetteer.improve_later(spelling, code):
                asked += 1
                break
    return asked


def take_neptun() -> tuple[int, int]:
    """Read NEPTUN once, and record what it has. Returns (tracks, alerts).

    Everything it gives arrives already placed, so none of it goes near the
    gazetteer or the text reader -- which is the point of having it.
    """
    # Fetched BEFORE anything is cleared. Clearing first and then failing
    # would empty the map on a blip, which is the worst possible way for a
    # second source to behave: the first source is fine and the screen goes
    # blank anyway.
    #
    # And fetched INDEPENDENTLY of each other. These are two endpoints and
    # either can be refused on its own; letting one refusal abort the whole
    # read meant the tracks and the warnings could only ever arrive together,
    # so whichever was asked for second lost every time. That is how a map
    # came to show every warning NEPTUN had declared and none of the tracks
    # they were declared about.
    tracks = declared = None
    refused = []
    try:
        tracks = neptun.threats()
    except neptun.TooSoon as exc:
        refused.append(exc)
    try:
        declared = neptun.alerts()
    except neptun.TooSoon as exc:
        refused.append(exc)
    if tracks is None and declared is None:
        # Neither was due. Nothing is cleared and nothing is drawn: the
        # caller's TooSoon branch keeps what is already on the map.
        raise refused[0]
    now = time.time()

    with _lock:
        # Their track ids are stable across updates, so a track that has moved
        # replaces its own mark instead of adding a second one. Everything
        # NEPTUN gave last time and has not given this time is gone: their
        # snapshot is the whole state, not a delta, so a track absent from it
        # has ended.
        #
        # Only the half that was actually re-read, though. Clearing the
        # warnings because the tracks came back would take every warning off
        # the map on a poll where their alert endpoint was merely not due.
        if tracks is not None:
            _events[:] = [e for e in _events
                          if e.get("by") != "neptun" or e["kind"] == "alert"]
            _alerts[:] = [a for a in _alerts
                          if a.get("by") != "neptun" or a["kind"] == "alert"]
        if declared is not None:
            _events[:] = [e for e in _events
                          if e.get("by") != "neptun" or e["kind"] != "alert"]
            _alerts[:] = [a for a in _alerts
                          if a.get("by") != "neptun" or a["kind"] != "alert"]

    drawn = 0
    alive: set[str] = set()
    for track in tracks or ():
        ident = f"NP-{track['id']}"
        alive.add(ident)
        if _is_dismissed({"id": ident, "source": track["id"]}):
            continue
        drawn += 1
        _record_neptun(track, now)
    raised = 0
    for alert in declared or ():
        ident = f"NP-alert-{alert.get('key') or alert['name']}"
        if _is_dismissed({"id": ident, "source": ident}):
            continue
        raised += 1
        _record_neptun_alert(alert, ident, now)
    return drawn, raised


def _record_neptun(track: dict[str, Any], now: float) -> None:
    """One NEPTUN track, as a mark."""
    ident = f"NP-{track['id']}"
    area_only = track["area_only"]
    # Their outline for the region, where this is a region rather than a
    # place. This is what finally gives a region mark its real shape without
    # a geocoding request: their alert keys index the same boundary file.
    shape = neptun.shape_for(track["region"]) if area_only else None
    here = places.lookup(track["region"]) if area_only else None

    _events.append({
        "id": ident,
        "source": track["id"],
        "by": "neptun",
        "kind": track["kind"],
        "cause": None,
        "place": track["place"],
        "place_match": track["place"],
        "place_category": "boundary" if area_only else "place",
        "region": track["region"],
        # An areaOnly track knows its province and nothing finer, so the
        # province IS its oblast for the purpose of raising a warning over it.
        "oblast": track["region"] if area_only else None,
        "lat": track["lat"], "lon": track["lon"],
        "origin_lat": track["lat"], "origin_lon": track["lon"],
        "heading": track["heading"],
        "course": track["heading"],
        # "stated", because that is what this map's vocabulary calls a course
        # that came from the source rather than from an inference -- and it is
        # what draws a SOLID arrow rather than a hollow one. Inventing a
        # fifth value here would have fallen through to the hollow drawing,
        # marking somebody else's stated course as this app's guess.
        "course_from": None if track["heading"] is None else "stated",
        # No destination, ever. They give a course and a speed; turning that
        # into "arrives in eleven minutes" would be this app's arithmetic
        # presented as their data.
        "toward": None, "dest_lat": None, "dest_lon": None, "dest_km": None,
        "count": track["count"],
        "motion": MOTION.get(track["kind"], "track"),
        "placed": True,
        "shape": shape,
        "bbox": list(here["bbox"]) if here else None,
        # Their own uncertainty, where they give one, which is a better number
        # than any default: it is how far out they think the position is.
        # Every event carries an area -- it is what sizes the mark -- so a
        # None here left a mark with no size at all.
        "area_km": (area_km(here) if here
                    else max(2.0, track["uncertainty_km"] or 6.0)),
        # "There is no dot." An areaOnly track is drawn as the region it
        # names, never as a point in it.
        "region_scope": "located" if area_only else None,
        "region_wide": False,
        "area_only": area_only,
        # Surveillance rather than a signal to hide. Drawn quietly.
        "advisory": track["advisory"],
        "confidence": track["confidence"],
        "uncertainty_km": track["uncertainty_km"],
        "summary": track["summary"] or track["title"] or track["kind"],
        # Their speed, credited to them. See neptun.read_threat.
        "speed_kmh": track["speed_kmh"],
        # The moment they last confirmed this position. With the course and
        # the speed beside it, this is everything their own predict() needs,
        # and it is what lets the mark move between snapshots instead of
        # sitting still for thirty seconds and then jumping. See project().
        "confirmed_at": track["confirmed_at"],
        "seen": now,
        "channel": NEPTUN_SOURCE,
        "text": track["summary"] or "",
        "photos": [], "link": neptun.BASE,
        "age_minutes": 0.0,
    })
    _alerts.append({
        "id": ident, "source": track["id"], "by": "neptun",
        "kind": track["kind"], "cause": None,
        "rank": KINDS[track["kind"]]["rank"],
        "summary": track["summary"] or track["title"] or track["kind"],
        "place": track["place"], "placed": True, "why_unplaced": None,
        "channel": NEPTUN_SOURCE, "region": track["region"], "seen": now,
        "text": track["summary"] or "", "photos": [], "link": neptun.BASE,
        "advisory": track["advisory"],
    })


def _record_neptun_alert(alert: dict[str, Any], ident: str, now: float) -> None:
    """One official alert from NEPTUN, as a warning over its region."""
    name = alert["name"]
    shape = neptun.shape_for(alert.get("key")) or neptun.shape_for(name)
    here = places.lookup(name) or places.lookup(alert.get("oblast") or "")
    if not here and not shape:
        # Nowhere to draw it. Listed rather than dropped, because "an alert
        # was declared for a region this cannot place" is a visible outcome.
        _alerts.append({
            "id": ident, "source": ident, "by": "neptun", "kind": "alert",
            "cause": None, "rank": KINDS["alert"]["rank"],
            "summary": f"Air alert — {name}", "place": name, "placed": False,
            "why_unplaced": f'"{name}" is not a region this map knows',
            "channel": NEPTUN_SOURCE, "region": alert.get("oblast"),
            "seen": now, "text": "", "photos": [], "link": neptun.BASE,
        })
        return

    lat = here["lat"] if here else _middle(shape)[0]
    lon = here["lon"] if here else _middle(shape)[1]
    _events.append({
        "id": ident, "source": ident, "by": "neptun", "kind": "alert",
        "cause": None,
        "place": name, "place_match": name, "place_category": "boundary",
        "region": alert.get("oblast"), "oblast": None,
        "lat": lat, "lon": lon, "origin_lat": lat, "origin_lon": lon,
        "heading": None, "course": None, "course_from": None,
        "toward": None, "dest_lat": None, "dest_lon": None, "dest_km": None,
        "count": 1, "motion": "still", "placed": True,
        "shape": shape,
        "bbox": list(here["bbox"]) if here else None,
        "area_km": area_km(here) if here else None,
        "region_scope": "covers" if shape else None,
        "region_wide": bool(shape),
        "official": True,
        "summary": f"Air alert — {name}",
        "seen": now, "channel": NEPTUN_SOURCE, "text": "",
        "photos": [], "link": neptun.BASE, "age_minutes": 0.0,
    })
    _alerts.append({
        "id": ident, "source": ident, "by": "neptun", "kind": "alert",
        "cause": None, "rank": KINDS["alert"]["rank"],
        "summary": f"Air alert — {name}", "place": name, "placed": True,
        "why_unplaced": None, "channel": NEPTUN_SOURCE,
        "region": alert.get("oblast"), "seen": now, "text": "",
        "photos": [], "link": neptun.BASE, "official": True,
    })


def _middle(shape: Any) -> tuple[float, float]:
    """The middle of a polygon's extent. Only used when nothing else knows."""
    lats: list[float] = []
    lons: list[float] = []

    def walk(part: Any) -> None:
        if (isinstance(part, (list, tuple)) and len(part) == 2
                and all(isinstance(n, (int, float)) for n in part)):
            lons.append(float(part[0]))
            lats.append(float(part[1]))
        elif isinstance(part, (list, tuple)):
            for inner in part:
                walk(inner)

    walk(shape.get("coordinates") if isinstance(shape, dict) else None)
    if not lats:
        return (48.4, 31.2)
    return ((min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2)


def _poll_state(trouble: list[str], feed_state: str) -> str:
    """What the panel says the poll did. One place, so the two exits agree."""
    said = f"reading {len(CHANNELS)} channels and {NEPTUN_SOURCE}"
    parts = [*trouble[:1], *([feed_state] if feed_state else []), said]
    return "; ".join(parts)


def poll() -> dict[str, Any]:
    """Read the channels once, and turn anything new into events.

    Called on a background thread by start_poll(), and directly by the tests,
    which want it synchronous.
    """
    global _state, _last_poll
    fresh: list[dict[str, Any]] = []
    trouble: list[str] = []
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=LOOKBACK_MINUTES)
    seen_now: dict[str, dict[str, Any]] = {}
    # All four channels at once, not one after another.
    #
    # They were sequential, which meant a poll took the sum of four page
    # fetches -- measured at 1.4 s against a 350 ms floor, so three quarters of
    # it was waiting for one server while three others sat idle. They are four
    # unrelated GETs to the same host with no ordering between them, which is
    # the textbook case for doing them together.
    #
    # Threads rather than async because everything around this is synchronous
    # and requests is blocking: four threads that spend their whole lives in a
    # socket read cost nothing, and making the whole module async to save them
    # would be a rewrite for no gain.
    with futures.ThreadPoolExecutor(max_workers=len(CHANNELS)) as pool:
        got = {channel["name"]: pool.submit(_fetch_channel, channel["name"])
               for channel in CHANNELS}

    for channel in CHANNELS:
        tally = seen_now[channel["name"]] = {
            "region": channel["region"], "posts": 0, "fresh": 0,
            "read": 0, "placed": 0, "problem": None,
        }
        try:
            posts = got[channel["name"]].result()
        except TrackerError as exc:
            trouble.append(str(exc))
            tally["problem"] = str(exc)[:120]
            continue
        except Exception as exc:  # noqa: BLE001 - a thread must not take the poll down
            # A fetch raising something unexpected used to be impossible
            # because it ran inline and the caller's handler caught it. In a
            # pool it would surface here as whatever it is, and one bad
            # channel must not lose the other three.
            trouble.append(f"{channel['name']}: {exc}")
            tally["problem"] = str(exc)[:120]
            continue
        tally["posts"] = len(posts)
        for post in posts:
            post["countries"] = channel["countries"]
            post["region"] = channel["region"]
            if post["id"] in _seen:
                continue
            try:
                when = dt.datetime.fromisoformat(post["when"].replace("Z", "+00:00"))
            except (AttributeError, TypeError, ValueError):
                # No usable timestamp, so it cannot be known to be current,
                # so it is not drawn: a mark that might be from last week is
                # worse than no mark. Widened from ValueError alone because a
                # post whose "when" is missing entirely gives AttributeError
                # on .replace(), which took the whole poll down rather than
                # one post.
                continue
            if when < cutoff:
                # Older than the longest any kind is held for, so there is
                # nothing it could become that would still be on the map.
                # Remembered so it is not weighed again next poll.
                mark_seen(post["id"])
                continue
            tally["fresh"] += 1
            fresh.append(post)

    now = time.time()
    # And the second source, which fails independently of the first.
    #
    # The channels are prose that has to be read and geocoded, and every step
    # of that can be wrong. NEPTUN has already done that work and hands over
    # coordinates, so a night where the reader misses a name is a night where
    # this still has the track -- and vice versa, since they carry different
    # things. That independence is the whole reason to have two.
    feed_state = ""
    caught = 0
    try:
        # Every poll. Their message feed is every channel they aggregate, and
        # it is the widest source this app has -- see catch_up(). The first
        # pass fills the gap before the process started; the ones after it
        # are how the other sources keep arriving at all.
        caught = catch_up()
    except neptun.NeptunError:
        # Not worth a word in the panel: the snapshot below is the reading
        # that the panel's NEPTUN row is about, and this is prose on top of
        # it. Failing silently is right here where it is not there.
        pass
    try:
        drawn, declared = take_neptun()
        seen_now[NEPTUN_SOURCE] = {
            "region": "Ukraine", "posts": drawn + declared, "fresh": drawn + declared,
            "read": drawn + declared + caught,
            "placed": drawn + declared + caught, "problem": None,
        }
    except neptun.TooSoon:
        # Asked again inside their interval. Their snapshot is CDN-cached and
        # they ask for one poll every five seconds at most, so the answer is
        # to keep what is already drawn rather than to ask again -- and NOT to
        # clear it, which take_neptun() is careful about.
        kept = sum(1 for e in _events if e.get("by") == "neptun")
        seen_now[NEPTUN_SOURCE] = {
            "region": "Ukraine", "posts": kept, "fresh": 0, "read": kept,
            "placed": kept, "problem": None,
        }
    except neptun.NeptunError as exc:
        feed_state = str(exc)
        seen_now[NEPTUN_SOURCE] = {
            "region": "Ukraine", "posts": 0, "fresh": 0, "read": 0,
            "placed": 0, "problem": str(exc)[:120],
        }
    _remember_sources(seen_now)

    # The neighbours' boundaries, asked for here as well as when a picture is
    # exported. Asking only at export would mean the first picture of the
    # night had a black half: forty-odd boundaries at one request every three
    # seconds is a couple of minutes, and nobody waits two minutes with the
    # export dialog open. Asked from the poll they are simply there.
    want_neighbours()

    if not fresh:
        # Nothing new from the channels. Not "nothing new" any more: the feed
        # was still read, and on a quiet night that is the whole of what is on
        # the map.
        _state = _poll_state(trouble, feed_state)
        _last_poll = now
        return current()

    # Newest first, so that where anything has to be cut it is the oldest
    # that goes. They arrive grouped by channel, which is an order that means
    # nothing: cutting that list cut whole channels.
    fresh.sort(key=_when, reverse=True)

    # Read by rule. Every post, every place in it, no model.
    #
    # The model is gone from this path, and not because it was slow -- though
    # it was, by two orders of magnitude against four page fetches. It is that
    # it was worse at the job. It was asked to pull a kind, a place and a
    # course out of a post; the patterns do that from a grammar these channels
    # genuinely follow, and every disagreement that got measured came down on
    # the patterns' side. The model transliterated names the gazetteer holds
    # in Cyrillic and turned "Кагарлик" into "Kagul", which is in Moldova.
    #
    # What reads these posts is structure, not comprehension. The busiest post
    # of the night is the movement digest: fifteen settlements across five
    # oblasts, each section stating its own course. reports.read_all() returns
    # one reading per place, so that post is seventeen arrows with a westerly
    # bearing instead of one courseless ring in the middle of a province.
    #
    # So a poll is regular expressions and dict lookups end to end: no daemon
    # to be down, no model to be missing, no "reading 3 of 20 reports without
    # it", and nothing on the map that came from a guess.
    for post in fresh:
        mark_seen(post["id"])
        for plain in reports.read_all(post.get("text", "")):
            plain["kind"] = fold_kind(plain.get("kind"))
            item = _clean({**plain, "id": post["id"]})
            if not item:
                continue
            item["by"] = "rules"
            _record(item, post, post.get("countries", ""))

    with _lock:
        _expire(now)
        # What each channel actually contributed, counted from what is on the
        # map rather than tallied as it went.
        #
        # Counting inline got this wrong as soon as one post could produce
        # more than one mark: a digest naming fifteen towns is one post and
        # fifteen events, and no total kept in the loop stayed in step with
        # both. Reading the answer off the result cannot drift from it.
        on_map = {e.get("source") for e in _events}
        in_stream = on_map | {a.get("source") for a in _alerts}
    for post in fresh:
        tally = _sources.get(post.get("channel"))
        if tally is None:
            continue
        if post["id"] in in_stream:
            tally["read"] += 1
        if post["id"] in on_map:
            tally["placed"] += 1

    _state = _poll_state(trouble, feed_state)
    _last_poll = now
    return current()


def refresh() -> dict[str, Any]:
    """What is in the air. Answers now; reads the channels in the background.

    This used to poll inline, so the request that happened to be first in a
    given minute waited for the whole thing -- four page fetches and a model
    call -- before the map got anything. Opening the panel could sit there for
    seconds and it looked like the layer was broken rather than busy.
    """
    start_poll()
    return current()


def start_poll() -> bool:
    """Begin a read of the channels if one is due and none is running.

    Returns whether it started one. The lock is held only long enough to
    decide and to claim the job, so two requests arriving together cannot both
    start a poll and the second does not wait for the first.

    _last_poll is set when the poll STARTS rather than when it finishes, which
    is the thing that keeps the floor honest: setting it at the end meant a
    slow read let the next request straight through, which is how a rate limit
    used to keep itself alive on the hosted model.
    """
    global _polling, _last_poll
    with _lock:
        if _polling or time.time() - _last_poll < MIN_POLL_SECONDS:
            return False
        _polling = True
        _last_poll = time.time()

    def work() -> None:
        global _polling
        try:
            poll()
        except Exception:  # noqa: BLE001 - a background thread must not die silently
            log.exception("background poll failed")
        finally:
            with _lock:
                _polling = False

    # Daemon, so a poll in flight never holds the process open at shutdown.
    threading.Thread(target=work, name="tracker-poll", daemon=True).start()
    return True


def current() -> dict[str, Any]:
    """Every live event and recent alert, carried forward to now."""
    now = time.time()
    with _lock:
        _expire(now)
        events = [project(e, now) for e in _events]
        alerts = sorted(_alerts, key=lambda a: a["seen"], reverse=True)
        events = hide_dismissed(events, alerts)
        # Over the alert window, not since the process started. A running
        # total answers a question nobody asked -- what matters is whether
        # the names coming in tonight are being found.
        placed = sum(1 for a in alerts if a["placed"])
        unplaced = len(alerts) - placed
        # Inside the lock, because it reads the event list. Cached, so this is
        # a tuple comparison on all but the first call after a change.
        masses = masses_now(events)
        borrow_course(events, masses)
    return {
        "events": events,
        # Sent whether or not concentrate mode is on, so switching it is
        # instant rather than a round trip. It is a few dozen entries.
        "masses": masses,
        "mass_within_km": MASS_WITHIN_KM,
        "mass_least": MASS_LEAST,
        "count": len(events),
        "alerts": [dict(a) for a in alerts],
        "state": _state,
        "keep_minutes": KEEP_MINUTES,
        "keep": KEEP,
        "alert_minutes": ALERT_MINUTES,
        # The furthest a mark is carried past its last confirmed
        # position. Sent rather than written into the page as well, so
        # the cap is one number: the map keeps drifting between polls
        # and has to stop where this stops.
        "drift_cap_minutes": MOST_DRIFT_MINUTES,
        "kinds": KINDS,
        "channels": [c["name"] for c in CHANNELS],
        # Channel readings NEPTUN stood in for -- see _record. Said out
        # loud so a thin channel row is explained rather than puzzling.
        "shadowed": _shadowed,
        "regions": sorted({c["region"] for c in CHANNELS}),
        # Whether a read of the channels is happening right now. The page uses
        # it to ask again in a couple of seconds instead of waiting out its
        # whole minute, which is what makes a first open feel immediate: the
        # empty answer arrives at once and fills in as the poll lands.
        # NEPTUN's credit, carried WITH the data rather than hard-coded in
        # the page. Their one condition of use is a visible link beside the
        # map, and putting it in the payload means the marks and the credit
        # cannot get separated -- a page that has the one has the other.
        "attribution": neptun.ATTRIBUTION,
        # How many marks a person has taken off by hand. Said out loud,
        # because "the map is missing things" and "I hid those" look identical
        # from across a room and only one of them is a bug.
        "dismissed": len(_dismissed),
        "polling": _polling,
        "last_poll": _last_poll or None,
        # Said out loud, because an empty map with a healthy feed behind it is
        # the failure the previous version hid.
        "reports": {"placed": placed, "unplaced": unplaced},
        "gazetteer": gazetteer.stats(),
        # Per channel, so an empty map can be traced to which of the four
        # possible reasons it is.
        "sources": [{"channel": name, **tally} for name, tally in _sources.items()],
    }


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

# Name, destination, count, kind -- and the coordinates the gazetteer would
# return for them, so the demo runs the real placing code rather than a
# parallel copy of it that could drift out of step.
DEMO_PLACES = {
    "Nikopol": (47.5665, 34.4053), "Kherson": (46.6354, 32.6169),
    "Beryslav": (46.8397, 33.4269), "Odesa": (46.4825, 30.7233),
    "Mykolaiv": (46.9750, 31.9946), "Kharkiv oblast": (49.7, 36.3),
    "Kharkiv": (49.9935, 36.2304), "Ochakiv": (46.6128, 31.5406),
    "Kaharlyk": (49.8556, 30.8125), "Zaporizhzhia": (47.8388, 35.1396),
    "Kyiv oblast": (50.05, 30.75),
    # The Russian side, so the demo shows it being treated exactly the same.
    "Белгородская область": (50.72, 37.5),
    # A cluster of real towns within an oblast, close enough together to make
    # one mass. Concentrate mode is invisible without one, and the last time a
    # feature's explanation was the only part the offline build could not show,
    # it was the part nobody could check.
    "Brovary": (50.5111, 30.7900), "Boryspil": (50.3527, 30.9550),
    "Vyshhorod": (50.5840, 30.4890), "Obukhiv": (50.1069, 30.6314),
    "Fastiv": (50.0781, 29.9169),
    # And a corridor: five towns where every hop is under the sixty-kilometre
    # threshold but the two ends are a hundred and eighty kilometres apart.
    # Chosen by computing the distances rather than by eye -- the first
    # attempt at this picked three towns that looked adjacent on a map and
    # were ninety kilometres apart, so no chain formed and the demo showed
    # nothing. This is the case single-link clustering gets right and
    # nearest-centre does not.
    "Sumy": (50.9077, 34.7981), "Lebedyn": (50.5833, 34.4833),
    "Nedryhailiv": (50.8300, 33.8770), "Romny": (50.7450, 33.4747),
    "Konotop": (51.2378, 33.2020),
}

# A real movement digest, in the shape these channels post them: a heading
# per oblast, a list of settlements, and the course stated once for the
# section in the instrumental. Read by reports.read_all() like any other post.
DEMO_DIGEST = (
    "\u26a0\ufe0f \u0429\u043e\u0434\u043e \u0440\u0443\u0445\u0443 \u0443\u0434\u0430\u0440\u043d\u0438\u0445 \u0411\u043f\u041b\u0410: "
    "\U0001f6f8 \u0421\u0443\u043c\u0449\u0438\u043d\u0430: \U0001f6e9 \u0411\u043f\u041b\u0410 \u0432 \u0440-\u043d\u0456 \u043d.\u043f. "
    "\u041f\u0443\u0442\u0438\u0432\u043b\u044c, \u0413\u043b\u0443\u0445\u0456\u0432, \u041a\u0440\u043e\u043b\u0435\u0432\u0435\u0446\u044c, \u0411\u0443\u0440\u0438\u043d\u044c \u0442\u0430 \u041b\u0435\u0431\u0435\u0434\u0438\u043d "
    "\u0440\u0443\u0445\u0430\u044e\u0442\u044c\u0441\u044f \u0437\u0430\u0445\u0456\u0434\u043d\u0438\u043c \u043a\u0443\u0440\u0441\u043e\u043c; "
    "\U0001f6f8 \u0427\u0435\u0440\u043d\u0456\u0433\u0456\u0432\u0449\u0438\u043d\u0430: \U0001f6e9 \u0411\u043f\u041b\u0410 \u0432 \u0440-\u043d\u0456 \u043d.\u043f. "
    "\u0411\u0430\u0442\u0443\u0440\u0438\u043d, \u0421\u043e\u0441\u043d\u0438\u0446\u044f, \u041d\u0456\u0436\u0438\u043d, \u041a\u043e\u0437\u0435\u043b\u0435\u0446\u044c \u0442\u0430 "
    "\u0413\u043e\u043d\u0447\u0430\u0440\u0456\u0432\u0441\u044c\u043a\u0435 \u0440\u0443\u0445\u0430\u044e\u0442\u044c\u0441\u044f \u0437\u0430\u0445\u0456\u0434\u043d\u0438\u043c \u043a\u0443\u0440\u0441\u043e\u043c; "
    "\U0001f6f8 \u0416\u0438\u0442\u043e\u043c\u0438\u0440\u0449\u0438\u043d\u0430: \U0001f6e9 \u0411\u043f\u041b\u0410 \u0432 \u0440-\u043d\u0456 \u043d.\u043f. "
    "\u041c\u0430\u043b\u0438\u043d, \u041a\u043e\u0440\u043e\u0441\u0442\u0435\u043d\u044c \u0442\u0430 \u041d\u043e\u0432\u0430 \u0411\u043e\u0440\u043e\u0432\u0430 "
    "\u0440\u0443\u0445\u0430\u044e\u0442\u044c\u0441\u044f \u0437\u0430\u0445\u0456\u0434\u043d\u0438\u043c \u043a\u0443\u0440\u0441\u043e\u043c."
)

# The Russian radar channel's two states, in its own words and word order.
# Read by the same reader, so the offline build shows the drone/missile colour
# split rather than asserting it exists somewhere.
DEMO_WARNINGS = (
    "Lipetsk Oblast Drone Alert",
    "Voronezh Oblast Missile Alert",
    "Republic of Tatarstan Drone Alert",
)

# NEPTUN-shaped tracks, in their documented shape, read by the same reader
# the live path uses. Two states that are easy to draw wrongly and impossible
# to check without them: an advisory (observed, not an alarm) and an areaOnly
# track (a region centroid, which is not a place).
DEMO_NEPTUN = (
    {"id": "demo_np_1", "type": "uav", "title": "Шахед",
     "region": "Полтавська область", "locality": "Миргород",
     "lat": 49.9667, "lon": 33.6083, "heading": 300, "count": 3,
     "velocity": {"bearingDeg": 300, "speedKmh": 189},
     "status": "active", "confidenceLevel": "high", "uncertaintyKm": 5,
     "explanationShort": "БпЛА курсом на північний захід", "advisory": False,
     "areaOnly": False},
    {"id": "demo_np_2", "type": "mig31k", "title": "МіГ-31К",
     "region": "Брянская область", "lat": 53.24, "lon": 34.36,
     "status": "active", "advisory": True, "areaOnly": False,
     "explanationShort": "Зліт МіГ-31К — спостереження, не сигнал ховатися"},
    {"id": "demo_np_3", "type": "missile", "region": "Одеська область",
     "lat": 46.60, "lon": 30.20, "status": "active", "areaOnly": True,
     "positionQuality": "approx",
     "explanationShort": "Ракета на Одещину — названо лише область"},
    {"id": "demo_np_4", "type": "kab", "title": "КАБ",
     "region": "Харківська область", "locality": "Вовчанськ",
     "lat": 50.2894, "lon": 36.9436, "heading": 200, "status": "active",
     "velocity": {"bearingDeg": 200, "speedKmh": 208},
     "explanationShort": "КАБ у напрямку Вовчанська"},
)

DEMO_WITH_PICTURES = "Nikopol"

# The region the demo pretends it has no boundary for. See below.
DEMO_WITHOUT_AN_OUTLINE = "Вінницька область"
DEMO_WITHOUT_AN_OUTLINE_EN = "Vinnytsia oblast"

DEMO_SEED = [
    # kind, place, toward, course, count, summary
    ("drone", "Nikopol", "Kherson", None, 2,
     "Two drones over Nikopol heading for Kherson"),
    # A course and no destination -- the case that drew a stationary burst
    # over a town it was flying past, which is what prompted all of this.
    ("jet_drone", "Kaharlyk", None, "N", 1,
     "Jet drone past Kaharlyk on a course north"),
    # An aircraft, which is the one airborne kind that is neither a drone nor
    # a missile and is kept separate for that reason. Replaces a recon drone,
    # a distinction the data does not support.
    ("aircraft", "Zaporizhzhia", None, None, 1,
     "Tactical aviation reported over Zaporizhzhia"),
    # Located to an oblast and nowhere finer, which is most of what these
    # channels actually post. The demo needs one so the "somewhere in this
    # region" band is drawn at all in the build with no network.
    ("drone", "Kharkiv oblast", None, None, 3,
     "Three drones over Kharkiv oblast"),
    # Located to the one oblast the demo has no outline for, so the offline
    # build exercises a region-level report with no boundary -- the case that
    # used to be drawn as a point on the province's centroid.
    ("drone", "Vinnytsia oblast", None, None, 2,
     "Two drones over Vinnytsia oblast"),
    # A missile with a named destination, so the demo exercises a heading
    # computed between two places rather than read off a compass word.
    ("missile", "Ochakiv", "Odesa", None, 1,
     "Missile past Ochakiv towards Odesa"),
    # And one with a course and no destination, which is the other way a
    # heading arrives. Two objects, so the trailing line is drawn as well.
    ("missile", "Nikopol", None, "W", 2, "Two missiles past Nikopol, heading west"),
    # One report deliberately old, so the demo shows a faded mark beside a
    # fresh one and the fade has more than one point on it. The three strikes
    # that used to do this job are gone with the kind; a warning is now the
    # longest-lived thing on the map and so the one worth watching age.
    ("alert", "Kharkiv", None, None, 1, "Air raid warning for Kharkiv", 70),
    # A warning over the SAME oblast a drone above is located to, so both land
    # on that region's centroid -- which is the picture this arrived as: a
    # drone arrow sitting on top of a warning triangle, because neither is
    # really at that point and both are drawn there. Without this row the
    # offline build cannot reach the case at all. See declump() in the page.
    ("alert", "Kharkiv oblast", None, None, 1,
     "Air raid warning across Kharkiv oblast", 20),
    ("alert", "Dnipro", None, None, 1, "Air raid warning for Dnipro", 35),
    # A warning covering a whole region rather than a town, so the demo shows
    # the boundary being drawn instead of a circle over the middle of it.
    ("alert", "Kyiv oblast", None, None, 1, "Air raid warning across Kyiv oblast"),
    # A Russian-side report, read and drawn by the same code as every other:
    # same kinds, same region outline, same everything.
    ("alert", "Белгородская область", None, None, 1,
     "UAV danger across Belgorod oblast"),
    # An FPV, close to the line and heading across it, because the whole
    # point of the kind is that one on the map says whatever launched it is
    # a few kilometres away. Without a row here the offline build never
    # draws the mark at all.
    # Three minutes old, not eight: an FPV is kept for eight, so an
    # eight-minute-old one is at its own expiry and never appears.
    ("fpv", "Kupiansk", None, "N", 1,
     "FPV drone over Kupiansk heading north", 3),
    # The case the previous version hid: a real report that cannot be placed.
    # It belongs in the alert stream and nowhere else.
    # ── Concentrate mode ────────────────────────────────────────
    #
    # Every one of these carries its own age rather than taking one from its
    # position, because they were added after the rows above and a positional
    # age would have shifted all of them. It did, the first time: five of the
    # originals aged past their own expiry and vanished from the demo.
    #
    # Five towns around Kyiv: one mass in concentrate mode, five separate
    # glyphs without it. Both readings are true, and the point of the mode is
    # that the first is the one you want first.
    # Two with a stated course and three without, so the group's trajectory
    # is averaged from the two and lent to the three -- which draws two solid
    # arrows and three hollow ones, and is the case the borrowing exists for.
    # Real reports from these channels almost always give a direction, so a
    # demo where none of a group did would not be representative either.
    ("drone", "Brovary", None, "W", 1, "Drone over Brovary heading west", 3),
    ("drone", "Boryspil", None, "W", 1, "Drone over Boryspil heading west", 4),
    ("drone", "Vyshhorod", None, None, 1, "Drone over Vyshhorod", 4),
    ("drone", "Obukhiv", None, None, 1, "Drone over Obukhiv", 5),
    ("drone", "Fastiv", None, None, 1, "Drone over Fastiv", 6),
    # A corridor, Sumy to Konotop: every hop under sixty kilometres, the two
    # ends a hundred and eighty apart. The case single-link clustering gets
    # right and nearest-centre does not.
    # The corridor, all stated, all heading the same way -- so its arrow is
    # averaged from five of five and nothing is borrowed.
    ("drone", "Sumy", None, "SW", 1, "Drone over Sumy heading south-west", 7),
    ("drone", "Lebedyn", None, "SW", 1, "Drone over Lebedyn", 7),
    ("drone", "Nedryhailiv", None, "W", 1, "Drone over Nedryhailiv", 8),
    ("drone", "Romny", None, "W", 1, "Drone over Romny", 8),
    ("drone", "Konotop", None, "W", 1, "Drone over Konotop", 9),
    ("drone", "Somewhere unnamed", None, None, 1,
     "Drone activity reported, no location given"),
]


# How long the demo runs before starting over.
#
# Long enough for everything in flight to expire on age, so a cycle shows
# every ending a track has: arriving, and timing out. Deliberately NOT long
# enough to outlive a strike or a warning -- that would mean a day of demo with
# nothing in the air on it after the first twenty minutes. Those simply carry
# over from one cycle to the next, which is what they do in the real thing too.
DEMO_CYCLE = (KEEP_MINUTES + 4) * 60

_demo_epoch = 0.0


# Roughly how big each demo place is, so the alert areas differ the way real
# ones do: a strike in a town is a few kilometres across and a warning over an
# oblast is most of a region.
# The four things a channel can be doing, cycled across whatever channels are
# configured: reading and placing, reading nothing placeable, posting nothing,
# and not answering at all. One of each so the panel that explains a thin map
# can itself be seen without a network.
# One per thing a channel can be doing, so that with the channels configured
# every state is drawn at least once. A state more than there are channels
# would never appear, and a duplicate would leave one undrawn -- which is what
# happened when this list was first shortened and the "quiet, and nothing
# wrong" row disappeared from the demo.
#
# Five now, because the fifth channel is the Russian radar one and it posts
# warnings and nothing else -- which is its own state worth seeing: read and
# placed, but every mark a warning rather than a thing in the air.
DEMO_SOURCE_STATES = (
    # One per source the app actually has, which is now two: the channel and
    # the feed. It was five, for the five things a channel can be doing, back
    # when there were five channels to be in them -- a state with no source to
    # sit in simply never appeared, so the list said more than the demo could
    # show.
    #
    # What is lost is the offline view of a channel that is unreachable or
    # unreadable. Those states are still built and still tested; they are just
    # no longer visible in the demo, because inventing a second channel to put
    # one in would be the demo telling a lie about which channels exist.
    {"posts": 14, "fresh": 6, "read": 6, "placed": 5, "problem": None},
)

DEMO_EXTENT = {"Kharkiv oblast": 1.6, "Kharkiv": 0.12, "Kyiv oblast": 1.3,
               "Белгородская область": 1.1}


def _demo_photo(label: str, tint: str) -> str:
    """A stand-in picture, as a data URL.

    Drawn rather than fetched, for the obvious reason -- the build with no
    network has no network -- and drawn as an obvious placeholder rather than
    as anything photographic. A demo that showed a convincing picture of a
    strike would be the single worst thing in this app to get wrong.

    It exists because the popup's picture layout is otherwise unreachable
    offline, and a feature whose only demonstration needs a network is a
    feature nobody checks.
    """
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200">'
        f'<rect width="320" height="200" fill="{tint}"/>'
        f'<rect x="8" y="8" width="304" height="184" fill="none" '
        f'stroke="#ffffff" stroke-opacity="0.35" stroke-dasharray="6 6"/>'
        f'<text x="160" y="96" text-anchor="middle" fill="#ffffff" '
        f'font-family="system-ui,sans-serif" font-size="17">{label}</text>'
        f'<text x="160" y="122" text-anchor="middle" fill="#ffffff" '
        f'fill-opacity="0.7" font-family="system-ui,sans-serif" '
        f'font-size="12">demo — not a photograph</text></svg>')
    return "data:image/svg+xml;utf8," + quote(svg, safe="")


def _demo_stamp(when: float) -> str:
    """An epoch moment in the spelling NEPTUN use, for the demo's tracks."""
    return dt.datetime.fromtimestamp(when, dt.timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def _demo_ring(lat: float, lon: float, half: float) -> dict[str, Any]:
    """A rough outline for a demo region: a lumpy ring, not a rectangle.

    Lumpy on purpose. A neat box would look like a bounding box and would not
    show whether the page is drawing a real boundary or falling back to one.
    """
    ring = []
    for i in range(24):
        angle = 2 * math.pi * i / 24
        wobble = half * (0.72 + 0.28 * math.cos(3 * angle))
        ring.append([round(lon + wobble * math.cos(angle) * 1.5, 4),
                     round(lat + wobble * math.sin(angle), 4)])
    ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}


def _demo_place(name: str, lat: float, lon: float, half: float) -> dict[str, Any]:
    """One offline gazetteer answer, in the shape find() returns."""
    region = half > 1
    return {
        "lat": lat, "lon": lon, "name": name,
        "kind": "administrative" if region else "town",
        "category": "boundary" if region else "place",
        "bbox": [lat - half, lat + half, lon - half, lon + half],
        # Through the same door the live boundaries come through, so the demo
        # exercises that path rather than a copy of it.
        "shape": _demo_outline(name, lat, lon, half) if region else None,
    }


def _demo_outline(name: str, lat: float, lon: float, half: float) -> Any:
    """A region's boundary in the demo, or None for the one that has none.

    One region has none deliberately. Everything else here hands back an
    outline for every province, which meant the offline build always took the
    happy path and could not reach a report located to a region whose
    boundary has not arrived -- the case that used to be drawn as a mark on
    the province's arithmetic centre. Three drawing bugs have now reached a
    screenshot because the demo could not reach the case they were in.
    """
    if name == DEMO_WITHOUT_AN_OUTLINE or name == DEMO_WITHOUT_AN_OUTLINE_EN:
        return None
    return neptun.shape_for(name) or _demo_ring(lat, lon, half)


def _demo_lookup(name: str, countries: str = "") -> dict[str, Any] | None:
    """A small gazetteer, for the build with no network."""
    found = DEMO_PLACES.get(name)
    if not found:
        # The digest names a dozen towns that the hand-written demo table has
        # no reason to carry, and they are all in the built-in table already.
        # Falling through to it keeps the demo honest -- the same lookup the
        # live path makes first -- without duplicating fifty coordinates.
        known = places.lookup(name)
        if not known:
            return None
        found = (known["lat"], known["lon"])
        # And its own extent, so an oblast reached this way is treated as an
        # oblast. Falling back to a town's radius made every region the demo
        # did not list by hand into a point -- which is not what the live
        # path does with the same name, so the demo disagreed with it.
        lat, lon = found
        half = (known["bbox"][1] - known["bbox"][0]) / 2
        return _demo_place(name, lat, lon, half)
    lat, lon = found
    half = DEMO_EXTENT.get(name, 0.06)
    region = half > 1
    return {
        "lat": lat, "lon": lon, "name": name,
        "kind": "administrative" if region else "town",
        "category": "boundary" if region else "place",
        "bbox": [lat - half, lat + half, lon - half, lon + half],
        "shape": _demo_outline(name, lat, lon, half) if region else None,
    }


def _demo_masses(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The demo's masses, with the group course lent out as it is live."""
    found = massed(events)
    borrow_course(events, found)
    # Recomputed, because lending a course to a mark changes what the group's
    # own average is worked out from -- and the number the popup quotes has to
    # be the one it was actually averaged from, not the one after.
    return found


def demo() -> dict[str, Any]:
    """Synthetic reports, for the build with no network.

    Anchored to a fixed instant rather than rebuilt against the clock on every
    call: the first version did that, and it quietly made the demo a lie -- the
    events were always the same few minutes old, so nothing ever aged, arrived
    or expired, and none of the endings could be seen.

    Everything here goes through the same place_event, project and expire code
    the live path uses. A demo that reimplements what it is demonstrating
    proves nothing about it.
    """
    global _demo_epoch
    now = time.time()
    with _lock:
        if not _demo_epoch or now - _demo_epoch > DEMO_CYCLE:
            _demo_epoch = now
        epoch = _demo_epoch

    # One real movement digest, read by the real reader.
    #
    # The rest of the demo is a table of pre-read rows, which is fine for what
    # it shows and useless for this: a seeded row proves nothing about the
    # code that turns a post into rows. This one goes through read_all() the
    # way a live post does, so the offline build actually exercises the path
    # where one post becomes fourteen marks.
    #
    # Worth insisting on. Three separate drawing bugs reached a screenshot
    # because the offline build could not reach the case they were in.
    seed = list(DEMO_SEED)
    # The Russian side's warnings, read the same way -- so the offline build
    # shows what the two warning colours look like beside each other, which is
    # the whole point of having two.
    for text in DEMO_WARNINGS:
        for got in reports.read_all(text):
            seed.append((got["kind"], got["place"], None, None, 1,
                         got["summary"], 12, got.get("cause"),
                         got.get("region")))
    for got in reports.read_all(DEMO_DIGEST):
        seed.append((got["kind"], got["place"], None, got["course"], 1,
                     got["summary"], 6, None, got.get("region")))

    # Boundaries, so the offline build draws warnings as regions rather than
    # as rectangles. Rings rather than real outlines -- an oblast border is
    # thousands of points and this file is not the place for twenty-five of
    # them -- but they go in through neptun.remember_shapes() and out through
    # neptun.shape_for(), which is the same path the live boundaries take.
    # The alternative was a demo in which the whole feature was invisible.
    # One region deliberately left without one, because the demo could
    # otherwise not reach the case that produced the dots in the middle of
    # provinces: a report located to an oblast whose boundary has not been
    # fetched yet. With an outline for every region the offline build always
    # took the happy path, and "a mark on a province centroid" was a thing
    # only the live map could show.
    neptun.remember_shapes({
        name.casefold(): _demo_ring(lat, lon, places.WIDE.get(name, 1.0))
        for name, (lat, lon) in places.REGIONS.items()
        if name != DEMO_WITHOUT_AN_OUTLINE
    })

    events, alerts = [], []
    for i, row in enumerate(seed, start=1):
        kind, place, toward, course, count, summary = row[:6]
        cause = row[7] if len(row) > 7 else None
        # The oblast the reader found, as _record keeps it. Without this the
        # demo could not produce a derived warning at all -- which is how the
        # offline build came to show none of them while the live path did.
        oblast = row[8] if len(row) > 8 else None
        # Staggered by position, unless the row says how old it should be.
        seen = epoch - (row[6] * 60 if len(row) > 6 else i * 90)
        item = {"kind": kind, "place": place, "toward": toward,
                "course": read_course(course), "region": None,
                "count": count, "summary": summary, "cause": cause}
        placed = place_event(item, "ua", lookup=_demo_lookup)
        ident = f"AO{100 + i * 7:04d}"
        alerts.append({
            "id": ident, "kind": kind, "rank": KINDS[kind]["rank"],
            "cause": cause, "summary": summary, "place": place, "placed": placed["placed"],
            "why_unplaced": placed.get("why_unplaced"),
            "channel": "demo", "region": "Ukraine", "seen": seen,
            "text": f"Demo report — {summary}.",
        })
        if not placed["placed"]:
            continue
        # Pictures on one report, because a real feed carries them and a
        # feature whose only demonstration needs a network is a feature
        # nobody checks. It used to be the strikes, which is where a channel
        # actually puts them; with those gone it is this one row, named
        # explicitly so it cannot quietly stop being any row at all.
        shots = ([_demo_photo(f"{place} · 1", "#5a3550"),
                  _demo_photo(f"{place} · 2", "#3a4a62")]
                 if place == DEMO_WITH_PICTURES else [])
        event = {**placed, "id": ident, "oblast": oblast,
                 "origin_lat": placed["lat"], "origin_lon": placed["lon"],
                 "seen": seen, "channel": "demo", "region": "Ukraine",
                 "source": f"demo/{i}", "text": f"Demo report — {summary}.",
                 "photos": shots, "link": None}
        if not _alive(event, now):
            continue
        events.append(project(event, now))

    # A NEPTUN-shaped track and an areaOnly one, read by the same reader the
    # live path uses. The offline build could otherwise show neither, and
    # both are the states most easily got wrong: an advisory drawn as an
    # alarm, and a region centroid drawn as a place.
    for raw in DEMO_NEPTUN:
        # The confirmation stamp is minted here rather than written into the
        # table above. A fixed one would be hours stale by the second time
        # anybody opened the demo, so every track would sit pinned at the
        # drift cap and the one thing this is meant to show -- a mark moving
        # -- would be the one thing it never did.
        track = neptun.read_threat(
            {**raw, "confirmedAt": _demo_stamp(now - 90)})
        if not track:
            continue
        _record_neptun(track, epoch - 240)
    with _lock:
        # Aged like everything else in here. Without this they sat on the map
        # through the demo's whole cycle while the seeded marks came and went,
        # so the one thing the demo exists to show -- things arriving and
        # expiring -- was false for a quarter of what it drew.
        made_np = [project(e, now) for e in _events
                   if e.get("by") == "neptun" and _alive(e, now)]
        events += made_np
        drawn_np = {e["id"] for e in made_np}
        alerts += [a for a in _alerts
                   if a.get("by") == "neptun" and a["id"] in drawn_np]
        _events[:] = [e for e in _events if e.get("by") != "neptun"]
        _alerts[:] = [a for a in _alerts if a.get("by") != "neptun"]

    # And the same dismissals. Without this the button was inert offline.
    with _lock:
        events = hide_dismissed(events, alerts)

    alerts = [a for a in alerts
              if now - a["seen"] <= max(ALERT_MINUTES, keep_minutes(a["kind"])) * 60]
    return {
        "events": events, "count": len(events),
        "alerts": sorted(alerts, key=lambda a: a["seen"], reverse=True),
        "state": "demo — synthetic reports",
        "attribution": neptun.ATTRIBUTION,
        "dismissed": len(_dismissed),
        "keep_minutes": KEEP_MINUTES, "keep": KEEP,
        "alert_minutes": ALERT_MINUTES,
        # The furthest a mark is carried past its last confirmed
        # position. Sent rather than written into the page as well, so
        # the cap is one number: the map keeps drifting between polls
        # and has to stop where this stops.
        "drift_cap_minutes": MOST_DRIFT_MINUTES,
        "kinds": KINDS,
        # Two of them, one tight and one a corridor, so concentrate mode can
        # be seen and checked without a network.
        # Through the same borrow_course the live path runs, or the demo would
        # be the one place hollow arrows never appear.
        "masses": _demo_masses(events),
        "mass_within_km": MASS_WITHIN_KM, "mass_least": MASS_LEAST,
        "channels": [c["name"] for c in CHANNELS],
        "regions": sorted({c["region"] for c in CHANNELS}),
        "last_poll": now,
        # Counted from the reports' own flags, not from how many markers
        # survive. A track that has arrived or aged off the map was placed
        # perfectly well; calling it unplaced would make the gazetteer look
        # worse than it is, which is precisely the number people will read.
        "reports": {"placed": sum(1 for a in alerts if a["placed"]),
                    "unplaced": sum(1 for a in alerts if not a["placed"])},
        # One of each of the four things a channel can be doing, so the panel
        # that explains a thin map is itself visible in the build with no
        # network -- otherwise the section that exists to answer "why can I
        # not see anything" is the one part nobody can look at.
        # One of each of the four things a channel can be doing, so the panel
        # that explains a thin map is itself visible in the build with no
        # network -- otherwise the section that exists to answer "why can I
        # not see anything" is the one part nobody can look at.
        #
        # Built from CHANNELS rather than written out. It used to be written
        # out, and it went stale the moment the channel list changed: the panel
        # confidently listed three channels the app no longer read, which is
        # exactly the kind of wrong that a demo is supposed to catch rather
        # than cause.
        "sources": [
            *({"channel": channel["name"], "region": channel["region"],
               **DEMO_SOURCE_STATES[i % len(DEMO_SOURCE_STATES)]}
              for i, channel in enumerate(CHANNELS)),
            # And the feed, which is a source and not a channel. It was
            # missing from this list while its marks were on the demo map --
            # so the panel section that exists to answer "why can I not see
            # anything from NEPTUN" had nothing to say about NEPTUN.
            {"channel": NEPTUN_SOURCE, "region": "Ukraine",
             "posts": len(DEMO_NEPTUN), "fresh": len(DEMO_NEPTUN),
             "read": len(DEMO_NEPTUN), "placed": len(DEMO_NEPTUN),
             "problem": None},
        ],
        "gazetteer": {"remembered": len(DEMO_PLACES), "lookups": 0},
    }
