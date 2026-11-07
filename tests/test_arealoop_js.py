"""Run the area loop's tests alongside the Python ones.

The backend lists the frames; this half sizes the pictures, asks for them and
plays them. Both are run by one command, so neither quietly stops being run.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SUITE = Path(__file__).with_name("arealoop.test.mjs")


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node is not installed; the area loop tests need it")
def test_area_loop() -> None:
    done = subprocess.run(
        ["node", "--test", str(SUITE)],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
