"""Crawlers that discover public video URLs for an account or Page."""

from __future__ import annotations

from bottle_net.crawlers.base import CrawlResult
from bottle_net.crawlers.facebook import FacebookCrawler
from bottle_net.crawlers.tiktok import TikTokCrawler

__all__ = ["CrawlResult", "FacebookCrawler", "TikTokCrawler"]
