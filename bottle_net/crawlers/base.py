"""Types shared by the crawlers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from bottle_net.errors import BottleNetError
from bottle_net.utils.urls import Platform

#: Called with each newly discovered (de-duplicated) video URL.
FoundCallback = Callable[[str], None]
#: Called before a retry with (attempt number, error, delay in seconds).
RetryCallback = Callable[[int, BottleNetError, float], None]


@dataclass
class CrawlResult:
    """Video URLs discovered for one account/Page."""

    platform: Platform
    name: str
    target_url: str
    urls: list[str] = field(default_factory=list)
    #: False if the crawl stopped early (error mid-way, or more results exist
    #: that are not publicly reachable).
    complete: bool = True
    #: Repeated URLs that were found and skipped.
    duplicates: int = 0
    notes: list[str] = field(default_factory=list)
