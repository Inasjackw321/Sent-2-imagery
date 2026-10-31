"""SEN2SR: a trained network that redraws Sentinel-2 at 2.5 m.

The other way this app sharpens imagery -- superres.py -- merges several dates
and solves for what lies between their samples. It needs the dates, and what
it recovers is what those dates genuinely saw. SEN2SR (ESA OpenSR, Aybar et
al. 2025) is the other kind of answer: one date, and a network trained on
pairs of Sentinel-2 and 2.5 m aerial imagery that has learnt what the finer
picture usually looks like. It is sharper than anything a merge can honestly
claim, and some of that sharpness is the network's knowledge of the world
rather than this pass's measurement of it. The interface says so beside every
picture it makes.

What it works on, and why:

  The light model, SEN2SRLite "NonReference_RGBN_x4": red, green, blue and
  near-infrared in, the same four out, four times finer. 472 thousand
  parameters, three megabytes of weights, and fast enough on a laptop CPU --
  the full model needs a GPU and a CUDA build of mamba-ssm, which is not a
  thing a double-clicked desktop app can ask for.

  The native 10 m grid. The network was trained on Sentinel-2 pixels as the
  satellite delivers them; warped first onto the map's grid, every pixel would
  be a blend of its neighbours and the network would be sharpening a blur it
  has never seen. So the four bands are read straight out of the files with no
  resampling, enhanced there, and only then put on the map.

Nothing here runs code from the internet. The project's own loader (mlstac)
downloads a load.py beside the weights and exec()s it. That script does two
things -- builds the network and reads two safetensors files into it -- so
those two things are done here instead, with the classes from the installed
sen2sr package, and the weights are read as safetensors, which is a format
that holds numbers and cannot hold instructions.

PyTorch is optional. It is a large download that only this button needs, so
the app runs without it and offers to install it the first time it is asked
for -- see install().
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import platform
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import numpy as np
import requests
from scipy import ndimage

from . import config

# What is enhanced: the four 10 m bands, in the order the network takes them.
# B04, B03, B02, B08 -- red, green, blue, near-infrared.
BANDS = ("red", "green", "blue", "nir")
SCALE = 4
NATIVE_RES_M = 10.0
RES_M = NATIVE_RES_M / SCALE

# The tile the network was trained on, and how far neighbouring tiles overlap
# so their seams fall where both sides have context. Both are the package's
# own figures from its predict_large.
TILE = 128
OVERLAP = 32

# The largest area asked of it, in native pixels a side: 10.24 km at 10 m,
# which comes out at 4096 pixels -- the largest picture this app makes at all.
# Measured on four CPU threads: 0.5 s for one 128-pixel tile, 5 s for 640
# pixels a side, so about 13 s here. A wait, not a hang.
MOST_NATIVE = 1024

# Where the weights come from. The same two files the project's own loader
# fetches, from the same place, with their sizes as a cheap check that what
# arrived is the whole of what was asked for.
MODEL = "SEN2SRLite/NonReference_RGBN_x4"
BASE = f"https://huggingface.co/tacofoundation/SEN2SR/resolve/main/{MODEL}/"
FILES = {"model.safetensor": 2_308_216, "hard_constraint.safetensor": 1_048_656}
CREDIT = ("SEN2SR — ESA OpenSR (Aybar et al., 2025), "
          "SEN2SRLite RGBN ×4, CC0")

# What the button needs installed. The import name and what to give pip.
NEEDS = {"torch": "torch", "safetensors": "safetensors", "sen2sr": "sen2sr>=0.8,<1"}

# PyTorch's own CPU-only wheels. On Linux the ordinary wheel carries NVIDIA's
# libraries -- gigabytes -- whether or not there is a card to use them on.
CPU_INDEX = "https://download.pytorch.org/whl/cpu"


class NeuralError(RuntimeError):
    pass


class NotInstalled(NeuralError):
    pass


# ── Is it here ─────────────────────────────────────────────────


def missing() -> list[str]:
    """The packages this needs that are not installed."""
    importlib.invalidate_caches()
    return [name for name in NEEDS if importlib.util.find_spec(name) is None]


def weights_dir() -> Path:
    """Where the weights are kept: beside the app's other state, once."""
    override = os.environ.get("SEN2SR_DIR")
    if override:
        return Path(override)
    from .launcher import state_dir
    return state_dir() / "sen2sr" / MODEL.replace("/", "_")


def have_weights(where: Path | None = None) -> bool:
    where = where or weights_dir()
    return all((where / name).is_file() and (where / name).stat().st_size == size
               for name, size in FILES.items())


_install_lock = threading.Lock()
_install: dict[str, Any] = {"running": False, "error": None, "log": ""}


def status() -> dict[str, Any]:
    """What the page needs to know before offering the button."""
    gone = missing()
    with _install_lock:
        state = dict(_install)
    return {
        "installed": not gone,
        "missing": gone,
        "weights": have_weights(),
        "installing": state["running"],
        "install_error": state["error"],
        "command": " ".join(install_command(gone or list(NEEDS))[0]),
        "model": MODEL,
        "credit": CREDIT,
        "most_km": MOST_NATIVE * NATIVE_RES_M / 1000,
        "resolution_m": RES_M,
        "bands": list(BANDS),
    }


# ── Installing it ──────────────────────────────────────────────


def _has_nvidia() -> bool:
    return shutil.which("nvidia-smi") is not None


def install_command(names: list[str]) -> list[list[str]]:
    """The pip commands that install what is missing, in order.

    PyTorch first and on its own where that saves gigabytes: Linux with no
    NVIDIA card gets the CPU-only build. The rest after it, so pip finds torch
    already satisfied and does not fetch the large build anyway as a
    dependency of sen2sr.
    """
    base = [sys.executable, "-m", "pip", "install"]
    if sys.prefix == getattr(sys, "base_prefix", sys.prefix) and os.name != "nt":
        base.append("--user")
    rest = [NEEDS[n] for n in names if n != "torch"]
    if "torch" not in names:
        return [base + rest] if rest else []
    if platform.system() == "Linux" and not _has_nvidia():
        return [base + ["torch", "torchvision", "--index-url", CPU_INDEX],
                base + rest]
    return [base + ["torch", "torchvision", *rest]]


def install(run=subprocess.run) -> bool:
    """Install what is missing. Blocking; returns whether it worked.

    Only ever the fixed list in NEEDS -- nothing about what is installed comes
    from a request.
    """
    with _install_lock:
        if _install["running"]:
            return False
        _install.update(running=True, error=None, log="")
    try:
        for command in install_command(missing()):
            done = run(command, capture_output=True, text=True)
            with _install_lock:
                _install["log"] = (done.stdout or "")[-2000:] + (done.stderr or "")[-2000:]
            if done.returncode != 0:
                tail = (done.stderr or done.stdout or "").strip().splitlines()[-1:] or [""]
                raise NeuralError(f"pip could not install it: {tail[0][:300]}")
        if missing():
            raise NeuralError("pip finished but these are still missing: "
                              + ", ".join(missing()))
        return True
    except (OSError, NeuralError) as exc:
        with _install_lock:
            _install["error"] = str(exc)
        return False
    finally:
        with _install_lock:
            _install["running"] = False


def install_in_background() -> bool:
    """Start install() on a thread. False if one is already running."""
    with _install_lock:
        if _install["running"]:
            return False
    threading.Thread(target=install, name="sen2sr-install", daemon=True).start()
    return True


# ── The weights ────────────────────────────────────────────────


def fetch_weights(where: Path | None = None, get=requests.get) -> Path:
    """Download the two weight files once. Three megabytes, kept for good."""
    where = where or weights_dir()
    if have_weights(where):
        return where
    where.mkdir(parents=True, exist_ok=True)
    for name, size in FILES.items():
        target = where / name
        if target.is_file() and target.stat().st_size == size:
            continue
        try:
            resp = get(BASE + name, timeout=60, headers={
                "User-Agent": config.USER_AGENT})
        except requests.RequestException as exc:
            raise NeuralError(f"The SEN2SR weights could not be downloaded: {exc}") from exc
        if not resp.ok:
            raise NeuralError(
                f"Hugging Face answered {resp.status_code} for the SEN2SR weights")
        body = resp.content
        if len(body) != size:
            raise NeuralError(
                f"{name} arrived as {len(body)} bytes, not {size} — not saved")
        # Written aside and moved into place, so an interrupted download is
        # never mistaken for a finished one.
        partial = target.with_suffix(".part")
        partial.write_bytes(body)
        partial.replace(target)
    return where


# ── The network ────────────────────────────────────────────────

_model_lock = threading.Lock()
_model: Any = None


def _build(where: Path):
    """The network, built as the project's load.py builds it.

    CNNSR(4, 4, 24, 4, True, False, 6) is that script's own line: four bands
    in and out, 24 feature channels, ×4, bias, inference mode, six blocks.
    Loaded strictly, so a file that is not these weights fails here rather
    than producing a picture.
    """
    import safetensors.torch
    import torch
    from sen2sr.models.opensr_baseline.cnn import CNNSR
    from sen2sr.models.tricks import HardConstraint
    from sen2sr.nonreference import srmodel

    device = "cuda" if torch.cuda.is_available() else "cpu"
    net = CNNSR(4, 4, 24, 4, True, False, 6)
    net.load_state_dict(safetensors.torch.load_file(str(where / "model.safetensor")))
    net = net.eval().to(device)
    for param in net.parameters():
        param.requires_grad = False
    mask = safetensors.torch.load_file(str(where / "hard_constraint.safetensor"))
    constraint = HardConstraint(low_pass_mask=mask["weights"].to(device), device=device)
    return srmodel(net, constraint, device), device


def model():
    """The loaded network, built once per process."""
    global _model
    gone = missing()
    if gone:
        raise NotInstalled("SEN2SR needs " + ", ".join(gone) + " installed")
    with _model_lock:
        if _model is None:
            _model = _build(fetch_weights())
        return _model


def forget() -> None:
    global _model
    with _model_lock:
        _model = None


# ── Using it ───────────────────────────────────────────────────


def _filled(band: np.ma.MaskedArray) -> np.ndarray:
    """A band with its holes filled from the nearest measured pixel.

    The project's examples fill holes with zero. Beside a cloud edge that is a
    black wall, and a network that sharpens edges sharpens that one -- a dark
    halo round every masked area. Nearest-neighbour filling gives it nothing
    to find there; the holes are masked again afterwards.
    """
    data = np.ma.getdata(band).astype("float32")
    holes = np.ma.getmaskarray(band) | ~np.isfinite(data)
    if not holes.any():
        return data
    if holes.all():
        return np.zeros_like(data)
    _, (rows, cols) = ndimage.distance_transform_edt(holes, return_indices=True)
    return data[rows, cols]


def starts(length: int, tile: int = TILE, overlap: int = OVERLAP) -> list[int]:
    """Where each tile begins along one axis.

    A step of tile less overlap, and the last one pulled back to end exactly
    on the edge rather than running off it -- the rule the project's own
    tiler follows. Assumes length >= tile, which enhance() makes true.
    """
    step = tile - overlap
    return sorted({min(at, length - tile) for at in range(0, length, step)})


def tiled(stack: np.ndarray, run, scale: int = SCALE, tile: int = TILE,
          overlap: int = OVERLAP) -> np.ndarray:
    """Run a tile-at-a-time network over a whole (bands, rows, cols) stack.

    Written here rather than taken from sen2sr.predict_large: the release on
    PyPI (0.8.5) sizes its output from the HEIGHT on both axes and mixes up
    which axis is which at the edges, so any area that is not square comes
    out wrong or fails. The fixed version is in their repository and not yet
    released; this follows it. Each tile keeps all but half the overlap on
    any side that has a neighbour, so every seam falls where both tiles had
    context, and a tile at an edge keeps that edge.
    """
    bands, rows, cols = stack.shape
    out = np.zeros((bands, rows * scale, cols * scale), dtype="float32")
    skip = overlap * scale // 2
    for y in starts(rows, tile, overlap):
        for x in starts(cols, tile, overlap):
            result = np.asarray(run(stack[:, y:y + tile, x:x + tile]), dtype="float32")
            if result.shape != (bands, tile * scale, tile * scale):
                raise NeuralError(
                    f"SEN2SR returned a {result.shape} tile, not ×{scale}")
            top = skip if y > 0 else 0
            left = skip if x > 0 else 0
            bottom = tile * scale if y + tile >= rows else tile * scale - skip
            right = tile * scale if x + tile >= cols else tile * scale - skip
            out[:, y * scale + top:y * scale + bottom,
                x * scale + left:x * scale + right] = result[:, top:bottom, left:right]
    return out


def _torch_runner():
    """One 128-pixel tile through the loaded network, numpy in and out."""
    import torch

    net, device = model()

    def run(tile: np.ndarray) -> np.ndarray:
        with torch.inference_mode():
            x = torch.from_numpy(np.ascontiguousarray(tile)).float().to(device)
            return net(x[None])[0].float().cpu().numpy()

    return run


def enhance(bands: dict[str, np.ma.MaskedArray], run=None
            ) -> dict[str, np.ma.MaskedArray]:
    """The four 10 m bands, four times finer. Reflectance in, reflectance out.

    `run` stands in for the network -- one (4, 128, 128) tile in, one
    (4, 512, 512) out -- so everything around it can be checked without
    PyTorch.
    """
    absent = [b for b in BANDS if b not in bands]
    if absent:
        raise NeuralError("SEN2SR needs " + ", ".join(absent))
    shape = bands[BANDS[0]].shape
    if max(shape) > MOST_NATIVE:
        raise NeuralError(
            f"That area is {shape[1] * NATIVE_RES_M / 1000:.1f} × "
            f"{shape[0] * NATIVE_RES_M / 1000:.1f} km. SEN2SR works at 2.5 m "
            f"and is limited to {MOST_NATIVE * NATIVE_RES_M / 1000:.1f} km a "
            "side — draw a smaller shape.")

    stack = np.nan_to_num(np.stack([_filled(bands[b]) for b in BANDS]))
    # At least one whole tile each way: the network's constraint is built for
    # 128-pixel tiles and nothing else. Padded by repeating the edge, as the
    # project's own tiler does, and cut back off afterwards.
    short = (max(0, TILE - shape[0]), max(0, TILE - shape[1]))
    if any(short):
        stack = np.pad(stack, ((0, 0), (0, short[0]), (0, short[1])), mode="edge")
    holes = np.zeros(shape, dtype=bool)
    for b in BANDS:
        holes |= np.ma.getmaskarray(bands[b])

    out = tiled(stack, run or _torch_runner())[:, :shape[0] * SCALE, :shape[1] * SCALE]
    fine_holes = np.repeat(np.repeat(holes, SCALE, axis=0), SCALE, axis=1)
    return {b: np.ma.masked_array(out[i], mask=fine_holes)
            for i, b in enumerate(BANDS)}
