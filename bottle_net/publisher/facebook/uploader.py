"""Upload videos to a Facebook Page with the Graph API's chunked upload.

``POST https://graph-video.facebook.com/{page-id}/videos`` in three phases:

1. ``upload_phase=start`` (file size) -> ``upload_session_id``, ``video_id``
   and the first byte range to send.
2. ``upload_phase=transfer`` for each chunk, until start and end offsets
   are equal. The offsets are saved, so an interrupted upload resumes.
3. ``upload_phase=finish`` with title and description, publishing now or
   at ``scheduled_publish_time`` (``published=false``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import requests

from bottle_net.publisher.errors import PublishError, UploadCancelled, friendly
from bottle_net.publisher.facebook.auth import FacebookAuth, graph_url, graph_version
from bottle_net.publisher.platform import (
    ProgressCallback,
    UploadRequest,
    UploadResult,
    json_body,
    response_detail,
    send,
    server_error,
)

logger = logging.getLogger(__name__)

PLATFORM = "Facebook"
RATE_LIMIT_CODES = frozenset({4, 17, 32, 613})
TEMPORARY_CODES = frozenset({1, 2})
MAX_TITLE = 255


def video_endpoint(page_id: str) -> str:
    return f"https://graph-video.facebook.com/{graph_version()}{page_id}/videos"


def with_hashtags(description: str, tags: list[str]) -> str:
    """Append tags as hashtags (Facebook videos have no separate tag field)."""
    hashtags = " ".join("#" + "".join(ch for ch in tag if ch.isalnum() or ch == "_") for tag in tags)
    hashtags = " ".join(h for h in hashtags.split() if len(h) > 1)
    if not hashtags:
        return description
    return f"{description.rstrip()}\n\n{hashtags}" if description.strip() else hashtags


def facebook_error(response: requests.Response) -> PublishError:
    """Translate a Graph API error response into a friendly PublishError."""
    error = json_body(response).get("error", {})
    code = int(error.get("code", 0) or 0)
    subcode = int(error.get("error_subcode", 0) or 0)
    api_message = str(error.get("message", ""))[:300]
    detail = response_detail(response)

    if response.status_code == 429 or code in RATE_LIMIT_CODES:
        return PublishError(friendly(PLATFORM, "Facebook is limiting how many requests Bottle Net can make.",
                                     "Bottle Net will wait and try again automatically."),
                            retryable=True, code="rate_limited", detail=detail)
    if error.get("is_transient") or code in TEMPORARY_CODES or response.status_code >= 500:
        if not error:
            return server_error(PLATFORM, response)
        return PublishError(friendly(PLATFORM, "Temporary API error at Facebook.",
                                     "Bottle Net will try again automatically."),
                            retryable=True, code="server_error", detail=detail)
    if code in (102, 190) or response.status_code == 401:
        return PublishError(friendly(PLATFORM, "Your Facebook connection has expired or was revoked.",
                                     "Reconnect Facebook in Accounts and try again."),
                            code="auth", detail=detail, reconnect=True)
    if code == 3 or code == 10 or 200 <= code <= 299 or response.status_code == 403:
        return PublishError(friendly(PLATFORM, "The connected account does not have permission to publish videos "
                                               "to this Page.",
                                     "Reconnect Facebook, allow all requested Page permissions, and try again."),
                            code="forbidden", detail=detail, reconnect=True)
    if code == 100 and subcode == 33:
        return PublishError(friendly(PLATFORM, "The selected Facebook Page could not be found or is not accessible.",
                                     "Reconnect Facebook and choose the Page again."),
                            code="invalid_page", detail=detail, reconnect=True)
    if code == 368:
        return PublishError(friendly(PLATFORM, "Facebook temporarily blocked this action on the Page.",
                                     "Check the Page's notifications on Facebook, then retry later."),
                            code="blocked", detail=detail)
    if code in (351, 352, 6000, 6001):
        return PublishError(friendly(PLATFORM, "Facebook could not process the video file.",
                                     "Check that the file plays correctly and is a supported format, then retry."),
                            code="invalid_video", detail=detail)
    if code == 100:
        explanation = "Facebook rejected the video details."
        if api_message:
            explanation += f" Facebook said: {api_message}"
        return PublishError(friendly(PLATFORM, explanation, "Edit the video details or schedule and try again."),
                            code="invalid_metadata", detail=detail)
    return PublishError(friendly(PLATFORM, f"Facebook returned an unexpected error (HTTP {response.status_code}).",
                                 "Try again. If it keeps failing, check the log file for details."),
                        code="unexpected", detail=detail)


class FacebookUploader:
    """Uploads one video to the connected Facebook Page."""

    def __init__(self, auth: FacebookAuth, http: Any) -> None:
        self.auth = auth
        self._http = http

    def upload(
        self,
        request: UploadRequest,
        *,
        progress: ProgressCallback,
        is_cancelled: Callable[[], bool],
        session: dict[str, Any],
        save_session: Callable[[dict[str, Any]], None],
    ) -> UploadResult:
        page_id, token = self.auth.page_credentials()
        path = request.video_path
        if not path.is_file():
            raise PublishError(friendly(PLATFORM, f"The video file no longer exists: {path}",
                                        "Add the video to the library again."), code="missing_file")
        total = path.stat().st_size
        if total == 0:
            raise PublishError(friendly(PLATFORM, "The video file is empty.", "Choose a valid video file."),
                               code="invalid_video")
        if len(request.title) > MAX_TITLE:
            raise PublishError(friendly(PLATFORM, f"The title is longer than {MAX_TITLE} characters.",
                                        "Shorten the title and try again."), code="invalid_metadata")

        endpoint = video_endpoint(page_id)
        state = session if session.get("upload_session_id") and session.get("size") == total else {}
        if not state:
            state = self._start(endpoint, token, total)
            save_session(state)

        start, end = int(state["start_offset"]), int(state["end_offset"])
        progress(start, total)
        while start < end:
            if is_cancelled():
                raise UploadCancelled
            with path.open("rb") as fh:
                fh.seek(start)
                chunk = fh.read(end - start)
            response = send(self._http, PLATFORM, "POST", endpoint, data={
                "upload_phase": "transfer", "upload_session_id": state["upload_session_id"],
                "start_offset": str(start), "access_token": token,
            }, files={"video_file_chunk": (path.name, chunk, "application/octet-stream")}, timeout=300)
            body = json_body(response)
            if response.status_code != 200 or "start_offset" not in body:
                raise facebook_error(response)
            start, end = int(body["start_offset"]), int(body["end_offset"])
            state = {**state, "start_offset": start, "end_offset": end}
            save_session(state)
            progress(min(start, total), total)

        finish: dict[str, str] = {
            "upload_phase": "finish", "upload_session_id": state["upload_session_id"], "access_token": token,
            "title": request.title, "description": request.description,
        }
        if request.publish_at is not None:
            finish["published"] = "false"
            finish["scheduled_publish_time"] = str(int(request.publish_at.timestamp()))
        response = send(self._http, PLATFORM, "POST", endpoint, data=finish, timeout=120)
        if response.status_code != 200 or not json_body(response).get("success"):
            raise facebook_error(response)
        progress(total, total)

        video_id = str(state["video_id"])
        result = UploadResult(remote_id=video_id, url=f"https://www.facebook.com/{page_id}/videos/{video_id}")
        post = self._post_details(video_id, token)
        if post.get("post_id"):
            result.post_id = str(post["post_id"])
        if post.get("permalink_url"):
            link = str(post["permalink_url"])
            result.url = link if link.startswith("http") else f"https://www.facebook.com{link}"
        if request.thumbnail:
            warning = self._set_thumbnail(video_id, token, request)
            if warning:
                result.warnings.append(warning)
        return result

    def _start(self, endpoint: str, token: str, total: int) -> dict[str, Any]:
        response = send(self._http, PLATFORM, "POST", endpoint, data={
            "upload_phase": "start", "file_size": str(total), "access_token": token})
        body = json_body(response)
        if response.status_code != 200 or "upload_session_id" not in body:
            raise facebook_error(response)
        return {"upload_session_id": str(body["upload_session_id"]), "video_id": str(body["video_id"]),
                "start_offset": int(body["start_offset"]), "end_offset": int(body["end_offset"]), "size": total}

    def _post_details(self, video_id: str, token: str) -> dict[str, Any]:
        """Best effort: the post ID and link of the published video."""
        try:
            response = send(self._http, PLATFORM, "GET", graph_url(video_id),
                            params={"fields": "post_id,permalink_url", "access_token": token}, timeout=30)
        except PublishError:
            return {}
        return json_body(response) if response.status_code == 200 else {}

    def _set_thumbnail(self, video_id: str, token: str, request: UploadRequest) -> str | None:
        assert request.thumbnail is not None
        warning = "Facebook did not accept the custom thumbnail; the video was published with an automatic one."
        try:
            data = request.thumbnail.read_bytes()
            response = send(self._http, PLATFORM, "POST", graph_url(f"{video_id}/thumbnails"),
                            data={"is_preferred": "true", "access_token": token},
                            files={"source": (request.thumbnail.name, data)}, timeout=60)
        except (OSError, PublishError):
            return warning
        if response.status_code != 200:
            logger.warning("Facebook thumbnail failed: %s", response_detail(response))
            return warning
        return None
