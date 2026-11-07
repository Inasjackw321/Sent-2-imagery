"""Run the SEN2SR and SR4RS button tests alongside the Python ones.

The backend refuses what a network cannot do; this half decides whether
each button is offered at all.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SUITE = Path(__file__).with_name("networks.test.mjs")


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node is not installed; the SEN2SR button tests need it")
def test_network_buttons() -> None:
    done = subprocess.run(
        ["node", "--test", str(SUITE)],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
