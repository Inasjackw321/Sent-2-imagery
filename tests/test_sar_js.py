"""Run the SAR panel's tests alongside the Python ones.

The backend decides what a pass can draw; this half decides whether the
person choosing between passes can see which is which. Both are run by one
command, so neither quietly stops being run.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SUITE = Path(__file__).with_name("sar.test.mjs")


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node is not installed; the SAR panel tests need it")
def test_sar_panel() -> None:
    done = subprocess.run(
        ["node", "--test", str(SUITE)],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
