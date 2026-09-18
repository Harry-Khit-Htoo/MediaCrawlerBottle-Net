"""Video downloaders for each supported platform."""

from __future__ import annotations

from bottle_net.config import Config
from bottle_net.downloaders.auto import AutoDownloader
from bottle_net.downloaders.common import (
    BatchCallbacks,
    BatchDownloader,
    BatchSummary,
    DownloadResult,
    DownloadStatus,
    VideoDownloader,
    VideoInfo,
    retry_call,
    run_batch,
)
from bottle_net.downloaders.facebook import FacebookDownloader
from bottle_net.downloaders.tiktok import TikTokDownloader
from bottle_net.utils.urls import Platform

__all__ = [
    "AutoDownloader",
    "BatchCallbacks",
    "BatchDownloader",
    "BatchSummary",
    "DownloadResult",
    "DownloadStatus",
    "FacebookDownloader",
    "TikTokDownloader",
    "VideoDownloader",
    "VideoInfo",
    "get_downloader",
    "retry_call",
    "run_batch",
]

_DOWNLOADERS: dict[Platform, type[VideoDownloader]] = {
    Platform.TIKTOK: TikTokDownloader,
    Platform.FACEBOOK: FacebookDownloader,
}


def get_downloader(platform: Platform, config: Config, **kwargs: object) -> VideoDownloader:
    """Return a downloader instance for *platform*."""
    return _DOWNLOADERS[platform](config, **kwargs)  # type: ignore[arg-type]
