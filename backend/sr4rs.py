"""SR4RS: Rémi Cresson's (INRAE) network that redraws Sentinel-2 at 2.5 m.

The second network-drawn sharpening beside SEN2SR (neural.py), and the same
kind of answer: one date, four bands, four times finer, the fine detail a
trained estimate rather than a measurement. Trained on 250 Spot-6/7 scenes
over mainland France in 2020, calibrated to Sentinel-2's radiometry -- so it
is at its best on European farmland and towns and draws everything else in
that likeness. The page says what drew the picture beside every one.

Why it runs in a Python of its own:

  The model is a TensorFlow 1 SavedModel, and TensorFlow has no build for the
  newest Pythons (3.14 at the time of writing; its wheels stop at 3.13). The
  app may well be running on one. So the first press sets up a small separate
  runtime with uv -- a Python TensorFlow does support, with TensorFlow and
  numpy in it -- kept beside the app's other state, and the network runs
  there as a child process. That also keeps a minute of TensorFlow's work off
  the app's own process, where it would hold up everything else.

  The project's own way to run it is OTBTF (Orfeo ToolBox with TensorFlow),
  a separate desktop install. Nothing here needs it: sr4rs_worker.py loads the
  SavedModel with plain TensorFlow and tiles exactly as the project's sr.py
  tells OTBTF to.

Where the model comes from: the project's README, a zip on INRAE's own file
server. It is checked for being a SavedModel before it is unpacked, and
unpacked only inside its own folder.
"""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import numpy as np
import requests

from . import config
from .neural import NeuralError, NotInstalled, _filled

BANDS = ("red", "green", "blue", "nir")      # B4, B3, B2, B8: the model's order
SCALE = 4
NATIVE_RES_M = 10.0
RES_M = NATIVE_RES_M / SCALE
DN = 10_000.0                                # the model takes reflectance x 10 000

# 10.24 km a side at 10 m, as for SEN2SR. Slower than SEN2SR -- a heavier
# network, on the CPU -- so this is the most worth waiting for: about 64 tiles.
MOST_NATIVE = 1024

MODEL = "sr4rs_sentinel2_bands4328_france2020"
MODEL_URL = ("https://nextcloud.inrae.fr/s/boabW9yCjdpLPGX/download/"
             "sr4rs_sentinel2_bands4328_france2020_savedmodel.zip")
CREDIT = "SR4RS — R. Cresson, INRAE (2022), Sentinel-2 RGBN ×4, MIT"
# A zip bigger than this is not the model, whatever it says it is.
MOST_ZIP_BYTES = 1 << 30
MOST_UNPACKED_BYTES = 2 << 30
MOST_FILES = 200

# The runtime. TensorFlow 2.16 onwards still loads TF1 SavedModels through
# tf.compat.v1; the CPU build where there is one, since that is all a desktop
# app can count on and it is a third of the size.
if platform.system() in ("Windows", "Linux") and platform.machine().lower() in ("amd64", "x86_64"):
    TENSORFLOW = "tensorflow-cpu>=2.16,<3"
else:
    TENSORFLOW = "tensorflow>=2.16,<3"
UV = "uv>=0.4,<1"
# The Python the runtime gets when the app's own is one TensorFlow has no
# build for. uv fetches it -- a standalone build, nothing installed system-wide.
RUNTIME_PYTHON = "3.12"
SUPPORTED = ((3, 10), (3, 14))               # [from, to) TensorFlow has wheels for

WORKER = Path(__file__).with_name("sr4rs_worker.py")
RUN_TIMEOUT_S = 20 * 60


# ── Where things are ───────────────────────────────────────────


def home() -> Path:
    override = os.environ.get("SR4RS_DIR")
    if override:
        return Path(override)
    from .launcher import state_dir
    return state_dir() / "sr4rs"


def runtime_dir() -> Path:
    return home() / "runtime"


def runtime_python() -> Path:
    if os.name == "nt":
        return runtime_dir() / "Scripts" / "python.exe"
    return runtime_dir() / "bin" / "python"


def _ready_mark() -> Path:
    # Written only once TensorFlow has been seen to import, so a half-made
    # runtime -- an install cut off part way -- is never taken for a working one.
    return runtime_dir() / ".earthviewer-ready"


def runtime_ready() -> bool:
    return runtime_python().is_file() and _ready_mark().is_file()


def model_dir() -> Path:
    return home() / "model"


def saved_model(where: Path | None = None) -> Path | None:
    """The folder holding saved_model.pb, at the top of `where` or one down."""
    where = where or model_dir()
    if (where / "saved_model.pb").is_file():
        return where
    if where.is_dir():
        for child in sorted(where.iterdir()):
            if child.is_dir() and (child / "saved_model.pb").is_file():
                return child
    return None


def have_weights(where: Path | None = None) -> bool:
    found = saved_model(where)
    return bool(found and (found / "variables").is_dir())


# ── Status ─────────────────────────────────────────────────────

_install_lock = threading.Lock()
_install: dict[str, Any] = {"running": False, "error": None, "step": "", "log": ""}


def status() -> dict[str, Any]:
    with _install_lock:
        state = dict(_install)
    return {
        "installed": runtime_ready(),
        "weights": have_weights(),
        "installing": state["running"],
        "install_error": state["error"],
        "step": state["step"],
        "command": f"{Path(sys.executable).name} app.py --sr4rs",
        "model": MODEL,
        "credit": CREDIT,
        "most_km": MOST_NATIVE * NATIVE_RES_M / 1000,
        "resolution_m": RES_M,
        "bands": list(BANDS),
    }


# ── Setting up the runtime ─────────────────────────────────────


def _pip_base() -> list[str]:
    base = [sys.executable, "-m", "pip", "install"]
    if sys.prefix == getattr(sys, "base_prefix", sys.prefix) and os.name != "nt":
        base.append("--user")
    return base


def runtime_for(version=None) -> str:
    """Which Python the runtime is made from: this one if TensorFlow has a
    build for it, otherwise one uv fetches."""
    version = tuple((version or sys.version_info)[:2])
    if SUPPORTED[0] <= version < SUPPORTED[1]:
        return sys.executable
    return RUNTIME_PYTHON


def install_steps(have_uv: bool | None = None, version=None) -> list[tuple[str, list[str]]]:
    """What install() runs, in order, each with what to say while it does.

    A fixed list: nothing in it comes from a request.
    """
    if have_uv is None:
        have_uv = importlib.util.find_spec("uv") is not None
    uv = [sys.executable, "-m", "uv"]
    steps = []
    if not have_uv:
        steps.append(("Installing uv", _pip_base() + [UV]))
    steps += [
        ("Making the SR4RS runtime",
         uv + ["venv", "--python", runtime_for(version), str(runtime_dir())]),
        ("Installing TensorFlow into it (a few hundred MB, once)",
         uv + ["pip", "install", "--python", str(runtime_python()), TENSORFLOW, "numpy"]),
        ("Checking TensorFlow loads",
         [str(runtime_python()), "-I", "-c", "import numpy, tensorflow"]),
    ]
    return steps


def _quiet() -> dict:
    """No console window flashing up on Windows for each step."""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def install(run=subprocess.run) -> bool:
    """Set up the runtime. Blocking; returns whether it worked."""
    with _install_lock:
        if _install["running"]:
            return False
        _install.update(running=True, error=None, step="", log="")
    try:
        if runtime_dir().exists() and not runtime_ready():
            shutil.rmtree(runtime_dir(), ignore_errors=True)   # a half-made one
        runtime_dir().parent.mkdir(parents=True, exist_ok=True)
        for said, command in install_steps():
            with _install_lock:
                _install["step"] = said
            done = run(command, capture_output=True, text=True, **_quiet())
            with _install_lock:
                _install["log"] = (done.stdout or "")[-2000:] + (done.stderr or "")[-2000:]
            if done.returncode != 0:
                tail = (done.stderr or done.stdout or "").strip().splitlines()[-1:] or [""]
                raise NeuralError(f"{said} failed: {tail[0][:300]}")
        _ready_mark().write_text("ok\n")
        return True
    except (OSError, NeuralError) as exc:
        with _install_lock:
            _install["error"] = str(exc)
        return False
    finally:
        with _install_lock:
            _install["running"] = False
            _install["step"] = ""


def install_in_background() -> bool:
    with _install_lock:
        if _install["running"]:
            return False
    threading.Thread(target=install, name="sr4rs-install", daemon=True).start()
    return True


# ── The model ──────────────────────────────────────────────────


def _member_ok(name: str) -> bool:
    path = PurePosixPath(name.replace("\\", "/"))
    return bool(name) and not path.is_absolute() and ".." not in path.parts \
        and not (path.parts and path.parts[0].endswith(":"))


def unpack(archive: Path, where: Path) -> Path:
    """Unpack the model zip into `where`, refusing anything that is not one.

    Every name stays inside the folder; the sizes and the count are bounded;
    and there has to be a saved_model.pb with its variables, or nothing is
    kept.
    """
    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as exc:
        raise NeuralError("The SR4RS download is not a zip file") from exc
    with zf:
        infos = zf.infolist()
        if len(infos) > MOST_FILES:
            raise NeuralError("The SR4RS zip holds too many files to be the model")
        if sum(i.file_size for i in infos) > MOST_UNPACKED_BYTES:
            raise NeuralError("The SR4RS zip unpacks too large to be the model")
        if not all(_member_ok(i.filename) for i in infos):
            raise NeuralError("The SR4RS zip has a file that would land outside its folder")
        names = [PurePosixPath(i.filename) for i in infos]
        if not any(n.name == "saved_model.pb" and len(n.parts) <= 2 for n in names):
            raise NeuralError("The SR4RS zip has no saved_model.pb — not the model")
        staging = where.with_name(where.name + ".part")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            zf.extractall(staging)
            if not have_weights(staging):
                raise NeuralError("The SR4RS zip has no variables folder — not the model")
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    shutil.rmtree(where, ignore_errors=True)
    staging.replace(where)
    return where


def fetch_weights(where: Path | None = None, get=requests.get) -> Path:
    """Download and unpack the SavedModel once."""
    where = where or model_dir()
    if have_weights(where):
        return saved_model(where)
    where.parent.mkdir(parents=True, exist_ok=True)
    archive = where.with_name("model.zip.part")
    try:
        with get(MODEL_URL, timeout=120, stream=True,
                 headers={"User-Agent": config.USER_AGENT}) as resp:
            if not resp.ok:
                raise NeuralError(f"INRAE's server answered {resp.status_code} for the SR4RS model")
            size = 0
            with open(archive, "wb") as f:
                for chunk in resp.iter_content(1 << 20):
                    size += len(chunk)
                    if size > MOST_ZIP_BYTES:
                        raise NeuralError("The SR4RS download is too large to be the model")
                    f.write(chunk)
        unpack(archive, where)
    except requests.RequestException as exc:
        raise NeuralError(f"The SR4RS model could not be downloaded: {exc}") from exc
    finally:
        archive.unlink(missing_ok=True)
    return saved_model(where)


# ── Using it ───────────────────────────────────────────────────


def _worker_runner():
    """The whole stack through the worker in the runtime, numpy in and out."""
    if not runtime_ready():
        raise NotInstalled("SR4RS needs its TensorFlow runtime set up")
    model = fetch_weights()

    def run(stack: np.ndarray) -> np.ndarray:
        with tempfile.TemporaryDirectory(prefix="sr4rs-") as tmp:
            source, target = Path(tmp) / "in.npy", Path(tmp) / "out.npy"
            np.save(source, stack.astype("float32"), allow_pickle=False)
            env = {**os.environ, "TF_CPP_MIN_LOG_LEVEL": "2", "PYTHONIOENCODING": "utf-8"}
            try:
                done = subprocess.run(
                    [str(runtime_python()), "-I", str(WORKER), str(model),
                     str(source), str(target)],
                    capture_output=True, text=True, timeout=RUN_TIMEOUT_S, env=env,
                    **_quiet())
            except subprocess.TimeoutExpired as exc:
                raise NeuralError("SR4RS took longer than "
                                  f"{RUN_TIMEOUT_S // 60} minutes — try a smaller area") from exc
            if done.returncode != 0 or not target.is_file():
                tail = (done.stderr or done.stdout or "").strip().splitlines()[-1:] or ["no output"]
                raise NeuralError(f"SR4RS failed: {tail[0][:300]}")
            return np.load(target, allow_pickle=False)

    return run


def enhance(bands: dict[str, np.ma.MaskedArray], run=None
            ) -> dict[str, np.ma.MaskedArray]:
    """The four 10 m bands, four times finer. Reflectance in, reflectance out.

    `run` stands in for the worker: (4, rows, cols) in digital numbers, and
    (4, rows*4, cols*4) back.
    """
    absent = [b for b in BANDS if b not in bands]
    if absent:
        raise NeuralError("SR4RS needs " + ", ".join(absent))
    shape = bands[BANDS[0]].shape
    if max(shape) > MOST_NATIVE:
        raise NeuralError(
            f"That area is {shape[1] * NATIVE_RES_M / 1000:.1f} × "
            f"{shape[0] * NATIVE_RES_M / 1000:.1f} km. SR4RS works at 2.5 m "
            f"and is limited to {MOST_NATIVE * NATIVE_RES_M / 1000:.1f} km a "
            "side — draw a smaller shape.")
    stack = np.nan_to_num(np.stack([_filled(bands[b]) for b in BANDS])) * DN
    holes = np.zeros(shape, dtype=bool)
    for b in BANDS:
        holes |= np.ma.getmaskarray(bands[b])
    out = np.asarray((run or _worker_runner())(stack), dtype="float32")
    want = (len(BANDS), shape[0] * SCALE, shape[1] * SCALE)
    if out.shape != want:
        raise NeuralError(f"SR4RS returned {out.shape}, not {want}")
    out = out / DN
    fine_holes = np.repeat(np.repeat(holes, SCALE, axis=0), SCALE, axis=1)
    return {b: np.ma.masked_array(out[i], mask=fine_holes)
            for i, b in enumerate(BANDS)}
