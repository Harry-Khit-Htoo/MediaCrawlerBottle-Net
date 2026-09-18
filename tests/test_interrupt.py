"""Ctrl+C handling, tested in a real subprocess with a real SIGINT."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HARNESS = Path(__file__).with_name("interrupt_harness.py")


def run_harness(workdir: Path, interrupt_after: str, *url_list: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "COLUMNS": "120"}
    return subprocess.run(
        [sys.executable, str(HARNESS), str(workdir), interrupt_after, *url_list],
        capture_output=True, text=True, encoding="utf-8", timeout=60, env=env,
    )


def test_ctrl_c_during_batch_then_resume(tmp_path: Path) -> None:
    # Video 1 finishes instantly; video 2 takes ~4 s and is interrupted after ~1 s.
    interrupted = run_harness(tmp_path, "1.0")

    assert interrupted.returncode == 130, interrupted.stderr
    err = interrupted.stderr
    assert "Traceback" not in err and "KeyboardInterrupt" not in err
    assert "[!] Download interrupted by user." in err
    assert "Completed:     1" in err
    assert "Remaining:     2" in err
    assert "Partial files have been handled safely" in err
    assert interrupted.stdout == ""  # nothing but data may go to stdout

    dest = tmp_path / "downloads" / "tiktok" / "videos"
    assert [p.name for p in dest.glob("*_1.mp4")], "video 1 should be complete"
    assert list(dest.glob("*_2.mp4.part")), "video 2 should be left as a .part file"
    assert not list(dest.glob("*_2.mp4")), "a partial file must never look complete"

    failed = (tmp_path / "output" / "failed_tiktok.txt").read_text(encoding="utf-8")
    assert "video/2" in failed and "video/3" in failed and "video/1\n" not in failed

    # Resuming from the failed-URL file puts videos into the ORIGINAL folder,
    # not into a new "failed_tiktok" folder named after the list.
    resumed = run_harness(tmp_path, "never", "output/failed_tiktok.txt")
    assert resumed.returncode == 0, resumed.stderr
    assert "Using the download folder recorded" in resumed.stderr
    assert sorted(p.name.split("_")[-1] for p in dest.glob("*.mp4")) == ["1.mp4", "2.mp4", "3.mp4"]
    assert not list(dest.glob("*.part"))
    assert not (tmp_path / "downloads" / "tiktok" / "failed_tiktok").exists()
