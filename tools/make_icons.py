#!/usr/bin/env python3
"""Generate the app icons (and a macOS .icns) from one vector-ish drawing.

    python tools/make_icons.py

Writes frontend/icons/. Re-run after changing the design.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ICONS = ROOT / "frontend" / "icons"

BG_TOP = (22, 28, 39)
BG_BOTTOM = (13, 16, 21)
ACCENT = (76, 194, 255)
ACCENT_2 = (55, 224, 160)
INK = (232, 237, 245)

SIZES = [16, 32, 48, 64, 128, 180, 192, 256, 512, 1024]


def draw_icon(size: int, maskable: bool = False) -> Image.Image:
    """The mark: a globe with the swath of one pass laid across it.

    Deliberately three shapes and no more. An icon is read at sixteen pixels
    more often than at any other size, and everything beyond a circle, a
    meridian and the strip becomes mud at that size.
    """
    s = size * 8  # supersample, then downscale for clean edges
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Background plate. Maskable icons must survive a circular crop, so they
    # get a full bleed and a tighter globe.
    radius = 0 if maskable else int(s * 0.22)
    for y in range(s):
        t = y / s
        d.line([(0, y), (s, y)], fill=tuple(
            round(BG_TOP[c] + (BG_BOTTOM[c] - BG_TOP[c]) * t) for c in range(3)) + (255,))
    if radius:
        mask = Image.new("L", (s, s), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, s - 1, s - 1], radius, fill=255)
        img.putalpha(mask)

    inset = 0.30 if maskable else 0.24
    cx = cy = s / 2
    r = s * (0.5 - inset)
    lw = max(2, round(s * 0.031))

    # The swath, drawn on its own layer and then cut to the globe: a strip of
    # ground lies on the sphere, it does not cross in front of it.
    swath = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    sd = ImageDraw.Draw(swath)
    def place(x, y):
        """A point from the logo's own 64-unit drawing, put on this canvas."""
        return cx + (x - 32) / 21 * r, cy + (y - 32) / 21 * r

    a, ctrl, b = place(-8, 50), place(32, 31), place(72, 10)
    curve = [(
        (1 - t) ** 2 * a[0] + 2 * (1 - t) * t * ctrl[0] + t * t * b[0],
        (1 - t) ** 2 * a[1] + 2 * (1 - t) * t * ctrl[1] + t * t * b[1],
    ) for t in [i / 48 for i in range(49)]]
    sd.line(curve, fill=ACCENT_2 + (255,), width=round(r * 0.45), joint="curve")

    hole = Image.new("L", (s, s), 0)
    ImageDraw.Draw(hole).ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    img.alpha_composite(Image.composite(swath, Image.new("RGBA", (s, s), (0, 0, 0, 0)), hole))

    d = ImageDraw.Draw(img)
    # One meridian, so the circle reads as a globe rather than a ring.
    d.ellipse([cx - r * 0.40, cy - r, cx + r * 0.40, cy + r],
              outline=ACCENT + (150,), width=max(1, round(lw * 0.6)))
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=ACCENT + (255,), width=lw)

    return img.resize((size, size), Image.LANCZOS)


# ── macOS .icns ────────────────────────────────────────────────
# ICNS entries may hold PNG data directly, so the file can be built anywhere --
# no need for macOS-only iconutil.
ICNS_TYPES = [
    (b"ic07", 128), (b"ic08", 256), (b"ic09", 512),
    (b"ic10", 1024), (b"ic11", 32), (b"ic12", 64),
    (b"ic13", 256), (b"ic14", 512),
]


def build_icns(render) -> bytes:
    entries = b""
    for code, size in ICNS_TYPES:
        buf = _png_bytes(render(size))
        entries += code + struct.pack(">I", len(buf) + 8) + buf
    return b"icns" + struct.pack(">I", len(entries) + 8) + entries


def _png_bytes(img: Image.Image) -> bytes:
    import io

    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def main() -> int:
    ICONS.mkdir(parents=True, exist_ok=True)
    cache: dict[int, Image.Image] = {}

    def render(size: int) -> Image.Image:
        if size not in cache:
            cache[size] = draw_icon(size)
        return cache[size]

    for size in SIZES:
        render(size).save(ICONS / f"icon-{size}.png")
    draw_icon(512, maskable=True).save(ICONS / "icon-maskable-512.png")

    # Multi-resolution .ico for Windows shortcuts and the browser tab.
    render(256).save(ICONS / "favicon.ico",
                     sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    (ICONS / "icon.icns").write_bytes(build_icns(render))

    print(f"wrote {len(SIZES) + 3} files to {ICONS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
