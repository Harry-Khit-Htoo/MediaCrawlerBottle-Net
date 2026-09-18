"""TikTok profile crawler.

Uses yt-dlp's ``tiktok:user`` extractor in "flat" mode: it pages through the
public video list of an account (following TikTok's own pagination cursor)
and yields video URLs without downloading anything.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from typing import Any

from bottle_net.config import Config
from bottle_net.crawlers.base import CrawlResult, FoundCallback, RetryCallback
from bottle_net.downloaders.common import retry_call
from bottle_net.errors import BottleNetError, ExtractionError, InvalidURLError, UnsupportedURLError
from bottle_net.utils.urls import (
    Platform,
    normalize_tiktok_video_url,
    parse_tiktok_account,
    tiktok_profile_url,
    video_key,
)
from bottle_net.ytdlp import YDLFactory, base_options, classify_error, default_factory

logger = logging.getLogger(__name__)


class TikTokCrawler:
    """Discover public video URLs for a TikTok account."""

    platform = Platform.TIKTOK

    def __init__(
        self,
        config: Config,
        *,
        ydl_factory: YDLFactory = default_factory,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self._factory = ydl_factory
        self._sleep = sleep

    def options(self) -> dict[str, Any]:
        """yt-dlp options for listing (not downloading) an account's videos."""
        opts = base_options(self.config)
        opts.update(
            {
                "extract_flat": "in_playlist",
                "skip_download": True,
                "noplaylist": False,
                # Pause between pagination requests to stay polite.
                "sleep_interval_requests": self.config.request_delay,
            }
        )
        return opts

    def crawl(
        self,
        account: str,
        *,
        limit: int | None = None,
        on_found: FoundCallback | None = None,
        on_retry: RetryCallback | None = None,
    ) -> CrawlResult:
        """Crawl *account* (``@name``, ``name`` or profile URL).

        Raises :class:`BottleNetError` if the profile cannot be loaded at
        all. If an error occurs after some videos were found, the partial
        result is returned with ``complete=False``.
        """
        username = parse_tiktok_account(account)
        profile_url = tiktok_profile_url(username)
        result = CrawlResult(platform=self.platform, name=username, target_url=profile_url)

        def report_retry(attempt: int, error: BottleNetError, delay: float) -> None:
            if on_retry is None:
                return
            if isinstance(error, ExtractionError):
                error = ExtractionError(
                    "TikTok did not return the profile data", label="TikTok did not return the profile data"
                )
            on_retry(attempt, error, delay)

        with self._factory(self.options()) as ydl:
            try:
                # TikTok intermittently serves profile pages without the
                # embedded account data ("Unable to extract secondary user
                # ID"); asking again usually succeeds, so extraction errors
                # are retried here too.
                info = retry_call(
                    lambda: ydl.extract_info(profile_url, download=False, process=False),
                    retries=self.config.max_retries,
                    sleep=self._sleep,
                    on_retry=report_retry,
                    also_retry=(ExtractionError,),
                )
            except BottleNetError as exc:
                raise self._explain(exc, username) from exc

            entries = info.get("entries") if isinstance(info, dict) else None
            if entries is None:
                raise ExtractionError(
                    f"TikTok did not return a video list for @{username}.",
                    hint="The account may not exist, or TikTok may have changed its public interface.",
                )

            seen: set[str] = set()
            pages = iter(self._entry_urls(entries, username))
            while limit is None or len(result.urls) < limit:
                # Only errors from TikTok's pagination are handled here; errors
                # raised by on_found (e.g. a closed output pipe) propagate.
                try:
                    url = next(pages)
                except StopIteration:
                    break
                except KeyboardInterrupt:
                    raise
                except Exception as exc:  # noqa: BLE001 - classified below
                    error = classify_error(exc)
                    if not result.urls:
                        raise self._explain(error, username) from exc
                    result.complete = False
                    result.notes.append(f"Crawl stopped early: {error.summary}")
                    logger.debug("TikTok pagination failed", exc_info=exc)
                    break

                key = video_key(url)
                if key in seen:
                    result.duplicates += 1
                    continue
                seen.add(key)
                result.urls.append(url)
                if on_found:
                    on_found(url)
            else:
                result.notes.append(f"Stopped at the requested limit of {limit} videos.")

        if not result.urls:
            result.notes.append(
                "No public videos were found. The account may be private, have no videos, "
                "or TikTok may be limiting anonymous access."
            )
        return result

    @staticmethod
    def _entry_urls(entries: Iterable[Any], username: str) -> Iterable[str]:
        """Yield canonical video URLs from yt-dlp playlist entries."""
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            candidates = [entry.get("url"), entry.get("webpage_url")]
            if entry.get("id"):
                candidates.append(f"https://www.tiktok.com/@{username}/video/{entry['id']}")
            for candidate in candidates:
                if not candidate:
                    continue
                try:
                    yield normalize_tiktok_video_url(str(candidate))
                    break
                except (InvalidURLError, UnsupportedURLError):
                    continue  # photo posts and unexpected URLs are skipped

    @staticmethod
    def _explain(error: BottleNetError, username: str) -> BottleNetError:
        """Add crawl-specific context to an error."""
        if isinstance(error, ExtractionError):
            return ExtractionError(
                f"Could not load the TikTok profile @{username}. Check that the username exists and the "
                "account is public. TikTok sometimes refuses anonymous requests for a while, so trying "
                "again later can also help.",
                hint="If public profiles keep failing, try updating yt-dlp:\n\n  pip install --upgrade yt-dlp",
                label="Unable to load the profile",
            )
        return error
