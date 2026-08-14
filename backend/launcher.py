"""Opening Sent-2 as a desktop app: port selection, app windows, shortcuts."""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "Sent-2"
APP_ID = "sent-2-imagery"


# ── Ports ──────────────────────────────────────────────────────


def port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def probe(host: str, port: int, timeout: float = 0.6) -> dict | None:
    """Return the health payload if *our* server is already on this port."""
    url = f"http://{host}:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            return data if data.get("ok") else None
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def choose_port(host: str, preferred: int, span: int = 20) -> int:
    """The preferred port if we can have it, else the next free one nearby."""
    if port_is_free(host, preferred):
        return preferred
    for candidate in range(preferred + 1, preferred + span):
        if port_is_free(host, candidate):
            return candidate
    with socket.socket() as sock:          # let the OS pick anything free
        sock.bind((host, 0))
        return sock.getsockname()[1]


def wait_until_ready(host: str, port: int, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe(host, port, timeout=0.4):
            return True
        time.sleep(0.15)
    return False


# ── Finding a browser that can host an app window ──────────────

_CANDIDATES = {
    "Darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi",
    ],
    "Windows": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    ],
    "Linux": [],
}

_LINUX_COMMANDS = [
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "microsoft-edge", "brave-browser", "vivaldi-stable",
]


def find_app_browser() -> str | None:
    """A Chromium-family browser, which is what can open a chrome-less window."""
    override = os.environ.get("SENT2_BROWSER")
    if override:
        return override if Path(override).exists() or shutil.which(override) else None

    for path in _CANDIDATES.get(platform.system(), []):
        if Path(path).exists():
            return path
    for command in _LINUX_COMMANDS:
        found = shutil.which(command)
        if found:
            return found
    # Playwright's bundled Chromium, if this machine happens to have one.
    bundled = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")) / "chromium"
    return str(bundled) if bundled.exists() else None


def state_dir() -> Path:
    """Somewhere to keep the app window's own browser profile."""
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif system == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    path = base / APP_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


def open_app_window(url: str, width: int = 1500, height: int = 950):
    """Launch a stand-alone window. Returns the process, or None if unavailable."""
    browser = find_app_browser()
    if not browser:
        return None

    # A dedicated profile matters: without it the browser hands the URL to an
    # already-running instance and exits immediately, so we could not tell when
    # the window was closed.
    profile = state_dir() / "window-profile"
    args = [
        browser,
        f"--app={url}",
        f"--user-data-dir={profile}",
        f"--window-size={width},{height}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,MediaRouter",
    ]

    # Chromium refuses to start as root with its sandbox on. That only happens
    # in containers and on some server setups; ordinary users keep the sandbox.
    if os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0:
        args.append("--no-sandbox")

    try:
        return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None


def open_native_window(url: str, width: int = 1500, height: int = 950) -> bool:
    """A real OS window via pywebview. Blocks until the window is closed.

    Returns False if pywebview is absent or has no GUI toolkit to talk to, so
    the caller can fall back to a browser window.
    """
    try:
        import webview
    except ImportError:
        return False
    try:
        webview.create_window(
            f"{APP_NAME} imagery studio", url,
            width=width, height=height, min_size=(900, 600),
            background_color="#0d1015",
        )
        webview.start()
        return True
    except Exception as exc:            # no GTK/Qt/WebKit backend, or no display
        log_reason = str(exc).split("\n", 1)[0]
        print(f"  Native window unavailable ({log_reason}); using a browser window.")
        return False


def open_tab(url: str) -> None:
    import webbrowser

    webbrowser.open(url)


# ── Desktop shortcuts ──────────────────────────────────────────


def install_shortcut() -> str:
    """Create a double-clickable entry for the current desktop. Returns a note."""
    system = platform.system()
    if system == "Darwin":
        return _install_macos_app()
    if system == "Windows":
        return _install_windows_shortcut()
    return _install_linux_desktop()


def _launch_command() -> list[str]:
    """app.py is the double-click entry point: it self-installs, then opens."""
    return [sys.executable, str(ROOT / "app.py")]


def _install_linux_desktop() -> str:
    applications = Path.home() / ".local" / "share" / "applications"
    applications.mkdir(parents=True, exist_ok=True)
    target = applications / f"{APP_ID}.desktop"
    exec_line = " ".join(_quote(part) for part in _launch_command())
    target.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME} imagery studio\n"
        "Comment=Look at free Sentinel-2 satellite imagery\n"
        f"Exec={exec_line}\n"
        f"Icon={ROOT / 'frontend' / 'icons' / 'icon-512.png'}\n"
        f"Path={ROOT}\n"
        "Terminal=false\n"
        "Categories=Science;Graphics;Education;\n"
        "Keywords=satellite;sentinel;imagery;gis;\n"
        f"StartupWMClass={APP_NAME}\n"
    )
    target.chmod(0o755)
    # Refresh the menu cache if the tool is there; plenty of systems lack it.
    if shutil.which("update-desktop-database"):
        try:
            subprocess.run(["update-desktop-database", str(applications)],
                           check=False, capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            pass
    return f"Added {target} — look for “{APP_NAME}” in your applications menu."


def _install_macos_app() -> str:
    bundle = Path.home() / "Applications" / f"{APP_NAME}.app"
    macos = bundle / "Contents" / "MacOS"
    resources = bundle / "Contents" / "Resources"
    macos.mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)

    (bundle / "Contents" / "Info.plist").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        f"  <key>CFBundleName</key><string>{APP_NAME}</string>\n"
        f"  <key>CFBundleDisplayName</key><string>{APP_NAME} imagery studio</string>\n"
        f"  <key>CFBundleIdentifier</key><string>studio.sent2.app</string>\n"
        "  <key>CFBundleVersion</key><string>1.0</string>\n"
        "  <key>CFBundleShortVersionString</key><string>1.0</string>\n"
        "  <key>CFBundlePackageType</key><string>APPL</string>\n"
        f"  <key>CFBundleExecutable</key><string>{APP_NAME}</string>\n"
        "  <key>CFBundleIconFile</key><string>icon</string>\n"
        "  <key>LSMinimumSystemVersion</key><string>10.13</string>\n"
        "  <key>NSHighResolutionCapable</key><true/>\n"
        "  <key>LSUIElement</key><false/>\n"
        "</dict></plist>\n"
    )

    launcher = macos / APP_NAME
    launcher.write_text(
        "#!/bin/bash\n"
        f'cd "{ROOT}" || exit 1\n'
        f'exec {_quote(sys.executable)} "{ROOT / "app.py"}"\n'
    )
    launcher.chmod(0o755)

    icns = ROOT / "frontend" / "icons" / "icon.icns"
    if icns.exists():
        shutil.copyfile(icns, resources / "icon.icns")
    return f"Created {bundle} — drag it to the Dock, or open it from Launchpad."


def _install_windows_shortcut() -> str:
    desktop = Path.home() / "Desktop"
    start_menu = (Path(os.environ.get("APPDATA", Path.home())) / "Microsoft"
                  / "Windows" / "Start Menu" / "Programs")
    icon = ROOT / "frontend" / "icons" / "favicon.ico"
    made = []

    for folder in (desktop, start_menu):
        if not folder.exists():
            continue
        link = folder / f"{APP_NAME}.lnk"
        script = (
            "$s=(New-Object -COM WScript.Shell).CreateShortcut('{link}');"
            "$s.TargetPath='{python}';"
            "$s.Arguments='\"{script_path}\"';"
            "$s.WorkingDirectory='{root}';"
            "$s.IconLocation='{icon}';"
            "$s.Description='Sentinel-2 imagery studio';"
            "$s.Save()"
        ).format(link=link, python=_pythonw(), script_path=ROOT / "app.py",
                 root=ROOT, icon=icon)
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            check=False, capture_output=True,
        )
        if result.returncode == 0:
            made.append(str(link))

    if made:
        return "Created " + " and ".join(made)
    return ("Could not create a shortcut automatically — "
            f"use {ROOT / 'launchers' / 'Sent-2.bat'} instead.")


def _pythonw() -> str:
    """pythonw.exe runs without a console window."""
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return str(candidate if candidate.exists() else sys.executable)


def _quote(value: str) -> str:
    return f'"{value}"' if " " in str(value) else str(value)
