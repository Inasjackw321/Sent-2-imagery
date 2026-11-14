"""Run the video editor tests alongside the Python ones.

Crops, output sizes, the look and file names.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SUITE = Path(__file__).with_name("videoeditor.test.mjs")


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node is not installed; the video editor tests need it")
def test_video_editor() -> None:
    done = subprocess.run(
        ["node", "--test", str(SUITE)],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
