"""Logging setup and secret redaction.

Normal output goes through :mod:`bottle_net.utils.console`. The standard
:mod:`logging` module carries diagnostic detail, shown on stderr only when
``--debug`` is used, so everyday output stays clean.

Bottle Net Tool never uses credentials, but third-party messages (yt-dlp
debug lines, error texts) can contain signed media URLs or header dumps.
:func:`redact_secrets` masks such values; it is applied to every log record
and to error messages shown to the user.
"""

from __future__ import annotations

import logging
import re

from rich.console import Console
from rich.logging import RichHandler

LOGGER_NAME = "bottle_net"
REDACTED = "[REDACTED]"

_SENSITIVE = r"(?:cookie|set-cookie|authorization|proxy-authorization|x-csrf-token|x-auth-token|password|passwd)"
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Header lines and dict entries: "Cookie: a=b", "'Authorization': 'Bearer x'"
    (re.compile(rf"(?im)(\b['\"]?{_SENSITIVE}['\"]?\s*[:=]\s*)(['\"]?)[^'\"\r\n,}}]+"), rf"\1\2{REDACTED}"),
    # Query parameters that carry tokens or signatures: ?x-signature=...&token=...
    (re.compile(r"(?i)([?&][\w.-]*(?:token|sig|signature|auth|key|session|cookie|secret|password|pwd)[\w.-]*=)"
                r"[^&\s\"'#]+"), rf"\1{REDACTED}"),
    # Credentials embedded in URLs: https://user:pass@host
    (re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), rf"\1{REDACTED}@"),
)


def redact_secrets(text: str) -> str:
    """Mask cookies, authorization headers, tokens and passwords in *text*."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    """Logging filter that applies :func:`redact_secrets` to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = redact_secrets(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


def setup_logging(*, debug: bool = False, console: Console | None = None) -> logging.Logger:
    """Configure the ``bottle_net`` logger and return it.

    With ``debug`` enabled, detailed messages (including yt-dlp output) are
    printed to stderr; otherwise only warnings from libraries appear.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    handler = RichHandler(
        console=console or Console(stderr=True),
        show_time=debug,
        show_path=debug,
        markup=False,
        rich_tracebacks=debug,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if debug else logging.WARNING)
    logger.propagate = False
    return logger
