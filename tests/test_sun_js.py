"""Run the front end's solar-arithmetic tests alongside the Python ones.

The sun module is JavaScript because it has to run in the browser sixty times
an hour without asking anything of the network. Its tests are JavaScript for
the same reason -- they exercise the file the browser actually loads, not a
translation of it.

This wrapper exists so nobody has to remember that. A test suite that has to be
run two different ways is a test suite where one of the two quietly stops being
run, and the solar code is the last place to want that: its mistakes draw a
perfectly smooth curve in the wrong place.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SUITE = Path(__file__).with_name("sun.test.mjs")


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node is not installed; the solar tests need it")
def test_sun_module() -> None:
    done = subprocess.run(
        ["node", "--test", str(SUITE)],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    # The runner's own report is far more use here than an assertion message,
    # so it is handed over whole when anything fails.
    assert done.returncode == 0, done.stdout + done.stderr
