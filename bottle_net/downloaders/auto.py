"""Downloader that detects the platform of every URL (used by the universal commands)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from bottle_net.config import Config
from bottle_net.downloaders.common import DownloadResult, DownloadStatus, VideoDownloader
from bottle_net.errors import UnsupportedURLError
from bottle_net.utils.urls import Platform, detect_platform


class AutoDownloader:
    """Routes each URL to the TikTok or Facebook downloader.

    With a *subfolder*, videos are saved to ``<dest_dir>/<platform>/<subfolder>``
    (e.g. ``downloads/tiktok/example``); without one, directly in ``dest_dir``.
    """

    def __init__(
        self,
        config: Config,
        downloader_for: Callable[[Platform], VideoDownloader],
        *,
        subfolder: str | None = None,
    ) -> None:
        self.config = config
        self._downloader_for = downloader_for
        self._downloaders: dict[Platform, VideoDownloader] = {}
        self.subfolder = subfolder

    def destination(self, dest_dir: Path, platform: Platform) -> Path:
        """Return the folder videos for *platform* are saved in."""
        return dest_dir / platform.value / self.subfolder if self.subfolder else dest_dir

    def download(self, url: str, dest_dir: Path, **kwargs: Any) -> DownloadResult:
        """Download *url*, choosing the platform from the URL itself."""
        platform = detect_platform(url)
        if platform is None:
            error = UnsupportedURLError("Unsupported or unknown URL (supported: TikTok, Facebook).")
            return DownloadResult(url=url, status=DownloadStatus.FAILED, error=error)
        if platform not in self._downloaders:
            self._downloaders[platform] = self._downloader_for(platform)
        return self._downloaders[platform].download(url, self.destination(dest_dir, platform), **kwargs)
