"""Just enough miniSEED to draw a seismogram.

Waveform data from every FDSN data centre arrives in this format, and there is
no way round reading it: the one service that would draw the picture for you
was a single archive's own extension, and it has been retired. The standard
outlived it, which is the argument for depending on the standard.

A miniSEED file is a run of fixed-length records. Each carries a 48-byte header
naming the station, the start time and the sample rate, then a run of
"blockettes" of which only one matters here -- blockette 1000, which says how
the samples are encoded and in which byte order -- and then the samples.

Most of the work is Steim compression. Seismic data is a slow-moving signal
sampled fast, so consecutive samples are close together and the *differences*
between them are small. Steim stores those differences at whatever width they
fit in -- four bits, six bits, thirty -- packed into 32-bit words, with a table
of two-bit codes at the head of each 64-byte frame saying which packing each
word used. Undoing it means reading the codes, unpacking the differences and
adding them up from a known starting sample.

Only what is needed to plot a trace is implemented. There is no attempt at
gap-filling, timing corrections, or the older SEED features nobody sends.
"""

from __future__ import annotations

import datetime as dt
import struct

import numpy as np


class MiniSeedError(RuntimeError):
    pass


HEADER_BYTES = 48

# Blockette 1000 is the one that says how to read the samples. Without it the
# record cannot be decoded at all, and every data centre sends it.
BLOCKETTE_1000 = 1000

# Encodings this understands, by the code blockette 1000 uses.
STEIM_1 = 10
STEIM_2 = 11
_PLAIN = {
    1: ("i2", 2),    # 16-bit integers
    3: ("i4", 4),    # 32-bit integers
    4: ("f4", 4),    # IEEE single
    5: ("f8", 8),    # IEEE double
}

# Steim packs its words into frames of sixteen, the first of which is the table
# of two-bit codes describing the other fifteen.
_FRAME_WORDS = 16


def samples(data: bytes) -> dict:
    """Decode a miniSEED byte stream into one continuous trace.

    Returns the samples, the sample rate, the start time and the channel it
    came from. Records are decoded in the order they arrive, which is the order
    a data centre sends them in; a gap in the middle shows as a step rather
    than as a hole, which is honest enough for a picture.
    """
    pieces: list[np.ndarray] = []
    start = None
    rate = None
    channel = None

    for record in _records(data):
        if not len(record["samples"]):
            continue
        if start is None:
            start, rate, channel = record["start"], record["rate"], record["channel"]
        pieces.append(record["samples"])

    if not pieces:
        raise MiniSeedError("the reply held no samples")

    return {
        "samples": np.concatenate(pieces),
        "rate": rate or 1.0,
        "start": start,
        "channel": channel,
    }


def _records(data: bytes):
    """Walk the stream one record at a time."""
    offset = 0
    while offset + HEADER_BYTES <= len(data):
        record = _record(data, offset)
        yield record
        # A record length that did not advance would spin here for ever on a
        # malformed stream, which is a worse failure than a short read.
        if record["length"] <= 0:
            return
        offset += record["length"]


def _record(data: bytes, offset: int) -> dict:
    head = data[offset:offset + HEADER_BYTES]
    if len(head) < HEADER_BYTES:
        raise MiniSeedError("truncated record header")

    big = _byte_order(head)
    end = ">" if big else "<"

    station = head[8:13].decode("ascii", "replace").strip()
    location = head[13:15].decode("ascii", "replace").strip()
    channel = head[15:18].decode("ascii", "replace").strip()
    network = head[18:20].decode("ascii", "replace").strip()

    year, day, hour, minute, second, _, ten_thousandths = struct.unpack(
        f"{end}HHBBBBH", head[20:30])
    count, factor, multiplier = struct.unpack(f"{end}Hhh", head[30:36])
    data_offset, first_blockette = struct.unpack(f"{end}HH", head[44:48])

    encoding, word_order, length = _blockette_1000(data, offset, first_blockette, end)

    payload = data[offset + data_offset:offset + length]
    values = _decode(payload, encoding, word_order, count, end)

    return {
        "network": network, "station": station, "location": location,
        "channel": channel,
        "start": _moment(year, day, hour, minute, second, ten_thousandths),
        "rate": _rate(factor, multiplier),
        "samples": values[:count],
        "length": length,
    }


def _byte_order(head: bytes) -> bool:
    """Which way round the header is written.

    miniSEED does not say. The convention is big-endian and almost everything
    is, but the year is the reliable tell: read the wrong way round, 2026
    becomes 58121.
    """
    year = struct.unpack(">H", head[20:22])[0]
    return 1900 <= year <= 2100


def _blockette_1000(data: bytes, offset: int, first: int, end: str) -> tuple[int, bool, int]:
    """Find the encoding, byte order and record length."""
    where = first
    seen = 0
    while where and seen < 20:
        try:
            kind, following = struct.unpack(f"{end}HH", data[offset + where:offset + where + 4])
        except struct.error:
            break
        if kind == BLOCKETTE_1000:
            encoding, word_order, exponent = struct.unpack(
                "BBB", data[offset + where + 4:offset + where + 7])
            # The length is stored as a power of two, which is why 4096-byte
            # records arrive as the number 12.
            return encoding, bool(word_order), 1 << exponent
        where = following
        seen += 1
    raise MiniSeedError("record has no blockette 1000, so its encoding is unknown")


def _rate(factor: int, multiplier: int) -> float:
    """Sample rate from the two fields SEED spreads it across.

    Both can be negative, and a negative one means "per this many seconds"
    rather than "this many per second" -- which is how a station that reports
    once a minute is expressed.
    """
    if factor > 0 and multiplier > 0:
        return float(factor * multiplier)
    if factor > 0 and multiplier < 0:
        return -factor / multiplier
    if factor < 0 and multiplier > 0:
        return -multiplier / factor
    if factor < 0 and multiplier < 0:
        return 1.0 / (factor * multiplier)
    return 1.0


def _moment(year, day, hour, minute, second, ten_thousandths) -> dt.datetime | None:
    try:
        base = dt.datetime(year, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(days=day - 1)
        return base + dt.timedelta(
            hours=hour, minutes=minute, seconds=second,
            microseconds=ten_thousandths * 100)
    except (ValueError, OverflowError):
        return None


def _decode(payload: bytes, encoding: int, big: bool, count: int, end: str) -> np.ndarray:
    if encoding in _PLAIN:
        kind, width = _PLAIN[encoding]
        usable = len(payload) // width * width
        return np.frombuffer(payload[:usable], dtype=np.dtype(f"{end}{kind}")).astype("float64")
    if encoding in (STEIM_1, STEIM_2):
        return _steim(payload, big, count, level=1 if encoding == STEIM_1 else 2)
    raise MiniSeedError(f"encoding {encoding} is not one this can read")


def _steim(payload: bytes, big: bool, count: int, level: int) -> np.ndarray:
    """Undo Steim-1 or Steim-2 compression.

    The samples are stored as differences. The first frame also carries the
    first sample outright and the last sample as a check, so the run can be
    rebuilt by adding each difference to the one before.
    """
    fmt = ">I" if big else "<I"
    usable = len(payload) // 4 * 4
    words = [w[0] for w in struct.iter_unpack(fmt, payload[:usable])]

    first = None
    last = None
    diffs: list[int] = []

    for base in range(0, len(words) - _FRAME_WORDS + 1, _FRAME_WORDS):
        frame = words[base:base + _FRAME_WORDS]
        codes = frame[0]
        start = 1
        if base == 0:
            # Words one and two of the first frame are the first sample and
            # the last, not compressed differences.
            first = _signed(frame[1], 32)
            last = _signed(frame[2], 32)
            start = 3
        for index in range(start, _FRAME_WORDS):
            code = (codes >> (30 - 2 * index)) & 0b11
            diffs.extend(_unpack(frame[index], code, level))

    if first is None:
        raise MiniSeedError("compressed record held no frames")

    # The leading difference is against the previous record's last sample,
    # which is not here. The first sample is carried outright instead, so that
    # difference is dropped rather than applied to nothing.
    values = np.empty(len(diffs), dtype="float64")
    running = first
    values[0] = running
    for i in range(1, len(diffs)):
        running += diffs[i]
        values[i] = running

    trimmed = values[:count] if count else values
    # The record says what its last sample should be. Disagreeing means the
    # differences were unpacked wrongly, and a wrong trace is worse than none.
    if count and len(trimmed) == count and last is not None and trimmed[-1] != last:
        raise MiniSeedError(
            "decompressed trace does not match the record's own checksum sample")
    return trimmed


def _unpack(word: int, code: int, level: int) -> list[int]:
    """The differences packed into one 32-bit word."""
    if code == 0:
        # Not data: headers and padding live here.
        return []
    if code == 1:
        return [_signed(word >> shift, 8) for shift in (24, 16, 8, 0)]

    if level == 1:
        if code == 2:
            return [_signed(word >> 16, 16), _signed(word, 16)]
        return [_signed(word, 32)]

    # Steim-2 reuses codes 2 and 3 for several widths, and says which in the
    # top two bits of the word itself.
    which = word >> 30
    if code == 2:
        if which == 1:
            return [_signed(word, 30)]
        if which == 2:
            return [_signed(word >> 15, 15), _signed(word, 15)]
        if which == 3:
            return [_signed(word >> 20, 10), _signed(word >> 10, 10), _signed(word, 10)]
        return []
    if which == 0:
        return [_signed(word >> shift, 6) for shift in (24, 18, 12, 6, 0)]
    if which == 1:
        return [_signed(word >> shift, 5) for shift in (25, 20, 15, 10, 5, 0)]
    if which == 2:
        return [_signed(word >> shift, 4) for shift in (24, 20, 16, 12, 8, 4, 0)]
    return []


def _signed(value: int, bits: int) -> int:
    """Read the low `bits` of a word as a two's-complement number."""
    value &= (1 << bits) - 1
    return value - (1 << bits) if value & (1 << (bits - 1)) else value
