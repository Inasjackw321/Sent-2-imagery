"""Reading air-threat reports without a language model.

The model is the better reader and it is not a dependable one. It is a free
tier with a daily ceiling, and when that ceiling is reached the whole layer
went dark -- an empty map and a line saying the model was rate limiting,
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

from . import places

# ---------------------------------------------------------------------------
# What kind of thing
# ---------------------------------------------------------------------------

# Order matters: the first match wins, so the narrow readings come first.
# "реактивний БпЛА" has to be seen before the plain "БпЛА" inside it, and a
# report of something being shot down is about the shooting down whatever kind
# of thing it was.
# Several of these channels post in English, either natively or translated,
# and the first version of this read only Cyrillic -- so a night of English
# posts produced an empty map with nothing to say why. Each kind now carries
# both, in one pattern, so the ordering that matters stays in one place.
# Arabic, Hebrew and Farsi, for the Lebanon and Middle East channels. Their
# posts were going entirely unread, which is why nothing from those countries
# ever reached the map.
#
# Only the kind is read from these. Place names in a script with no capital
# letters are a different problem from the one the patterns above solve, and
# guessing at them would be exactly the invention this module refuses to make
# elsewhere. So these reports are identified and listed, and placed only if
# the model is available to read them properly.
KIND_WORDS: tuple[tuple[str, str], ...] = (
    ("explosion",
     r"вибух|прильот|приліт|уражен|збит|сбит|взрыв|падіння уламк"
     # Word boundaries on every English term, and they are not decoration:
     # "blast" without one is inside "Oblast", so before this every report
     # naming a Ukrainian region was filed as an explosion.
     r"|\bexplosions?\b|\bblast\b|\bimpacts?\b|\bshot down\b|\bshootdown\b"
     r"|\bintercepted\b|\bstruck\b|\bdebris fell\b|\bhit recorded\b"
     r"|انفجار|انفجارات|قصف|استهداف|اعتراض"
     r"|פיצוץ|נפילה|יירוט"
     r"|انفجار|اصابت"),
    # The all-clear, and it must be tried BEFORE "alert" -- "відбій тривоги"
    # contains "тривога", so the broader pattern would swallow it and draw a
    # warning at the moment one was lifted. Same ordering reason as jet_drone
    # before drone.
    ("all_clear",
     r"відбій\s+(?:повітряної\s+)?тривог|відбій\s+загроз|отбой\s+"
     r"(?:воздушной\s+)?тревог|отбой\s+угроз|\ball[- ]clear\b"
     r"|\balert (?:is )?over\b|\ball clear\b"),
    ("alert",
     r"повітряна тривога|тривога|воздушная тревога"
     r"|угроза|опасность|ракетная опасность|внимание"
     r"|\bair (?:raid|alert|alarm)\b|\bthreats?\b"
     r"|\bdangers?\b|\bwarnings?\b"
     r"|إنذار|انذار|صفارات|تحذير|غارة|غارات"
     r"|אזעקה|התרעה|צבע אדום"
     r"|آژیر|هشدار"),
    ("ballistic",
     r"баліст|баллист|іскандер|искандер|кинжал|кинджал"
     r"|\bballistic\b|\biskander\b|\bkinzhal\b"
     r"|بالستي|בליסטי|بالستیک"),
    ("cruise",
     r"крилат|крылат|калібр|калибр|х-101|x-101|х-555|онікс|оникс"
     r"|\bcruise missiles?\b|\bkalibr\b|\bonyx\b"
     r"|صاروخ|صواريخ|טיל|טילים|موشک"),
    ("recon",
     r"розвідувальн|разведыват|орлан|zala|supercam|суперкам|"
     r"борт-розвідник|розвідник"
     r"|\brecon\b|\breconnaissance\b|\borlan\b|\bobservation (?:uav|drone)\b"),
    ("jet_drone",
     r"реактивн\w*\s+(?:бпла|шахед|дрон)|шахед-?238|герань-?3"
     r"|\bjet[- ](?:powered\s+)?(?:uav|drone)\b|\bshahed-?238\b|\bgeran-?3\b"),
    ("drone",
     r"бпла|шахед|шахид|герань|geran|дрон|безпілотн|беспилотн"
     r"|\buavs?\b|\bfpvs?\b|\bdrones?\b|\bshahed\b|\bgeran\b|\bunmanned\b"
     r"|مسيرة|مسيّرة|مسيرات|طائرة مسيرة|درون"
     r"|כטב\"ם|כטבם|רחפן|רחפנים"
     r"|پهپاد|پهپادها"),
    ("helicopter", r"гелікоптер|вертол|\bhelicopters?\b"),
    ("aircraft",
     r"літак|самол[её]т|міг-|миг-|ту-95|ту-22|су-34|су-35"
     r"|\baircraft\b|\bwarplanes?\b|\bmig-|\btu-95\b|\btu-22\b"
     r"|\bsu-34\b|\bsu-35\b"),
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
    (r"\bnorth[- ]?east(?:ern)?\b", "NE"), (r"\bnorth[- ]?west(?:ern)?\b", "NW"),
    (r"\bsouth[- ]?east(?:ern)?\b", "SE"), (r"\bsouth[- ]?west(?:ern)?\b", "SW"),
    (r"північ\w*|север\w*", "N"),
    (r"південь|південн\w*|юг|южн\w*", "S"),
    (r"сх[іо]д\w*|восток|восточн\w*", "E"),
    (r"зах[іі]д\w*|запад|западн\w*", "W"),
    # English, after the Cyrillic and after the diagonals above, for the same
    # reason the diagonals come first: "north-east" must not be read as
    # "north" and end up forty-five degrees out.
    (r"\bnorth(?:ern|wards?)?\b", "N"), (r"\bsouth(?:ern|wards?)?\b", "S"),
    (r"\beast(?:ern|wards?)?\b", "E"), (r"\bwest(?:ern|wards?)?\b", "W"),
    (r"\bпн[-\s]?сх\b", "NE"), (r"\bпн[-\s]?зх\b", "NW"),
    (r"\bпд[-\s]?сх\b", "SE"), (r"\bпд[-\s]?зх\b", "SW"),
    (r"\bпн\b", "N"), (r"\bпд\b", "S"), (r"\bсх\b", "E"), (r"\bзх\b", "W"),
)

# "курсом на X" / "у напрямку X" -- the phrase that introduces a heading,
# whether what follows is a compass point or a town.
HEADED = re.compile(
    r"(?:курс(?:ом|у)?\s+на|у\s+напрямку|в\s+напрямку|прямує\s+(?:на|до)|"
    r"рух\w*\s+на|лет\w*\s+на|прямую\w*\s+на|в\s+сторону|курсом"
    r"|heading(?:\s+(?:for|to|towards?))?|course\s+(?:for|to|towards?)?"
    r"|moving\s+(?:to|towards?)?|towards?|bound\s+for|en\s+route\s+to)\s+"
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

# The Russian regions these channels report on, by nickname. Kept apart from
# the Ukrainian table only for readability -- they are looked up together, and
# the country each belongs to is settled by the channel's own country list
# rather than by which table the name came out of.
RU_REGIONS: dict[str, str] = {
    "белгородчин": "Белгородская область", "брянщин": "Брянская область",
    "курщин": "Курская область", "воронежчин": "Воронежская область",
    "ростовщин": "Ростовская область", "кубан": "Краснодарский край",
    "ставрополь": "Ставропольский край", "крым": "Республика Крым",
    "подмосковь": "Московская область",
}

# Written out in full, in either language: "Харківській області", "Сумська
# обл.", "Белгородская область", "Брянской области". The stem differs by a
# single letter between the two -- Ukrainian "ськ", Russian "ск" -- so one
# pattern covers both and the soft sign is optional.
OBLAST_FULL = re.compile(
    r"([А-ЯІЇЄҐЁ][а-яіїєґёʼ'’\-]+?)с[ьк]?к\w*\s+"
    r"(обл|кра|окру|республик)", re.I)

# The prepositions that introduce the place a report is about. Anything after
# one of these that looks like a proper noun is where this is happening.
NEAR = re.compile(
    r"(?:повз|в\s+районі|у\s+районі|поблизу|над|біля|коло"
    r"|в\s+районе|около|близ|возле|мимо|рядом\s+с)\s+"
    r"(?P<what>[А-ЯІЇЄҐЁ][А-Яа-яІЇЄҐЁіїєґё'’\-]+"
    r"(?:\s+[А-ЯІЇЄҐЁ][А-Яа-яІЇЄҐЁіїєґё'’\-]+)?)")

# "Вибухи в Одесі", "тривога в Харкові" -- a bare locative after в/у.
AT = re.compile(
    r"(?:^|[\s,:;])[вуна]\s+(?P<what>[А-ЯІЇЄҐЁ][А-Яа-яІЇЄҐЁіїєґё'’\-]{2,})")

# A leading "Київщина:" or "Одещина —" naming the region the post is about.
# A leading "Київщина:" or "Белгородская область —" naming the region the
# post is about. Up to three words, because the Russian form is two.
LEAD = re.compile(
    r"^\s*(?P<what>[А-ЯІЇЄҐЁ][А-Яа-яІЇЄҐЁіїєґё'’\-]+"
    r"(?:\s+[а-яіїєґё'’\-]+){0,2})\s*[:—–-]")

# ── The same, in English ──────────────────────────────────────
#
# A capitalised word after a preposition, and a region named by its type word.
# Latin script needs its own patterns: the Cyrillic ones above key off letter
# ranges that no English report contains.

# "Belgorod District", "Kharkiv Oblast", "Sumy region". The type word is what
# makes it a region rather than a town, and it is kept in the name because
# that is what a gazetteer is asked for.
# NOT re.I on the whole pattern. With it, [A-Z] matches lowercase too, so
# "Air alert in Kharkiv Oblast" captured "in Kharkiv" as the region's name --
# and the place then came out as "in Kharkiv oblast". Only the type word is
# allowed to be written either way.
REGION_EN = re.compile(
    r"\b([A-Z][\w'’\-]+(?:\s+[A-Z][\w'’\-]+)?)\s+"
    r"((?i:oblast|region|district|governorate|province|emirate|krai|republic))\b")

# The other word order: "Emirate of Dubai", "Governorate of Baghdad". Common
# outside the post-Soviet channels and it reads backwards to the pattern above.
REGION_OF_EN = re.compile(
    r"\b((?i:emirate|governorate|province|region|district|republic))\s+of\s+"
    r"([A-Z][\w'’\-]+(?:\s+[A-Z][\w'’\-]+)?)")

# "over Nikopol", "in Novy Olshanets", "near Kupiansk".
NEAR_EN = re.compile(
    r"\b(?:over|above|near|past|in|at|around)\s+"
    r"(?P<what>[A-Z][\w'’\-]+(?:\s+[A-Z][\w'’\-]+)?)")

# Words that look like place names and are not.
NOT_PLACES = {
    "увага", "терміново", "тривога", "відбій", "загроза", "ракетна",
    "повітряна", "балістика", "шахед", "бпла", "вибух", "вибухи", "новини",
    "підписатися", "джерело", "переслати", "україна", "росія", "рф",
    "чорним", "азовським", "морем", "море", "моря", "чорного", "азовського",
    "внимание", "угроза", "опасность", "срочно", "область", "области",
    # The English ones. Every one of these has turned up capitalised, after a
    # preposition, in a real post.
    "danger", "again", "alert", "alarm", "threat", "detection", "warning",
    "russian", "federation", "ukraine", "air", "uav", "uavs", "fpv", "fpvs",
    "drone", "drones", "attention", "the", "direction", "north", "south",
    "east", "west", "lpr", "dpr", "missile", "aircraft", "bomb", "waiver",
    "update", "report", "subscribe", "channel", "source",
}

# A report about the sea is not a report about a place a marker goes. The
# gazetteer would refuse the water anyway, but saying so here keeps a
# nonsense name out of the alert list as well as off the map.
WATER = re.compile(r"мор[еяію]м?|затоц[іи]|лиман", re.I)

# How many of a thing, when the report counts them.
# The leading boundary stops "2024 ракет" being read as 24. The trailing one
# is on the English words ONLY: the Cyrillic entries are stems, and "шахедів"
# is "шахед" with three more letters on it, so a boundary there matches
# nothing at all.
COUNT = re.compile(
    r"\b(\d{1,3})\s*(?:[хx]\s*)?"
    r"(?:бпла|шахед|дрон|ракет|ціл"
    r"|uavs?\b|fpvs?\b|drones?\b|missiles?\b|targets?\b)",
    re.I)

# The endings Ukrainian puts on a place name in the locative and genitive.
# Nominatim copes with a lot, but not all, and undoing the commonest few is a
# few lines here against a whole class of misses.
# Ukrainian case endings, and what the nominative actually is.
#
# "ові" used to map to nothing at all, which turned "Харкові" into "Харк" and
# "Львові" into "Льв" -- names no gazetteer holds, so every strike reported in
# either city went unplaced. The locative of a masculine name does not just
# lose its ending: the stem vowel alternates back, which is why it is "Харків"
# and not "Харков".
ENDINGS = (
    ("щині", "щина"), ("ській", "ська"), ("ському", "ське"),
    ("ові", "ів"), ("єві", "їв"), ("аві", "ава"), ("олі", "іль"),
)


def _tidy(name: str) -> str | None:
    """A place name, trimmed of the sentence it was found in."""
    text = " ".join(str(name or "").split()).strip(" .,;:!?()«»\"'")
    if len(text) < 3 or text.lower() in NOT_PLACES:
        return None
    # A trailing preposition or conjunction swept up by a greedy match.
    text = re.sub(r"\s+(?:та|і|й|з|на|у|в|до)$", "", text, flags=re.I).strip()
    return text[:80] or None


def _canonical(name: str | None) -> str | None:
    """The name a place calls itself, if the built-in table knows it.

    Only for display. A report about "Кременчуці" is placed by that spelling
    -- the table is keyed by it -- but the line a person reads should say
    "Кременчук". Anything the table has not heard of comes back untouched
    rather than guessed at.
    """
    if not name:
        return name
    known = places.lookup(name)
    return known["name"] if known else name


def _nominative(name: str) -> str:
    """Undo the commonest Ukrainian case endings on a place name.

    Conservative on purpose. A wrong nominative is worse than an inflected
    name, because variants() de-inflects again at lookup time and will try the
    name as written first -- so leaving it alone costs nothing, and mangling it
    costs the report its place. "Харкові" becoming "Харк" was exactly that.
    """
    # A name the built-in table already knows is left exactly as it is. The
    # rules below are guesses, and a guess has no business overruling a known
    # answer: the feminine rule at the bottom turned "Кременчуці" -- which is
    # in the table, pointing at Kremenchuk -- into "Кременчуца", which is in
    # nothing, and sent it to Nominatim to fail slowly.
    if places.lookup(name):
        return name
    low = name.lower()
    for ending, replacement in ENDINGS:
        # >= rather than >, which was one character too strict: "Києві" is
        # five letters and "єві" is three, so the rule that turns it into
        # "Київ" never fired and a feminine rule turned it into "Києва".
        if low.endswith(ending) and len(low) >= len(ending) + 2:
            made = name[: -len(ending)] + replacement
            # Nothing shorter than a short name. The guard above counts the
            # ending, not the stem, so it let "Харкові" through to "Харк".
            if len(made) < 4:
                return name
            return made
    # Locative singular of a feminine name: "в Одесі" -> "Одеса".
    if low.endswith("і") and len(low) > 4:
        return name[:-1] + "а"
    return name


# What the shortened forms in OBLAST_FULL stand for.
RU_TAIL = {"обл": "область", "кра": "край", "окру": "округ",
           "республик": "республика"}


def variants(name: str) -> list[str]:
    """A place name, and the de-inflected forms worth trying if it misses.

    Slavic place names arrive in whatever case the sentence put them in --
    "в Белгороде", "над Нікополем", "Волинської області" -- and OpenStreetMap
    holds the nominative. Rather than guess at one transformation and send it
    instead of what was written, the name as written goes first and these
    follow only if it comes back unknown.

    Ordered best-first and capped, and that is not tidiness. Every variant is
    a Nominatim request, and Nominatim is asked at most once a second, so four
    junk guesses are four seconds a report spends unplaced. An earlier version
    of this produced "Волинської област" for "Волинської області" -- a form no
    map has ever held -- and charged a second for it.

    Which is why a recognised pattern stops the generic guessing: a name
    ending in "ої області" is an oblast in the genitive and there is exactly
    one thing worth trying, so the letter-stripping rules below are not also
    applied to it.
    """
    name = " ".join(str(name or "").split())
    # Nothing to look up. Returned as no variants rather than as one empty
    # one: a caller loops over these and asks the gazetteer for each, and an
    # empty string is a request that can only fail.
    if len(name) < 2:
        return []
    out = [name]
    low = name.lower()

    # A leading preposition, in case the model leaves one on. Cheap insurance:
    # "у Харкові" as a whole is not a place and never will be.
    for lead in ("у ", "в ", "на ", "над ", "під ", "біля ", "поблизу ",
                 "около ", "возле ", "по "):
        if low.startswith(lead):
            return variants(name[len(lead):])

    # ── Oblasts, which are most of what these reports name ───────
    #
    # A match here is conclusive, so nothing further is guessed at.
    for tail, becomes in (("ої області", "а область"),
                          ("ой области", "ая область"),
                          ("ій області", "а область"),
                          (" області", " область"),
                          (" области", " область")):
        if low.endswith(tail):
            out.append(name[:-len(tail)] + becomes)
            return list(dict.fromkeys(out))

    # The one-word form: "Харківщини" and "Харківщина" both mean Kharkiv
    # oblast, and neither is what the map calls it.
    for tail in ("щини", "щину", "щина", "щине"):
        if low.endswith(tail) and len(low) - len(tail) >= 3:
            out.append(f"{name[:-len(tail)]}ська область")
            return list(dict.fromkeys(out))

    # ── Towns ────────────────────────────────────────────────────
    if len(low) > 4:
        # Instrumental, which is what "над X" produces and which was missing:
        # "над Нікополем" -> "Нікополь", "над Харковом" -> "Харков".
        if low.endswith("ем"):
            out.append(f"{name[:-2]}ь")
        if low.endswith("ом"):
            out.append(name[:-2])
        # Locative of a masculine name: "Белгороде" -> "Белгород".
        if low.endswith(("е", "і", "и")):
            stem = name[:-1]
            out.append(stem)
            # Ukrainian alternates the vowel in a closed syllable, which the
            # plain strip above gets wrong for a large family of names:
            # "Харкові" -> "Харков" is not a place, "Харків" is. Same for
            # Львові and Тернополі.
            #
            # Only these two. A wider set was tried and produced junk that
            # each cost a second against the rate limit: "-од" gave "Ужгорід"
            # and "Белгорід" for two names that keep their о, and "-ор" had no
            # real cases at all. A rule that fires on names it does not apply
            # to is worse than no rule.
            for was, becomes in (("ов", "ів"), ("ол", "іль")):
                if stem.lower().endswith(was):
                    out.append(stem[:-2] + becomes)
        # Locative of a feminine one: "Одессе" -> "Одесса".
        if low.endswith("е"):
            out.append(f"{name[:-1]}а")
        # Genitive after "в районе X".
        if low.endswith("а"):
            out.append(name[:-1])

    # A ceiling, not a filter. Nothing above currently reaches it -- the most
    # any name produces is exactly four, for a form like "Харкове" -- so this
    # slice never fires today and is here to stop a fifth rule being added
    # without somebody noticing the cost. Four tries is four seconds against
    # the rate limit, and a report is unplaced for every one of them.
    return list(dict.fromkeys(out))[:4]


def find_region(text: str) -> str | None:
    """The region a report is about, in whichever way it named one."""
    low = text.lower()
    for table in (OBLASTS, RU_REGIONS):
        for stem, oblast in table.items():
            if stem in low:
                return oblast
    full = OBLAST_FULL.search(text)
    if full:
        # Rebuilt in the nominative, in whichever language it was written, so
        # the gazetteer gets a name it knows rather than an inflected one.
        head, tail = full.group(1), full.group(2).lower()
        # Which language, decided by the letters Ukrainian has and Russian
        # does not. Looking for Russian-only letters instead was the wrong way
        # round: "Воронежской области" contains none of them, so it came back
        # as "Воронежська область" -- a Russian region with a Ukrainian ending,
        # which no gazetteer knows.
        ukrainian = re.search(r"[іїєґ]", text.lower())
        stem = f"{head}ська" if ukrainian else f"{head}ская"
        return f"{stem} {'область' if tail == 'обл' else RU_TAIL[tail]}"
    english = REGION_EN.search(text)
    if english:
        name = _tidy(english.group(1))
        if name:
            return f"{name} {english.group(2).lower()}"
    backwards = REGION_OF_EN.search(text)
    if backwards:
        name = _tidy(backwards.group(2))
        if name:
            return f"{name} {backwards.group(1).lower()}"
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
            # "над Брянской областью" -- the preposition caught the adjective
            # and the noun after it is the region this already understands.
            # The region is the name a gazetteer knows; the adjective alone is
            # not.
            if region and found.lower()[:6] in region.lower():
                return region
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

    # The same two, for a report written in English.
    near = NEAR_EN.search(text)
    if near:
        found = _tidy(near.group("what"))
        # Skip the region's own name -- "in Kharkiv Oblast" is the region,
        # which is handled as the region and not as a town called Kharkiv.
        if (found and not WATER.search(near.group(0))
                and not REGION_EN.match(f"{found} x")
                and not (region and found.lower() in region.lower())):
            return found

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


# Arabic, Hebrew and Farsi letters. A report written in one of these is
# identified and listed but not placed here: without capital letters there is
# no equivalent of the "capitalised word after a preposition" rule the Latin
# and Cyrillic patterns lean on, and guessing would be the invention this
# module exists to refuse.
OTHER_SCRIPT = re.compile(r"[\u0590-\u05FF\u0600-\u06FF]")


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
        # A report in a script whose place names this cannot read is still a
        # report, and listing it beats the silence those channels used to get.
        # Anything else with a kind and nothing else -- a fundraising post
        # that mentions Shaheds, a statistic, a headline -- is not a report of
        # anything happening.
        if not OTHER_SCRIPT.search(text):
            return None
    if toward and place and toward.lower() == place.lower():
        toward = None
    count = find_count(text)
    # The summary says the place by its own name, not by the case the
    # sentence happened to put it in: "Explosions reported in Кременчук",
    # not "... in Кременчуці". The inflected spelling is kept in `place`,
    # because that is what the gazetteer is asked for and what the table
    # resolves; this is the line a person reads.
    said = _canonical(place)
    named = _canonical(toward)

    return {
        "kind": kind,
        "place": place,
        # Only worth sending as a hint if it is not the place itself.
        "region": region if region and region != place else None,
        "toward": toward,
        "course": course,
        "count": count,
        "summary": summarise(kind, said, named, course, count),
        # Marked, always. A reading from a handful of regular expressions is
        # not the same claim as one from a model that read the sentence, and
        # the interface says which is which rather than blurring them.
        "by": "rules",
    }
