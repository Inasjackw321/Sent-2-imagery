"""Which build is actually running.

Worth having. The app is run from a clone, and a clone that has not been
pulled -- or a server left running from before one -- behaves exactly like a
bug that was already fixed. Printing the commit at startup turns "it still
does the thing" into a question anyone can answer for themselves.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def commit() -> str:
    """The short commit this copy is checked out at, or empty if unknowable."""
    try:
        done = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def described() -> str:
    """The commit, and whether it has uncommitted changes on top."""
    at = commit()
    if not at:
        return "unknown build"
    try:
        dirty = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--porcelain"],
            capture_output=True, text=True, timeout=3, check=False)
        edited = bool(dirty.stdout.strip()) if dirty.returncode == 0 else False
    except (OSError, subprocess.SubprocessError):
        edited = False
    return f"{at}{' + local changes' if edited else ''}"
