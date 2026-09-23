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
# Posts that name a weapon and are not a report of one in the air.
#
# These were drawing a purple missile arrow over a town for saying that air
# defence works, or that debris had been found, or that there was nothing up.
# Each is a sentence with "ракета" or "missile" in it and no missile in it.
#
# Narrow on purpose. A wider "anything about air defence" would swallow "ППО
# збила ракету над Києвом", which is a real event -- an interception -- and
# belongs to the explosion pattern below rather than to nothing.
NOT_A_REPORT = re.compile(
    r"протиракетн|anti-?missile|missile\s+defen[cs]e"
    r"|\bno\s+(?:missiles?|drones?|uavs?|threats?)\b",
    re.I)

KIND_WORDS: tuple[tuple[str, str], ...] = (
    ("explosion",
     # "ракетний удар по Одесі" is a strike that has landed, not a missile
     # on its way -- and it is how these posts are written. Without it the
     # commonest strike sentence in the language drew an inbound missile.
     # "збито" is the form these posts use most, but "ППО збила ракету" is
     # the other one and the stem "збит" does not reach it. Both spelt out.
     r"вибух|прильот|приліт|уражен|збит|збив|збил|сбит|сбил|взрыв"
     r"|ракетн\w*\s+удар|ракетный\s+удар|удар\w*\s+по\s"
     # Debris is by definition something that has come down.
     r"|уламк|обломк|\bdebris\b"
     # Word boundaries on every English term, and they are not decoration:
     # "blast" without one is inside "Oblast", so before this every report
     # naming a Ukrainian region was filed as an explosion.
     r"|\bexplosions?\b|\bblast\b|\bimpacts?\b|\bshot down\b|\bshootdown\b"
     r"|\bintercepted\b|\bstruck\b|\bdebris fell\b|\bhit recorded\b"
     # "was the target of a massive drone strike" -- something that happened,
     # not something in the air. Without these the sentence matched on the
     # word "drone" and put an aircraft over a town that had already been hit.
     r"|\b(?:drone|missile|uav|air)\s+strikes?\b|\bstrikes?\s+(?:on|against)\b"
     r"|\bwas\s+(?:the\s+)?target\b|\bcame\s+under\s+attack\b"
     r"|\bdamage\s+reported\b|\bfires?\s+broke\s+out\b"
     r"|انفجار|انفجارات|قصف|استهداف|اعتراض"
     r"|פיצוץ|נפילה|יירוט"
     r"|انفجار|اصابت"),
    # The all-clear, and it must be tried BEFORE "alert" -- "відбій тривоги"
    # contains "тривога", so the broader pattern would swallow it and draw a
    # warning at the moment one was lifted. Same ordering reason as jet_drone
    # before drone.
    ("all_clear",
     # "Отбой" on its own, and that is deliberate. It was written as
     # отбой+тревоги and отбой+угрозы, which left out the phrase the Russian
     # side uses most: "Отбой опасности БПЛА". That fell past this pattern
     # into "drone" -- so the moment a warning was LIFTED, a drone appeared in
     # the air over the region. In these channels the word has no other use;
     # the same goes for its Ukrainian twin.
     r"\bвідбій\b|\bотбой\b"
     # And the other way of saying it: the alert was cancelled rather than
     # stood down.
     r"|отмен\w*\s+(?:воздушн\w*\s+)?(?:тревог|угроз|опасност)"
     r"|скасуванн\w*\s+(?:повітряної\s+)?тривог|\ball[- ]clear\b"
     # "UAV alert cleared" / "missile threat lifted" -- how the Russian radar
     # channels write a stand-down in English. Without these the post was read
     # as a DRONE, so a warning being lifted put a drone in the air.
     r"|\balert (?:is )?(?:over|cleared|lifted)\b|\ball clear\b"
     # The words can be a clause apart -- "The UAV danger in Rostov region has
     # been cancelled" -- so the noun and the verb are not required to be
     # neighbours. Bounded, because a gap this cannot see the end of would
     # eventually join two unrelated sentences and lift a warning nobody
     # lifted.
     #
     # "danger" is in the noun list because the Russian side writes it that
     # way more often than "threat", and it was in neither this pattern nor
     # the one below it -- so "UAV danger cancelled" read as a drone.
     r"|\b(?:threat|alert|alarm|danger|warning)s?\b[^.!?]{0,48}?"
     r"\b(?:cancell?ed|call(?:ed)? off|stood down|lifted|cleared|"
     r"deactivated|removed|over)\b"
     # And the same pair the other way round, which is how a headline writes
     # it: "Cancellation of the UAV danger in Belgorod region".
     r"|\b(?:cancell?ation|lifting|removal)\s+of\b[^.!?]{0,24}?"
     r"\b(?:threat|alert|alarm|danger|warning)s?\b"
     r"|\bno longer\b.{0,20}\b(?:threat|alert)\b"
     # "No threats currently" is a stand-down, and the alert pattern below
     # matches the bare word "threats" -- so without this it raised a warning
     # for a post saying there was none. Exactly the failure all_clear exists
     # for, in English: "відбій тривоги" contains "тривога" the same way.
     #
     # Only the words about the WARNING state. "No missiles in the air" is
     # about objects rather than about an alert, and lifting warnings on the
     # strength of it would be a stronger act than the sentence supports.
     r"|\bno\s+(?:current\s+)?(?:threats?|alerts?|alarms?)\b"),
    ("alert",
     # Every one of these in whatever ending the sentence put it in, which is
     # what this pattern did not do and is why "not all the alerts work".
     #
     # They were written in the nominative and nothing else: "тривога",
     # "угроза", "опасность". Almost no post says them that way. What the
     # Russian regional channels write is "объявлен жёлтый уровень
     # ОПАСНОСТИ по БПЛА" and "под УГРОЗОЙ атаки БПЛА"; what the Ukrainian
     # ones write is "оголошено повітряну ТРИВОГУ". None of those matched,
     # so the post fell past this pattern into the weapon words below it and
     # a declared warning over a whole republic was drawn as a single drone
     # in flight -- or, where the name could not be placed, as nothing at
     # all. Measured on eight real phrasings: three read as a drone, one as
     # a ballistic missile, one as nothing.
     #
     # "загроз" is here for the first time. It is the ordinary Ukrainian
     # word for a threat -- "загроза застосування балістики" -- and it was
     # in no pattern at all, so that sentence was read as a ballistic
     # missile on its way rather than a warning about one.
     r"тривог\w*|тревог\w*|загроз\w*|угроз\w*|опасност\w*|небезпек\w*"
     # "Внимание" only where it is not the sign-off. "Спасибо за внимание"
     # ends a great many of these posts, and with a place named anywhere in
     # the text it raised an air alert over that province -- a warning
     # nobody declared, from a pleasantry.
     r"|(?<!за )внимание"
     # A bare "Alert", because "Lipetsk Oblast Drone Alert" is a WARNING for
     # Lipetsk and not a drone over it. This pattern is tried before the
     # weapon words on purpose: in these posts the weapon says what the
     # warning is ABOUT, and find_cause() below reads it for that. Before this
     # every Russian-side warning was drawn as an aircraft in flight, at a
     # point, in the middle of a province nobody had reported anything over.
     r"|\bair (?:raid|alert|alarm)\b|\balerts?\b|\balarms?\b"
     r"|\bthreats?\b"
     r"|\bdangers?\b|\bwarnings?\b"
     r"|إنذار|انذار|صفارات|تحذير|غارة|غارات"
     r"|אזעקה|התרעה|צבע אדום"
     r"|آژیر|هشدار"),
    ("ballistic",
     r"баліст|баллист|іскандер|искандер|кинжал|кинджал"
     r"|\bballistic\b|\biskander\b|\bkinzhal\b"
     r"|بالستي|בליסטי|بالستیک"),
    # Guided bombs, which the reader had no name for at all. NEPTUN send them
    # as "kab" and the map has drawn them since, but a channel writing "КАБ на
    # Вовчанськ" produced nothing -- find_kind returned "unknown" and read()
    # gives up on an unknown kind, so the report was dropped whole.
    #
    # Above "cruise" because neither pattern can match the other's words, and
    # a reader is better served by the specific one being tried first. A KAB
    # glides tens of kilometres from an aircraft near the line; drawing it as
    # a missile would answer "how long have I got" with the wrong number.
    ("kab",
     # The endings matter: "КАБи", "КАБів", "КАБами" are how these posts
     # actually write it, and a bare \bкаб\b matches none of them. Spelt out
     # rather than as \w*, which would swallow "кабель" and "кабінет".
     r"\bкаб(?:и|ів|ам|ами|ах|ах|ом|у|а)?\b|\bкаб-\d+\b"
     r"|керован\w*\s+авіабомб|управляем\w*\s+авиабомб"
     r"|коригован\w*\s+авіабомб|\bумпк\b"
     r"|\bguided\s+(?:aerial\s+)?bombs?\b|\bglide\s+bombs?\b|\bkabs?\b"),
    ("cruise",
     # The bare word, which was in no pattern at all: "Ракети на Львівщину"
     # was read as nothing and produced no mark. Safe below "alert", which
     # catches "ракетна небезпека" and "ракетная опасность" first -- those are
     # warnings ABOUT missiles rather than reports of them.
     r"ракет\w*|ракети|"
     r"крилат|крылат|калібр|калибр|х-101|x-101|х-555|онікс|оникс"
     # "Швидкісна ціль" and its Russian twin are what both sides call a
     # missile before anybody has identified which one. The phrase is
     # unambiguous in these channels and it was read as nothing.
     r"|швидкісн\w*\s+ціл|высокоскоростн\w*\s+цел"
     # And the bare English word. "Cruise missile" was covered and "missile"
     # was not, so the Russian-side channel -- which posts in English -- had
     # every plain missile report dropped. Safe below "alert", which takes
     # "Missile Alert" first: there the weapon says what the warning is
     # about, and find_cause reads it for that.
     r"|\bmissiles?\b|\bcruise missiles?\b|\bkalibr\b|\bonyx\b"
     r"|صاروخ|صواريخ|טיל|טילים|موشک"),
    ("recon",
     r"розвідувальн|разведыват|орлан|zala|supercam|суперкам|"
     r"борт-розвідник|розвідник"
     r"|\brecon\b|\breconnaissance\b|\borlan\b|\bobservation (?:uav|drone)\b"),
    ("jet_drone",
     r"реактивн\w*\s+(?:бпла|шахед|дрон)|шахед-?238|герань-?3"
     r"|\bjet[- ](?:powered\s+)?(?:uav|drone)\b|\bshahed-?238\b|\bgeran-?3\b"),
    # Above "drone" for the same reason jet_drone is: every one of these
    # words also matches the general drone pattern below, and the first rule
    # to match wins.
    #
    # An FPV is flown by a person on a video link. Its range is a few
    # kilometres rather than a few hundred, so a mark that says FPV says the
    # thing that launched it is close -- which is a different fact from a
    # Shahed crossing an oblast, and the one worth drawing separately.
    # Only wording that means an FPV and nothing else. "камікадзе" is not
    # here on purpose: a Shahed is routinely called a дрон-камікадзе, so
    # taking that word would relabel the long-range strikes this map exists
    # for as something launched from the next field.
    ("fpv", r"fpv|фпв|\bfirst[- ]person[- ]view\b"),
    ("drone",
     r"бпла|шахед|шахид|герань|geran|дрон|безпілотн|беспилотн"
     r"|\buavs?\b|\bdrones?\b|\bshahed\b|\bgeran\b|\bunmanned\b"
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
# The consonant before "-ька" is CAPTURED rather than assumed to be "с".
#
# It was hardcoded, and four oblasts do not have an "с" there: Запорізька,
# Донецька, Вінницька and Хмельницька. So "Повітряна тривога у Запорізькій
# області" came back as the place "Запорізькій" -- the adjective on its own,
# with "області" lost -- which no gazetteer holds. That is a warning for a
# whole province going unplaced, and because the declared one never landed, a
# DERIVED warning was raised beside it from the drones underneath: two
# triangles over one oblast, which is what the screenshot showed.
OBLAST_FULL = re.compile(
    r"(?P<head>[А-ЯІЇЄҐЁ][а-яіїєґёʼ'’\-]+?)(?P<hiss>[сцз])"
    # The soft sign, captured rather than skipped over. It is what tells
    # Ukrainian from Russian in this exact word -- "Житомирська" against
    # "Житомирская" -- and it is the only reliable signal, because ten of
    # Ukraine's twenty-five oblast names contain not one letter Russian
    # lacks. See find_region.
    r"(?P<soft>ь?)к\w*\s+"
    r"(?P<tail>обл|кра|окру|республик)", re.I)

# The prepositions that introduce the place a report is about. Anything after
# one of these that looks like a proper noun is where this is happening.
NEAR = re.compile(
    # "через" -- through -- is how these posts describe a corridor: "через
    # зону відчуження Чорнобильської АЕС" is a drone crossing the Chornobyl
    # exclusion zone, which is a position and was being read as no position at
    # all. Without it the report fell through to whatever came after "курсом
    # на" and the mark went on the destination.
    r"(?:повз|через|в\s+районі|у\s+районі|поблизу|над|біля|коло"
    r"|в\s+районе|около|близ|возле|мимо|рядом\s+с)\s+"
    # Up to two lowercase words may stand between the preposition and the
    # name: "через ЗОНУ ВІДЧУЖЕННЯ Чорнобильської АЕС", "в районі МІСТА Суми",
    # "над СЕЛИЩЕМ Козелець". The pattern wanted the capital immediately after
    # the preposition, so every one of those read as no place at all.
    #
    # Two, not any number: the further this reaches the more likely it is to
    # walk past the phrase and pick up an unrelated name later in the sentence.
    r"(?:[а-яіїєґё'’\-]+\s+){0,2}"
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

# The same word order in Cyrillic: "в Республике Татарстан", "Республика
# Башкортостан", "над Республикой Крым".
#
# A federal subject that is a republic puts its type word FIRST, so
# OBLAST_FULL above -- which wants an adjective in front of the type word --
# matches none of them. Nothing else did either, so "Воздушная тревога в
# Республике Татарстан" came back as the place "Республике": the word
# "republic" on its own, in the dative, with the republic's actual name
# dropped. Twenty-odd Russian regions are named this way and every warning
# over one of them was unplaceable.
#
# Only the type word declines; the name after it does not, so what is
# captured is already the form a gazetteer holds.
# Case-insensitive on the TYPE WORD ONLY, the same care REGION_EN takes: with
# re.I on the whole pattern, [А-ЯЁ] matches lowercase too and the capture
# would run on into the rest of the sentence.
REPUBLIC_RU = re.compile(
    r"\b(?i:республик\w*)\s+([А-ЯЁ][а-яё'’\-]+(?:\s+[А-ЯЁ][а-яё'’\-]+)?)")

# The other word order: "Emirate of Dubai", "Governorate of Baghdad". Common
# outside the post-Soviet channels and it reads backwards to the pattern above.
REGION_OF_EN = re.compile(
    r"\b((?i:emirate|governorate|province|region|district|republic))\s+of\s+"
    r"([A-Z][\w'’\-]+(?:\s+[A-Z][\w'’\-]+)?)")

# Capitalised words that follow a region's name and are not part of it.
#
# These channels write headlines, so the words after the name are capitalised
# too: "Republic of Tatarstan Drone Alert". The name patterns take up to two
# capitalised words, so that came out as the region "Tatarstan Drone" -- a
# place no gazetteer has, which meant every warning written in that word order
# was listed as unplaceable while the ones written "Lipetsk Oblast Drone Alert"
# worked. A headline is not a sentence and cannot be parsed like one.
NOT_PART_OF_A_NAME = re.compile(
    r"^(?:drone|drones|uav|uavs|missile|missiles|alert|alerts|alarm|alarms"
    r"|threat|threats|warning|warnings|strike|strikes|attack|attacks"
    r"|air|raid|danger|update|breaking|urgent)$", re.I)


def _trim_en(name: str | None) -> str | None:
    """A captured English name with the headline words stripped off it.

    Both ends. "Republic of Tatarstan Drone Alert" puts them after the name
    and "BREAKING Kursk Oblast" puts them before it, and these channels write
    both -- so a pattern that takes up to two capitalised words picks up
    whichever is adjacent.
    """
    if not name:
        return name
    words = name.split()
    while len(words) > 1 and NOT_PART_OF_A_NAME.match(words[-1]):
        words.pop()
    while len(words) > 1 and NOT_PART_OF_A_NAME.match(words[0]):
        words.pop(0)
    return " ".join(words) if words else None


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
    # The feminine instrumental, which is what "над" produces and which these
    # posts are full of: "над Охтиркою", "над Шосткою", "над Вінницею". There
    # was no rule for it at all, so every one of those was a name no gazetteer
    # has -- and unlike a wrong guess it failed silently, as an unplaced row.
    ("ою", "а"), ("ею", "я"),
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
    if known:
        return known["name"]
    # The de-inflected forms too, so a line reads "heading for Одеса" rather
    # than "heading for Одесу". Only for display -- `place` and `toward` keep
    # the spelling that was written, because that is what the gazetteer is
    # asked for first.
    for attempt in variants(name)[1:]:
        known = places.lookup(attempt)
        if known:
            return known["name"]
    return name


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
                          # The accusative, which is what "курсом на" produces:
                          # "курсом на Житомирську область". Without it that
                          # form never resolved, so a report naming the same
                          # province as both its position and its destination
                          # could not tell that they were the same place.
                          ("ську область", "ська область"),
                          ("скую область", "ская область"),
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
            stem = name[:-2]
            out.append(stem)
            # And the vowel alternation, which the strip alone gets wrong for
            # a whole family: "над Фастовом" gives "Фастов", and the place is
            # "Фастів". The same rule was already applied after the locative
            # strip below and not after this one, so half the names it exists
            # for went unplaced.
            for was, becomes in (("ов", "ів"), ("ол", "іль")):
                if stem.lower().endswith(was):
                    out.append(stem[:-2] + becomes)
        # Locative of a masculine name: "Белгороде" -> "Белгород".
        #
        # Not for a plural instrumental, which also ends in "и" and is handled
        # below: stripping "Броварами" to "Броварам" is a form no map holds,
        # and because it is appended first it was tried first -- a wasted
        # second of the rate limit ahead of the answer.
        if low.endswith(("е", "і", "и")) and not low.endswith(("ами", "ями")):
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
        # Feminine instrumental, the other thing "над X" produces: "над
        # Охтиркою" -> "Охтирка", "над Шосткою" -> "Шостка". The masculine
        # forms above were here and this was not, so half the names these
        # posts write after "над" had no rule at all -- and unlike a wrong
        # guess that fails loudly, this failed as a silent unplaced row.
        if low.endswith("ою"):
            out.append(f"{name[:-2]}а")
        elif low.endswith("ею"):
            out.append(f"{name[:-2]}я")
        # Plural instrumental. A large share of these towns have plural names
        # -- Бровари, Прилуки, Лубни, Ромни, Черкаси, Суми -- and "над
        # Броварами" had no rule at all, so every one of them was an unplaced
        # row whenever a post used the commonest preposition in the feed.
        elif low.endswith("ами"):
            out.append(f"{name[:-3]}и")
        elif low.endswith("ями"):
            out.append(f"{name[:-3]}і")
        # Masculine instrumental of an adjectival name: "над Кропивницьким"
        # -> Кропивницький. A whole class of Ukrainian city names is
        # adjectival and every one of them arrived in this form unplaced.
        elif low.endswith("им") and len(low) > 5:
            out.append(f"{name[:-2]}ий")
        # Feminine accusative, which is what "курсом на X" produces:
        # "курсом на Одесу" -> Одеса, "на Вінницю" -> Вінниця. Only for a
        # name that is not already known, since the table check in
        # _nominative() guards the read-time side and this is the lookup side.
        elif low.endswith("у") and not places.lookup(name):
            out.append(f"{name[:-1]}а")
        elif low.endswith("ю") and not places.lookup(name):
            out.append(f"{name[:-1]}я")

    # A ceiling, not a filter. Nothing above currently reaches it -- the most
    # any name produces is exactly four, for a form like "Харкове" -- so this
    # slice never fires today and is here to stop a fifth rule being added
    # without somebody noticing the cost. Four tries is four seconds against
    # the rate limit, and a report is unplaced for every one of them.
    return list(dict.fromkeys(out))[:4]


def oblast_named(word: str) -> str | None:
    """The oblast a single word names, by its nickname stem. None if it is not one.

    First match, plainly, because no stem in either table is contained in
    another -- checked, and it is a property of the names rather than luck:
    these are twenty-four distinct province nicknames. A longest-match rule
    was written here first and then removed, because nothing could reach it
    and a condition nothing reaches is one nobody would notice breaking.
    """
    low = (word or "").lower()
    for table in (OBLASTS, RU_REGIONS):
        for stem, oblast in table.items():
            if stem in low:
                return oblast
    return None


def find_region(text: str) -> str | None:
    """The region a report is about, in whichever way it named one.

    Whichever region is named FIRST, which was the whole of a real
    misplacement. This used to walk the table of oblast nicknames in
    dictionary order and return the first stem that appeared anywhere in the
    sentence -- so "БпЛА на Сумщині курсом на Полтавщину" came back as
    Poltava, because "полтавщин" happens to sit above "сумщин" in the table.
    The drone was drawn in the province it was heading for rather than the one
    it was over, which is a hundred and fifty kilometres wrong and the
    commonest sentence shape these channels write.

    Position first, destination second, is how the sentence is built: the
    course phrase comes after the place. So the earliest name in the text is
    the one the report is about.
    """
    low = text.lower()
    at = len(low) + 1
    first = None
    for table in (OBLASTS, RU_REGIONS):
        for stem, oblast in table.items():
            where = low.find(stem)
            # Earliest wins outright. No tie is possible: two stems can only
            # start at the same index if one is inside the other, and none of
            # them is -- see oblast_named().
            if 0 <= where < at:
                at, first = where, oblast
    if first:
        return first
    full = OBLAST_FULL.search(text)
    if full:
        # Rebuilt in the nominative, in whichever language it was written, so
        # the gazetteer gets a name it knows rather than an inflected one.
        head = full.group("head")
        hiss = full.group("hiss")
        tail = full.group("tail").lower()
        # Which language, decided by the WORD rather than by the sentence.
        #
        # It used to be decided by whether the sentence anywhere contained a
        # letter Ukrainian has and Russian does not. That is true of most
        # Ukrainian oblast names and false of ten of the twenty-five:
        # Житомирська, Сумська, Луганська, Донецька, Херсонська, Одеська,
        # Полтавська, Черкаська, Хмельницька and Закарпатська are spelt
        # entirely in letters the two alphabets share. Every one of them came
        # back rebuilt in Russian -- "Житомирская область" -- which NEPTUN's
        # boundary file does not hold, so the warning lost its outline and
        # was drawn as a triangle on a point.
        #
        # The soft sign settles it exactly and locally: Ukrainian writes
        # -ська and -ський, Russian writes -ская and -ский. Looking for
        # Russian-only letters was the other wrong way round, and the case
        # that catches is still covered -- "Воронежской области" has no soft
        # sign, so it rebuilds in Russian as it should.
        ukrainian = bool(full.group("soft")) or bool(
            re.search(r"[іїєґ]", full.group(0).lower()))
        word = "область" if tail == "обл" else RU_TAIL[tail]
        # And the gender has to agree with the noun. "край" and "округ" are
        # masculine, so "Краснодарская край" is not a thing anybody writes and
        # not a thing any gazetteer holds -- it wants "Краснодарский край".
        if word in ("край", "округ"):
            ending = "ький" if ukrainian else "кий"
        else:
            ending = "ька" if ukrainian else "кая"
        return f"{head}{hiss}{ending} {word}"
    republic = REPUBLIC_RU.search(text)
    if republic:
        return f"Республика {republic.group(1)}"
    english = REGION_EN.search(text)
    if english:
        name = _trim_en(_tidy(english.group(1)))
        if name:
            return f"{name} {english.group(2).lower()}"
    backwards = REGION_OF_EN.search(text)
    if backwards:
        name = _trim_en(_tidy(backwards.group(2)))
        if name:
            return f"{name} {backwards.group(1).lower()}"
    return None


def find_kind(text: str) -> str:
    low = text.lower()
    for kind, pattern in KIND_WORDS:
        if re.search(pattern, low):
            # Checked here rather than before the loop, so an interception --
            # "ППО збила ракету над Києвом" -- still reaches the explosion
            # pattern above it. Only the kinds that would put an OBJECT in the
            # air are second-guessed; a post about air defence working names a
            # weapon and reports nothing flying.
            if kind in ("cruise", "ballistic", "drone", "jet_drone", "fpv",
                        "recon", "kab", "aircraft", "helicopter") \
                    and NOT_A_REPORT.search(low):
                return "unknown"
            return kind
    return "unknown"


# What a warning is ABOUT. Only meaningful for alerts and all-clears.
#
# "Lipetsk Oblast Drone Alert" and "Voronezh Oblast Missile Alert" are both
# warnings, and the difference between them is the difference between fifteen
# minutes and ninety seconds. The kind patterns above deliberately read both as
# "alert" -- the weapon word there says what the warning is about, not what is
# in the air -- so it is read here instead and drawn as colour: yellow for a
# drone warning, red for a missile one.
CAUSE_WORDS: tuple[tuple[str, str], ...] = (
    ("missile",
     r"ракет\w*|баліст|баллист|крилат|крылат|калібр|калибр|іскандер|искандер"
     r"|кинжал|кинджал"
     r"|\bmissiles?\b|\bballistic\b|\bcruise\b|\biskander\b|\bkinzhal\b"
     r"|\bkalibr\b|\brocket\b"),
    ("drone",
     r"бпла|шахед|шахид|герань|дрон|безпілотн|беспилотн"
     r"|\buavs?\b|\bdrones?\b|\bshahed\b|\bgeran\b|\bunmanned\b"),
)


def find_cause(text: str) -> str | None:
    """Which weapon a warning is about, or None if it does not say.

    Missile first. A post naming both -- "drone and missile threat" -- is
    warning about the faster and more dangerous of the two, and a warning
    drawn as the milder of two stated threats is the wrong way to be wrong.
    """
    low = str(text or "").lower()
    for cause, pattern in CAUSE_WORDS:
        if re.search(pattern, low):
            return cause
    return None


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
            # The oblast THIS phrase names, rather than whichever oblast the
            # sentence mentions first. "над Сумщиною" is Sumy oblast even in
            # a sentence that goes on to name another one, and deferring to
            # the text-wide answer was how a position became a destination.
            named = oblast_named(found)
            if named:
                return named
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
        # "в Курской области" is the region, which find_region has already
        # read properly as "Курская область". Taking the adjective on its own
        # gives "Курской", which no gazetteer holds, so the warning went
        # unplaced. The prefix test below could not see it: it compares six
        # characters, and "курско" against "курская" differs at the fifth.
        # What tells a region from a town is the type word after it, so that
        # is what is looked at.
        if OBLAST_FULL.match(text[match.start("what"):]):
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
    "fpv": "FPV drone",
    "cruise": "Cruise missile", "ballistic": "Ballistic missile",
    "aircraft": "Aircraft", "helicopter": "Helicopter",
    "explosion": "Explosions reported", "alert": "Air alert",
    # The all-clear had no wording at all, so it fell through to the default
    # and a lifted warning read "Report over Республика Башкортостан" -- a
    # line that says nothing about the one fact in it, which is that the
    # warning ENDED. It is the commonest post on the Russian radar channel.
    "all_clear": "All clear",
    # Two names for one thing, and both are needed. "kab" is what the reader
    # returns, "bomb" is what the map calls it after tracker.FOLD; a line is
    # written from the reader's name, before the fold.
    "kab": "Guided bomb",
    "bomb": "Guided bomb",
    "unknown": "Unidentified",
}

WAYS = {"N": "north", "NE": "north-east", "E": "east", "SE": "south-east",
        "S": "south", "SW": "south-west", "W": "west", "NW": "north-west"}


def summarise(kind: str, place: str | None, toward: str | None,
              course: str | None, count: int) -> str:
    what = SAYS.get(kind, "Report")
    if count > 1 and kind not in ("alert", "all_clear"):
        # Not for a warning. "3 × air alert in Sumy oblast" is not a thing
        # that can happen: a province is under a warning or it is not.
        what = f"{count} × {what.lower()}"
    if kind == "all_clear":
        # "All clear — Sumy oblast", not "All clear in Sumy oblast". It reads
        # as a statement about the region rather than as something happening
        # inside it, which is what a stand-down is.
        where = f" — {place}" if place else ""
    else:
        where = f" over {place}" if place and kind not in (
            "explosion", "alert") else (f" in {place}" if place else "")
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


# ---------------------------------------------------------------------------
# Digest posts: one message, many places
# ---------------------------------------------------------------------------
#
# The most-read post on these channels is not one report. It is the running
# movement summary, and it looks like this:
#
#   Щодо руху ударних БпЛА:
#   🛸 Сумщина:
#   🛩 БпЛА в р-ні н.п. Путивль, Глухів, Кролевець, Буринь та Лебедин
#      рухаються західним курсом;
#   🛸 Чернігівщина:
#   🛩 БпЛА в р-ні н.п. Батурин, Сосниця, Ніжин, Козелець та Гончарівське...
#
# Fifteen settlements across five oblasts, every one with a stated course.
# read() returned ONE reading for it -- on whichever oblast matched last, with
# no course at all -- so the busiest post of the night drew a single courseless
# ring in the middle of a province and fifteen towns went undrawn.
#
# Both halves had a cause. The place came from a pattern that finds the first
# plausible name and stops. The course was missing because these posts write it
# in the instrumental -- "рухаються західним курсом" -- and find_heading() only
# looks for "курсом на <point>".
#
# This reads the shape instead: split at the oblast headings, and within each
# section take the list of settlements and the course that section states.

# An oblast heading inside a digest: "Сумщина:", "Київська область:". The colon
# is what makes it a heading rather than a mention, and what separates one
# section from the next.
SECTION = re.compile(
    r"(?:^|[\s;.])(?P<name>[А-ЯІЇЄҐЁ][А-Яа-яІЇЄҐЁіїєґё'’\-]*щин[аиуі]"
    r"|[А-ЯІЇЄҐЁ][А-Яа-яІЇЄҐЁіїєґё'’\-]+\s+обл(?:асть|асти|\.)?)\s*:")

# What introduces the list of settlements in a section. "н.п." is "населений
# пункт" -- settlement -- and is how these posts always write it.
ROLL = re.compile(
    r"(?:н\.?\s*п\.?|в\s+р[-—–]?ні|у\s+р[-—–]?ні|в\s+районі|у\s+районі"
    r"|в\s+районе|поблизу|над|біля)\s*(?P<list>.+)", re.I | re.S)

# The "settlement" marker itself, stripped off the head of a list once the
# pattern above has found it.
MARKER = re.compile(r"^\s*(?:н\.?\s*п\.?|нп)\s*", re.I)

# The separators inside such a list, in both languages.
BETWEEN = re.compile(r"\s*(?:[,;]|\bта\b|\bі\b|\bй\b|\bи\b|\band\b)\s*", re.I)

# Words that appear inside a settlement list and are not settlements: the verb
# and the course phrase the section ends with.
NOT_A_SETTLEMENT = re.compile(
    r"рух\w*|курс\w*|прямую\w*|лет\w*|напрям\w*|бпла|шахед\w*|ракет\w*"
    r"|moving|course|heading|drone|uav", re.I)

# Geography that is not a settlement. "в р-ні Київського водосховища" is the
# Kyiv Reservoir -- a hundred kilometres of water, in the genitive, that no
# gazetteer will answer for as written. The oblast the section names is the
# honest place for that mark.
NOT_A_PLACE = re.compile(
    r"водосховищ|водохранилищ|річк|реки|озер|лиман|заток|мор[еяію]"
    r"|кордон|границ|акватор|reservoir|river|\blake\b|\bbay\b", re.I)

# Enough for the longest real digest, and a ceiling so a malformed post cannot
# turn into a hundred marks.
MOST_PER_SECTION = 12


def _settlements(section: str) -> list[str]:
    """The place names listed in one section of a digest."""
    roll = ROLL.search(section)
    if not roll:
        return []
    # "в р-ні н.п. Путивль" matches on "в р-ні", leaving "н.п." at the head of
    # the list. It is lowercase, so the first chunk failed the capital-letter
    # test and the whole list was abandoned at its first entry -- every digest
    # fell back to its oblast centre and fifteen towns went undrawn.
    listed = MARKER.sub("", roll.group("list"), count=1)
    out: list[str] = []
    for chunk in BETWEEN.split(listed):
        name = _tidy(chunk)
        if not name:
            break
        # The last name in a list carries the section's course phrase with no
        # comma before it -- "Лебедин рухаються західним курсом" -- so the
        # phrase is cut off the name BEFORE the name is judged. Checking first
        # and cutting second threw away the last settlement of every section.
        name = NOT_A_SETTLEMENT.split(name)[0].strip(" .,;:-—–()")
        if not name or not name[0].isupper():
            # The list has ended: what follows is the course phrase, the next
            # sentence, or punctuation.
            break
        if len(name) < 3 or name.lower() in NOT_PLACES:
            continue
        if NOT_A_PLACE.search(name):
            continue
        if name not in out:
            out.append(name)
        if len(out) >= MOST_PER_SECTION:
            break
    return out


# A post that names several regions and then says one thing about all of them:
#
#   Republic of Tatarstan, Republic of Bashkortostan — UAV alert cleared.
#   Chuvash Republic, Mari El Republic — UAV alert cleared.
#   Белгородская, Курская области — отбой угрозы БпЛА
#
# The Russian radar channels write most of their stand-downs this way, and
# read() took the first region and dropped the rest -- so a warning lifted
# across four provinces was lifted on the map across one.
REGION_KIND = (
    r"[Oo]blast|[Rr]egion|[Rr]epublic|[Kk]rai|[Oo]krug"
    r"|обл(?:асть|асти|\.)|респ(?:ублика|ублики|\.)|край|округ"
)
LISTED_REGIONS = re.compile(
    rf"^(?P<list>[^—–\-:;.]*?(?:{REGION_KIND})[^—–:;.]*?)\s*[—–-]\s*(?P<says>.+)$")


# The same list with no dash in it: "Ivanovo Oblast, Vladimir Oblast, Drone
# Alert". The Russian radar channel writes them this way -- commas all the
# way, with the thing being said as the last item -- and the dashed pattern
# above cannot see it, so a post naming three oblasts raised a warning over
# the first and left the other two with nothing.
#
# Told apart by the type word. Every region in the run has to carry one --
# Oblast, Region, Republic, Krai, область -- and the tail has to lack one.
# That is stricter than the dashed form, which allows a trailing region to
# drop its type word, and deliberately: without a dash there is nothing else
# to say where the list of places stops and the sentence about them begins.
# A full stop ends the list as readily as a comma: "Belgorod Oblast, Voronezh
# Oblast. Missile alert." A sentence boundary rather than any dot, so it takes
# a capital after it.
#
# There were lookbehinds here guarding "обл." and "респ." from being split at
# their own dots. They were removed after being measured: a chunk that loses
# its dot reads as a bare "обл", REGION_KIND does not match that, so the run
# of regions fails to form either way and every probe gave the same answer
# with them and without. A guard that cannot change an answer is a guard
# against nothing.
SENTENCE = re.compile(r"\.\s+(?=[A-ZА-ЯЁ])")


def _run_of_regions(text: str) -> tuple[list[str], str] | None:
    pieces = []
    for part in BETWEEN.split(text.rstrip(" .")):
        pieces.extend(SENTENCE.split(part))
    chunks = [_tidy(c) for c in pieces]
    names: list[str] = []
    for i, chunk in enumerate(chunks):
        if chunk and chunk[0].isupper() and re.search(REGION_KIND, chunk):
            names.append(chunk)
            continue
        # The first thing that is not a region ends the list, and everything
        # from there on is what was said about them.
        rest = " ".join(c for c in chunks[i:] if c)
        # Two, because one region is not a list -- read() handles that post
        # and gives the same answer. Kept as the definition of the thing
        # rather than because it is currently observable.
        return (names, rest) if len(names) >= 2 and rest else None
    # Every chunk was a region and nothing was said about them, which is a
    # list of places rather than a report.
    return None


def _listed(text: str) -> tuple[list[str], str] | None:
    """The regions a post names before its dash, and what it says about them."""
    match = LISTED_REGIONS.match(text)
    if not match:
        return _run_of_regions(text)
    names = []
    for chunk in BETWEEN.split(match.group("list")):
        name = _tidy(chunk)
        if not name or not name[0].isupper():
            continue
        if not re.search(REGION_KIND, name):
            # "Republic of Tatarstan, Bashkortostan" -- the second one drops
            # the type word and is still a region. Kept, because the pattern
            # above already established that this line is a list of them.
            if len(names) == 0:
                return None
        names.append(name)
    return (names, match.group("says")) if len(names) >= 2 else None


def read_all(text: str) -> list[dict[str, Any]]:
    """Every report in one post. Usually one; for a digest, one per place.

    Ordinary posts go straight to read() and come back as a list of one, so
    every caller can treat a post as a list and the common case costs one
    regular expression that does not match.
    """
    text = " ".join(str(text or "").split())

    # Several regions, one thing said about all of them. Checked before the
    # digest split because it is a different shape -- a comma list ahead of a
    # dash rather than headings with colons -- and because the thing being
    # said is usually a stand-down, which has to reach every region named or
    # the warnings stay up on the ones it missed.
    listed = _listed(text)
    if listed:
        names, says = listed
        kind = find_kind(text)
        if kind != "unknown":
            cause = (find_cause(text)
                     if kind in ("alert", "all_clear") else None)
            out = []
            for name in names:
                region = find_region(name) or name
                said = _canonical(region)
                out.append({
                    "kind": kind, "cause": cause, "place": region,
                    "region": None, "toward": None, "course": None,
                    "count": 1,
                    "summary": summarise(kind, said, None, None, 1),
                    "by": "rules",
                })
            if out:
                return out

    heads = list(SECTION.finditer(text))
    if len(heads) < 2:
        # One heading is an ordinary report that happens to name its oblast.
        # It takes two to be a digest.
        one = read(text)
        return [one] if one else []

    out: list[dict[str, Any]] = []
    for i, head in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        section = text[head.end():end]
        region = find_region(head.group("name")) or find_region(section)
        if not region:
            continue
        kind = find_kind(section)
        if kind == "unknown":
            kind = find_kind(text)
        if kind == "unknown":
            continue
        # The course is stated once per section and applies to everything in
        # it: "рухаються західним курсом" -- moving on a westerly course.
        course = None if kind in ("explosion", "alert") else find_course(section)
        found = _settlements(section)
        if not found:
            # A section naming no settlement is still a report about the
            # oblast, and the province is the honest answer rather than
            # nothing.
            found = [region]
        for name in found:
            said = _canonical(name)
            out.append({
                "kind": kind,
                "cause": (find_cause(section) or find_cause(text)
                          if kind in ("alert", "all_clear") else None),
                "place": name,
                "region": region if region != name else None,
                "toward": None,
                "course": course,
                "count": 1,
                "summary": summarise(kind, said, None, course, 1),
                "by": "rules",
            })
    if not out:
        one = read(text)
        return [one] if one else []
    return out


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

    course, toward = (None, None) if kind in ("explosion", "alert") else find_heading(text)

    # Where it IS, which is not where it is going.
    #
    # "Реактивний БпЛА через зону відчуження Чорнобильської АЕС курсом на
    # Житомирщину" says both, and this used to take Zhytomyr oblast -- the
    # destination -- as the position. The mark went on the destination and the
    # popup said "over Житомирська область, heading for Житомирська область":
    # a thing drawn where it has not got to yet, claiming to be heading for
    # where it already is.
    #
    # The fix turned out to be in the patterns rather than here. "через" was
    # missing from the prepositions, and a name could not be found behind a
    # lowercase word or two ("через ЗОНУ ВІДЧУЖЕННЯ Чорнобильської АЕС"), so
    # the report named no position at all and the destination was all that was
    # left to find.
    #
    # Two things were tried here first and are deliberately NOT kept. Cutting
    # the sentence at the course phrase broke the English word order, where
    # the course comes first ("UAV heading west past Kaharlyk"). Blanking the
    # destination name turned out to be unreachable -- no pattern here can
    # match the name after "курсом на", because AT's preposition class is a
    # single character and cannot match a two-letter "на". Keeping code that
    # guards against nothing, with a comment saying what it guards against, is
    # worse than not having it: the comment is then the only evidence, and it
    # is wrong. If AT is ever widened to multi-letter prepositions, this is
    # where the destination would start being read as the position.
    region = find_region(text)
    place = find_place(text, region)
    if not place and not toward and not course:
        # A report in a script whose place names this cannot read is still a
        # report, and listing it beats the silence those channels used to get.
        # Anything else with a kind and nothing else -- a fundraising post
        # that mentions Shaheds, a statistic, a headline -- is not a report of
        # anything happening.
        if not OTHER_SCRIPT.search(text):
            return None
    # Heading for where it already is, compared by what the names RESOLVE to
    # rather than by the strings.
    #
    # "Житомирщину" and "Житомирська область" are the same province written
    # two ways, so a string comparison said they were different and the popup
    # claimed a journey from a place to itself.
    if toward and place and (
            toward.lower() == place.lower()
            or (_canonical(toward) or "").lower() == (_canonical(place) or "").lower()):
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
        # What the warning is about, for alerts and all-clears only. Drawn as
        # colour -- yellow for a drone warning, red for a missile one -- which
        # is the difference between fifteen minutes and ninety seconds.
        "cause": find_cause(text) if kind in ("alert", "all_clear") else None,
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
