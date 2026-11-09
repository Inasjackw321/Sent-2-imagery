"""Run the fire incident tests alongside the Python ones.

Which detections are shown for the frame on screen, and how they are said.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SUITE = Path(__file__).with_name("incidents.test.mjs")


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node is not installed; the fire incident tests need it")
def test_fire_incidents() -> None:
    done = subprocess.run(
        ["node", "--test", str(SUITE)],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
