"""Run the picture-saving tests alongside the Python ones.

Same reason as the wrappers next door: saving happens in the browser, its
tests are JavaScript so they exercise the file the browser loads, and this
wrapper exists so nobody has to remember to run the suite a second way.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SUITE = Path(__file__).with_name("saving.test.mjs")


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node is not installed; the saving tests need it")
def test_saving_a_picture() -> None:
    done = subprocess.run(
        ["node", "--test", str(SUITE)],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
