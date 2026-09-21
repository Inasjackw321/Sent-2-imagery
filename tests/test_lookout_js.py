"""Run the lookout panel's tests alongside the Python ones.

The backend decides what is a hit; this half decides whether a reader can
tell a hit from a miss from a square nobody could answer for. Both halves
matter and both are run by one command.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SUITE = Path(__file__).with_name("lookout.test.mjs")


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node is not installed; the panel's tests need it")
def test_lookout_panel() -> None:
    done = subprocess.run(
        ["node", "--test", str(SUITE)],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
