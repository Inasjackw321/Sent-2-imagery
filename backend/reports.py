"""Reading air-threat reports without a language model.

The model is the better reader and it is not a dependable one. It is a free
tier with a daily ceiling, and when that ceiling is reached the whole layer
went dark -- an empty map and a line saying OpenRouter was rate limiting,
which from the outside is indistinguishable from the feature being broken.

But these reports barely need a model. They are written to be scanned at a
glance during an air raid, and they are formulaic to the point of being a
grammar:

    Київщина: реактивний БпЛА повз Кагарлик курсом на північ.
    БпЛА на Полтавщині, курс на Кременчук
    Вибухи в Одесі
    Повітряна тривога в Харківській області

A handful of patterns get the kind, the place and the direction out of most of
them. That is what this is: a plain reader with no network, no key and no
quota, which runs when the model cannot and is never the reason nothing
appears on screen.

It is deliberately worse than the model and deliberately obvious about it.
Everything it produces is marked `by: "rules"` so the interface can say which
reports were read how, and anything it cannot parse confidently is left alone
rather than guessed at -- the same bargain the gazetteer makes.

Two things it does that the model version cannot:

  Oblast nicknames. "Київщина" is Kyiv oblast and "Донеччина" is Donetsk
  oblast. There are twenty-four of them, they appear in a large fraction of
  reports, and they are a closed list -- so they are simply listed, which is
  more reliable than any amount of asking nicely.

  Cyrillic straight through. Nominatim knows Ukrainian place names in
  Ukrainian, so there is no transliteration step to get wrong. The model was
  being asked to romanise names and "Кагарлик" came back as "Kagul"; this
  hands over the letters that were actually written.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# What kind of thing
# ---------------------------------------------------------------------------

# Order matters: the first match wins, so the narrow readings come first.
# "реактивний БпЛА" has to be seen before the plain "БпЛА" inside it, and a
# report of something being shot down is about the shooting down whatever kind
# of thing it was.
KIND_WORDS: tuple[tuple[str, str], ...] = (
    ("explosion", r"вибух|прильот|приліт|уражен|збит|сбит|взрыв|падіння уламк"),
    ("alert", r"повітряна тривога|тривога|відбій|отбой|воздушная тревога"),
    ("ballistic", r"баліст|баллист|іскандер|искандер|кинжал|кинджал"),
    ("cruise", r"крилат|крылат|калібр|калибр|х-101|x-101|х-555|онікс|оникс"),
    ("recon", r"розвідувальн|разведыват|орлан|zala|supercam|суперкам|"
              r"борт-розвідник|розвідник"),
    ("jet_drone", r"реактивн\w*\s+(?:бпла|шахед|дрон)|шахед-?238|герань-?3"),
    ("drone", r"бпла|шахед|шахид|герань|geran|дрон|безпілотн|беспилотн"),
    ("helicopter", r"гелікоптер|вертол"),
    ("aircraft", r"літак|самол[её]т|міг-|миг-|ту-95|ту-22|су-34|су-35"),
)

# ---------------------------------------------------------------------------
# Which way
# ---------------------------------------------------------------------------

# Longest first, so "північний схід" is not read as "північ".
COURSE_WORDS: tuple[tuple[str, str], ...] = (
    (r"північно[- ]сх[іо]дн\w*|північний схід|northeast|северо[- ]восток", "NE"),
    (r"північно[- ]зах[іі]дн\w*|північний захід|northwest|северо[- ]запад", "NW"),
    (r"південно[- ]сх[іо]дн\w*|південний схід|юго[- ]восток", "SE"),
    (r"південно[- ]зах[іі]дн\w*|південний захід|юго[- ]запад", "SW"),
    (r"північ\w*|север\w*", "N"),
    (r"південь|південн\w*|юг|южн\w*", "S"),
    (r"сх[іо]д\w*|восток|восточн\w*", "E"),
    (r"зах[іі]д\w*|запад|западн\w*", "W"),
    (r"\bпн[-\s]?сх\b", "NE"), (r"\bпн[-\s]?зх\b", "NW"),
    (r"\bпд[-\s]?сх\b", "SE"), (r"\bпд[-\s]?зх\b", "SW"),
    (r"\bпн\b", "N"), (r"\bпд\b", "S"), (r"\bсх\b", "E"), (r"\bзх\b", "W"),
)

# "курсом на X" / "у напрямку X" -- the phrase that introduces a heading,
# whether what follows is a compass point or a town.
HEADED = re.compile(
    r"(?:курс(?:ом|у)?\s+на|у\s+напрямку|в\s+напрямку|прямує\s+(?:на|до)|"
    r"рух\w*\s+на|лет\w*\s+на|прямую\w*\s+на|в\s+сторону|курсом)\s+"
    r"(?P<what>[^,.;!)]+)", re.I)

# ---------------------------------------------------------------------------
# Where
# ---------------------------------------------------------------------------

# The oblast nicknames. A closed list of twenty-four, in a large fraction of
# reports, and not something to make a model guess at. The value is what goes
# to the gazetteer, which knows these in Ukrainian.
OBLASTS: dict[str, str] = {
    "київщин": "Київська область", "полтавщин": "Полтавська область",
    "харківщин": "Харківська область", "сумщин": "Сумська область",
    "одещин": "Одеська область", "чернігівщин": "Чернігівська область",
    "дніпропетровщин": "Дніпропетровська область",
    "донеччин": "Донецька область", "луганщин": "Луганська область",
    "запоріжж": "Запорізька область", "миколаївщин": "Миколаївська область",
    "херсонщин": "Херсонська область", "черкащин": "Черкаська область",
    "житомирщин": "Житомирська область", "вінниччин": "Вінницька область",
    "хмельниччин": "Хмельницька область", "рівненщин": "Рівненська область",
    "волин": "Волинська область", "львівщин": "Львівська область",
    "тернопільщин": "Тернопільська область", "закарпатт": "Закарпатська область",
    "буковин": "Чернівецька область", "чернівеччин": "Чернівецька область",
    "кіровоградщин": "Кіровоградська область",
    "івано-франківщин": "Івано-Франківська область",
    "прикарпатт": "Івано-Франківська область",
    "крим": "Автономна Республіка Крим",
}

# Written out in full: "на Харківській області", "Сумська обл."
OBLAST_FULL = re.compile(
    r"([А-ЯІЇЄҐ][а-яіїєґ'’\-]+?)ськ\w*\s+обл", re.I)

# The prepositions that introduce the place a report is about. Anything after
# one of these that looks like a proper noun is where this is happening.
NEAR = re.compile(
    r"(?:повз|в\s+районі|у\s+районі|поблизу|над|біля|коло)\s+"
    r"(?P<what>[А-ЯІЇЄҐ][А-Яа-яІЇЄҐіїєґ'’\-]+(?:\s+[А-ЯІЇЄҐ][А-Яа-яІЇЄҐіїєґ'’\-]+)?)")

# "Вибухи в Одесі", "тривога в Харкові" -- a bare locative after в/у.
AT = re.compile(
    r"(?:^|[\s,:;])[вуна]\s+(?P<what>[А-ЯІЇЄҐ][А-Яа-яІЇЄҐіїєґ'’\-]{2,})")

# A leading "Київщина:" or "Одещина —" naming the region the post is about.
LEAD = re.compile(r"^\s*(?P<what>[А-ЯІЇЄҐ][А-Яа-яІЇЄҐіїєґ'’\-]+)\s*[:—–-]")

# Words that look like place names and are not.
NOT_PLACES = {
    "увага", "терміново", "тривога", "відбій", "загроза", "ракетна",
    "повітряна", "балістика", "шахед", "бпла", "вибух", "вибухи", "новини",
    "підписатися", "джерело", "переслати", "україна", "росія", "рф",
    "чорним", "азовським", "морем", "море", "моря", "чорного", "азовського",
}

# A report about the sea is not a report about a place a marker goes. The
# gazetteer would refuse the water anyway, but saying so here keeps a
# nonsense name out of the alert list as well as off the map.
WATER = re.compile(r"мор[еяію]м?|затоц[іи]|лиман", re.I)

# How many of a thing, when the report counts them.
COUNT = re.compile(r"\b(\d{1,3})\s*(?:х\s*)?(?:бпла|шахед|дрон|ракет|ціл)", re.I)

# The endings Ukrainian puts on a place name in the locative and genitive.
# Nominatim copes with a lot, but not all, and undoing the commonest few is a
# few lines here against a whole class of misses.
ENDINGS = (
    ("щині", "щина"), ("ській", "ська"), ("ському", "ське"),
    ("ові", ""), ("еві", ""), ("аві", "ава"),
)


def _tidy(name: str) -> str | None:
    """A place name, trimmed of the sentence it was found in."""
    text = " ".join(str(name or "").split()).strip(" .,;:!?()«»\"'")
    if len(text) < 3 or text.lower() in NOT_PLACES:
        return None
    # A trailing preposition or conjunction swept up by a greedy match.
    text = re.sub(r"\s+(?:та|і|й|з|на|у|в|до)$", "", text, flags=re.I).strip()
    return text[:80] or None


def _nominative(name: str) -> str:
    """Undo the commonest Ukrainian case endings on a place name."""
    low = name.lower()
    for ending, replacement in ENDINGS:
        if low.endswith(ending) and len(low) > len(ending) + 2:
            return name[: -len(ending)] + replacement
    # Locative singular of a feminine name: "в Одесі" -> "Одеса".
    if low.endswith("і") and len(low) > 4:
        return name[:-1] + "а"
    return name


def find_region(text: str) -> str | None:
    """The oblast a report is about, from its nickname or its full name."""
    low = text.lower()
    for stem, oblast in OBLASTS.items():
        if stem in low:
            return oblast
    full = OBLAST_FULL.search(text)
    if full:
        return f"{full.group(1)}ська область"
    return None


def find_kind(text: str) -> str:
    low = text.lower()
    for kind, pattern in KIND_WORDS:
        if re.search(pattern, low):
            return kind
    return "unknown"


def find_course(phrase: str) -> str | None:
    """A compass point out of the words after "курсом на"."""
    low = phrase.lower()
    for pattern, point in COURSE_WORDS:
        if re.search(pattern, low):
            return point
    return None


def find_heading(text: str) -> tuple[str | None, str | None]:
    """Where it is going: a compass point, or a place name. Never both.

    A course phrase is followed by one or the other -- "курсом на північ" or
    "курсом на Кременчук" -- and telling them apart is just asking whether the
    words are a compass point. That question has a definite answer, which is
    why this can be done without a model at all.
    """
    for match in HEADED.finditer(text):
        what = match.group("what").strip()
        point = find_course(what)
        if point:
            return point, None
        name = _tidy(what)
        if name and name[0].isupper():
            return None, _nominative(name)
    return None, None


def find_place(text: str, region: str | None) -> str | None:
    """The place a report is about.

    A named town beats the oblast: "БпЛА повз Кагарлик" over Kyiv oblast is a
    marker on Kaharlyk, not one in the middle of the region. The oblast is the
    fallback, and it is a good one -- it is what the report gave.
    """
    near = NEAR.search(text)
    if near:
        found = _tidy(near.group("what"))
        if found and not WATER.search(near.group(0)):
            # "над Сумщиною" is the oblast under another of its endings, and
            # the oblast has a proper name that a gazetteer knows. Preferring
            # the inflected nickname would send "Сумщиною" to Nominatim and
            # get nothing back.
            if any(stem in found.lower() for stem in OBLASTS):
                return region or _nominative(found)
            return _nominative(found)

    # A bare locative, but only if it is not the oblast nickname this has
    # already understood -- "вибухи в Одесі" is Odesa, "БпЛА на Одещині" is
    # the oblast and is handled as the region.
    for match in AT.finditer(text):
        found = _tidy(match.group("what"))
        if not found:
            continue
        if any(stem in found.lower() for stem in OBLASTS):
            continue
        if region and found.lower()[:6] in region.lower():
            continue
        if WATER.search(text[match.end():match.end() + 12]):
            continue
        return _nominative(found)

    if region:
        return region

    lead = LEAD.search(text)
    if lead:
        found = _tidy(lead.group("what"))
        if found:
            return _nominative(found)
    return None


def find_count(text: str) -> int:
    match = COUNT.search(text)
    if not match:
        return 1
    try:
        found = int(match.group(1))
    except ValueError:
        return 1
    return found if 1 <= found <= 999 else 1


# What to call each kind in the one-line summary. English, because that is
# what the rest of the interface is in and the wall display reads it out.
SAYS = {
    "recon": "Reconnaissance drone", "drone": "Drone", "jet_drone": "Jet drone",
    "cruise": "Cruise missile", "ballistic": "Ballistic missile",
    "aircraft": "Aircraft", "helicopter": "Helicopter",
    "explosion": "Explosions reported", "alert": "Air alert",
    "unknown": "Unidentified",
}

WAYS = {"N": "north", "NE": "north-east", "E": "east", "SE": "south-east",
        "S": "south", "SW": "south-west", "W": "west", "NW": "north-west"}


def summarise(kind: str, place: str | None, toward: str | None,
              course: str | None, count: int) -> str:
    what = SAYS.get(kind, "Report")
    if count > 1:
        what = f"{count} × {what.lower()}"
    where = f" over {place}" if place and kind not in ("explosion", "alert") else (
        f" in {place}" if place else "")
    going = ""
    if toward:
        going = f", heading for {toward}"
    elif course:
        going = f", heading {WAYS.get(course, course)}"
    return f"{what}{where}{going}"[:160]


def read(text: str) -> dict[str, Any] | None:
    """One report, read without a model. None if there is nothing in it.

    The bar for returning anything is a kind AND a place. A report this cannot
    identify, or cannot locate, is left for the model -- or left alone. Half a
    reading is worse than none: it would put a marker somewhere on the strength
    of a keyword.
    """
    text = " ".join(str(text or "").split())
    if len(text) < 6:
        return None

    kind = find_kind(text)
    if kind == "unknown":
        return None

    region = find_region(text)
    place = find_place(text, region)
    course, toward = (None, None) if kind in ("explosion", "alert") else find_heading(text)
    if not place and not toward and not course:
        # A kind and nothing else: a fundraising post that mentions Shaheds,
        # a statistic, a headline. Not a report of anything happening.
        return None
    if toward and place and toward.lower() == place.lower():
        toward = None
    count = find_count(text)

    return {
        "kind": kind,
        "place": place,
        # Only worth sending as a hint if it is not the place itself.
        "region": region if region and region != place else None,
        "toward": toward,
        "course": course,
        "count": count,
        "summary": summarise(kind, place, toward, course, count),
        # Marked, always. A reading from a handful of regular expressions is
        # not the same claim as one from a model that read the sentence, and
        # the interface says which is which rather than blurring them.
        "by": "rules",
    }
