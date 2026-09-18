"""What every platform integration provides, and shared HTTP helpers."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import requests

from bottle_net.publisher.errors import PublishError, friendly
from bottle_net.utils.logger import redact_secrets

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], None]


@dataclass
class UploadRequest:
    """Everything needed to publish one video to one platform."""

    video_path: Path
    title: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    thumbnail: Path | None = None
    privacy: str = "public"
    made_for_kids: bool = False
    category: str = "22"
    #: Set when the platform itself should publish the video at this time.
    publish_at: datetime | None = None


@dataclass
class UploadResult:
    """What the platform returned."""

    remote_id: str
    post_id: str | None = None
    url: str | None = None
    #: Non-fatal problems, e.g. "the custom thumbnail could not be set".
    warnings: list[str] = field(default_factory=list)


@dataclass
class AccountInfo:
    """Non-secret details about a connected account, safe to show in the UI."""

    display_name: str
    detail: str = ""
    remote_id: str = ""


class Uploader(Protocol):
    """A platform upload client.

    ``session`` holds resumable-upload state from an earlier, interrupted
    attempt; ``save_session`` persists new state so an upload can resume
    after a network error or an application restart.
    """

    def upload(
        self,
        request: UploadRequest,
        *,
        progress: ProgressCallback,
        is_cancelled: Callable[[], bool],
        session: dict[str, Any],
        save_session: Callable[[dict[str, Any]], None],
    ) -> UploadResult: ...


def send(http: Any, platform: str, method: str, url: str, **kwargs: Any) -> requests.Response:
    """Perform an HTTP request, turning connection problems into retryable PublishErrors."""
    kwargs.setdefault("timeout", 60)
    try:
        return http.request(method, url, **kwargs)
    except requests.Timeout as exc:
        raise PublishError(
            friendly(platform, "The connection timed out.", "Bottle Net will try again automatically."),
            retryable=True, code="timeout", detail=redact_secrets(repr(exc)),
        ) from exc
    except requests.RequestException as exc:
        raise PublishError(
            friendly(platform, "Could not connect to the server (the connection failed or was reset).",
                     "Check your internet connection. Bottle Net will try again automatically."),
            retryable=True, code="network", detail=redact_secrets(repr(exc)),
        ) from exc


def response_detail(response: requests.Response) -> str:
    """Technical description of a failed response, with secrets removed (for logs only)."""
    body = response.text[:2000] if response.content else ""
    return redact_secrets(f"HTTP {response.status_code} {response.request.method if response.request else ''} "
                          f"{response.url}: {body}")


def server_error(platform: str, response: requests.Response) -> PublishError:
    """Generic mapping for 429 and 5xx responses (both temporary)."""
    if response.status_code == 429:
        return PublishError(
            friendly(platform, "The platform is limiting how many requests Bottle Net can make (rate limit).",
                     "Bottle Net will wait and try again automatically."),
            retryable=True, code="rate_limited", detail=response_detail(response))
    return PublishError(
        friendly(platform, f"The {platform} server had a temporary problem (HTTP {response.status_code}).",
                 "Bottle Net will try again automatically."),
        retryable=True, code="server_error", detail=response_detail(response))


def json_body(response: requests.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
