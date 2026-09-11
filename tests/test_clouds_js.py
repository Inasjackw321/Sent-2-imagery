"""Run the cloud mask's tests alongside the Python ones.

Same reason as the solar tests next door: the mask is JavaScript because it
runs on every tile in the browser, its tests are JavaScript so they exercise
the file the browser actually loads, and this wrapper exists so that nobody
has to remember to run the suite a second way. A suite that needs remembering
is a suite that quietly stops being run.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SUITE = Path(__file__).with_name("clouds.test.mjs")


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node is not installed; the cloud-mask tests need it")
def test_cloud_mask() -> None:
    done = subprocess.run(
        ["node", "--test", str(SUITE)],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
