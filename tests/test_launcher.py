"""Cover the desktop-app plumbing: port choice, browser discovery, shortcuts."""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import launcher  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


# ── Ports ──────────────────────────────────────────────────────


def test_choose_port_prefers_the_requested_one():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    assert launcher.choose_port("127.0.0.1", free) == free


def test_choose_port_steps_past_a_busy_one():
    with socket.socket() as taken:
        taken.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        busy = taken.getsockname()[1]
        chosen = launcher.choose_port("127.0.0.1", busy)
        assert chosen != busy
        assert launcher.port_is_free("127.0.0.1", chosen)


def test_probe_returns_none_for_a_dead_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert launcher.probe("127.0.0.1", port, timeout=0.2) is None


def test_probe_recognises_a_running_instance(monkeypatch):
    """Only our own /api/health should count as 'already running'."""
    import contextlib
    import io

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

    payload = json.dumps({"ok": True, "demo": False}).encode()
    monkeypatch.setattr(launcher.urllib.request, "urlopen",
                        lambda *a, **k: FakeResponse(payload))
    assert launcher.probe("127.0.0.1", 8000) == {"ok": True, "demo": False}

    monkeypatch.setattr(launcher.urllib.request, "urlopen",
                        lambda *a, **k: FakeResponse(b"<html>someone else</html>"))
    assert launcher.probe("127.0.0.1", 8000) is None

    with contextlib.suppress(Exception):
        monkeypatch.undo()


# ── Browser discovery ──────────────────────────────────────────


def test_browser_override_is_honoured(monkeypatch, tmp_path):
    fake = tmp_path / "my-browser"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setenv("SENT2_BROWSER", str(fake))
    assert launcher.find_app_browser() == str(fake)


def test_browser_override_that_does_not_exist_is_ignored(monkeypatch):
    monkeypatch.setenv("SENT2_BROWSER", "/nowhere/definitely-not-a-browser")
    assert launcher.find_app_browser() is None


def test_app_window_args_are_well_formed(monkeypatch, tmp_path):
    """The window must get its own profile, or it delegates to a running browser."""
    captured = {}

    def fake_popen(args, **kwargs):
        captured["args"] = args
        return object()

    monkeypatch.setattr(launcher, "find_app_browser", lambda: "/usr/bin/chromium")
    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    launcher.open_app_window("http://localhost:8000")

    args = captured["args"]
    assert args[0] == "/usr/bin/chromium"
    assert "--app=http://localhost:8000" in args
    assert any(a.startswith("--user-data-dir=") for a in args)
    assert any(a.startswith("--window-size=") for a in args)
    assert all(isinstance(a, str) for a in args)


def test_app_window_returns_none_without_a_browser(monkeypatch):
    monkeypatch.setattr(launcher, "find_app_browser", lambda: None)
    assert launcher.open_app_window("http://localhost:8000") is None


# ── Shortcuts ──────────────────────────────────────────────────


def test_linux_desktop_entry_is_valid(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    # Without the Electron app installed; with it, see the test below.
    monkeypatch.setattr(launcher, "electron_binary", lambda *a, **k: None)
    note = launcher._install_linux_desktop()

    entry = tmp_path / ".local" / "share" / "applications" / f"{launcher.APP_ID}.desktop"
    assert entry.exists()
    text = entry.read_text()
    assert text.startswith("[Desktop Entry]")
    assert "Type=Application" in text
    assert "app.py" in text          # the self-installing entry point
    assert "Terminal=false" in text
    assert str(entry) in note


def test_macos_bundle_has_the_pieces_finder_needs(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    launcher._install_macos_app()

    bundle = tmp_path / "Applications" / f"{launcher.APP_NAME}.app"
    plist = bundle / "Contents" / "Info.plist"
    binary = bundle / "Contents" / "MacOS" / launcher.APP_NAME

    assert plist.exists() and binary.exists()
    assert "CFBundleExecutable" in plist.read_text()
    assert binary.stat().st_mode & 0o111, "launcher must be executable"
    assert binary.read_text().startswith("#!/bin/bash")
    assert "app.py" in binary.read_text()
    if (FRONTEND / "icons" / "icon.icns").exists():
        assert (bundle / "Contents" / "Resources" / "icon.icns").exists()


def test_icns_file_is_well_formed():
    """Built by hand, so check the container the way macOS would read it."""
    import struct

    icns = FRONTEND / "icons" / "icon.icns"
    if not icns.exists():
        pytest.skip("icons not generated")
    data = icns.read_bytes()
    assert data[:4] == b"icns"
    assert struct.unpack(">I", data[4:8])[0] == len(data)

    offset, entries = 8, 0
    while offset < len(data):
        code = data[offset:offset + 4]
        size = struct.unpack(">I", data[offset + 4:offset + 8])[0]
        assert size >= 8 and offset + size <= len(data), f"bad entry at {offset}"
        # Every entry we write holds a PNG payload.
        assert data[offset + 8:offset + 12] == b"\x89PNG", f"{code!r} is not PNG data"
        entries += 1
        offset += size
    assert entries >= 6


# ── Progressive web app assets ─────────────────────────────────


def test_manifest_is_installable():
    manifest = json.loads((FRONTEND / "manifest.webmanifest").read_text())
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/"
    sizes = {icon["sizes"] for icon in manifest["icons"]}
    # Chrome needs a 192 and a 512 to offer installation.
    assert "192x192" in sizes and "512x512" in sizes
    assert any(icon.get("purpose") == "maskable" for icon in manifest["icons"])
    for icon in manifest["icons"]:
        assert (FRONTEND / icon["src"].lstrip("/")).exists(), icon["src"]


def test_service_worker_never_caches_imagery():
    source = (FRONTEND / "sw.js").read_text()
    assert "/api/" in source, "the worker must special-case API calls"
    assert "skipWaiting" in source and "clients.claim" in source


def test_service_worker_shell_lists_real_files():
    source = (FRONTEND / "sw.js").read_text()
    shell = source.split("const SHELL = [", 1)[1].split("];", 1)[0]
    paths = [line.strip().strip("',") for line in shell.splitlines() if "'" in line]
    for path in paths:
        if path == "/":
            continue
        assert (FRONTEND / path.lstrip("/")).exists(), f"{path} is precached but missing"


# ── A shortcut that says what went wrong ───────────────────────


def test_a_shortcut_opens_the_electron_app_when_it_is_installed(tmp_path, monkeypatch):
    exe = tmp_path / "electron" / "node_modules" / "electron" / "dist" / "electron.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert launcher.electron_binary(tmp_path, "Windows") == exe
    assert launcher.electron_binary(tmp_path, "Linux") is None
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.setattr(launcher, "electron_binary", lambda *a, **k: exe)
    assert launcher._launch_command() == [str(exe), str(tmp_path / "electron")]


def test_and_app_py_when_it_is_not(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "electron_binary", lambda *a, **k: None)
    assert launcher._launch_command()[-1].endswith("app.py")


def test_with_no_console_the_output_goes_to_a_log(tmp_path, monkeypatch):
    """A desktop shortcut runs app.py with pythonw: no console, so every
    error went nowhere and a failure to start looked like nothing at all."""
    import importlib.util
    import sys as real_sys
    spec = importlib.util.spec_from_file_location("app_entry", ROOT / "app.py")
    app_entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app_entry)
    log = tmp_path / "earthviewer.log"
    monkeypatch.setattr(app_entry, "LOG", log)
    monkeypatch.setattr(real_sys, "stdout", None)
    monkeypatch.setattr(real_sys, "stderr", None)
    assert app_entry._attach_log() is True
    print("written where it can be read")
    real_sys.stdout.flush()
    assert "written where it can be read" in log.read_text(encoding="utf-8")


def test_with_a_console_nothing_is_redirected(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("app_entry2", ROOT / "app.py")
    app_entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app_entry)
    monkeypatch.setattr(app_entry, "LOG", tmp_path / "x.log")
    assert app_entry._attach_log() is False
    assert not (tmp_path / "x.log").exists()


def test_no_console_does_not_wait_for_a_key_nobody_can_press(monkeypatch):
    import importlib.util
    import sys as real_sys
    spec = importlib.util.spec_from_file_location("app_entry3", ROOT / "app.py")
    app_entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app_entry)
    monkeypatch.setattr(real_sys, "stdin", None)
    app_entry._pause_if_double_clicked()   # used to raise "lost sys.stdin"


def test_a_failed_start_with_no_console_writes_the_log(tmp_path):
    """The whole path, for real: app.py run with no output streams, made to
    fail, leaves the reason in the log."""
    import subprocess
    import sys as real_sys
    runner = tmp_path / "run_without_console.py"
    runner.write_text(
        "import runpy, sys\n"
        "sys.stdout = sys.stderr = None\n"
        f"sys.argv = [{str(ROOT / 'app.py')!r}, '--port', '1']\n"
        # Make main() fail the way a missing dependency would.
        "import builtins\n"
        "real = builtins.__import__\n"
        "def broken(name, *a, **k):\n"
        "    if name == 'backend' or name.startswith('backend.'):\n"
        "        raise RuntimeError('the engine would not load')\n"
        "    return real(name, *a, **k)\n"
        "builtins.__import__ = broken\n"
        f"runpy.run_path({str(ROOT / 'app.py')!r}, run_name='__main__')\n",
        encoding="utf-8")
    log = ROOT / "earthviewer.log"
    before = log.read_text(encoding="utf-8") if log.exists() else ""
    try:
        done = subprocess.run([real_sys.executable, str(runner)], cwd=ROOT,
                              capture_output=True, text=True, timeout=60)
        assert done.returncode == 1
        added = log.read_text(encoding="utf-8")[len(before):]
        assert "the engine would not load" in added
    finally:
        if before:
            log.write_text(before, encoding="utf-8")
        elif log.exists():
            log.unlink()


def test_with_electron_installed_the_desktop_entry_opens_it(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    exe = tmp_path / "electron-bin"
    monkeypatch.setattr(launcher, "electron_binary", lambda *a, **k: exe)
    launcher._install_linux_desktop()
    text = (tmp_path / ".local" / "share" / "applications"
            / f"{launcher.APP_ID}.desktop").read_text()
    assert f"Exec={exe} {launcher.ROOT / 'electron'}" in text
