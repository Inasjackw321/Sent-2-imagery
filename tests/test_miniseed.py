"""Reading miniSEED, checked against records built here.

Waveform data cannot be fetched from this environment, so the records under
test are constructed rather than downloaded. That is not a weaker test than a
recorded fixture would be: the encoder here is written from the format
description independently of the reader, so a misreading of the spec has to be
made twice, in opposite directions, to go unnoticed.

The cases that matter are the ones a real archive actually produces: Steim
compression at both levels, every packing width within them, little-endian
records, a rate expressed as a negative factor, and several records in a row.
"""

from __future__ import annotations

import struct

import numpy as np
import pytest

from backend import miniseed


# ── Building records to read back ──────────────────────────────


def _header(count, *, start=(2026, 240, 12, 30, 15, 0), rate=(100, 1),
            big=True, encoding=miniseed.STEIM_2, exponent=9, data_offset=64):
    """A 48-byte fixed header followed by blockette 1000."""
    end = ">" if big else "<"
    year, day, hour, minute, second, tenk = start
    head = bytearray(b"\x00" * miniseed.HEADER_BYTES)
    head[0:6] = b"000001"
    head[6:7] = b"D"
    head[8:13] = b"DKL  "
    head[13:15] = b"00"
    head[15:18] = b"HHZ"
    head[18:20] = b"KO"
    head[20:30] = struct.pack(f"{end}HHBBBBH", year, day, hour, minute, second, 0, tenk)
    head[30:36] = struct.pack(f"{end}Hhh", count, rate[0], rate[1])
    head[39] = 1                     # one blockette follows
    head[44:48] = struct.pack(f"{end}HH", data_offset, miniseed.HEADER_BYTES)

    blockette = struct.pack(f"{end}HH", miniseed.BLOCKETTE_1000, 0) + struct.pack(
        "BBBB", encoding, 1 if big else 0, exponent, 0)
    body = bytes(head) + blockette
    return body.ljust(data_offset, b"\x00")


def _frame(codes, words, big=True):
    """One 64-byte Steim frame: the code table, then fifteen words."""
    fmt = ">I" if big else "<I"
    table = 0
    for index, code in enumerate(codes):
        table |= (code & 0b11) << (30 - 2 * (index + 1))
    out = struct.pack(fmt, table)
    for word in words:
        out += struct.pack(fmt, word & 0xFFFFFFFF)
    return out.ljust(64, b"\x00")


def _pack(values, widths):
    """Squeeze several signed values into one 32-bit word."""
    word = 0
    shift = 32
    for value, width in zip(values, widths):
        shift -= width
        word |= (value & ((1 << width) - 1)) << shift
    return word


def _record(payload, count, **kw):
    """A whole record, padded to the length its blockette 1000 declares."""
    exponent = kw.get("exponent", 9)
    head = _header(count, **kw)
    return (head + payload).ljust(1 << exponent, b"\x00")


# ── Steim-2 ────────────────────────────────────────────────────


def _steim2_of(values, big=True):
    """Encode a run of samples as one Steim-2 frame of four-byte differences.

    Code 1 packs four 8-bit differences per word, which is enough to carry any
    sequence whose steps are small.
    """
    diffs = [0] + [values[i] - values[i - 1] for i in range(1, len(values))]
    words, codes = [], []
    for i in range(0, len(diffs), 4):
        chunk = (diffs[i:i + 4] + [0, 0, 0])[:4]
        words.append(_pack(chunk, [8, 8, 8, 8]))
        codes.append(1)
    # Words one and two of the first frame carry the first and last samples.
    return _frame([0, 0] + codes, [values[0], values[-1]] + words, big=big)


def test_a_steim2_record_reads_back_as_the_samples_that_went_in():
    values = [1000, 1004, 1001, 995, 1002, 1010, 1007, 1000]
    data = _record(_steim2_of(values), len(values))
    out = miniseed.samples(data)
    assert list(out["samples"].astype(int)) == values
    assert out["rate"] == 100.0
    assert out["channel"] == "HHZ"
    assert out["start"].year == 2026


# The first difference in a record is against the last sample of the record
# before, which is not present, so the format carries the first sample outright
# instead and that difference is discarded. Every frame therefore needs a
# throwaway difference before the ones under test.
_LEAD_2 = (2, _pack([1, 0], [2, 30]))     # Steim-2: one 30-bit zero
_LEAD_1 = (3, 0)                          # Steim-1: one 32-bit zero


def _run(level, code, word, first, steps, count=None):
    """One frame carrying the mandatory leading difference, then `word`."""
    lead_code, lead_word = _LEAD_1 if level == 1 else _LEAD_2
    expected = [first] + list(np.cumsum(steps) + first)
    frame = _frame([0, 0, lead_code, code], [first, expected[-1], lead_word, word])
    encoding = miniseed.STEIM_1 if level == 1 else miniseed.STEIM_2
    out = miniseed.samples(_record(frame, count or len(expected), encoding=encoding))
    return list(out["samples"].astype(int)), expected


@pytest.mark.parametrize("which, widths, chunk", [
    (1, [2, 30], 1),          # one 30-bit difference
    (2, [2, 15, 15], 2),      # two 15-bit
    (3, [2, 10, 10, 10], 3),  # three 10-bit
])
def test_every_steim2_code_two_packing_is_unpacked(which, widths, chunk):
    """Steim-2 reuses one code for three different widths, and says which in
    the top two bits of the word. Getting that wrong shifts every sample."""
    steps = [7, -5, 3][:chunk]
    got, expected = _run(2, 2, _pack([which] + steps, widths), 500, steps)
    assert got == expected


@pytest.mark.parametrize("which, pad, width, chunk", [
    (0, 0, 6, 5),   # five 6-bit, filling the word exactly
    (1, 0, 5, 6),   # six 5-bit, likewise
    # Seven 4-bit values are only 28 bits, so two sit unused between the
    # selector and the data. Packing them tight instead shifts every value.
    (2, 2, 4, 7),
])
def test_every_steim2_code_three_packing_is_unpacked(which, pad, width, chunk):
    steps = [1, -2, 3, -1, 2, -3, 1][:chunk]
    widths = [2] + ([pad] if pad else []) + [width] * chunk
    values = [which] + ([0] if pad else []) + steps
    got, expected = _run(2, 3, _pack(values, widths), 400, steps)
    assert got == expected


def test_the_leading_difference_belongs_to_the_previous_record_and_is_dropped():
    """The first difference in a record refers to a sample that is not in it.

    It is the step from the last sample of the record before, which is why the
    record carries its own first sample outright as well. Applying it anyway
    shifts every sample by however far the ground had moved since the previous
    record -- a plausible-looking trace of the wrong thing.
    """
    word = _pack([77, 5, -3, 2], [8, 8, 8, 8])
    frame = _frame([0, 0, 1], [1000, 1004, word])
    out = miniseed.samples(_record(frame, 4))
    assert list(out["samples"].astype(int)) == [1000, 1005, 1002, 1004]


def test_a_negative_difference_is_read_as_negative():
    """Two's complement, not a large positive number.

    Read unsigned, a downward step of one becomes a jump of 255 and the trace
    walks off the top of the plot.
    """
    steps = [-1, -2, -3]
    got, expected = _run(2, 1, _pack([-1, -2, -3, 0], [8, 8, 8, 8]), 100, steps, count=4)
    assert got == [100, 99, 97, 94]
    assert got == expected


# ── Steim-1 ────────────────────────────────────────────────────


def test_steim1_sixteen_bit_differences():
    """Code 2 means something different at level 1 than at level 2."""
    steps = [300, -400]
    got, expected = _run(1, 2, _pack(steps, [16, 16]), 10000, steps)
    assert got == [10000, 10300, 9900]
    assert got == expected


def test_steim1_thirty_two_bit_difference():
    steps = [-70000]
    got, expected = _run(1, 3, _pack(steps, [32]), 1, steps)
    assert got == [1, -69999]
    assert got == expected


# ── Uncompressed encodings ─────────────────────────────────────


def test_plain_thirty_two_bit_integers():
    values = [5, -5, 12345, -12345]
    payload = b"".join(struct.pack(">i", v) for v in values)
    out = miniseed.samples(_record(payload, len(values), encoding=3))
    assert list(out["samples"].astype(int)) == values


def test_plain_floats():
    values = [1.5, -2.25, 0.0]
    payload = b"".join(struct.pack(">f", v) for v in values)
    out = miniseed.samples(_record(payload, len(values), encoding=4))
    assert list(out["samples"]) == values


def test_an_encoding_this_cannot_read_says_so():
    with pytest.raises(miniseed.MiniSeedError, match="not one this can read"):
        miniseed.samples(_record(b"\x00" * 64, 4, encoding=19))


# ── Headers ────────────────────────────────────────────────────


def test_a_little_endian_record_is_read_the_right_way_round():
    """Rare, but real. Read big-endian, the year 2026 becomes 58121, which is
    why the year is what decides the byte order."""
    values = [10, 12, 9, 11]
    data = _record(_steim2_of(values, big=False), len(values), big=False)
    out = miniseed.samples(data)
    assert list(out["samples"].astype(int)) == values
    assert out["start"].year == 2026


@pytest.mark.parametrize("factor, multiplier, expected", [
    (100, 1, 100.0),      # 100 samples a second
    (20, 1, 20.0),
    (-10, 1, 0.1),        # one sample every ten seconds
    (1, -2, 0.5),
])
def test_the_two_sample_rate_fields_are_combined_correctly(factor, multiplier, expected):
    """A negative field means "per this many seconds", not a negative rate.

    Read as a plain multiplication, a station reporting once a minute comes out
    at minus sixty hertz and the time axis is nonsense.
    """
    values = [1, 2, 3, 4]
    data = _record(_steim2_of(values), len(values), rate=(factor, multiplier))
    assert miniseed.samples(data)["rate"] == pytest.approx(expected)


def test_a_record_without_blockette_1000_is_refused():
    """Without it there is no way to know the encoding, and guessing would
    produce a plausible-looking trace of nothing."""
    head = bytearray(_header(4)[:miniseed.HEADER_BYTES])
    head[46:48] = struct.pack(">H", 0)
    with pytest.raises(miniseed.MiniSeedError, match="blockette 1000"):
        miniseed.samples(bytes(head).ljust(512, b"\x00"))


def test_several_records_are_joined_into_one_trace():
    first = _record(_steim2_of([1, 2, 3, 4]), 4)
    second = _record(_steim2_of([5, 6, 7, 8]), 4)
    out = miniseed.samples(first + second)
    assert list(out["samples"].astype(int)) == [1, 2, 3, 4, 5, 6, 7, 8]


def test_an_empty_reply_is_refused_rather_than_drawn():
    with pytest.raises(miniseed.MiniSeedError):
        miniseed.samples(b"")


def test_a_record_that_disagrees_with_its_own_last_sample_is_refused():
    """Steim carries the final sample outright as a check on the arithmetic.

    A silently wrong trace is worse than no trace: it looks exactly like a
    reading of the ground.
    """
    frame = _frame([0, 0, 1], [100, 999, _pack([0, 1, 1, 1], [8, 8, 8, 8])])
    with pytest.raises(miniseed.MiniSeedError, match="checksum"):
        miniseed.samples(_record(frame, 4))
