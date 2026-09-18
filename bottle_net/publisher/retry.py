"""Automatic retry policy for temporary upload failures.

Default schedule (4 attempts in total)::

    Attempt 1 -> at the scheduled time
    Attempt 2 -> +1 minute
    Attempt 3 -> +5 minutes
    Attempt 4 -> +15 minutes

Only errors marked ``retryable`` (network timeouts, connection resets,
HTTP 429, temporary server errors) are retried. Permanent errors (invalid
credentials, missing permissions, invalid video or metadata) fail at once.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_DELAYS = (60, 300, 900)


@dataclass(frozen=True)
class RetryPolicy:
    """How often and how late to retry."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    #: Seconds to wait before attempt 2, 3, 4, ...; the last value repeats.
    delays: tuple[int, ...] = DEFAULT_DELAYS

    def delay_after(self, attempts_made: int) -> int | None:
        """Seconds to wait after *attempts_made* failed attempts, or ``None`` to give up."""
        if attempts_made >= self.max_attempts or not self.delays:
            return None
        return self.delays[min(attempts_made - 1, len(self.delays) - 1)]
