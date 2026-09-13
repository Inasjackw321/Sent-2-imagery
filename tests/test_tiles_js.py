"""Run the tile-probe tests alongside the Python ones.

Same wrapper as the solar and cloud-mask suites next door, for the same
reason: a test suite that has to be remembered is a test suite that quietly
stops being run.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SUITE = Path(__file__).with_name("tiles.test.mjs")


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node is not installed; the tile-probe tests need it")
def test_tile_probe() -> None:
    done = subprocess.run(
        ["node", "--test", str(SUITE)],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_tracker_script_parses_as_a_browser_module():
    """`node --check` is not enough for this file, and that cost a broken page.

    A stray brace left after an edit closed the enclosing function, which made
    the `return` after it top-level. In CommonJS -- which is how `node --check`
    reads a .js -- a top-level return is LEGAL, so the check passed; the
    browser refused the whole script with "Illegal return statement" and the
    layer simply never appeared.

    Parsed as a module here, which is how the page loads it, so the same
    mistake fails in the test suite rather than on a screen.
    """
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for script in sorted((root / "frontend" / "js").glob("*.js")):
        got = subprocess.run(
            ["node", "--input-type=module", "--check"],
            input=script.read_text(), capture_output=True, text=True)
        assert got.returncode == 0, f"{script.name}:\n{got.stderr}"
    print("checked", file=sys.stderr)
