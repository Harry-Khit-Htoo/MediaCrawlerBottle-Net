"""Integration with `yt-dlp <https://github.com/yt-dlp/yt-dlp>`_.

yt-dlp does the platform-specific work of extracting public video
information and media URLs. This module builds its options, routes its log
output into :mod:`logging`, and translates its exceptions into the tool's
own error types.

No credentials or TLS impersonation are configured. Cookies are used only
when the user explicitly passes ``--browser`` for Facebook: then the
Facebook cookies of their own signed-in browser are added to yt-dlp's
cookie jar (see :mod:`bottle_net.browser_cookies`). Without it, only content
available to an anonymous visitor is processed.
"""

from __future__ import annotations

import logging
import re
import shutil
from collections.abc import Callable
from typing import Any

import yt_dlp
from yt_dlp.networking.exceptions import HTTPError, TransportError

from bottle_net.config import Config
from bottle_net.errors import (
    BlockedError,
    BottleNetError,
    ExtractionError,
    LoginRequiredError,
    NetworkError,
    NetworkTimeoutError,
    PrivateContentError,
    RateLimitedError,
    StorageError,
    UnsupportedURLError,
    VideoUnavailableError,
)
from bottle_net.utils.logger import redact_secrets

logger = logging.getLogger("bottle_net.ytdlp")

#: Factory creating a yt-dlp client from an options dict (swappable in tests).
YDLFactory = Callable[[dict[str, Any]], Any]

UPDATE_HINT = "Try updating yt-dlp:\n\n  pip install --upgrade yt-dlp"

_UNAVAILABLE = "It may have been deleted, made private, or restricted in your region."

# Ordered: the first matching rule wins. The message explains what happened;
# the error class provides the short headline (see bottle_net.errors).
_ERROR_RULES: list[tuple[re.Pattern[str], type[BottleNetError], str]] = [
    (re.compile(r"unable to open for writing|unable to rename|no space left|permission denied|"
                r"file name too long|read-only file system|\[errno (?:2|13|28|36)\]", re.I), StorageError,
     "The video file could not be written to disk. Check free space and folder permissions."),
    # TikTok answers requests for deleted/removed posts with this status message.
    (re.compile(r"blocked from accessing this post", re.I), VideoUnavailableError, _UNAVAILABLE),
    (re.compile(r"\b429\b|too many requests|rate.?limit", re.I), RateLimitedError,
     "The platform is temporarily refusing requests. Wait a while before trying again."),
    (re.compile(r"captcha|verify you are human|bot detection|unusual traffic", re.I), BlockedError,
     "The platform asked for a CAPTCHA or blocked automated access. Bottle Net Tool does not bypass this."),
    (re.compile(r"\bdrm\b", re.I), BlockedError,
     "The video is DRM-protected. Bottle Net Tool does not bypass DRM."),
    (re.compile(r"private", re.I), PrivateContentError,
     "This content is private or not visible to the account being used."),
    (re.compile(r"log ?in|sign ?in|cookies|authenticat|account is required", re.I), LoginRequiredError,
     "The platform requires signing in to view this content."),
    (re.compile(r"unsupported url", re.I), UnsupportedURLError, "yt-dlp does not support this URL."),
    (re.compile(r"\b(?:404|410)\b|not found|unavailable|removed|deleted|does not exist|no longer|"
                r"not available|no video formats", re.I), VideoUnavailableError, _UNAVAILABLE),
    (re.compile(r"timed? ?out", re.I), NetworkTimeoutError,
     "The platform did not respond in time. Check your connection and try again."),
    (re.compile(r"connection|getaddrinfo|name resolution|network|ssl|reset by peer|"
                r"remote end closed|incompleteread|\b50[0234]\b", re.I), NetworkError,
     "A connection problem occurred. Check your internet connection and try again."),
    (re.compile(r"\b403\b|forbidden", re.I), BlockedError, "The platform refused the request (HTTP 403)."),
]


class YtDlpLogger:
    """Routes yt-dlp messages to :mod:`logging` instead of printing them."""

    def debug(self, msg: str) -> None:
        logger.debug(msg)

    def info(self, msg: str) -> None:
        logger.debug(msg)

    def warning(self, msg: str) -> None:
        # yt-dlp warnings are mostly advisory; keep them for --debug.
        logger.debug("yt-dlp warning: %s", msg)

    def error(self, msg: str) -> None:
        logger.debug("yt-dlp error: %s", msg)


def has_ffmpeg() -> bool:
    """Return True if ffmpeg is available for merging video and audio."""
    return shutil.which("ffmpeg") is not None


def format_selector(ffmpeg: bool) -> str:
    """Return a yt-dlp format selector.

    With ffmpeg, the best video and audio streams are merged. Without it,
    the best single file that already contains both is chosen.
    """
    if ffmpeg:
        return "bv*+ba/b"
    return "b[ext=mp4][acodec!=none][vcodec!=none]/b[acodec!=none][vcodec!=none]/b"


def base_options(config: Config) -> dict[str, Any]:
    """Options shared by every yt-dlp call made by the tool."""
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": YtDlpLogger(),
        "socket_timeout": config.timeout,
        # Bottle Net handles retries itself (see downloaders.common.retry_call),
        # but lets yt-dlp retry individual fragments of a stream.
        "retries": 0,
        "fragment_retries": max(config.max_retries, 1),
        "extractor_retries": 0,
        "noplaylist": True,
        # Unfinished downloads stay as ".part" files and resume next time.
        "continuedl": True,
        "nopart": False,
        "overwrites": False,
        "windowsfilenames": True,
        # Never read credentials files; browser cookies are added only with --browser.
        "cookiefile": None,
        "usenetrc": False,
    }


def default_factory(options: dict[str, Any]) -> yt_dlp.YoutubeDL:
    """Create a real :class:`yt_dlp.YoutubeDL` client."""
    return yt_dlp.YoutubeDL(options)


def _clean_message(message: str) -> str:
    message = re.sub(r"^ERROR:\s*", "", message.strip())
    message = re.sub(r"^\[[\w:]+\]\s*(?:[\w.@-]+:\s*)?", "", message)
    # Drop yt-dlp's generic "please report this issue" boilerplate.
    message = re.split(r";\s*please report this issue|\s+please report this issue", message, flags=re.I)[0]
    message = re.split(r"\s+Use --cookies", message)[0]
    return redact_secrets(message.strip().rstrip(";").strip()) or "Unknown error"


def classify_error(exc: BaseException) -> BottleNetError:
    """Translate a yt-dlp (or network) exception into a :class:`BottleNetError`.

    The original (redacted) yt-dlp message is kept in ``error.detail`` and
    logged at debug level, so ``--debug`` shows the technical cause behind
    every friendly message.
    """
    if isinstance(exc, BottleNetError):
        return exc

    cause: BaseException | None = exc
    exc_info = getattr(exc, "exc_info", None)
    if exc_info and len(exc_info) > 1 and exc_info[1] is not None:
        cause = exc_info[1]
    # ExtractorError wraps the underlying network error in .cause.
    inner = getattr(cause, "cause", None)
    if isinstance(inner, BaseException):
        cause = inner

    raw = _clean_message(str(exc))
    logger.debug("yt-dlp reported: %s", raw)
    error = _classify(cause, raw)
    error.detail = raw
    return error


def _classify(cause: BaseException | None, raw: str) -> BottleNetError:
    if isinstance(cause, HTTPError):
        status = cause.status
        if status == 429:
            return RateLimitedError(
                "The platform is temporarily refusing requests (HTTP 429). Wait a while before trying again."
            )
        if status in (404, 410):
            return VideoUnavailableError(f"The platform reports that it does not exist (HTTP {status}).")
        if status == 401:
            return LoginRequiredError(
                "The platform requires signing in (HTTP 401). Bottle Net Tool does not sign in."
            )
        if status >= 500:
            return NetworkError(f"The platform returned a server error (HTTP {status}). Try again later.")
    elif isinstance(cause, TimeoutError) or "timed out" in raw.lower():
        return NetworkTimeoutError("The platform did not respond in time. Check your connection and try again.")
    elif isinstance(cause, (TransportError, ConnectionError)):
        return NetworkError("A connection problem occurred. Check your internet connection and try again.")

    for pattern, error_type, message in _ERROR_RULES:
        if pattern.search(raw):
            return error_type(message)

    return ExtractionError(
        "The website may have changed or the video may no longer be publicly available.",
        hint=UPDATE_HINT,
    )
