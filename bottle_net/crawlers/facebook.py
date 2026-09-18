"""Facebook Page/profile crawler.

Facebook serves the first batch of a public Page's videos and reels inside
the HTML of its ``/videos`` and ``/reels`` tabs, which anyone can open
without signing in. This crawler reads those public pages and extracts the
video links.

Loading *more* videos than that first batch requires Facebook's private,
session-bound API. Bottle Net Tool does not use it and does not bypass login
walls, so for Pages with many videos the result may be incomplete; this is
reported to the user.

Pages that are only visible to signed-in users can be crawled with the
user's own browser session (``--browser``, see :mod:`bottle_net.browser_cookies`):
only facebook.com cookies are sent, and only to facebook.com.
"""

from __future__ import annotations

import html as html_lib
import http.cookiejar
import logging
import re
import time
from collections.abc import Callable
from typing import Any, Protocol

import requests

from bottle_net.config import Config
from bottle_net.crawlers.base import CrawlResult, FoundCallback, RetryCallback
from bottle_net.downloaders.common import retry_call
from bottle_net.errors import (
    BlockedError,
    BottleNetError,
    InvalidURLError,
    LoginRequiredError,
    NetworkError,
    RateLimitedError,
)
from bottle_net.utils.urls import FacebookTarget, Platform, facebook_video_url, parse_facebook_target, video_key

logger = logging.getLogger(__name__)

# Ordinary desktop browser headers. Cookies are sent only with --browser (the user's own session).
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Mode": "navigate",
}

#: Page tabs that list videos, in crawl order.
VIDEO_TABS = ("videos", "reels")

_VIDEO_LINK = re.compile(r"facebook\.com/(?P<owner>[\w.-]+)/videos/(?:[\w.%-]+/)*?(?P<id>\d{8,})")
_RELATIVE_VIDEO_LINK = re.compile(r"[\"'](?:/(?P<owner>[\w.-]+))?/videos/(?:[\w.%-]+/)*?(?P<id>\d{8,})")
_WATCH_LINK = re.compile(r"/watch/?\?v=(?P<id>\d{8,})")
_REEL_LINK = re.compile(r"/reels?/(?P<id>\d{8,})")
_VIDEO_ID_FIELD = re.compile(r'"(?:video_id|videoId)"\s*:\s*"(?P<id>\d{8,})"')
_HAS_NEXT_PAGE = re.compile(r'"has_next_page"\s*:\s*true')
_LOGIN_MARKERS = ("/login/", "/login.php", "checkpoint")


class HTTPSession(Protocol):
    """The subset of :class:`requests.Session` used by the crawler."""

    def get(self, url: str, **kwargs: object) -> requests.Response: ...


def extract_video_urls(page_html: str, *, owner: str | None = None) -> list[str]:
    """Extract canonical video/reel URLs from a Facebook page's HTML.

    Links like ``/<page>/videos/<id>`` that belong to a *different* Page than
    *owner* (e.g. suggested content) are ignored. Results are de-duplicated
    by video ID and returned in page order.
    """
    text = html_lib.unescape(page_html.replace("\\/", "/"))
    found: dict[str, str] = {}
    owner_key = owner.lower() if owner else None

    def add(video_id: str, *, reel: bool = False, link_owner: str | None = None) -> None:
        if owner_key and link_owner and link_owner.lower() not in (owner_key, "watch"):
            return
        found.setdefault(video_id, facebook_video_url(video_id, reel=reel))

    matches: list[tuple[int, str, bool, str | None]] = []
    for pattern in (_VIDEO_LINK, _RELATIVE_VIDEO_LINK):
        matches += [(m.start(), m.group("id"), False, m.group("owner")) for m in pattern.finditer(text)]
    matches += [(m.start(), m.group("id"), False, None) for m in _WATCH_LINK.finditer(text)]
    matches += [(m.start(), m.group("id"), True, None) for m in _REEL_LINK.finditer(text)]
    matches += [(m.start(), m.group("id"), False, None) for m in _VIDEO_ID_FIELD.finditer(text)]

    for _, video_id, reel, link_owner in sorted(matches, key=lambda m: m[0]):
        add(video_id, reel=reel, link_owner=link_owner)
    return list(found.values())


def looks_like_login_wall(final_url: str, page_html: str) -> bool:
    """Return True if Facebook redirected to, or only served, a login page."""
    if any(marker in final_url for marker in _LOGIN_MARKERS):
        return True
    return 'id="login_form"' in page_html and "/videos/" not in page_html and "/reel/" not in page_html


class FacebookCrawler:
    """Discover publicly accessible video URLs on a Facebook Page/profile."""

    platform = Platform.FACEBOOK

    def __init__(
        self,
        config: Config,
        *,
        session: HTTPSession | None = None,
        sleep: Callable[[float], None] = time.sleep,
        cookies: http.cookiejar.CookieJar | None = None,
        browser_name: str | None = None,
    ) -> None:
        self.config = config
        self._session = session or requests.Session()
        self._sleep = sleep
        #: Facebook cookies of the user's own browser session (``--browser``), if any.
        self._cookies = cookies
        self._browser_name = browser_name

    def fetch(self, url: str, *, on_retry: RetryCallback | None = None) -> str:
        """GET a public Facebook page, mapping HTTP problems to tool errors."""

        def attempt() -> str:
            try:
                extra: dict[str, Any] = {"cookies": self._cookies} if self._cookies is not None else {}
                response = self._session.get(url, headers=REQUEST_HEADERS, timeout=self.config.timeout, **extra)
            except requests.Timeout as exc:
                raise NetworkError(f"Timed out after {self.config.timeout:.0f}s loading {url}") from exc
            except requests.RequestException as exc:
                raise NetworkError(f"Could not connect to Facebook: {exc.__class__.__name__}") from exc

            status = response.status_code
            if status == 429:
                raise RateLimitedError("Facebook is rate limiting requests (HTTP 429).")
            if status == 404:
                raise InvalidURLError(f"Facebook Page not found: {url}")
            if status in (401, 403):
                raise BlockedError(f"Facebook refused the request (HTTP {status}).")
            if status >= 500:
                raise NetworkError(f"Facebook returned a server error (HTTP {status}).")
            if status >= 400:
                raise BlockedError(
                    f"Facebook rejected the request (HTTP {status}).",
                    hint="Facebook may be blocking automated access from your network.",
                )
            body = response.text
            if looks_like_login_wall(response.url or url, body):
                if self._cookies is not None:
                    raise LoginRequiredError(
                        f"Facebook still asks to sign in, using the login from {self._browser_name}.",
                        hint="The session may have expired (log in to facebook.com in that browser again), or this "
                             "account is not allowed to see the Page. Bottle Net does not bypass access restrictions.",
                    )
                raise LoginRequiredError(
                    "Facebook requires signing in to view this Page.",
                    hint="If your Facebook account can see it, log in to facebook.com in your browser and add "
                         "--browser chrome (or firefox, edge, brave, ...). Bottle Net never asks for your password.",
                )
            return body

        return retry_call(attempt, retries=self.config.max_retries, sleep=self._sleep, on_retry=on_retry)

    def crawl(
        self,
        target: str,
        *,
        limit: int | None = None,
        on_found: FoundCallback | None = None,
        on_retry: RetryCallback | None = None,
    ) -> CrawlResult:
        """Crawl the public video tabs of *target* (Page name or URL)."""
        page = parse_facebook_target(target)
        result = CrawlResult(platform=self.platform, name=page.name, target_url=page.url)
        errors: list[BottleNetError] = []
        more_available = False
        seen: set[str] = set()

        for position, tab in enumerate(VIDEO_TABS):
            if position and self.config.request_delay:
                self._sleep(self.config.request_delay)
            url = page.tab_url(tab)
            try:
                body = self.fetch(url, on_retry=on_retry)
            except BottleNetError as exc:
                logger.debug("Failed to load %s: %s", url, exc)
                errors.append(exc)
                continue

            more_available = more_available or bool(_HAS_NEXT_PAGE.search(body))
            for video_url in extract_video_urls(body, owner=self._owner_filter(page)):
                key = video_key(video_url)
                if key in seen:
                    result.duplicates += 1  # e.g. the same video listed on both tabs
                    continue
                seen.add(key)
                result.urls.append(video_url)
                if on_found:
                    on_found(video_url)
                if limit is not None and len(result.urls) >= limit:
                    result.notes.append(f"Stopped at the requested limit of {limit} videos.")
                    return result

        if errors and len(errors) == len(VIDEO_TABS):
            raise errors[0]
        if errors:
            result.complete = False
            result.notes.append(f"Some tabs could not be loaded: {errors[0].summary}")
        if more_available:
            # Facebook signals more results that only a signed-in session or its
            # private API can load; the CLI explains this limitation.
            result.complete = False
        if not result.urls:
            result.notes.append(
                "No public videos were found. The Page may have no public videos, "
                "or Facebook may not show them without signing in."
            )
        return result

    @staticmethod
    def _owner_filter(page: FacebookTarget) -> str | None:
        # Numeric profiles link videos by vanity name, so owner filtering
        # only applies to named Pages.
        return None if page.numeric_profile else page.name
