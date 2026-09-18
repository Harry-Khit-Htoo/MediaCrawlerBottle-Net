"""Upload videos with the YouTube Data API v3 resumable upload protocol.

1. ``POST .../upload/youtube/v3/videos?uploadType=resumable`` with the video
   metadata returns an upload session URL.
2. The file is sent in chunks with ``PUT`` + ``Content-Range``; YouTube
   answers ``308`` (send more) until the last chunk returns the video.
3. If the connection drops or Bottle Net restarts, the saved session URL is
   asked how much it already has (``Content-Range: bytes */size``) and the
   upload continues from there instead of starting again.

A custom thumbnail is set afterwards with ``thumbnails.set``; if YouTube
refuses it (custom thumbnails need a verified channel), the video is still
published and a warning is recorded.
"""

from __future__ import annotations

import logging
import mimetypes
import re
from collections.abc import Callable
from datetime import UTC
from typing import Any

import requests

from bottle_net.publisher.errors import PublishError, UploadCancelled, friendly
from bottle_net.publisher.platform import (
    ProgressCallback,
    UploadRequest,
    UploadResult,
    json_body,
    response_detail,
    send,
    server_error,
)
from bottle_net.publisher.youtube.auth import YouTubeAuth

logger = logging.getLogger(__name__)

UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
THUMBNAIL_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
#: Must be a multiple of 256 KiB.
DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024
MAX_TITLE = 100
MAX_DESCRIPTION_BYTES = 5000
MAX_TAGS_CHARS = 500
MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024

PLATFORM = "YouTube"


def validate_metadata(title: str, description: str, tags: list[str]) -> list[str]:
    """Return problems YouTube would reject the upload for (empty list = fine)."""
    problems = []
    if not title.strip():
        problems.append("The title is empty.")
    if len(title) > MAX_TITLE:
        problems.append(f"The YouTube title is longer than {MAX_TITLE} characters.")
    if any(ch in title + description for ch in "<>"):
        problems.append("YouTube does not allow the characters < and > in titles or descriptions.")
    if len(description.encode("utf-8")) > MAX_DESCRIPTION_BYTES:
        problems.append("The YouTube description is longer than 5000 bytes.")
    # YouTube counts quotes around tags that contain spaces, plus separating commas.
    tag_chars = sum(len(tag) + (2 if " " in tag else 0) for tag in tags) + max(0, len(tags) - 1)
    if tag_chars > MAX_TAGS_CHARS:
        problems.append(f"The YouTube tags are longer than {MAX_TAGS_CHARS} characters in total.")
    return problems


def youtube_error(response: requests.Response) -> PublishError:
    """Translate a YouTube API error response into a friendly PublishError."""
    status = response.status_code
    if status == 429 or status >= 500:
        return server_error(PLATFORM, response)
    error = json_body(response).get("error", {})
    errors = error.get("errors") or [{}]
    reason = str(errors[0].get("reason", "")) if isinstance(errors[0], dict) else ""
    api_message = str(error.get("message", ""))[:300]
    detail = response_detail(response)

    if status == 401:
        return PublishError(friendly(PLATFORM, "Your YouTube connection has expired or was revoked.",
                                     "Reconnect your YouTube account in Accounts and try again."),
                            code="auth", detail=detail, reconnect=True)
    if reason in ("rateLimitExceeded", "userRateLimitExceeded"):
        return PublishError(friendly(PLATFORM, "YouTube is limiting how fast videos can be uploaded.",
                                     "Bottle Net will wait and try again automatically."),
                            retryable=True, code="rate_limited", detail=detail)
    if reason in ("quotaExceeded", "dailyLimitExceeded"):
        return PublishError(friendly(PLATFORM, "The YouTube API upload quota for this app has been used up for today.",
                                     "Retry after the quota resets (midnight Pacific Time)."),
                            code="quota", detail=detail)
    if reason == "uploadLimitExceeded":
        return PublishError(friendly(PLATFORM, "This YouTube channel has reached its upload limit for now.",
                                     "Try again later."), code="upload_limit", detail=detail)
    if reason == "youtubeSignupRequired":
        return PublishError(friendly(PLATFORM, "The connected Google account has no YouTube channel.",
                                     "Create a channel on YouTube, then reconnect it in Accounts."),
                            code="no_channel", detail=detail, reconnect=True)
    if status == 403:
        return PublishError(friendly(PLATFORM, "The connected account does not have permission to perform this action.",
                                     "Reconnect your YouTube account and try again."),
                            code="forbidden", detail=detail, reconnect=True)
    if status == 400:
        explanation = "YouTube rejected the video details."
        if api_message:
            explanation += f" YouTube said: {api_message}"
        return PublishError(friendly(PLATFORM, explanation, "Edit the title, description, tags or schedule and try again."),
                            code="invalid_metadata", detail=detail)
    return PublishError(friendly(PLATFORM, f"YouTube returned an unexpected error (HTTP {status}).",
                                 "Try again. If it keeps failing, check the log file for details."),
                        code="unexpected", detail=detail)


class YouTubeUploader:
    """Uploads one video to the connected YouTube channel."""

    def __init__(self, auth: YouTubeAuth, http: Any, *, chunk_size: int = DEFAULT_CHUNK_SIZE) -> None:
        self.auth = auth
        self._http = http
        self.chunk_size = chunk_size

    def upload(
        self,
        request: UploadRequest,
        *,
        progress: ProgressCallback,
        is_cancelled: Callable[[], bool],
        session: dict[str, Any],
        save_session: Callable[[dict[str, Any]], None],
    ) -> UploadResult:
        problems = validate_metadata(request.title, request.description, request.tags)
        if problems:
            raise PublishError(friendly(PLATFORM, " ".join(problems), "Edit the video details and try again."),
                               code="invalid_metadata")
        path = request.video_path
        if not path.is_file():
            raise PublishError(friendly(PLATFORM, f"The video file no longer exists: {path}",
                                        "Add the video to the library again."), code="missing_file")
        total = path.stat().st_size
        if total == 0:
            raise PublishError(friendly(PLATFORM, "The video file is empty.", "Choose a valid video file."),
                               code="invalid_video")

        video: dict[str, Any] | None = None
        upload_url = session.get("upload_url") if session.get("size") == total else None
        offset = 0
        if upload_url:
            offset, video, upload_url = self._resume_point(upload_url, total)
        if upload_url is None and video is None:
            upload_url = self._start(request, total)
            save_session({"upload_url": upload_url, "size": total})
            offset = 0
        progress(offset, total)

        refreshed = False
        while video is None:
            assert upload_url is not None
            if is_cancelled():
                raise UploadCancelled
            with path.open("rb") as fh:
                fh.seek(offset)
                chunk = fh.read(self.chunk_size)
            end = offset + len(chunk) - 1
            response = send(self._http, PLATFORM, "PUT", upload_url, data=chunk, headers={
                **self.auth.auth_header(), "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {offset}-{end}/{total}",
            }, timeout=300)
            if response.status_code == 308:
                offset = self._next_offset(response)
                progress(offset, total)
            elif response.status_code in (200, 201):
                video = json_body(response)
                progress(total, total)
            elif response.status_code == 401 and not refreshed:
                refreshed = True
                self.auth.access_token(force_refresh=True)
            elif response.status_code in (404, 410):
                save_session({})
                raise PublishError(friendly(PLATFORM, "The upload session expired before the video was complete.",
                                            "Bottle Net will start the upload again automatically."),
                                   retryable=True, code="session_expired", detail=response_detail(response))
            else:
                raise youtube_error(response)

        video_id = str(video.get("id", ""))
        if not video_id:
            raise PublishError(friendly(PLATFORM, "YouTube did not return a video ID.", "Check YouTube Studio."),
                               code="unexpected")
        result = UploadResult(remote_id=video_id, url=f"https://www.youtube.com/watch?v={video_id}")
        if request.thumbnail:
            warning = self._set_thumbnail(video_id, request)
            if warning:
                result.warnings.append(warning)
        return result

    # ------------------------------------------------------------------ steps

    def _metadata(self, request: UploadRequest) -> dict[str, Any]:
        status: dict[str, Any] = {"privacyStatus": request.privacy,
                                  "selfDeclaredMadeForKids": request.made_for_kids}
        if request.publish_at is not None:
            # YouTube publishes a private video automatically at publishAt.
            status["privacyStatus"] = "private"
            status["publishAt"] = request.publish_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        snippet: dict[str, Any] = {"title": request.title, "description": request.description,
                                   "categoryId": request.category}
        if request.tags:
            snippet["tags"] = request.tags
        return {"snippet": snippet, "status": status}

    def _start(self, request: UploadRequest, total: int) -> str:
        mime = mimetypes.guess_type(request.video_path.name)[0] or "application/octet-stream"
        for attempt in range(2):
            response = send(self._http, PLATFORM, "POST", UPLOAD_URL,
                            params={"uploadType": "resumable", "part": "snippet,status"},
                            json=self._metadata(request),
                            headers={**self.auth.auth_header(force_refresh=attempt > 0),
                                     "X-Upload-Content-Length": str(total), "X-Upload-Content-Type": mime})
            if response.status_code == 401 and attempt == 0:
                continue
            if response.status_code == 200 and response.headers.get("Location"):
                return response.headers["Location"]
            raise youtube_error(response)
        raise youtube_error(response)  # pragma: no cover - loop always returns or raises

    def _resume_point(self, upload_url: str, total: int) -> tuple[int, dict[str, Any] | None, str | None]:
        """Ask an existing session how much it has: (offset, finished video, session url or None)."""
        response = send(self._http, PLATFORM, "PUT", upload_url, data=b"", headers={
            **self.auth.auth_header(), "Content-Length": "0", "Content-Range": f"bytes */{total}"})
        if response.status_code == 308:
            logger.info("Resuming YouTube upload session")
            return self._next_offset(response), None, upload_url
        if response.status_code in (200, 201):
            return total, json_body(response), upload_url  # finished before the interruption
        if response.status_code in (404, 410):
            return 0, None, None  # expired: start a new session
        raise youtube_error(response)

    @staticmethod
    def _next_offset(response: requests.Response) -> int:
        match = re.match(r"bytes=0-(\d+)", response.headers.get("Range", ""))
        return int(match.group(1)) + 1 if match else 0

    def _set_thumbnail(self, video_id: str, request: UploadRequest) -> str | None:
        thumbnail = request.thumbnail
        assert thumbnail is not None
        warning = ("YouTube did not accept the custom thumbnail (custom thumbnails require a verified "
                   "channel). The video was published with an automatic thumbnail.")
        try:
            data = thumbnail.read_bytes()
        except OSError:
            return "The thumbnail file could not be read; the video was published without it."
        if len(data) > MAX_THUMBNAIL_BYTES:
            return "The thumbnail is larger than YouTube's 2 MB limit; the video was published without it."
        mime = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
        try:
            response = send(self._http, PLATFORM, "POST", THUMBNAIL_URL,
                            params={"videoId": video_id, "uploadType": "media"}, data=data,
                            headers={**self.auth.auth_header(), "Content-Type": mime})
        except PublishError as exc:
            logger.warning("Thumbnail upload failed: %s", exc.detail)
            return warning
        if response.status_code != 200:
            logger.warning("Thumbnail upload failed: %s", response_detail(response))
            return warning
        return None
