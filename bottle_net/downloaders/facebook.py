"""Facebook video downloader."""

from __future__ import annotations

import re

from bottle_net.downloaders.common import VideoDownloader
from bottle_net.utils.urls import Platform

# Facebook titles often start with engagement counts, e.g.
# "10M views · 240K reactions | Come watch a spacewalk with us!".
_ENGAGEMENT_PREFIX = re.compile(
    r"^(?:[\d.,]+[KMB]?\s+(?:views?|reactions?|comments?|shares?|plays?)\s*[·|]\s*)+\|?\s*",
    re.IGNORECASE,
)


class FacebookDownloader(VideoDownloader):
    """Downloads publicly accessible Facebook videos and reels."""

    platform = Platform.FACEBOOK

    def clean_title(self, title: str) -> str:
        """Remove the view/reaction counts Facebook prepends to titles."""
        cleaned = _ENGAGEMENT_PREFIX.sub("", title).strip()
        return cleaned or title
