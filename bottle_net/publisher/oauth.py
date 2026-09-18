"""Shared OAuth 2.0 helpers: single-use ``state`` values and PKCE.

The authorization flows run in the user's browser on Google's and Meta's
own sign-in pages. Bottle Net only receives an authorization *code* at its
local callback URL, which it exchanges for tokens. It never sees passwords.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from bottle_net.publisher.errors import PublisherError

STATE_LIFETIME_SECONDS = 600


def pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for PKCE with S256 (RFC 7636)."""
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    return verifier, challenge


@dataclass
class _Pending:
    platform: str
    created: float
    data: dict[str, Any] = field(default_factory=dict)


class OAuthStateStore:
    """Remembers in-flight authorizations. Each ``state`` works once, for 10 minutes."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()
        self._clock = clock

    def create(self, platform: str, **data: Any) -> str:
        state = secrets.token_urlsafe(32)
        with self._lock:
            now = self._clock()
            self._pending = {k: v for k, v in self._pending.items() if now - v.created < STATE_LIFETIME_SECONDS}
            self._pending[state] = _Pending(platform, now, data)
        return state

    def consume(self, state: str | None, platform: str) -> dict[str, Any]:
        """Validate and remove *state*; raise if unknown, expired or for another platform."""
        with self._lock:
            pending = self._pending.pop(state or "", None)
        if pending is None or pending.platform != platform:
            raise PublisherError("This sign-in link is invalid or was already used. Start again from Accounts.",
                                 code="oauth_state")
        if self._clock() - pending.created >= STATE_LIFETIME_SECONDS:
            raise PublisherError("The sign-in took too long and expired. Start again from Accounts.",
                                 code="oauth_state")
        return pending.data
