"""Fakes for the publisher tests: HTTP, Google/YouTube and Meta/Facebook.

No test ever contacts Google or Facebook. :class:`FakeHTTP` stands in for
``requests.Session`` and routes requests to small in-memory "servers" that
implement the parts of the real protocols Bottle Net uses (OAuth token
endpoints, the YouTube resumable upload protocol, the Graph API chunked
upload phases).
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import requests

from bottle_net.publisher.models import PublishPlatform
from bottle_net.publisher.secrets import (
    ENV_FACEBOOK_ID,
    ENV_FACEBOOK_SECRET,
    ENV_YOUTUBE_ID,
    ENV_YOUTUBE_SECRET,
    CredentialsProvider,
)

YOUTUBE_CLIENT_SECRET = "GOCSPX-yt-client-SECRET-value"
FACEBOOK_APP_SECRET = "fb-app-SECRET-value-0123"
SECRET_VALUES = [YOUTUBE_CLIENT_SECRET, FACEBOOK_APP_SECRET, "ya29.ACCESS", "1//REFRESH-SECRET", "LONG-USER-TOKEN",
                 "PAGE-TOKEN-", "SHORT-USER-TOKEN"]


def fake_credentials() -> CredentialsProvider:
    return CredentialsProvider(environ={
        ENV_YOUTUBE_ID: "123-yt.apps.googleusercontent.com", ENV_YOUTUBE_SECRET: YOUTUBE_CLIENT_SECRET,
        ENV_FACEBOOK_ID: "987654321", ENV_FACEBOOK_SECRET: FACEBOOK_APP_SECRET,
    })


class FakeResponse:
    """Enough of ``requests.Response`` for the publisher."""

    def __init__(self, status: int = 200, body: Any = None, headers: dict[str, str] | None = None,
                 url: str = "", method: str = "GET") -> None:
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.url = url
        self.request = type("Req", (), {"method": method})()
        if body is None:
            self.content = b""
        elif isinstance(body, (dict, list)):
            self.content = json.dumps(body).encode()
        else:
            self.content = str(body).encode()
        self.text = self.content.decode()

    def json(self) -> Any:
        if not self.content:
            raise ValueError("no body")
        return json.loads(self.content)


@dataclass
class Call:
    method: str
    url: str
    kwargs: dict[str, Any]

    @property
    def params(self) -> dict[str, str]:
        query = {k: v[-1] for k, v in parse_qs(urlsplit(self.url).query).items()}
        return {**query, **{k: str(v) for k, v in (self.kwargs.get("params") or {}).items()}}

    @property
    def data(self) -> Any:
        return self.kwargs.get("data")

    @property
    def headers(self) -> dict[str, str]:
        return self.kwargs.get("headers") or {}


class FakeHTTP:
    """Routes requests by method and URL regex to handler functions."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], Callable[[Call], Any]]] = []
        self.calls: list[Call] = []

    def on(self, method: str, pattern: str, handler: Callable[[Call], Any]) -> None:
        self.routes.insert(0, (method, re.compile(pattern), handler))

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        call = Call(method, url, kwargs)
        self.calls.append(call)
        for route_method, pattern, handler in self.routes:
            if route_method == method and pattern.match(url):
                result = handler(call)
                if isinstance(result, BaseException):
                    raise result
                if isinstance(result, FakeResponse):
                    result.url = result.url or url
                    result.request.method = method
                return result
        return FakeResponse(404, {"error": {"message": f"no fake route for {method} {url}"}}, url=url, method=method)

    def calls_to(self, pattern: str) -> list[Call]:
        return [c for c in self.calls if re.search(pattern, c.url)]


def _id_token(email: str) -> str:
    def part(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()
    return f"{part({'alg': 'RS256'})}.{part({'email': email, 'sub': '42'})}.signature"


class FakeYouTube:
    """Google OAuth + YouTube Data API (resumable upload) in memory."""

    def __init__(self, http: FakeHTTP, *, email: str = "creator@example.com", channel: str = "My Channel") -> None:
        self.http = http
        self.email = email
        self.channel = channel
        self.has_channel = True
        self.grant_upload_scope = True
        self.token_counter = 1
        self.access_token = "ya29.ACCESS-1"
        self.refresh_token = "1//REFRESH-SECRET"
        self.sessions: dict[str, dict[str, Any]] = {}
        self.videos: list[dict[str, Any]] = []
        #: Injected failures: phase -> list of responses/exceptions, used in order.
        self.failures: dict[str, list[Any]] = {}
        self.refresh_fails_with: str | None = None
        http.on("POST", r"https://oauth2\.googleapis\.com/token", self.token)
        http.on("POST", r"https://oauth2\.googleapis\.com/revoke", lambda c: FakeResponse(200, {}))
        http.on("GET", r"https://www\.googleapis\.com/youtube/v3/channels", self.channels)
        http.on("POST", r"https://www\.googleapis\.com/upload/youtube/v3/videos", self.start)
        http.on("PUT", r"https://upload\.fake/session/", self.chunk)
        http.on("POST", r"https://www\.googleapis\.com/upload/youtube/v3/thumbnails/set", self.thumbnail)

    def fail(self, phase: str, *responses: Any) -> None:
        self.failures.setdefault(phase, []).extend(responses)

    def _injected(self, phase: str) -> Any:
        queue = self.failures.get(phase)
        return queue.pop(0) if queue else None

    def _authorized(self, call: Call) -> bool:
        return call.headers.get("Authorization") == f"Bearer {self.access_token}"

    def token(self, call: Call) -> Any:
        injected = self._injected("token")
        if injected is not None:
            return injected
        data = call.data
        if data["grant_type"] == "authorization_code":
            assert data["code_verifier"], "PKCE verifier missing"
            assert data["client_secret"] == YOUTUBE_CLIENT_SECRET
            scope = "openid https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/youtube.readonly"
            if self.grant_upload_scope:
                scope += " https://www.googleapis.com/auth/youtube.upload"
            return FakeResponse(200, {"access_token": self.access_token, "refresh_token": self.refresh_token,
                                      "expires_in": 3599, "scope": scope, "id_token": _id_token(self.email)})
        if data["grant_type"] == "refresh_token":
            if self.refresh_fails_with:
                return FakeResponse(400, {"error": self.refresh_fails_with})
            assert data["refresh_token"] == self.refresh_token
            self.token_counter += 1
            self.access_token = f"ya29.ACCESS-{self.token_counter}"
            return FakeResponse(200, {"access_token": self.access_token, "expires_in": 3599})
        return FakeResponse(400, {"error": "unsupported_grant_type"})

    def channels(self, call: Call) -> Any:
        if not self._authorized(call):
            return FakeResponse(401, {"error": {"code": 401}})
        items = [{"id": "UC123", "snippet": {"title": self.channel}}] if self.has_channel else []
        return FakeResponse(200, {"items": items})

    def start(self, call: Call) -> Any:
        injected = self._injected("start")
        if injected is not None:
            return injected
        if not self._authorized(call):
            return FakeResponse(401, {"error": {"code": 401, "errors": [{"reason": "authError"}]}})
        session_id = str(len(self.sessions) + 1)
        self.sessions[session_id] = {"size": int(call.headers["X-Upload-Content-Length"]), "data": b"",
                                     "metadata": call.kwargs["json"], "params": call.params}
        return FakeResponse(200, {}, headers={"Location": f"https://upload.fake/session/{session_id}"})

    def chunk(self, call: Call) -> Any:
        injected = self._injected("chunk")
        if injected is not None:
            return injected
        if not self._authorized(call):
            return FakeResponse(401, {"error": {"code": 401}})
        session = self.sessions.get(call.url.rsplit("/", 1)[-1])
        if session is None or session.get("expired"):
            return FakeResponse(404, {"error": {"code": 404}})
        content_range = call.headers["Content-Range"]
        if content_range.startswith("bytes */"):
            return self._status(session)
        start = int(re.match(r"bytes (\d+)-", content_range).group(1))
        assert start == len(session["data"]), "chunk does not continue where the upload stopped"
        session["data"] += call.data
        return self._status(session)

    def _status(self, session: dict[str, Any]) -> FakeResponse:
        received = len(session["data"])
        if received < session["size"]:
            headers = {"Range": f"bytes=0-{received - 1}"} if received else {}
            return FakeResponse(308, None, headers=headers)
        if "video" not in session:
            video = {"id": f"yt-video-{len(self.videos) + 1}", "metadata": session["metadata"], "data": session["data"]}
            self.videos.append(video)
            session["video"] = video
        return FakeResponse(200, {"id": session["video"]["id"], "kind": "youtube#video"})

    def thumbnail(self, call: Call) -> Any:
        injected = self._injected("thumbnail")
        if injected is not None:
            return injected
        return FakeResponse(200, {"items": [{}]})


class FakeFacebook:
    """Meta OAuth + Graph API Page video upload in memory."""

    CHUNK = 1000

    def __init__(self, http: FakeHTTP, *, pages: list[dict[str, Any]] | None = None) -> None:
        self.http = http
        self.pages = pages if pages is not None else [
            {"id": "111", "name": "My Facebook Page", "access_token": "PAGE-TOKEN-111",
             "tasks": ["ANALYZE", "ADVERTISE", "MODERATE", "CREATE_CONTENT", "MANAGE"]},
        ]
        self.sessions: dict[str, dict[str, Any]] = {}
        self.videos: list[dict[str, Any]] = []
        self.failures: dict[str, list[Any]] = {}
        self.revoked = False
        http.on("GET", r"https://graph\.facebook\.com/oauth/access_token", self.access_token)
        http.on("GET", r"https://graph\.facebook\.com/me/accounts", self.accounts)
        http.on("DELETE", r"https://graph\.facebook\.com/me/permissions", self.revoke)
        http.on("POST", r"https://graph-video\.facebook\.com/(\d+)/videos", self.videos_endpoint)
        http.on("GET", r"https://graph\.facebook\.com/v?\d", self.video_details)
        http.on("POST", r"https://graph\.facebook\.com/[\w-]+/thumbnails", self.thumbnail)

    def fail(self, phase: str, *responses: Any) -> None:
        self.failures.setdefault(phase, []).extend(responses)

    def _injected(self, phase: str) -> Any:
        queue = self.failures.get(phase)
        return queue.pop(0) if queue else None

    @staticmethod
    def error(code: int, message: str = "error", status: int = 400, **extra: Any) -> FakeResponse:
        return FakeResponse(status, {"error": {"message": message, "type": "OAuthException", "code": code,
                                               "fbtrace_id": "trace", **extra}})

    def access_token(self, call: Call) -> Any:
        params = call.params
        assert params["client_secret"] == FACEBOOK_APP_SECRET
        if "code" in params:
            return FakeResponse(200, {"access_token": "SHORT-USER-TOKEN", "token_type": "bearer"})
        return FakeResponse(200, {"access_token": "LONG-USER-TOKEN", "expires_in": 5183944})

    def accounts(self, call: Call) -> Any:
        assert call.params["access_token"] == "LONG-USER-TOKEN"
        return FakeResponse(200, {"data": self.pages})

    def revoke(self, call: Call) -> Any:
        self.revoked = True
        return FakeResponse(200, {"success": True})

    def videos_endpoint(self, call: Call) -> Any:
        page_id = re.search(r"/(\d+)/videos", call.url).group(1)
        data = call.data
        phase = data["upload_phase"]
        injected = self._injected(phase)
        if injected is not None:
            return injected
        page = next((p for p in self.pages if p["id"] == page_id), None)
        if page is None or data["access_token"] != page["access_token"]:
            return self.error(190, "Invalid OAuth access token.", 400)
        if phase == "start":
            session_id = str(len(self.sessions) + 1)
            size = int(data["file_size"])
            self.sessions[session_id] = {"size": size, "data": b"", "page": page_id, "video_id": f"99{session_id}"}
            return FakeResponse(200, {"upload_session_id": session_id, "video_id": f"99{session_id}",
                                      "start_offset": "0", "end_offset": str(min(self.CHUNK, size))})
        session = self.sessions[data["upload_session_id"]]
        if phase == "transfer":
            assert int(data["start_offset"]) == len(session["data"])
            chunk = call.kwargs["files"]["video_file_chunk"][1]
            session["data"] += chunk
            start = len(session["data"])
            return FakeResponse(200, {"start_offset": str(start), "end_offset": str(min(start + self.CHUNK, session["size"]))})
        if phase == "finish":
            assert len(session["data"]) == session["size"]
            self.videos.append({"id": session["video_id"], "page": page_id, "finish": dict(data), "data": session["data"]})
            return FakeResponse(200, {"success": True})
        return self.error(100, "bad phase")

    def video_details(self, call: Call) -> Any:
        video_id = urlsplit(call.url).path.strip("/").split("/")[-1]
        return FakeResponse(200, {"post_id": f"111_{video_id}", "permalink_url": f"/111/videos/{video_id}/", "id": video_id})

    def thumbnail(self, call: Call) -> Any:
        injected = self._injected("thumbnail")
        return injected if injected is not None else FakeResponse(200, {"success": True})


class FakeClock:
    """A controllable clock (timezone-aware UTC)."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 9, 19, 5, 0, tzinfo=UTC)  # 12:00 in Bangkok

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


class StubUploader:
    """Uploader whose outcomes are scripted: "ok", a PublishError, or a callable."""

    def __init__(self, platform: PublishPlatform, outcomes: list[Any] | None = None) -> None:
        self.platform = platform
        self.outcomes = outcomes or []
        self.requests: list[Any] = []
        self.sessions: list[dict[str, Any]] = []

    def upload(self, request: Any, *, progress: Any, is_cancelled: Any, session: Any, save_session: Any) -> Any:
        from bottle_net.publisher.platform import UploadResult

        self.requests.append(request)
        self.sessions.append(dict(session))
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if callable(outcome) and not isinstance(outcome, BaseException):
            outcome = outcome(progress=progress, is_cancelled=is_cancelled, save_session=save_session)
            if outcome is None:
                outcome = "ok"
        if isinstance(outcome, BaseException):
            raise outcome
        progress(50, 100)
        progress(100, 100)
        n = len(self.requests)
        return UploadResult(remote_id=f"{self.platform.value}-{n}", url=f"https://example.com/{self.platform.value}/{n}")


def requests_error(kind: str = "connection") -> Exception:
    return requests.Timeout("timed out") if kind == "timeout" else requests.ConnectionError("Connection reset by peer")

