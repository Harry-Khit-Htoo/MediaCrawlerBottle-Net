"""Run the GUI view tests (tests/web/*.test.mjs) with Node's built-in test runner."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

WEB_TESTS = sorted(Path(__file__).with_name("web").glob("*.test.mjs"))


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_gui_views_with_node() -> None:
    result = subprocess.run(["node", "--test", *map(str, WEB_TESTS)], capture_output=True, text=True,
                            encoding="utf-8", timeout=120, cwd=Path(__file__).parents[1])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "# fail 0" in result.stdout or "ℹ fail 0" in result.stdout
