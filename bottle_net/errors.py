"""Exception hierarchy used throughout Bottle Net Tool.

Every error the tool raises on purpose derives from :class:`BottleNetError`,
so the CLI can catch one type and turn it into a readable message:

.. code-block:: text

    [!] Video unavailable.            <- label (short headline)
        It may have been deleted...   <- message (what happened)
        Try ...                       <- hint (what to do next, optional)

Errors marked ``retryable`` are transient (network hiccups, rate limits) and
may be retried with backoff; all others are permanent for the given URL.
"""

from __future__ import annotations


class BottleNetError(Exception):
    """Base class for all expected, user-facing errors."""

    retryable: bool = False
    #: Short headline shown to the user, e.g. "Video unavailable".
    label: str = "Error"
    #: Technical detail (e.g. yt-dlp's original message), shown with --debug.
    detail: str | None = None

    def __init__(self, message: str, *, hint: str | None = None, label: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        if label is not None:
            self.label = label

    def __str__(self) -> str:
        return self.message

    @property
    def summary(self) -> str:
        """One line combining the label and the message (for lists and files)."""
        if not self.message or self.message.rstrip(".").lower() == self.label.lower():
            return f"{self.label}."
        return f"{self.label}: {self.message}"


class ConfigError(BottleNetError):
    """The configuration file is unreadable or contains invalid values."""

    label = "Configuration error"


class InvalidURLError(BottleNetError):
    """The input is not a valid URL (or username) for the requested action."""

    label = "URL is invalid"


class UnsupportedURLError(BottleNetError):
    """The URL belongs to an unsupported platform or content type."""

    label = "Unsupported platform"


class URLListError(BottleNetError):
    """A URL list file is missing, unreadable or empty."""

    label = "URL list problem"


class VideoUnavailableError(BottleNetError):
    """The video was deleted, never existed, or is otherwise unavailable."""

    label = "Video unavailable"


class PrivateContentError(BottleNetError):
    """The content is private and cannot be accessed anonymously."""

    label = "Content is private"


class LoginRequiredError(BottleNetError):
    """The platform requires signing in; the tool does not bypass logins."""

    label = "Login is required"


class BlockedError(BottleNetError):
    """The platform blocked automated access (CAPTCHA, bot check, DRM, ...)."""

    label = "Access blocked by the platform"


class ExtractionError(BottleNetError):
    """The platform response could not be understood (interface changed)."""

    label = "Unable to extract this video"


class StorageError(BottleNetError):
    """A file could not be written (missing folder, permissions, disk full)."""

    label = "Could not save the file"


class NetworkError(BottleNetError):
    """A connection problem or timeout occurred. Usually transient."""

    retryable = True
    label = "Network error"


class NetworkTimeoutError(NetworkError):
    """The platform did not answer in time."""

    label = "Network timeout"


class RateLimitedError(BottleNetError):
    """The platform is rate limiting requests (HTTP 429)."""

    retryable = True
    label = "Rate limit detected"
