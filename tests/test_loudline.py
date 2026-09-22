"""The line across a seismogram, and what it is allowed to claim.

A seismogram here is raw counts from an instrument nobody in this app has
calibrated, drawn on whatever vertical scale that hour needed. A line at a
fixed number of counts would therefore mean one thing on one station, another
on the next, and nothing at all on a third -- so the line is placed from the
trace's own background noise, and the tests below are about the two ways that
can go wrong.

It can be placed too low, where ordinary noise reaches on its own, and then it
means nothing: every window crosses it. Or the measurement of "background" can
be spoiled by the very events the line is meant to pick out, which raises it by
exactly the amount the event was worth and hides the next one.
"""

from __future__ import annotations

import io
import math

import numpy as np
import pytest
from PIL import Image

from backend import seismic


def noise(count: int, spread: float = 100.0, seed: int = 0) -> np.ndarray:
    values = np.random.default_rng(seed).normal(0, spread, count)
    return values - np.median(values)


# ── Where the line goes ────────────────────────────────────────


class TestHowFarUp:
    def test_a_longer_window_puts_the_line_higher(self):
        """Noise reaches further from quiet the more often you look at it, so
        one multiple cannot mean the same thing in a ten-minute window and a
        six-hour one."""
        assert seismic.loud_sigmas(12_000) < seismic.loud_sigmas(2_160_000)

    def test_the_line_stays_in_a_sensible_range(self):
        for count in (2, 100, 12_000, 360_000, 2_160_000, 50_000_000):
            level = seismic.loud_sigmas(count)
            assert seismic.LOUD_LEAST <= level <= seismic.LOUD_MOST, count

    def test_a_window_with_nothing_in_it_does_not_divide_by_it(self):
        assert seismic.loud_sigmas(0) == seismic.LOUD_LEAST
        assert seismic.loud_sigmas(1) == seismic.LOUD_LEAST

    def test_the_level_is_where_noise_alone_would_rarely_reach(self):
        """The claim the line makes, checked against the arithmetic it was
        derived from: the chance of one sample of ordinary noise being this
        far out, times the number of samples, is the number of crossings we
        are willing to call background."""
        for count in (12_000, 360_000):
            k = seismic.loud_sigmas(count)
            expected = count * math.erfc(k / math.sqrt(2))
            assert 0.2 * seismic.CROSSINGS_ALLOWED < expected \
                < 5 * seismic.CROSSINGS_ALLOWED, (count, k, expected)

    @pytest.mark.parametrize("count", [12_000, 72_000, 360_000])
    def test_measured_against_actual_noise(self, count):
        """The test that matters: generate background, and check it stays
        under the line nearly every time. Not once -- a hundred windows, so
        the rate is being measured rather than a single draw."""
        crossed = 0
        for seed in range(100):
            values = noise(count, 137.0, seed)
            level, _ = seismic.loud_level(values)
            if float(np.abs(values).max()) > level:
                crossed += 1
        assert crossed <= 12, (
            f"{crossed} of 100 windows of pure background crossed the line at "
            f"{count} samples -- a line the noise crosses on its own says "
            f"nothing")

    def test_a_real_event_does_cross_it(self):
        values = noise(72_000, 100.0, 3)
        level, _ = seismic.loud_level(values)
        values[40_000:40_400] += 1500.0            # fifteen times the spread
        assert float(np.abs(values).max()) > level


class TestMeasuringTheBackground:
    def test_the_line_barely_moves_when_an_event_is_in_the_window(self):
        """The reason for a median rather than an average. An event in the
        data raises a mean-based background by its own size, which lifts the
        line above the next event of the same kind."""
        quiet = noise(72_000, 100.0, 5)
        before, _ = seismic.loud_level(quiet)
        loud = quiet.copy()
        loud[30_000:31_000] += 4000.0
        after, _ = seismic.loud_level(loud)
        assert abs(after - before) / before < 0.05

    def test_a_noisier_station_gets_a_higher_line(self):
        small, _ = seismic.loud_level(noise(72_000, 10.0, 7))
        large, _ = seismic.loud_level(noise(72_000, 1000.0, 7))
        assert large > small * 50

    def test_the_line_scales_with_the_instrument_rather_than_the_counts(self):
        """Two recordings of the same ground at different gains must put the
        line in the same place on the picture."""
        one = noise(72_000, 100.0, 9)
        both = one * 37.0
        first, sigmas_one = seismic.loud_level(one)
        second, sigmas_two = seismic.loud_level(both)
        assert sigmas_one == sigmas_two
        assert second == pytest.approx(first * 37.0, rel=1e-9)

    def test_a_flat_channel_gets_no_line_at_all(self):
        level, _ = seismic.loud_level(np.zeros(5000))
        assert level == 0.0

    def test_a_channel_with_one_sample_gets_no_line(self):
        assert seismic.loud_level(np.array([7.0]))[0] == 0.0

    def test_nothing_at_all_gets_no_line(self):
        assert seismic.loud_level(np.array([]))[0] == 0.0


# ── The drawing ────────────────────────────────────────────────


def drawn(values: np.ndarray, rate: float = 20.0) -> Image.Image:
    png = seismic.plot({"samples": values.tolist(), "rate": rate},
                       "XX.TEST.HHZ", "Synthetic", 60)
    return Image.open(io.BytesIO(png)).convert("RGB")


def red_rows(image: Image.Image) -> list[int]:
    """Which rows of the picture the red line was drawn on."""
    pixels = image.load()
    out = []
    for y in range(image.height):
        red = sum(1 for x in range(image.width)
                  if pixels[x, y][0] > 180 and pixels[x, y][1] < 110
                  and pixels[x, y][2] < 110)
        if red > image.width // 8:
            out.append(y)
    return out


class TestTheLineOnThePicture:
    def test_two_lines_are_drawn_one_each_side_of_quiet(self):
        image = drawn(noise(72_000, 100.0, 11))
        rows = red_rows(image)
        assert len(rows) == 2, rows
        middle = image.height / 2
        assert rows[0] < middle < rows[1]
        # Symmetric about quiet, because the trace is demeaned and a bang
        # pushes the ground both ways.
        assert abs((middle - rows[0]) - (rows[1] - middle)) <= 2

    def test_the_line_is_inside_the_picture_on_a_quiet_hour(self):
        """The scale has to include the line, or a quiet hour -- whose
        largest excursion is three times the background -- puts it off the top
        of the plot and leaves nothing to compare the trace against."""
        rows = red_rows(drawn(noise(72_000, 100.0, 13)))
        assert len(rows) == 2, "the line fell off the picture on a quiet hour"
        assert all(4 < row < 246 for row in rows), rows

    def test_the_line_is_inside_the_picture_on_a_loud_one(self):
        values = noise(72_000, 100.0, 17)
        values[20_000:20_500] += 9000.0
        rows = red_rows(drawn(values))
        assert len(rows) == 2, "the line was lost when the scale grew"

    def test_a_quiet_hour_draws_well_inside_the_lines(self):
        """What the reader is meant to see at a glance: nothing happened."""
        image = drawn(noise(72_000, 100.0, 19))
        rows = red_rows(image)
        pixels = image.load()
        above = sum(1 for x in range(image.width)
                    for y in range(rows[0] - 6)
                    if pixels[x, y][2] > 200 and pixels[x, y][0] < 160)
        assert above == 0, f"{above} pixels of trace above the line on a quiet hour"

    def test_an_event_draws_past_them(self):
        values = noise(72_000, 100.0, 23)
        values[36_000:36_300] += 6000.0
        image = drawn(values)
        rows = red_rows(image)
        pixels = image.load()
        above = sum(1 for x in range(image.width)
                    for y in range(rows[0] - 4)
                    if pixels[x, y][2] > 200 and pixels[x, y][0] < 160)
        assert above > 0, "the event did not reach past the line"

    def test_a_flat_channel_is_drawn_without_a_line(self):
        assert red_rows(drawn(np.zeros(5000))) == []

    def test_the_picture_says_how_high_the_line_is(self):
        """The multiple changes with the window, so a picture that did not
        name it would be claiming something different every time without
        saying so."""
        from PIL import ImageDraw, ImageFont

        image = drawn(noise(72_000, 100.0, 29))
        # The label is drawn in the same red as the line, above it, so its
        # presence is checked by looking for red text pixels outside the two
        # full-width rows.
        rows = set(red_rows(image))
        pixels = image.load()
        label = sum(1 for x in range(image.width) for y in range(image.height)
                    if y not in rows and pixels[x, y][0] > 180
                    and pixels[x, y][1] < 140 and pixels[x, y][2] < 140)
        assert label > 40, f"only {label} red pixels of label"

    def test_the_plot_is_still_a_png_of_the_expected_size(self):
        image = drawn(noise(72_000, 100.0, 31))
        assert image.size == seismic.PLOT_SIZE
