"""Integration with the existing Bottle Net downloader ("Download & Schedule").

Downloads run exactly as ``bottle-net <platform> download`` does - same
downloader, same folders (``downloads/<platform>/``) - in a background
thread. When a download finishes, the file is added to the video library so
it can be scheduled. The downloader itself is not modified.
"""

from __future__ import annotations

import itertools
import logging
import threading
from collections.abc import Callable
from typing import Any

from bottle_net.config import Config, load_config
from bottle_net.downloaders import DownloadStatus, get_downloader
from bottle_net.errors import BottleNetError
from bottle_net.publisher.errors import ValidationError
from bottle_net.publisher.library import VideoLibrary
from bottle_net.utils.urls import detect_platform

logger = logging.getLogger(__name__)


class DownloadManager:
    """Runs downloads in the background and tracks their progress."""

    def __init__(self, library: VideoLibrary, *, config: Callable[[], Config] = load_config,
                 downloader_factory: Callable[..., Any] = get_downloader, run_async: bool = True) -> None:
        self._library = library
        self._config = config
        self._factory = downloader_factory
        self._run_async = run_async
        self._tasks: dict[int, dict[str, Any]] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def start(self, url: str) -> dict[str, Any]:
        platform = detect_platform(url or "")
        if platform is None:
            raise ValidationError("Enter a TikTok or Facebook video URL.")
        task_id = next(self._ids)
        task = {"id": task_id, "url": url.strip(), "platform": platform.value, "status": "downloading",
                "progress": 0.0, "error": None, "video_id": None}
        with self._lock:
            self._tasks[task_id] = task
        if self._run_async:
            threading.Thread(target=self._run, args=(task_id,), daemon=True, name=f"download-{task_id}").start()
        else:
            self._run(task_id)
        return self.get(task_id)

    def get(self, task_id: int) -> dict[str, Any]:
        with self._lock:
            return dict(self._tasks[task_id])

    def tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(t) for t in sorted(self._tasks.values(), key=lambda t: -t["id"])]

    def _update(self, task_id: int, **fields: Any) -> None:
        with self._lock:
            self._tasks[task_id].update(fields)

    def _run(self, task_id: int) -> None:
        task = self.get(task_id)
        platform = detect_platform(task["url"])
        assert platform is not None
        try:
            config = self._config()
            downloader = self._factory(platform, config)

            def hook(status: dict[str, Any]) -> None:
                total = status.get("total_bytes") or status.get("total_bytes_estimate")
                if total and status.get("downloaded_bytes") is not None:
                    self._update(task_id, progress=round(100 * status["downloaded_bytes"] / total, 1))

            result = downloader.download(task["url"], config.platform_download_dir(platform), progress_hook=hook)
            if result.status is DownloadStatus.FAILED or result.path is None:
                self._update(task_id, status="failed", error=result.reason or "Download failed.")
                return
            video = self._library.import_path(result.path, source="download", source_url=task["url"])
            self._update(task_id, status="completed", progress=100.0, video_id=video.id)
        except BottleNetError as exc:
            self._update(task_id, status="failed", error=exc.summary)
        except Exception as exc:  # noqa: BLE001 - report instead of crashing the thread
            logger.exception("Download task %s failed", task_id)
            self._update(task_id, status="failed", error=f"Unexpected error: {exc.__class__.__name__}")
