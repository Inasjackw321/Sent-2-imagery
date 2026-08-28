#!/usr/bin/env python3
"""EarthViewer — double-click this file to run it.

On first launch it installs anything that is missing, then opens the studio in
its own desktop window. No terminal needed.

    python app.py                 live satellite imagery
    python app.py --demo          synthetic imagery, works offline
    python app.py --tab           open in a browser tab instead
    python app.py --install-shortcut    add it to your applications menu
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# import name -> what to give pip
REQUIRED = {
    "fastapi": "fastapi>=0.115",
    "uvicorn": "uvicorn[standard]>=0.30",
    "rasterio": "rasterio>=1.3.9",
    "numpy": "numpy>=1.24",
    "scipy": "scipy>=1.11",
    "PIL": "Pillow>=10.0",
    "requests": "requests>=2.31",
}
OPTIONAL = {"webview": "pywebview>=5.0"}


def missing_packages(wanted: dict[str, str]) -> list[str]:
    import importlib.util

    return [spec for name, spec in wanted.items()
            if importlib.util.find_spec(name) is None]


def install(specs: list[str], label: str) -> bool:
    print(f"  Installing {label} ({len(specs)})…")
    command = [sys.executable, "-m", "pip", "install", "--upgrade", *specs]
    if _in_system_python():
        # Some Linux distributions refuse to install into the system Python.
        command.insert(4, "--user")
    try:
        subprocess.check_call(command, stdout=subprocess.DEVNULL)
        return True
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"  Could not install {label}: {exc}", file=sys.stderr)
        return False


def _in_system_python() -> bool:
    return sys.prefix == getattr(sys, "base_prefix", sys.prefix) and os.name != "nt"


def ensure_dependencies() -> bool:
    needed = missing_packages(REQUIRED)
    if not needed:
        return True
    print("\n  First run — setting up.")
    if not install(needed, "dependencies"):
        print("\n  Install them yourself with:\n"
              f"    {sys.executable} -m pip install -r {ROOT / 'requirements.txt'}\n",
              file=sys.stderr)
        return False
    still_missing = missing_packages(REQUIRED)
    if still_missing:
        print(f"  Still missing: {', '.join(still_missing)}", file=sys.stderr)
        return False
    print("  Ready.\n")
    return True


def main() -> int:
    argv = sys.argv[1:]
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0

    if not ensure_dependencies():
        _pause_if_double_clicked()
        return 1

    from backend import launcher

    if "--install-shortcut" in argv:
        print(launcher.install_shortcut())
        _pause_if_double_clicked()
        return 0

    if "--demo" in argv:
        os.environ["DEMO_MODE"] = "1"

    host = "127.0.0.1"
    port = _flag_value(argv, "--port", default=8000, cast=int)

    running = launcher.probe(host, port)
    if running:
        url = f"http://{host}:{port}"
        print(f"  Already running → {url}")
        _open(url, argv)
        return 0

    port = launcher.choose_port(host, port)
    url = f"http://localhost:{port}"

    from backend import version
    print(f"\n  EarthViewer → {url}")
    print(f"  Build: {version.described()}")
    print(f"  Imagery: {'SYNTHETIC DEMO DATA' if '--demo' in argv else 'live satellites'}\n")

    import uvicorn

    config = uvicorn.Config("backend.app:app", host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    if not launcher.wait_until_ready(host, port, timeout=45):
        print("  The server did not start. See the messages above.", file=sys.stderr)
        _pause_if_double_clicked()
        return 1

    _open(url, argv, blocking=True)

    server.should_exit = True
    thread.join(timeout=5)
    return 0


def _open(url: str, argv: list[str], blocking: bool = False) -> None:
    """Show the studio, preferring a real desktop window."""
    from backend import launcher

    if "--tab" in argv:
        launcher.open_tab(url)
        if blocking:
            _wait_for_enter()
        return

    if "--no-native" not in argv and _try_native(url):
        return                       # the native window ran until it was closed

    window = launcher.open_app_window(url)
    if window is not None:
        print("  Close the window to quit.\n")
        if blocking:
            window.wait()
        return

    print("  No desktop window available — opening a browser tab.\n")
    launcher.open_tab(url)
    if blocking:
        _wait_for_enter()


def _try_native(url: str) -> bool:
    """A true OS window via pywebview, installing it on demand the first time."""
    import importlib.util

    if importlib.util.find_spec("webview") is None:
        if os.environ.get("SENT2_NO_AUTO_NATIVE"):
            return False
        if not install([OPTIONAL["webview"]], "the desktop window"):
            return False

    from backend import launcher

    return launcher.open_native_window(url)


def _flag_value(argv: list[str], flag: str, default, cast=str):
    if flag in argv:
        index = argv.index(flag)
        if index + 1 < len(argv):
            try:
                return cast(argv[index + 1])
            except ValueError:
                pass
    return default


def _pause_if_double_clicked() -> None:
    """Keep the window open long enough to read the error."""
    if sys.stdin and sys.stdin.isatty():
        return
    try:
        input("\nPress return to close.")
    except (EOFError, KeyboardInterrupt):
        pass


def _wait_for_enter() -> None:
    print("  Press Ctrl+C (or close this window) to quit.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n  Stopped.")
