"""Russia's federal subjects, read out of the radar channel's posts.

The channel posts in Russian now, region first, a column of names and then one
line saying what about them. The general reader was built around commas and
dashes and the first region in a sentence, so two regions on a post kept the
first, "Республика Татарстан Опасность" took the next word into the name,
the Ossetian republic was cut at its dash and "Москва" produced nothing.

These are the posts as the channel writes them.
"""

from __future__ import annotations

import pytest

from backend import neighbours, places, reports, subjects


def said(text):
    return [(r["subject"], r["kind"]) for r in
            subjects.read(text, reports.find_kind, reports.find_cause)]


class TestTheChannelsOwnPosts:
    @pytest.mark.parametrize("post, expected", [
        ("Тамбовская область Опасность по БПЛА",
         [("Тамбовская область", "alert")]),
        # Two regions and no separator: the second used to be dropped.
        ("Саратовская область Пензенская область Отбой опасности по БПЛА",
         [("Саратовская область", "all_clear"),
          ("Пензенская область", "all_clear")]),
        ("Саратовская область\nПензенская область\nОтбой опасности по БПЛА",
         [("Саратовская область", "all_clear"),
          ("Пензенская область", "all_clear")]),
        ("Курская область Белгородская область Опасность по БПЛА",
         [("Курская область", "alert"), ("Белгородская область", "alert")]),
        # Type word first, and the next word used to be taken into the name.
        ("Республика Татарстан Опасность по БПЛА",
         [("Республика Татарстан", "alert")]),
        ("Республика Башкортостан Чувашская Республика Опасность по БПЛА",
         [("Республика Башкортостан", "alert"),
          ("Чувашская Республика", "alert")]),
        # The spaced dash inside the name used to cut it in half.
        ("Ставропольский край, Республика Северная Осетия - Алания "
         "Опасность по БПЛА",
         [("Ставропольский край", "alert"),
          ("Республика Северная Осетия — Алания", "alert")]),
        # A federal city, which produced nothing at all.
        ("Москва Опасность по БПЛА", [("город Москва", "alert")]),
        ("Санкт-Петербург Опасность по БПЛА",
         [("город Санкт-Петербург", "alert")]),
        ("Навля, Брянская область - опасность БПЛА",
         [("Брянская область", "alert")]),
        ("Кабардино-Балкарская Республика, Карачаево-Черкесская Республика "
         "Опасность по БПЛА",
         [("Кабардино-Балкарская Республика", "alert"),
          ("Карачаево-Черкесская Республика", "alert")]),
        ("ХМАО Опасность по БПЛА",
         [("Ханты-Мансийский автономный округ — Югра", "alert")]),
        # The older sentence forms, which must keep working.
        ("Опасность по БПЛА в Ростовской и Воронежской областях",
         [("Ростовская область", "alert"), ("Воронежская область", "alert")]),
        ("Белгородская, Курская и Брянская области — отбой угрозы БпЛА",
         [("Белгородская область", "all_clear"),
          ("Курская область", "all_clear"),
          ("Брянская область", "all_clear")]),
    ])
    def test_every_region_and_what_was_said_about_it(self, post, expected):
        assert said(post) == expected

    def test_what_a_warning_is_about_comes_with_it(self):
        got = subjects.read("Ростовская область Ракетная опасность",
                            reports.find_kind, reports.find_cause)
        assert [(r["subject"], r["cause"]) for r in got] == [
            ("Ростовская область", "missile")]


class TestEachStatementSaysItsOwnThing:
    def test_one_lifted_and_one_kept_in_the_same_post(self):
        # Read as one post, this was a stand-down for both.
        assert said("Отбой опасности в Курской области. "
                    "В Белгородской области опасность сохраняется") == [
            ("Курская область", "all_clear"), ("Белгородская область", "alert")]

    def test_a_region_in_a_statement_about_something_else_stays_there(self):
        # Kursk is where drones were shot down; the warning is Bryansk's.
        assert said("Над Курской областью сбито 5 БПЛА. "
                    "Опасность сохраняется в Брянской области") == [
            ("Брянская область", "alert")]

    def test_names_before_a_statement_about_something_else_go_with_it(self):
        # Kursk is the column above "сбито"; the warning line names its own.
        assert said("Курская область\nСбито 5 БПЛА\n"
                    "Белгородская область Опасность по БПЛА") == [
            ("Белгородская область", "alert")]

    def test_a_column_of_names_takes_the_line_after_it(self):
        assert said("Опасность по БПЛА\nКурская область\nОрловская область") \
            == [("Курская область", "alert"), ("Орловская область", "alert")]

    def test_the_later_statement_about_one_region_wins(self):
        assert said("Курская область Опасность по БПЛА. "
                    "Курская область Отбой опасности по БПЛА") == [
            ("Курская область", "all_clear")]


class TestItReadsOnlyWhatItShould:
    def test_not_a_warning_is_not_read_here(self):
        assert said("Над Курской областью сбито 5 БПЛА") == []

    def test_a_ukrainian_post_is_left_to_the_ukrainian_reader(self):
        # "з Курської області" is where a drone came FROM; the warning in a
        # Ukrainian post is over a Ukrainian oblast.
        assert said("Повітряна тривога в Сумській області, "
                    "БпЛА з Курської області") == []
        # And the nickname, which the two languages share.
        assert said("Повітряна тривога на Сумщині, БпЛА з Курщини") == []

    @pytest.mark.parametrize("text, expected", [
        # Inside another name.
        ("Томская область", ["Томская область"]),
        ("Ямало-Ненецкий автономный округ", ["Ямало-Ненецкий автономный округ"]),
        # An adjective with no type word after it is not a region.
        ("Курская АЭС", []),
        ("Кировск", []),
        # The city and the oblast around it are different subjects.
        ("Московская область", ["Московская область"]),
        ("в Москве", ["город Москва"]),
        # The city of Stavropol is a town in the krai; "Ставрополье" is the krai.
        ("в Ставрополе", []),
        ("Ставрополье", ["Ставропольский край"]),
        ("Республика Алтай", ["Республика Алтай"]),
        ("Алтайский край", ["Алтайский край"]),
    ])
    def test_names_are_told_apart(self, text, expected):
        assert subjects.find_all(text) == expected


class TestEveryNameLandsSomewhere:
    def test_every_subject_is_in_the_place_table(self):
        # So a reading resolves with no request at all.
        assert [n for n in subjects.SUBJECTS if not places.lookup(n)] == []

    def test_every_subject_but_two_has_an_outline(self):
        # Crimea and Sevastopol ship no outline, in either direction -- see
        # neighbours.py. Everything else shades.
        assert {n for n in subjects.SUBJECTS if not neighbours.shape_for(n)} \
            == {"Автономна Республіка Крим", "Севастополь"}

    def test_every_shipped_russian_region_can_be_named(self):
        # The other direction: a province with an outline and no pattern
        # could only ever be shaded by the general reader.
        russian = {n for n, row in neighbours.provinces().items()
                   if row["in"] == "ru"}
        assert russian - set(subjects.SUBJECTS) == set()


class TestTheLineAPersonReads:
    def summaries(self, text):
        return [r["summary"] for r in reports.read_all(text)]

    def test_the_town_is_said_beside_the_region(self):
        assert self.summaries("Навля, Брянская область - опасность БПЛА") == [
            "Air alert in Брянская область (Навля)"]

    def test_half_of_a_name_is_not_a_town(self):
        assert self.summaries("Санкт-Петербург Опасность по БПЛА") == [
            "Air alert in Санкт-Петербург"]

    def test_a_federal_city_is_called_by_its_name(self):
        assert self.summaries("Москва Опасность по БПЛА") == [
            "Air alert in Москва"]

    def test_each_reading_carries_its_subject(self):
        got = reports.read_all("Курская область Белгородская область "
                               "Опасность по БПЛА")
        assert [(r["place"], r["subject"]) for r in got] == [
            ("Курская область", "Курская область"),
            ("Белгородская область", "Белгородская область")]
