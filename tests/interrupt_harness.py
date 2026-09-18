"""Run the real ``bottle-net`` CLI in this process and interrupt it with SIGINT.

Used by ``test_interrupt.py`` as a subprocess:

    python interrupt_harness.py <workdir> <interrupt-after-seconds | never> [<url-list>]

The network is replaced by a fake yt-dlp whose second video downloads
slowly, writing a ``.part`` file like yt-dlp does. A timer raises a real
SIGINT (what Ctrl+C sends) while that download is in progress.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from conftest import FakeVideo, FakeYDL, FakeYDLFactory  # noqa: E402

from bottle_net import commands  # noqa: E402
from bottle_net.cli import main  # noqa: E402
from bottle_net.downloaders import get_downloader  # noqa: E402

URL = "https://www.tiktok.com/@user/video/{}"
SLOW_VIDEO = "2"


class SlowYDL(FakeYDL):
    """Writes the slow video in chunks to a ``.part`` file, like yt-dlp."""

    def process_ie_result(self, info: dict[str, Any], download: bool = True) -> dict[str, Any]:
        if info["id"] == SLOW_VIDEO:
            home = Path(self.params["paths"]["home"])
            part = home / f"{info['bottle_net_filename']}.{info['ext']}.part"
            for chunk in range(40):
                with part.open("ab") as fh:
                    fh.write(b"\x00" * 1024)
                for hook in self.params.get("progress_hooks", []):
                    hook({"status": "downloading", "downloaded_bytes": (chunk + 1) * 1024,
                          "total_bytes": 40 * 1024, "filename": str(part)})
                time.sleep(0.1)
            part.unlink()
        return super().process_ie_result(info, download)


class SlowFactory(FakeYDLFactory):
    def __call__(self, params: dict[str, Any]) -> SlowYDL:
        self.options.append(params)
        return SlowYDL(self, params)


def run(workdir: Path, interrupt_after: float | None, url_list: str = "videos.txt") -> int:
    os.chdir(workdir)
    factory = SlowFactory([FakeVideo(str(i)) for i in (1, 2, 3)])
    commands.get_downloader = lambda platform, config, **_: get_downloader(  # type: ignore[assignment]
        platform, config, ydl_factory=factory, sleep=lambda _: None, ffmpeg=False
    )
    Path("config.toml").write_text("request_delay = 0\n", encoding="utf-8")
    if not Path("videos.txt").exists():
        Path("videos.txt").write_text("\n".join(URL.format(i) for i in (1, 2, 3)) + "\n", encoding="utf-8")
    if interrupt_after is not None:
        # signal.raise_signal delivers a genuine SIGINT to this process; Python
        # turns it into KeyboardInterrupt in the main thread, exactly as Ctrl+C does.
        threading.Timer(interrupt_after, signal.raise_signal, args=(signal.SIGINT,)).start()
    return main(["tiktok", "download-list", url_list])


if __name__ == "__main__":
    delay = None if sys.argv[2] == "never" else float(sys.argv[2])
    sys.exit(run(Path(sys.argv[1]), delay, *sys.argv[3:4]))
