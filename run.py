#!/usr/bin/env python3
"""Start the Sent-2 imagery studio.

    python run.py                 # live Sentinel-2 imagery (needs internet)
    python run.py --demo          # synthetic imagery, works offline
    python run.py --port 8080 --no-browser
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser


def main() -> int:
    parser = argparse.ArgumentParser(description="Sent-2 imagery studio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--demo", action="store_true",
                        help="serve synthetic imagery instead of downloading real scenes")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument("--reload", action="store_true", help="auto-reload on code changes")
    args = parser.parse_args()

    if args.demo:
        os.environ["DEMO_MODE"] = "1"

    try:
        import uvicorn  # noqa: F401
        import rasterio  # noqa: F401
    except ImportError as exc:
        print(f"Missing dependency: {exc.name}\n\n  pip install -r requirements.txt\n",
              file=sys.stderr)
        return 1

    url = f"http://{'localhost' if args.host in ('0.0.0.0', '127.0.0.1') else args.host}:{args.port}"
    print(f"\n  Sent-2 imagery studio → {url}")
    print(f"  Source: {'SYNTHETIC DEMO DATA' if args.demo else 'Sentinel-2 L2A via Earth Search'}\n")

    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    import uvicorn
    uvicorn.run("backend.app:app", host=args.host, port=args.port,
                reload=args.reload, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
