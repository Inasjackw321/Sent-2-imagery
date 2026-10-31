"""Run the SEN2SR button's tests alongside the Python ones.

The backend refuses what SEN2SR cannot do; this half decides whether the
button is offered at all. Both are run by one command, so neither quietly
stops being run.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SUITE = Path(__file__).with_name("sen2sr.test.mjs")


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node is not installed; the SEN2SR button tests need it")
def test_sen2sr_button() -> None:
    done = subprocess.run(
        ["node", "--test", str(SUITE)],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
