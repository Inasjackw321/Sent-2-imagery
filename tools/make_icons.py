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
    """The mark: a scanned globe inside a framing reticle."""
    s = size * 4  # supersample, then downscale for clean edges
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
        d = ImageDraw.Draw(img)

    inset = 0.30 if maskable else 0.22
    cx = cy = s / 2
    r = s * (0.5 - inset)
    lw = max(2, int(s * 0.020))

    # Globe outline
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=ACCENT + (255,), width=lw)

    # Parallels: ellipses squashed towards the poles.
    for frac in (-0.55, 0.0, 0.55):
        ry = r * 0.30 if frac else r * 0.42
        y = cy + r * frac
        half = r * (1 - frac * frac) ** 0.5
        if half < r * 0.1:
            continue
        d.arc([cx - half, y - ry / 2, cx + half, y + ry / 2], 0, 360,
              fill=ACCENT + (150 if frac else 210,), width=max(1, lw // 2))

    # Meridians
    for frac in (0.42, 1.0):
        half = r * frac
        d.arc([cx - half, cy - r, cx + half, cy + r], 0, 360,
              fill=ACCENT + (150 if frac < 1 else 210,), width=max(1, lw // 2))

    # Reticle corners: the "select a region" idea.
    arm = s * 0.10
    pad = s * (0.20 if maskable else 0.13)
    for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
        x = cx + sx * (s / 2 - pad)
        y = cy + sy * (s / 2 - pad)
        d.line([(x, y), (x - sx * arm, y)], fill=ACCENT_2 + (255,), width=lw)
        d.line([(x, y), (x, y - sy * arm)], fill=ACCENT_2 + (255,), width=lw)

    # A pass line across the globe, like a swath.
    d.line([(cx - r * 0.95, cy + r * 0.55), (cx + r * 0.95, cy - r * 0.75)],
           fill=INK + (190,), width=max(1, lw // 2))

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
