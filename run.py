#!/usr/bin/env python3
"""Start EarthViewer.

    python run.py                     # open as an app window
    python run.py --demo              # synthetic imagery, works offline
    python run.py --tab               # open in a normal browser tab instead
    python run.py --install-shortcut  # add it to your applications / Dock / Start menu
    python run.py --headless          # just serve; open the URL yourself
"""

from __future__ import annotations

import argparse
import os
import sys
import threading


def main() -> int:
    parser = argparse.ArgumentParser(
        description="EarthViewer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("\n", 2)[2],
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000,
                        help="preferred port; the next free one is used if taken")
    parser.add_argument("--demo", action="store_true",
                        help="serve synthetic imagery instead of downloading real scenes")
    parser.add_argument("--app", action="store_true",
                        help="open as a stand-alone app window (the default)")
    parser.add_argument("--tab", action="store_true",
                        help="open in a normal browser tab rather than an app window")
    parser.add_argument("--native", action="store_true",
                        help="use a native OS window (needs: pip install pywebview)")
    parser.add_argument("--headless", "--no-browser", dest="headless", action="store_true",
                        help="serve only; do not open anything")
    parser.add_argument("--install-shortcut", action="store_true",
                        help="add a desktop/dock/start-menu entry, then exit")
    parser.add_argument("--reload", action="store_true", help="auto-reload on code changes")
    args = parser.parse_args()

    if args.demo:
        os.environ["DEMO_MODE"] = "1"

    try:
        import uvicorn
        import rasterio  # noqa: F401
    except ImportError as exc:
        print(f"Missing dependency: {exc.name}\n\n  pip install -r requirements.txt\n",
              file=sys.stderr)
        return 1

    from backend import launcher

    if args.install_shortcut:
        print(launcher.install_shortcut())
        return 0

    # If a copy is already running, just show it rather than starting a second.
    existing = launcher.probe(args.host, args.port)
    if existing:
        url = f"http://{args.host}:{args.port}"
        print(f"  {launcher.APP_NAME} is already running → {url}")
        if not args.headless:
            _show(url, args)
        return 0

    port = launcher.choose_port(args.host, args.port)
    url = f"http://{'localhost' if args.host in ('0.0.0.0', '127.0.0.1') else args.host}:{port}"

    print(f"\n  {launcher.APP_NAME} imagery studio → {url}")
    print(f"  Source: {'SYNTHETIC DEMO DATA' if args.demo else 'Sentinel-2 L2A via Earth Search'}")
    if port != args.port:
        print(f"  (port {args.port} was busy)")
    print()

    if args.reload:
        # The reloader needs the import-string form and owns the process.
        uvicorn.run("backend.app:app", host=args.host, port=port,
                    reload=True, log_level="info")
        return 0

    config = uvicorn.Config("backend.app:app", host=args.host, port=port, log_level="info")
    server = uvicorn.Server(config)

    if args.native:
        return _run_with_native_window(server, launcher, args.host, port, url)

    if not args.headless:
        threading.Thread(target=_open_when_ready, daemon=True,
                         args=(server, launcher, args.host, port, url, args)).start()

    server.run()
    return 0


def _show(url: str, args) -> None:
    """Open the UI the way the user asked for, falling back sensibly."""
    from backend import launcher

    if args.tab:
        launcher.open_tab(url)
        return
    if launcher.open_app_window(url) is None:
        print("  No Chromium-family browser found — opening a normal tab instead.")
        launcher.open_tab(url)


def _open_when_ready(server, launcher, host: str, port: int, url: str, args) -> None:
    """Wait for the server, open the window, and stop the server when it closes."""
    if not launcher.wait_until_ready(host, port):
        print("  Server did not come up in time; open the URL yourself.", file=sys.stderr)
        return

    if args.tab:
        launcher.open_tab(url)
        return

    import time

    window = launcher.open_app_window(url)
    if window is None:
        print("  No Chromium-family browser found — opening a normal tab instead.")
        launcher.open_tab(url)
        return

    print("  Running as an app window. Close the window to quit.\n")
    started = time.monotonic()
    window.wait()

    # A window that dies immediately never really opened (missing libraries, a
    # locked profile, a crash). Fall back to a tab rather than quitting on the
    # user the moment they launch the app.
    if time.monotonic() - started < 3.0:
        print("  The app window closed straight away — opening a normal tab instead.")
        launcher.open_tab(url)
        return

    server.should_exit = True


def _run_with_native_window(server, launcher, host: str, port: int, url: str) -> int:
    """pywebview must own the main thread, so the server moves to a background one."""
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    if not launcher.wait_until_ready(host, port):
        print("  Server did not come up in time.", file=sys.stderr)
        return 1
    if not launcher.open_native_window(url):
        print("  pywebview is not available — falling back to an app window.")
        window = launcher.open_app_window(url)
        if window is None:
            launcher.open_tab(url)
        else:
            window.wait()
    server.should_exit = True
    thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
