"""Publisher errors.

Every failure a user can see is a :class:`PublishError` whose ``message`` is
written for people ("The connected account does not have permission...")
while ``detail`` keeps the technical cause (HTTP status, API error body),
which only goes to the redacted log file, never to the UI.
"""

from __future__ import annotations


class PublisherError(Exception):
    """Base class for expected publisher errors (bad input, not found, ...)."""

    status_code = 400

    def __init__(self, message: str, *, code: str = "error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class NotFoundError(PublisherError):
    status_code = 404


class ValidationError(PublisherError):
    """The user's input cannot be accepted."""


class DuplicateError(PublisherError):
    """The same video is already published (or scheduled) on a platform."""

    status_code = 409

    def __init__(self, message: str, *, platforms: list[str]) -> None:
        super().__init__(message, code="duplicate")
        self.platforms = platforms


class ConflictError(PublisherError):
    """The action is not possible in the current state (e.g. editing a finished job)."""

    status_code = 409


class NotConfiguredError(PublisherError):
    """OAuth client credentials for a platform are missing."""

    status_code = 400


class PublishError(Exception):
    """An upload or API call failed.

    ``retryable`` marks temporary problems (timeouts, HTTP 429/5xx) that the
    retry system may try again; permanent problems (invalid credentials,
    missing permissions, invalid video or metadata) are never retried
    automatically.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        code: str = "error",
        detail: str | None = None,
        reconnect: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.code = code
        self.detail = detail
        #: True when the user must reconnect the account to fix the problem.
        self.reconnect = reconnect

    def __str__(self) -> str:
        return self.message


class UploadCancelled(Exception):
    """The user cancelled an upload that was in progress."""


def friendly(platform: str, explanation: str, action: str | None = None) -> str:
    """Format a user-facing failure message.

    ``YouTube upload failed.`` / blank line / explanation / blank line / action.
    """
    parts = [f"{platform} upload failed.", explanation]
    if action:
        parts.append(action)
    return "\n\n".join(parts)
