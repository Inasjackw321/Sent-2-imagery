"""Tests for several passes over one place as one animation.

A pair of dates side by side answers "is it different". An animation answers
"what changed", which is usually the question being asked -- and the ways it
can quietly lie are specific: frames out of order show a building being
demolished when it was built, and undated frames are a claim about change
that nobody can check.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from backend import animate


def png(colour=(20, 40, 60), size=(64, 48)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, format="PNG")
    return out.getvalue()


def frames_of(gif: bytes) -> list[Image.Image]:
    got = Image.open(io.BytesIO(gif))
    out = []
    try:
        while True:
            out.append(got.convert("RGB").copy())
            got.seek(got.tell() + 1)
    except EOFError:
        pass
    return out


class TestTheOrderFramesRunIn:
    def test_oldest_first_whatever_order_they_were_ticked(self):
        # An animation that runs backwards shows a building being demolished
        # when it was built, and nothing on the screen would say so.
        got = animate.order([
            {"date": "2026-09-09", "datetime": "2026-09-09T04:00Z"},
            {"date": "2026-08-01", "datetime": "2026-08-01T04:00Z"},
            {"date": "2026-09-01", "datetime": "2026-09-01T04:00Z"}])
        assert [s["date"] for s in got] == ["2026-08-01", "2026-09-01",
                                            "2026-09-09"]

    def test_two_passes_on_one_day_keep_their_times(self):
        got = animate.order([
            {"date": "2026-09-09", "datetime": "2026-09-09T16:00Z"},
            {"date": "2026-09-09", "datetime": "2026-09-09T04:00Z"}])
        assert [s["datetime"] for s in got] == ["2026-09-09T04:00Z",
                                                "2026-09-09T16:00Z"]

    def test_a_scene_with_no_time_sorts_on_its_date(self):
        got = animate.order([{"date": "2026-09-09"}, {"date": "2026-01-01"}])
        assert got[0]["date"] == "2026-01-01"

    def test_nothing_is_not_an_error(self):
        assert animate.order([]) == []


class TestWhatAFrameSaysAboutItself:
    def test_the_date(self):
        assert animate.stamp_of({"date": "2026-09-09"}) == "2026-09-09"

    def test_from_the_timestamp_where_there_is_no_date(self):
        assert animate.stamp_of({"datetime": "2026-09-09T04:31:00Z"}) \
            == "2026-09-09"

    def test_nothing_known_is_nothing_written(self):
        assert animate.stamp_of({}) == ""

    def test_the_stamp_is_burnt_into_the_frame(self):
        # Into the frame rather than beside the player: the frame is what
        # gets forwarded, and an undated picture of a changed thing is a
        # claim nobody can check.
        plain = Image.new("RGB", (200, 150), (20, 40, 60))
        marked = animate.stamp(plain, "2026-09-09")
        assert marked.convert("RGB").tobytes() != plain.tobytes()

    def test_and_nothing_is_burnt_in_when_there_is_nothing_to_say(self):
        plain = Image.new("RGB", (200, 150), (20, 40, 60))
        assert animate.stamp(plain, "") is plain

    def test_it_sits_on_a_plate_rather_than_on_the_picture(self):
        # These are satellite pictures. White text over snow or a sunlit roof
        # is not there at all.
        white = Image.new("RGB", (200, 150), (255, 255, 255))
        marked = animate.stamp(white, "2026-09-09").convert("RGB")
        corner = marked.crop((0, 100, 120, 150)).getcolors(20000)
        darkest = min(sum(c) for _, c in corner)
        assert darkest < 120, "the stamp should have a dark plate under it"


class TestBuildingTheAnimation:
    def test_two_frames_make_two_frames(self):
        gif = animate.build([png((20, 40, 60)), png((200, 40, 60))])
        assert len(frames_of(gif)) == 2

    def test_it_is_really_a_gif(self):
        gif = animate.build([png(), png((90, 90, 90))])
        assert gif[:6] in (b"GIF87a", b"GIF89a")

    def test_the_frames_differ_from_one_another(self):
        # The whole point. A GIF whose frames are identical is a still with
        # a larger file size.
        got = frames_of(animate.build([png((10, 10, 10)), png((240, 240, 240))]))
        assert got[0].tobytes() != got[1].tobytes()

    def test_one_date_is_not_an_animation(self):
        with pytest.raises(animate.AnimateError, match="at least"):
            animate.build([png()])

    def test_and_neither_is_a_hundred(self):
        with pytest.raises(animate.AnimateError, match="at most"):
            animate.build([png()] * (animate.MOST_FRAMES + 1))

    def test_a_frame_that_is_not_an_image_is_named(self):
        # "The animation failed" over a dozen dates is not something anybody
        # can act on.
        with pytest.raises(animate.AnimateError, match="frame 2"):
            animate.build([png(), b"not an image"])

    def test_frames_of_different_sizes_are_made_to_match(self):
        # A render of the same area on two dates can differ by a pixel from
        # rounding, and a GIF whose frames differ in size is refused by some
        # viewers and silently cropped by others.
        gif = animate.build([png(size=(64, 48)), png((200, 40, 60), (63, 47))])
        sizes = {f.size for f in frames_of(gif)}
        assert len(sizes) == 1

    def test_a_big_picture_is_brought_down_to_a_sensible_size(self):
        gif = animate.build([png(size=(3000, 2000)), png((90, 90, 90), (3000, 2000))])
        assert max(frames_of(gif)[0].size) == animate.WIDEST

    def test_and_a_small_one_is_never_blown_up(self):
        small = Image.new("RGB", (40, 30))
        assert animate.fit(small).size == (40, 30)

    def test_three_dates_run_forward_and_back(self):
        # So the loop does not jump from the last frame to the first, which
        # on a comparison reads as a fourth change that did not happen.
        gif = animate.build([png((10, 10, 10)), png((120, 120, 120)),
                             png((240, 240, 240))], bounce=True)
        assert len(frames_of(gif)) == 4

    def test_two_dates_are_not_doubled(self):
        # Two frames bounce into exactly the same two-frame loop, so doubling
        # them only holds each one twice as long.
        gif = animate.build([png((10, 10, 10)), png((240, 240, 240))],
                            bounce=True)
        assert len(frames_of(gif)) == 2

    def test_the_frames_of_a_built_gif_carry_their_dates(self):
        # Through build(), not through stamp() on its own: a version that
        # stamped correctly and never called it would pass that check and
        # produce an animation of undated pictures.
        #
        # Two DIFFERENT frames, because Pillow collapses a pair of identical
        # ones into a single frame and there is then nothing to compare.
        shot = [png((40, 60, 80)), png((200, 180, 160))]
        plain = frames_of(animate.build(shot))
        dated = frames_of(animate.build(shot, ["2026-08-01", "2026-09-09"]))
        assert len(plain) == len(dated) == 2
        for n in range(2):
            assert dated[n].tobytes() != plain[n].tobytes(), n

    def test_and_each_frame_carries_its_own_date(self):
        # Not the first one's on all of them, which would be a caption rather
        # than a timeline.
        one = frames_of(animate.build([png((40, 60, 80))] * 2,
                                      ["2026-08-01", "2026-09-09"]))
        # The pictures underneath are identical, so any difference between
        # the two frames is the stamp.
        assert len(one) == 2
        assert one[0].tobytes() != one[1].tobytes()

    def test_frames_are_made_to_match_before_anything_is_encoded(self):
        # Checked on the function, because a GIF reader reports the canvas
        # size for every frame whatever went in -- so reading the sizes back
        # out of the file cannot tell a resized frame from an unresized one.
        odd = [Image.new("RGB", (64, 48)), Image.new("RGB", (63, 47)),
               Image.new("RGB", (80, 60))]
        assert {f.size for f in animate.same_size(odd)} == {(64, 48)}

    def test_and_nothing_is_resized_when_they_already_match(self):
        same = [Image.new("RGB", (64, 48)), Image.new("RGB", (64, 48))]
        assert [f.size for f in animate.same_size(same)] == [(64, 48)] * 2

    def test_no_frames_is_not_an_error(self):
        assert animate.same_size([]) == []

    def test_the_frame_time_is_the_one_asked_for(self):
        gif = animate.build([png(), png((90, 90, 90))], ms=250)
        got = Image.open(io.BytesIO(gif))
        assert got.info.get("duration") == 250

    def test_an_absurd_frame_time_is_brought_into_range(self):
        # A GIF cannot hold a frame for less than a hundredth of a second,
        # and a zero means "as fast as possible", which is a strobe.
        gif = animate.build([png(), png((90, 90, 90))], ms=0)
        assert Image.open(io.BytesIO(gif)).info.get("duration") >= 40

    def test_it_loops_forever(self):
        gif = animate.build([png(), png((90, 90, 90))])
        assert Image.open(io.BytesIO(gif)).info.get("loop") == 0


class TestRenderingTheFrames:
    def scenes(self, n=3):
        return [{"id": f"s{i}", "date": f"2026-09-0{i + 1}",
                 "datetime": f"2026-09-0{i + 1}T04:00Z"} for i in range(n)]

    def test_one_frame_per_scene_in_time_order(self):
        asked = []

        def render(scene):
            asked.append(scene["date"])
            return png((20 * len(asked), 40, 60))

        gif, dates = animate.animate(self.scenes(), render)
        assert asked == ["2026-09-01", "2026-09-02", "2026-09-03"]
        assert dates == asked
        assert gif[:3] == b"GIF"

    def test_the_ticked_order_does_not_decide_the_frame_order(self):
        asked = []

        def render(scene):
            asked.append(scene["date"])
            return png()

        animate.animate(list(reversed(self.scenes())), render)
        assert asked == sorted(asked)

    def test_a_date_that_will_not_render_is_named(self):
        def render(scene):
            if scene["date"] == "2026-09-02":
                raise ValueError("no pixels there")
            return png()

        with pytest.raises(animate.AnimateError, match="2026-09-02"):
            animate.animate(self.scenes(), render)

    def test_and_so_is_why(self):
        def render(scene):
            raise ValueError("the pass clipped the edge")

        with pytest.raises(animate.AnimateError, match="clipped the edge"):
            animate.animate(self.scenes(2), render)

    def test_one_date_is_refused_before_anything_is_rendered(self):
        asked = []
        with pytest.raises(animate.AnimateError, match="at least"):
            animate.animate(self.scenes(1), lambda s: asked.append(s) or png())
        assert asked == []

    def test_too_many_are_refused_before_anything_is_rendered_too(self):
        # The expensive way round would be to render twelve and then refuse.
        asked = []
        with pytest.raises(animate.AnimateError, match="at most"):
            animate.animate(self.scenes(9) * 2,
                            lambda s: asked.append(s) or png())
        assert asked == []
