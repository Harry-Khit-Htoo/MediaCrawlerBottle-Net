"""TikTok video downloader."""

from __future__ import annotations

import re

from bottle_net.downloaders.common import VideoDownloader
from bottle_net.utils.urls import Platform

_HASHTAGS = re.compile(r"(?:\s*#[^\s#]+)+\s*$")


class TikTokDownloader(VideoDownloader):
    """Downloads public TikTok videos."""

    platform = Platform.TIKTOK

    def clean_title(self, title: str) -> str:
        """Drop the trailing hashtag block that TikTok captions usually end with."""
        cleaned = _HASHTAGS.sub("", title).strip()
        return cleaned or title
