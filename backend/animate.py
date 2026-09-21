"""Several passes over one place, as one animation.

A pair of dates side by side answers "is it different". An animation answers
"what changed", which is a different question and usually the one being asked:
the eye finds a moved ship or a new revetment in a blink when it flickers, and
does not find it at all in two pictures a screen apart.

Assembled here rather than in the browser, for two reasons. The frames come
from the same render path as a single picture, so a frame of an animation and
a picture saved on its own are the same pixels rather than two code paths that
drift. And a GIF encoder is a real thing -- palettes, LZW, disposal -- that
Pillow already does properly and correctly.

Three things are deliberate.

  EVERY FRAME IS DATED. An animation of undated pictures is a claim that
  something changed with no way to check it or cite it. The stamp is burnt
  into the frame rather than shown beside the player, because the frame is
  what gets sent to somebody else.

  THE FRAMES ARE IN TIME ORDER, OLDEST FIRST. Whatever order the dates were
  ticked in. An animation that runs backwards shows a building being
  demolished when it was built.

  ONE PALETTE FOR ALL OF THEM. A GIF holds 256 colours per frame, and letting
  each frame choose its own makes the whole picture shimmer between frames --
  which reads as change, in an image whose entire purpose is to show change.
"""

from __future__ import annotations

import io
from typing import Any, Callable

from PIL import Image, ImageDraw, ImageFont

# How long each frame is held, in milliseconds.
#
# Slow enough to read the stamp, fast enough that the flicker does the work.
# Below about 300 ms the eye stops resolving the frames and sees a blur; above
# about a second it stops seeing an animation and sees a slideshow.
FRAME_MS = 700

# How many frames one animation may hold.
#
# Each is a full render, so this is a limit on time and on somebody else's
# service as much as on file size.
MOST_FRAMES = 12
FEWEST_FRAMES = 2

# The longest side of a frame. A GIF of a 2048 px render is tens of megabytes
# and nothing that looks at one needs that.
WIDEST = 1024


class AnimateError(RuntimeError):
    pass


def order(scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Oldest first, whatever order they were ticked in.

    An animation that runs backwards shows a building being demolished when
    it was built, and nothing on the screen would say so.
    """
    return sorted(scenes, key=lambda s: (s.get("datetime") or s.get("date") or ""))


def stamp_of(scene: dict[str, Any]) -> str:
    """What a frame says about itself."""
    return str(scene.get("date") or scene.get("datetime") or "")[:10]


def fit(image: Image.Image, widest: int = WIDEST) -> Image.Image:
    """Down to a sensible size, keeping the shape. Never up."""
    if max(image.size) <= widest:
        return image
    scale = widest / max(image.size)
    return image.resize(
        (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
        Image.LANCZOS)


def _font(height: int) -> Any:
    size = max(11, round(height * 0.032))
    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def stamp(image: Image.Image, said: str) -> Image.Image:
    """Burn the date into the corner of a frame.

    Into the frame rather than beside the player: the frame is what gets sent
    to somebody, and an undated picture of a changed thing is a claim nobody
    can check.

    On a plate, because these are satellite pictures -- white text over snow
    or a sunlit roof is not there at all.
    """
    if not said:
        return image
    out = image.convert("RGBA")
    draw = ImageDraw.Draw(out)
    font = _font(out.height)
    box = draw.textbbox((0, 0), said, font=font)
    wide, tall = box[2] - box[0], box[3] - box[1]
    pad = max(4, round(tall * 0.45))
    x0, y0 = pad, out.height - tall - pad * 3
    draw.rectangle([x0, y0, x0 + wide + pad * 2, y0 + tall + pad * 2],
                   fill=(8, 11, 16, 200))
    draw.text((x0 + pad - box[0], y0 + pad - box[1]), said,
              font=font, fill=(233, 238, 247, 255))
    return out


def same_size(frames: list[Image.Image]) -> list[Image.Image]:
    """All frames the size of the first.

    A render of the same area on two dates can differ by a pixel from
    rounding, and a GIF whose frames differ in size is refused by some viewers
    and silently cropped by others.
    """
    if not frames:
        return frames
    want = frames[0].size
    return [f if f.size == want else f.resize(want, Image.LANCZOS)
            for f in frames]


def build(frames: list[bytes], stamps: list[str] | None = None,
          ms: int = FRAME_MS, bounce: bool = False) -> bytes:
    """Several rendered pictures, as one animated GIF.

    `bounce` runs the frames forward and then back again. For two dates that
    is the classic flicker comparison, and it means the loop does not jump
    from the last frame to the first -- which on a two-frame animation is the
    difference between a comparison and a strobe.
    """
    if len(frames) < FEWEST_FRAMES:
        raise AnimateError(
            f"an animation needs at least {FEWEST_FRAMES} dates")
    if len(frames) > MOST_FRAMES:
        raise AnimateError(
            f"an animation holds at most {MOST_FRAMES} dates; "
            f"{len(frames)} were asked for")
    said = list(stamps or [])
    opened = []
    for n, body in enumerate(frames):
        try:
            one = Image.open(io.BytesIO(body))
            one.load()
        except (OSError, ValueError) as exc:
            raise AnimateError(f"frame {n + 1} could not be read: {exc}") from exc
        opened.append(stamp(fit(one), said[n] if n < len(said) else ""))
    opened = same_size(opened)

    if bounce:
        # Two frames need no doubling and do not get any: the slice of a
        # two-item list is empty, so a pair bounces into exactly the same
        # two-frame loop it already was. Measured rather than guarded -- a
        # length check here changed nothing and read as though it did.
        opened = opened + opened[-2:0:-1]

    # One palette for all of them, taken from the frames together. Letting
    # each frame pick its own makes the whole picture shimmer between frames,
    # which reads as change in an image whose entire purpose is to show it.
    tall = Image.new("RGB", (opened[0].width, opened[0].height * len(opened)))
    for n, one in enumerate(opened):
        tall.paste(one.convert("RGB"), (0, n * opened[0].height))
    shared = tall.convert("P", palette=Image.ADAPTIVE, colors=255)
    flat = [one.convert("RGB").quantize(palette=shared, dither=Image.FLOYDSTEINBERG)
            for one in opened]

    out = io.BytesIO()
    flat[0].save(out, format="GIF", save_all=True, append_images=flat[1:],
                 duration=max(40, int(ms)), loop=0, optimize=True,
                 disposal=1)
    return out.getvalue()


def animate(scenes: list[dict[str, Any]], render: Callable[[dict], bytes],
            ms: int = FRAME_MS, bounce: bool = False) -> tuple[bytes, list[str]]:
    """Render one frame per scene and assemble them. Returns (gif, dates).

    `render` is handed one scene and returns its picture as PNG bytes. Passed
    in rather than reached for, so this is testable without a satellite and so
    a frame is the same render a single picture is.
    """
    in_order = order(scenes)
    if len(in_order) < FEWEST_FRAMES:
        raise AnimateError(f"an animation needs at least {FEWEST_FRAMES} dates")
    if len(in_order) > MOST_FRAMES:
        raise AnimateError(
            f"an animation holds at most {MOST_FRAMES} dates; "
            f"{len(in_order)} were asked for")
    frames = []
    for scene in in_order:
        try:
            frames.append(render(scene))
        except Exception as exc:                      # noqa: BLE001
            # Named, because "the animation failed" over a dozen dates is not
            # something anybody can act on. Which date, and why.
            raise AnimateError(
                f"{stamp_of(scene) or scene.get('id')} could not be "
                f"rendered: {exc}") from exc
    said = [stamp_of(s) for s in in_order]
    return build(frames, said, ms=ms, bounce=bounce), said
