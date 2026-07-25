"""Assembling client-rendered frames into animations."""

from __future__ import annotations

import base64
import io

from PIL import Image

MAX_FRAMES = 120
MAX_PIXELS = 3200 * 3200


class AnimationError(RuntimeError):
    pass


def _decode(frame: str) -> Image.Image:
    if frame.startswith("data:"):
        frame = frame.split(",", 1)[1]
    try:
        img = Image.open(io.BytesIO(base64.b64decode(frame)))
    except Exception as exc:  # pillow raises a grab-bag of errors here
        raise AnimationError(f"Frame could not be decoded: {exc}") from exc
    img.load()
    if img.width * img.height > MAX_PIXELS:
        raise AnimationError(
            f"Frame is too large ({img.width}x{img.height}); keep frames under 3200px a side"
        )
    return img.convert("RGB")


def build(
    frames: list[str],
    fps: float = 2.0,
    loop: int = 0,
    boomerang: bool = False,
    fmt: str = "gif",
    hold_last_ms: int = 0,
    colors: int = 256,
) -> tuple[bytes, str, str]:
    """Encode frames (PNG data URLs) as an animated GIF, WebP or APNG."""
    if not frames:
        raise AnimationError("No frames supplied")
    if len(frames) > MAX_FRAMES:
        raise AnimationError(f"Too many frames ({len(frames)}); the limit is {MAX_FRAMES}")

    images = [_decode(f) for f in frames]
    size = images[0].size
    images = [im if im.size == size else im.resize(size, Image.LANCZOS) for im in images]

    if boomerang and len(images) > 2:
        images = images + images[-2:0:-1]

    duration = max(int(round(1000.0 / max(float(fps), 0.1))), 20)
    durations = [duration] * len(images)
    if hold_last_ms:
        durations[-1] = duration + int(hold_last_ms)

    buf = io.BytesIO()
    fmt = fmt.lower()

    if fmt == "webp":
        images[0].save(
            buf, format="WEBP", save_all=True, append_images=images[1:],
            duration=durations, loop=int(loop), quality=88, method=4,
        )
        return buf.getvalue(), "image/webp", "webp"

    if fmt in ("apng", "png"):
        images[0].save(
            buf, format="PNG", save_all=True, append_images=images[1:],
            duration=durations, loop=int(loop),
        )
        return buf.getvalue(), "image/apng", "png"

    palette_source = _global_palette(images, colors)
    quantised = [
        im.quantize(palette=palette_source, dither=Image.FLOYDSTEINBERG) for im in images
    ]
    quantised[0].save(
        buf, format="GIF", save_all=True, append_images=quantised[1:],
        duration=durations, loop=int(loop), optimize=True, disposal=1,
    )
    return buf.getvalue(), "image/gif", "gif"


def _global_palette(images: list[Image.Image], colors: int) -> Image.Image:
    """One palette for the whole animation, so colours do not shimmer between frames."""
    colors = max(2, min(int(colors), 256))
    sample = images[:: max(1, len(images) // 12)] or images[:1]
    w, h = images[0].size
    scale = min(1.0, 256.0 / max(w, h))
    tiles = [im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.BILINEAR)
             for im in sample]
    strip = Image.new("RGB", (tiles[0].width, tiles[0].height * len(tiles)))
    for i, tile in enumerate(tiles):
        strip.paste(tile, (0, i * tiles[0].height))
    return strip.quantize(colors=colors, method=Image.MEDIANCUT)


def contact_sheet(frames: list[str], columns: int = 3, gap: int = 12,
                  background=(16, 18, 22)) -> bytes:
    """Lay frames out in a grid -- handy for a static 'progression' figure."""
    images = [_decode(f) for f in frames]
    if not images:
        raise AnimationError("No frames supplied")
    columns = max(1, min(int(columns), 6))
    rows = (len(images) + columns - 1) // columns
    w, h = images[0].size
    sheet = Image.new(
        "RGB",
        (columns * w + (columns + 1) * gap, rows * h + (rows + 1) * gap),
        background,
    )
    for i, im in enumerate(images):
        if im.size != (w, h):
            im = im.resize((w, h), Image.LANCZOS)
        col, row = i % columns, i // columns
        sheet.paste(im, (gap + col * (w + gap), gap + row * (h + gap)))
    buf = io.BytesIO()
    sheet.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
