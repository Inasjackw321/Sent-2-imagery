"""Run the markup editor's tests alongside the Python ones.

The arrow is the point of annotating a picture, and the way it goes quietly
wrong is a mark landing somewhere other than where it was drawn -- in the
file that gets sent, not in the preview anybody checked.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SUITE = Path(__file__).with_name("markup.test.mjs")


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node is not installed; the markup tests need it")
def test_markup() -> None:
    done = subprocess.run(
        ["node", "--test", str(SUITE)],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
