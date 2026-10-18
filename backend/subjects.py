"""Russia's federal subjects, found in a post however the post names them.

The Russian radar channel stopped writing English headlines and now posts in
Russian, region first, one line each:

    Саратовская область
    Пензенская область
    Отбой опасности по БПЛА

    Республика Татарстан Опасность по БПЛА
    Ставропольский край, Республика Северная Осетия - Алания Опасность по БПЛА
    Москва Опасность по БПЛА

The general reader in reports.py finds the FIRST region in a sentence and is
built around commas and dashes, so these came out wrong in every way a
warning can: two regions on one post kept the first and dropped the second,
"Республика Татарстан Опасность" swallowed the next word into the name, the
hyphenated Ossetian name was cut in half, and "Москва" on its own produced
nothing. A stand-down that reaches one region of two leaves the other shaded
for hours; a declaration that reaches none leaves the map blank.

Russia has eighty-odd subjects and they are a closed list, so -- the same
bargain the oblast nicknames make -- they are simply listed, each with the
forms it is written in. A post is searched for every one of them, and each
found is a warning or a stand-down of its own.

Every canonical name here is a row in places.REGIONS (or, for Crimea and
Sevastopol, a place the table already knows), so a reading resolves without a
request and finds its outline in the shipped provinces file.
"""

from __future__ import annotations

import re
from typing import Any

# What follows an adjective when it is a region's name: the type word, perhaps
# after more adjectives that share it -- "Белгородская, Курская и Брянская
# области" names three oblasts and writes "области" once.
_TYPE_WORD = (
    r"(?=(?:\s*(?:,|и\b)\s*[а-яё\-]+(?:ая|ой|ую|ий|ого|ому|ом|ие|их|ым))*"
    r"\s+(?:обл|кра[йяюе]|республик|автономн|ао\b))"
)

# Not inside another word: "омск" is in "томск", "ненецк" in "ямало-ненецк".
_START = r"(?<![а-яё\-])"


def _adj(stem: str) -> str:
    """An adjectival subject name -- "Курская область" in any case."""
    return rf"{_START}{stem}\w*{_TYPE_WORD}"


# canonical name -> the forms it is written in, as one pattern on lower case.
SUBJECTS: dict[str, str] = {
    # ── oblasts ───────────────────────────────────────────────────
    "Белгородская область": _adj("белгородск") + r"|белгородчин\w*",
    "Брянская область": _adj("брянск") + r"|брянщин\w*",
    "Курская область": _adj("курск") + r"|курщин\w*",
    "Воронежская область": _adj("воронежск") + r"|воронежчин\w*",
    "Ростовская область": _adj("ростовск") + r"|ростовщин\w*",
    "Орловская область": _adj("орловск") + r"|орловщин\w*",
    "Липецкая область": _adj("липецк"),
    "Тульская область": _adj("тульск"),
    "Рязанская область": _adj("рязанск"),
    "Тамбовская область": _adj("тамбовск") + r"|тамбовщин\w*",
    "Смоленская область": _adj("смоленск") + r"|смоленщин\w*",
    "Калужская область": _adj("калужск"),
    "Московская область": _adj("московск") + r"|подмосков\w*",
    "Нижегородская область": _adj("нижегородск"),
    "Пензенская область": _adj("пензенск"),
    "Саратовская область": _adj("саратовск"),
    "Волгоградская область": _adj("волгоградск"),
    "Астраханская область": _adj("астраханск"),
    "Ульяновская область": _adj("ульяновск"),
    "Самарская область": _adj("самарск"),
    "Оренбургская область": _adj("оренбургск"),
    "Владимирская область": _adj("владимирск"),
    "Ярославская область": _adj("ярославск"),
    "Ивановская область": _adj("ивановск"),
    "Костромская область": _adj("костромск"),
    "Тверская область": _adj("тверск"),
    "Псковская область": _adj("псковск"),
    "Новгородская область": _adj("новгородск"),
    "Ленинградская область": _adj("ленинградск") + r"|ленобласт\w*",
    "Вологодская область": _adj("вологодск"),
    "Архангельская область": _adj("архангельск"),
    "Мурманская область": _adj("мурманск"),
    "Калининградская область": _adj("калининградск"),
    "Кировская область": _adj("кировск"),
    "Курганская область": _adj("курганск"),
    "Челябинская область": _adj("челябинск"),
    "Свердловская область": _adj("свердловск"),
    "Тюменская область": _adj("тюменск"),
    "Омская область": _adj("омск"),
    "Новосибирская область": _adj("новосибирск"),
    "Томская область": _adj("томск"),
    "Кемеровская область": _adj("кемеровск") + r"|\bкузбасс\w*",
    "Иркутская область": _adj("иркутск"),
    "Амурская область": _adj("амурск"),
    "Магаданская область": _adj("магаданск"),
    "Сахалинская область": _adj("сахалинск"),
    "Еврейская автономная область": _adj("еврейск") + r"|\bеао\b",
    # ── krais ─────────────────────────────────────────────────────
    "Краснодарский край": _adj("краснодарск") + r"|\bкубан(?:ь|и|ью)\b",
    # "Ставрополье" and not "Ставрополь": the city is a point in the krai,
    # and a warning naming it is read where it is -- as a town -- by the
    # general reader, which then shades the krai around it.
    "Ставропольский край": _adj("ставропольск") + r"|ставрополь[еяю]\b",
    "Пермский край": _adj("пермск") + r"|прикамь\w*",
    "Алтайский край": _adj("алтайск"),
    "Красноярский край": _adj("красноярск"),
    "Забайкальский край": _adj("забайкальск"),
    "Хабаровский край": _adj("хабаровск"),
    "Приморский край": _adj("приморск") + r"|\bприморь\w*",
    "Камчатский край": _adj("камчатск"),
    # ── republics ─────────────────────────────────────────────────
    "Республика Татарстан": r"татарстан\w*",
    "Республика Башкортостан": r"башкортостан\w*|башкири\w*",
    "Чувашская Республика": _adj("чувашск") + r"|чуваши(?:я|и|ю|ей)\b",
    "Республика Марий Эл": r"марий\s+эл\b",
    "Удмуртская Республика": _adj("удмуртск") + r"|удмурти\w*",
    "Республика Мордовия": r"мордови\w*",
    "Республика Калмыкия": r"калмыки\w*",
    "Республика Коми": r"\bкоми\b",
    "Республика Карелия": r"карели(?:я|и|ю|ей)\b",
    "Республика Адыгея": r"адыге(?:я|и|ю|ей)\b",
    "Республика Дагестан": r"дагестан\w*",
    "Республика Алтай": r"республик\w*\s+алтай\b|горн\w+\s+алта\w*",
    "Карачаево-Черкесская Республика": r"карачаево[\s\-]*черкес\w*",
    "Кабардино-Балкарская Республика": r"кабардино[\s\-]*балкар\w*",
    # Both halves, because posts write either or both -- "Северная Осетия -
    # Алания" has a spaced dash in it that cut the name in two before.
    "Республика Северная Осетия — Алания": r"северн\w+\s+осети\w*|\bалани\w*",
    "Республика Ингушетия": r"ингушети\w*",
    "Чеченская Республика": r"чечн\w*|чеченск\w*",
    "Республика Бурятия": r"буряти\w*",
    "Республика Тыва": r"\bтыв(?:а|ы|е|у)\b",
    "Республика Хакасия": r"хакаси\w*",
    "Республика Саха (Якутия)": r"якути\w*|\bсаха\b",
    # ── autonomous okrugs ────────────────────────────────────────
    "Ханты-Мансийский автономный округ — Югра":
        _adj("ханты-мансийск") + r"|\bхмао\b|\bюгр(?:а|е|ы|у)\b",
    "Ямало-Ненецкий автономный округ": _adj("ямало-ненецк") + r"|\bянао\b",
    "Ненецкий автономный округ": _adj("ненецк"),
    # ── federal cities ───────────────────────────────────────────
    # Filed as "город ..." -- see places.ELSEWHERE_REGIONS. "Москва Опасность
    # по БПЛА" is a warning over the federal city, and it produced nothing.
    "город Москва": r"\bмоскв(?:а|ы|е|у|ой|ою)\b",
    "город Санкт-Петербург": r"санкт-петербург\w*|\bпетербург\w*|\bспб\b",
    # And the two the radar channel calls Russian, which this app ships no
    # outline for in either direction. Found anyway, so a stand-down for one
    # is a stand-down and not a stray word.
    "Автономна Республіка Крим": r"\bкрым(?:а|у|е|ом)?\b|республик\w*\s+крым\w*",
    "Севастополь": r"севастопол\w*",
}

_COMPILED = [(name, re.compile(pattern)) for name, pattern in SUBJECTS.items()]

# Ukrainian letters. A Ukrainian post is not one of these -- its regions are
# Ukraine's, read by reports.py with the nicknames it was built around -- and
# the stems here would only find the Russian region it says a drone came FROM.
UKRAINIAN = re.compile(r"[іїєґ]", re.I)

# Where one statement ends and the next begins. A newline is the commonest:
# these posts are a column of regions and then a line saying what about them.
CLAUSE = re.compile(r"[.!?;\n]+")


def spans(text: Any) -> list[tuple[int, int, str]]:
    """Where each subject is named in a piece of text: (start, end, name).

    Lower-casing and folding ё leave every letter where it was, so these
    index the text as given.
    """
    low = str(text or "").lower().replace("ё", "е")
    # No two of these can match the same letters. The ones that could --
    # "омск" is inside "томск", "ненецк" inside "ямало-ненецк" -- are kept
    # apart by _START, and a test holds each pair.
    return sorted((match.start(), match.end(), name)
                  for name, pattern in _COMPILED
                  for match in pattern.finditer(low))


def find_all(text: Any) -> list[str]:
    """Every subject a piece of text names, in the order it names them."""
    return list(dict.fromkeys(name for _, _, name in spans(text)))


def read(text: Any, kind_of, cause_of) -> list[dict[str, Any]]:
    """Each subject a warning post names, with what it says about that one.

    `kind_of` and `cause_of` are reports.find_kind and find_cause, passed in
    rather than imported so the two modules do not import each other.

    Returns [] for anything that is not a warning or a stand-down over named
    subjects, which leaves it to the general reader.

    The kind is read per statement rather than once for the post. "Отбой в
    Курской области, в Белгородской опасность сохраняется" lifts one and
    keeps the other, and reading the whole post as a stand-down would lift
    both. A statement that names regions and says nothing about them -- the
    column of names above "Отбой опасности по БПЛА" -- takes the kind of the
    next statement that does, or failing that the last one before it.
    """
    raw = str(text or "")
    if not raw.strip() or UKRAINIAN.search(raw):
        return []
    out: list[dict[str, Any]] = []
    waiting: list[str] = []
    last: tuple[str, str | None] | None = None
    for clause in CLAUSE.split(raw):
        names = find_all(clause)
        kind = kind_of(clause) if clause.strip() else "unknown"
        if kind not in ("alert", "all_clear", "unknown"):
            # A statement about something else -- "над Курской областью сбито
            # 5 БПЛА" -- owns the regions it names. Carried forward, they
            # would be put under the next warning in the post.
            waiting = []
            continue
        waiting += [n for n in names if n not in waiting]
        if kind == "unknown":
            continue
        last = (kind, cause_of(clause) or cause_of(raw))
        out += [{"subject": n, "kind": kind, "cause": last[1]} for n in waiting]
        waiting = []
    if waiting and last:
        out += [{"subject": n, "kind": last[0], "cause": last[1]} for n in waiting]
    # One reading per subject; if a post says two things about one region,
    # the later statement is the one in force.
    return list({row["subject"]: row for row in out}.values())


def is_subject(name: Any) -> bool:
    return name in SUBJECTS
