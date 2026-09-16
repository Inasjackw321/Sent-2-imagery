"""Tests for the NOTAM layer -- which airspace is shut, and when.

This module fetches nothing, which is the point of it. Three attempts at
fetching failed for three different reasons and there is no keyless worldwide
NOTAM service to move to, so the notices arrive as text somebody pasted. That
makes this reader the whole feature, and it makes these tests the whole
safety net: there is no service to blame for a wrong answer.

Most of what is asserted is about the two things that would put a closure in
the wrong place at the wrong size: a Q-line coordinate is degrees and minutes
run together with the hemisphere as a letter, and a Q-line radius is in
NAUTICAL MILES.
"""

from __future__ import annotations

import datetime as dt
import json
import time

import pytest

from backend import notams

# One notice, as every source that hands out the raw form writes it.
ONE = """A1234/26 NOTAMN
Q) UKBV/QRTCA/IV/BO/W/000/999/5020N03030E030
A) UKBV B) 2601150000 C) PERM
E) AIRSPACE CLOSED TO ALL CIVIL TRAFFIC. RWY 09/27 CLSD.
F) SFC
G) FL660"""


class TestTheCoordinates:
    """The one thing that puts a closure in the wrong country."""

    def test_the_q_line_form(self):
        assert notams.read_coord("4915N02330E") == (49.25, 23.5)
        assert notams.read_coord("491530N0233045E") == (49.25833, 23.5125)

    def test_both_hemispheres(self):
        assert notams.read_coord("3345S07030W") == (-33.75, -70.5)

    def test_and_it_refuses_anything_else(self):
        # Read as a number "4915N02330E" is forty-nine million, and read as
        # decimal degrees it is 4915. Either way the mark leaves the planet.
        for junk in ("49.25, 23.5", "4915", "", None, 4915, "NOT A COORDINATE",
                     "9915N02330E"):
            assert notams.read_coord(junk) is None, junk

    def test_the_dashed_form(self):
        assert notams.read_angle("49-15-00.000N") == 49.25
        assert notams.read_angle("070-30-00W") == -70.5
        for junk in ("49.25", "", None, 42, "49-15-"):
            assert notams.read_angle(junk) is None, junk


class TestOneRawNotice:
    def one(self, text=ONE):
        return notams.read_raw(text)

    def test_what_it_reads_out(self):
        got = self.one()
        assert got["id"] == "A1234/26"
        assert got["location"] == "UKBV"
        assert got["placed"] is True
        assert round(got["lat"], 4) == 50.3333
        assert round(got["lon"], 4) == 30.5

    def test_the_radius_is_nautical_miles(self):
        # 030 on the Q-line is thirty nautical miles. Read as kilometres the
        # closure comes out at a bit over half its real size.
        assert self.one()["radius_km"] == 55.56

    def test_a_missing_radius_gets_the_documented_default(self):
        got = notams.read_raw(ONE.replace("5020N03030E030", "5020N03030E"))
        assert got["radius_km"] == round(5.0 * 1.852, 2)

    def test_the_text_is_the_e_line_and_stops_at_the_next_one(self):
        said = self.one()["text"]
        assert said.startswith("AIRSPACE CLOSED TO ALL CIVIL TRAFFIC")
        # F) and G) are levels, not part of what the notice says.
        assert "SFC" not in said and "FL660" not in said

    def test_the_dates(self):
        got = self.one()
        assert got["from"] == dt.datetime(
            2026, 1, 15, tzinfo=dt.timezone.utc).timestamp()
        # PERM is no end at all, which is not an end in the past.
        assert got["to"] is None
        assert notams.in_force(got, got["from"] + 86_400) is True

    def test_a_notice_with_no_coordinate_is_read_but_not_placed(self):
        got = notams.read_raw(
            "A9999/26 NOTAMN\nQ) UKBV/QXXXX/IV/BO/W/000/999/\n"
            "A) UKBV B) 2601150000 C) PERM\nE) NO POSITION IN THIS ONE")
        assert got["id"] == "A9999/26"
        assert got["placed"] is False
        assert got["why_unplaced"]

    def test_a_coordinate_in_the_text_is_not_read_as_the_position(self):
        # Runs of digits shaped like a coordinate turn up in E) lines as
        # bearings and boundary lists. Reading one as the position puts the
        # closure somewhere nobody filed it.
        got = notams.read_raw(
            "A1239/26 NOTAMN\nQ) UKBV/QRTCA/IV/BO/W/000/999/\n"
            "A) UKBV B) 2601150000 C) PERM\n"
            "E) AREA BOUNDED BY 5020N03030E THEN 5100N03100E")
        assert got["placed"] is False

    def test_rubbish_is_refused_rather_than_guessed_at(self):
        assert notams.read_raw("") is None
        assert notams.read_raw("nothing here at all") is None
        # A number but nothing said: a mark with no text is a dot nobody can
        # act on.
        assert notams.read_raw("A1234/26 NOTAMN\nA) UKBV") is None


class TestSplittingAPaste:
    """A person pastes a page, not one notice."""

    def test_several_notices_come_out_separately(self):
        parts = notams.split_raw(ONE + "\n\n" + ONE.replace("A1234", "A1235"))
        assert len(parts) == 2
        assert "A1234/26" in parts[0] and "A1235/26" in parts[1]

    def test_a_reference_inside_a_notice_does_not_start_a_new_one(self):
        """"NOTAMR A1233/26" is a notice REPLACING another.

        Splitting on it would cut one notice in half and lose the Q-line off
        the second piece, which is the position -- so a real closure would go
        from placed to unplaced for the sake of a cross-reference.
        """
        text = ("A1234/26 NOTAMR A1233/26\n"
                "Q) UKBV/QRTCA/IV/BO/W/000/999/5020N03030E030\n"
                "A) UKBV B) 2601150000 C) PERM\nE) SOMETHING")
        assert len(notams.split_raw(text)) == 1
        assert notams.read_raw(text)["placed"] is True

    def test_a_numbered_list_still_splits(self):
        text = f"1. {ONE}\n\n2. {ONE.replace('A1234', 'A1235')}"
        assert len(notams.split_raw(text)) == 2

    def test_nothing_notam_shaped_splits_into_nothing(self):
        assert notams.split_raw("just some prose about airspace") == []


class TestReadingAWholePaste:
    def test_the_demo_set(self):
        got = notams.demo()
        # Two drawable, one too wide for a circle, one with no position, and
        # one expired that must not appear at all.
        assert [n["id"] for n in got["notams"]] == ["A0001/26", "A0002/26"]
        assert [n["id"] for n in got["wide"]] == ["A0003/26"]
        assert [n["id"] for n in got["unplaced"]] == ["A0004/26"]
        assert got["count"] == 4
        assert got["read"] == 5

    def test_an_expired_notice_is_not_drawn(self):
        got = notams.read_text(
            "A1111/26 NOTAMN\nQ) UKBV/QRTCA/IV/BO/W/000/999/5020N03030E030\n"
            "A) UKBV B) 2001010000 C) 2001020000\nE) OVER LONG AGO\n\n" + ONE)
        assert [n["id"] for n in got["notams"]] == ["A1234/26"]

    def test_one_that_has_not_started_is_counted_rather_than_drawn(self):
        """A NOTAM issued for next Tuesday is real and is not a closure now.

        Counted rather than silently dropped, because "nothing is closed yet"
        and "nothing was pasted" are different facts and an empty panel says
        both.
        """
        soon = dt.datetime.fromtimestamp(
            time.time() + 86_400 * 3, dt.timezone.utc).strftime("%y%m%d%H%M")
        got = notams.read_text(
            f"A2222/26 NOTAMN\nQ) UKBV/QRTCA/IV/BO/W/000/999/5020N03030E030\n"
            f"A) UKBV B) {soon} C) PERM\nE) NOT YET\n\n" + ONE)
        assert [n["id"] for n in got["notams"]] == ["A1234/26"]
        assert got["later"] == 1

    def test_the_same_notice_twice_is_kept_once(self):
        # One notice can be filed against two adjacent regions, so a paste
        # covering both carries it twice.
        got = notams.read_text(ONE + "\n\n" + ONE)
        assert got["read"] == 1

    def test_a_fir_wide_radius_is_listed_rather_than_drawn(self):
        # A circle of hundreds of miles washes over half a continent while
        # claiming to be a boundary.
        got = notams.read_text(ONE.replace("E030", "E300"))
        assert got["notams"] == []
        assert len(got["wide"]) == 1

    def test_text_copied_out_of_a_web_page(self):
        """Copying a page's source, or saving it and opening the file.

        The tags themselves sit harmlessly around a notice; what does not is
        markup INSIDE the E) line, which is where a page puts its line breaks
        and its escaping. Left in, the popup shows somebody "RWY 09/27
        &amp;amp; TWY A<br>CLSD" -- so this checks the text, not just that
        something was read.
        """
        inner = ONE.replace("RWY 09/27 CLSD.",
                            "RWY 09/27 &amp; TWY A<br/>CLSD.")
        page = f"<html><body><b>UKBV</b><pre>{inner}</pre></body></html>"
        got = notams.read_text(page)
        assert [n["id"] for n in got["notams"]] == ["A1234/26"]
        assert got["how"] == "text from a web page"
        said = got["notams"][0]["text"]
        assert "&amp;" not in said and "&" in said
        assert "<" not in said and ">" not in said

    def test_a_json_payload_from_a_service(self):
        # Somebody with a key, or a saved response, is asking the same
        # question in a different spelling.
        body = json.dumps({"items": [{"properties": {"coreNOTAMData": {
            "notam": {
                "number": "A5555/26", "location": "UKLV",
                "coordinates": "4950N02400E", "radius": 12,
                "effectiveStart": "2026-01-01T00:00:00Z",
                "effectiveEnd": "PERM",
                "text": "TEMPORARY RESERVED AREA",
            }}}}]})
        got = notams.read_text(body)
        assert got["how"] == "JSON records"
        assert [n["id"] for n in got["notams"]] == ["A5555/26"]
        assert round(got["notams"][0]["lat"], 4) == 49.8333

    def test_a_json_record_carrying_the_raw_notice_uses_the_raw_one(self):
        """What the state filed beats what a service parsed out of it.

        Their own parse is where a position gets lost or rounded; the raw
        text is the thing that was published.
        """
        body = json.dumps([{"notam_id": "A1234/26", "location": "UKBV",
                            "raw": ONE, "latitude": None, "longitude": None}])
        got = notams.read_text(body)
        assert got["notams"][0]["placed"] is True
        assert round(got["notams"][0]["lat"], 4) == 50.3333


class TestWhatItSaysWhenItCannotRead:
    """The failure that started all this was a silent one."""

    def test_nothing_pasted(self):
        with pytest.raises(notams.NotamError, match="nothing was pasted"):
            notams.read_text("   ")

    def test_something_that_is_not_a_notam(self):
        with pytest.raises(notams.NotamError) as caught:
            notams.read_text("Ukrainian airspace has been closed since 2022.")
        # And the message says what to paste instead, because "could not
        # read that" on its own is where the last three rounds went wrong.
        assert "Q)" in str(caught.value)

    def test_a_paste_too_big_to_be_a_briefing(self):
        with pytest.raises(notams.NotamError, match="characters"):
            notams.read_text("A1234/26 " * 100_000)

    def test_it_reaches_for_no_network_at_all(self):
        """The property the whole design rests on.

        Every previous version of this module had a URL in it and every one
        of them failed at that URL. There is nothing left to fail.
        """
        import inspect
        source = inspect.getsource(notams)
        for gone in ("requests", "http://", "https://", "urlopen"):
            assert gone not in source, gone


class TestTheLayerItself:
    """The page code, checked against its source the way the rest is."""

    def source(self, name="frontend/js/notams.js"):
        import pathlib
        return (pathlib.Path(__file__).resolve().parent.parent
                / name).read_text(encoding="utf-8")

    def test_it_fetches_nothing_from_anywhere(self):
        # The whole point. A layer that still reached for a service would
        # have the same 403 waiting for it.
        text = self.source()
        assert "faa.gov" not in text.replace(
            "https://www.notams.faa.gov/dinsQueryWeb/", "").replace(
            "https://notams.aim.faa.gov/notamSearch/", ""), \
            "something other than the two links is reaching for the FAA"
        assert "api.notams(" not in text

    def test_pasting_is_the_action(self):
        # A paste box with a submit button beside it asks somebody to do the
        # same thing twice.
        text = self.source()
        assert "onpaste:" in text
        assert "plot(e.target.value)" in text

    def test_it_says_where_to_copy_them_from(self):
        """"Paste NOTAMs here" is only easy if you know where to copy from.

        That was the real gap behind three rounds of this not working: the
        data was always a browser tab away.
        """
        text = self.source()
        assert "notams.faa.gov" in text
        assert "Copy the notices from" in text

    def test_it_goes_to_what_was_pasted(self):
        # Somebody who has just pasted a briefing for another FIR is looking
        # at the wrong part of the world.
        text = self.source()
        assert "map.fitBounds" in text

    def test_a_closure_is_not_coloured_like_a_threat(self):
        # Amber is a warning, purple a missile, yellow a drone. A NOTAM is a
        # rule rather than a threat and must not read as one.
        colour = self.source()
        colour = colour[colour.index("const INK ="):]
        colour = colour[:colour.index(";")]
        for threat in ("#ffb020", "#a855f7", "#ffd400", "#ff3b30"):
            assert threat not in colour, threat

    def test_it_draws_an_outline_rather_than_a_wash(self):
        block = self.source()
        block = block[block.index("function paint()"):]
        block = block[:block.index("\n}")]
        assert "dashArray" in block
        assert "fillOpacity: 0.06" in block

    def test_the_circles_get_a_real_renderer(self):
        # The map is built with preferCanvas, and a canvas-rendered circle is
        # pixels: no element, so the dashed outline applies to nothing. The
        # same lesson the tracker's areas learnt.
        text = self.source()
        assert "L.svg({ pane: 'notams' })" in text
        assert "renderer: ink," in text

    def test_the_ones_it_cannot_draw_are_still_listed(self):
        # A FIR-wide closure and one with no position are both in force, and
        # a layer that silently dropped them would be saying the sky is open.
        text = self.source()
        assert "got?.wide" in text and "got?.unplaced" in text

    def test_it_is_wired_into_the_map(self):
        page = self.source("frontend/js/map.js")
        assert "initNotams(map)" in page
        assert 'id="notamDock"' in self.source("frontend/index.html")
