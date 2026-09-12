"""Run the tile-probe tests alongside the Python ones.

Same wrapper as the solar and cloud-mask suites next door, for the same
reason: a test suite that has to be remembered is a test suite that quietly
stops being run.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SUITE = Path(__file__).with_name("tiles.test.mjs")


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node is not installed; the tile-probe tests need it")
def test_tile_probe() -> None:
    done = subprocess.run(
        ["node", "--test", str(SUITE)],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr
