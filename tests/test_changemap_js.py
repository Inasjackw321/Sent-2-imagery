"""Run the change map panel tests alongside the Python ones.

Its words, quick dates, plan summary and save addresses.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SUITE = Path(__file__).with_name("changemap.test.mjs")


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node is not installed; the change map panel tests need it")
def test_change_map_panel() -> None:
    done = subprocess.run(
        ["node", "--test", str(SUITE)],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
