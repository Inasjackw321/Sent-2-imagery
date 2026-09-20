"""Run the live-satellite panel's tests alongside the Python ones.

Same reason as the cloud-mask and solar wrappers next door: the panel is
JavaScript because it runs in the browser, its tests are JavaScript so they
exercise the file the browser actually loads, and this wrapper exists so
nobody has to remember to run the suite a second way. A suite that needs
remembering is a suite that quietly stops being run.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SUITE = Path(__file__).with_name("copernicus.test.mjs")


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node is not installed; the panel's tests need it")
def test_live_satellite_panel() -> None:
    done = subprocess.run(
        ["node", "--test", str(SUITE)],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
