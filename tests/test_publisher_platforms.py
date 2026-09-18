"""YouTube and Facebook clients against mocked APIs (no real network, no real uploads)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from publisher_fakes import FakeFacebook, FakeHTTP, FakeResponse, FakeYouTube, fake_credentials, requests_error

from bottle_net.publisher.errors import NotConfiguredError, PublisherError, PublishError
from bottle_net.publisher.facebook import FacebookAuth, FacebookUploader
from bottle_net.publisher.facebook.uploader import with_hashtags
from bottle_net.publisher.models import PublishPlatform
from bottle_net.publisher.oauth import OAuthStateStore, pkce_pair
from bottle_net.publisher.platform import UploadRequest
from bottle_net.publisher.secrets import CredentialsProvider, MemoryTokenStore
from bottle_net.publisher.youtube import YouTubeAuth, YouTubeUploader
from bottle_net.publisher.youtube.uploader import validate_metadata

REDIRECT = "http://127.0.0.1:8765/oauth/youtube/callback"


def upload_kwargs(sessions: list[dict[str, Any]] | None = None, session: dict[str, Any] | None = None,
                  cancel: bool = False) -> dict[str, Any]:
    progress: list[tuple[int, int]] = []
    saved = sessions if sessions is not None else []
    return {"progress": lambda d, t: progress.append((d, t)), "is_cancelled": lambda: cancel,
            "session": session or {}, "save_session": saved.append, "_progress": progress}


def run_upload(uploader: Any, request: UploadRequest, **kw: Any) -> tuple[Any, list[tuple[int, int]]]:
    kwargs = upload_kwargs(**kw)
    progress = kwargs.pop("_progress")
    return uploader.upload(request, **kwargs), progress


@pytest.fixture
def video(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp4"
    path.write_bytes(bytes(range(256)) * 12)  # 3072 bytes
    return path


# ------------------------------------------------------------------- YouTube


@pytest.fixture
def yt(tmp_path: Path) -> Any:
    http = FakeHTTP()
    server = FakeYouTube(http)
    tokens = MemoryTokenStore()
    auth = YouTubeAuth(fake_credentials(), tokens, http)
    uploader = YouTubeUploader(auth, http, chunk_size=1024)
    return type("YT", (), {"http": http, "server": server, "tokens": tokens, "auth": auth, "uploader": uploader})


def connect(yt: Any) -> Any:
    verifier, _ = pkce_pair()
    return yt.auth.complete("code", REDIRECT, verifier)


class TestYouTubeAuth:
    def test_authorization_url_uses_official_flow(self, yt: Any) -> None:
        verifier, challenge = pkce_pair()
        url = yt.auth.authorization_url(REDIRECT, "state123", challenge)
        parts = urlsplit(url)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://accounts.google.com/o/oauth2/v2/auth"
        assert query["code_challenge_method"] == "S256" and query["code_challenge"] == challenge
        assert query["state"] == "state123" and query["access_type"] == "offline"
        assert "https://www.googleapis.com/auth/youtube.upload" in query["scope"]
        assert query["redirect_uri"] == REDIRECT
        assert "SECRET" not in url  # the client secret never goes to the browser

    def test_code_exchange_stores_tokens_in_token_store(self, yt: Any) -> None:
        info = connect(yt)
        assert info.display_name == "creator@example.com" and info.detail == "My Channel"
        stored = yt.tokens.get(PublishPlatform.YOUTUBE)
        assert stored["refresh_token"] == "1//REFRESH-SECRET" and "password" not in stored
        assert yt.auth.is_connected()

    def test_missing_upload_permission(self, yt: Any) -> None:
        yt.server.grant_upload_scope = False
        with pytest.raises(PublisherError, match="permission to upload"):
            connect(yt)

    def test_account_without_channel(self, yt: Any) -> None:
        yt.server.has_channel = False
        with pytest.raises(PublisherError, match="no YouTube channel"):
            connect(yt)
        assert not yt.auth.is_connected()

    def test_refresh_expired_token(self, yt: Any) -> None:
        connect(yt)
        data = yt.tokens.get(PublishPlatform.YOUTUBE)
        yt.tokens.set(PublishPlatform.YOUTUBE, {**data, "expires_at": 0})
        assert yt.auth.access_token() == "ya29.ACCESS-2"

    def test_revoked_refresh_token_asks_to_reconnect(self, yt: Any) -> None:
        connect(yt)
        yt.server.refresh_fails_with = "invalid_grant"
        with pytest.raises(PublishError) as info:
            yt.auth.access_token(force_refresh=True)
        assert info.value.reconnect and not info.value.retryable
        assert "Reconnect your YouTube account" in info.value.message

    def test_not_configured(self) -> None:
        auth = YouTubeAuth(CredentialsProvider(environ={}), MemoryTokenStore(), FakeHTTP())
        assert not auth.is_configured()
        with pytest.raises(NotConfiguredError, match="BOTTLE_NET_YOUTUBE_CLIENT_ID"):
            auth.authorization_url(REDIRECT, "s", "c")

    def test_disconnect_revokes_and_forgets(self, yt: Any) -> None:
        connect(yt)
        yt.auth.disconnect()
        assert yt.http.calls_to("oauth2.googleapis.com/revoke")
        assert yt.tokens.get(PublishPlatform.YOUTUBE) is None


class TestYouTubeUpload:
    def request(self, video: Path, **extra: Any) -> UploadRequest:
        return UploadRequest(video_path=video, title="My Awesome Video", description="About it",
                             tags=["video", "tutorial"], **extra)

    def test_resumable_upload_in_chunks(self, yt: Any, video: Path) -> None:
        connect(yt)
        saved: list[dict[str, Any]] = []
        result, progress = run_upload(yt.uploader, self.request(video, privacy="unlisted"), sessions=saved)
        assert result.remote_id == "yt-video-1"
        assert result.url == "https://www.youtube.com/watch?v=yt-video-1"
        uploaded = yt.server.videos[0]
        assert uploaded["data"] == video.read_bytes()
        assert uploaded["metadata"]["snippet"] == {"title": "My Awesome Video", "description": "About it",
                                                   "categoryId": "22", "tags": ["video", "tutorial"]}
        assert uploaded["metadata"]["status"] == {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False}
        ranges = [c.headers["Content-Range"] for c in yt.http.calls_to("upload.fake/session")]
        assert ranges == ["bytes 0-1023/3072", "bytes 1024-2047/3072", "bytes 2048-3071/3072"]
        assert progress[-1] == (3072, 3072) and saved[0]["size"] == 3072

    def test_platform_scheduled_publishing(self, yt: Any, video: Path) -> None:
        connect(yt)
        run_upload(yt.uploader, self.request(video, publish_at=datetime(2026, 9, 19, 13, 0, tzinfo=UTC)))
        status = yt.server.videos[0]["metadata"]["status"]
        assert status["privacyStatus"] == "private" and status["publishAt"] == "2026-09-19T13:00:00Z"

    def test_resumes_an_interrupted_session(self, yt: Any, video: Path) -> None:
        connect(yt)
        saved: list[dict[str, Any]] = []
        # First attempt: the second chunk fails with a network error.
        original = yt.server.chunk
        calls = {"n": 0}

        def flaky(call: Any) -> Any:
            calls["n"] += 1
            return requests_error() if calls["n"] == 2 else original(call)

        yt.http.on("PUT", r"https://upload\.fake/session/", flaky)
        with pytest.raises(PublishError) as info:
            run_upload(yt.uploader, self.request(video), sessions=saved)
        assert info.value.retryable
        # Second attempt continues from byte 1024 with the saved session.
        yt.http.on("PUT", r"https://upload\.fake/session/", original)
        result, _ = run_upload(yt.uploader, self.request(video), session=saved[-1])
        assert result.remote_id == "yt-video-1"
        assert len(yt.server.sessions) == 1  # no second upload session was created
        assert yt.server.videos[0]["data"] == video.read_bytes()

    def test_expired_session_restarts(self, yt: Any, video: Path) -> None:
        connect(yt)
        saved: list[dict[str, Any]] = []
        yt.server.fail("chunk", FakeResponse(404, {"error": {"code": 404}}))
        with pytest.raises(PublishError) as info:
            run_upload(yt.uploader, self.request(video), sessions=saved)
        assert info.value.code == "session_expired" and info.value.retryable and saved[-1] == {}

    def test_expired_access_token_is_refreshed_mid_upload(self, yt: Any, video: Path) -> None:
        connect(yt)
        yt.server.fail("chunk", FakeResponse(401, {"error": {"code": 401}}))
        result, _ = run_upload(yt.uploader, self.request(video))
        assert result.remote_id == "yt-video-1" and yt.server.access_token == "ya29.ACCESS-2"

    @pytest.mark.parametrize(("response", "retryable", "phrase"), [
        (FakeResponse(403, {"error": {"code": 403, "errors": [{"reason": "forbidden"}]}}), False,
         "The connected account does not have permission\nto perform this action."),
        (FakeResponse(403, {"error": {"code": 403, "errors": [{"reason": "quotaExceeded"}]}}), False, "quota"),
        (FakeResponse(403, {"error": {"code": 403, "errors": [{"reason": "userRateLimitExceeded"}]}}), True, "limiting"),
        (FakeResponse(400, {"error": {"code": 400, "message": "Invalid title", "errors": [{"reason": "invalidTitle"}]}}),
         False, "YouTube said: Invalid title"),
        (FakeResponse(429, {}), True, "rate limit"),
        (FakeResponse(503, {}), True, "temporary problem"),
        (requests_error("timeout"), True, "timed out"),
        (requests_error(), True, "connection failed or was reset"),
    ])
    def test_errors_are_friendly(self, yt: Any, video: Path, response: Any, retryable: bool, phrase: str) -> None:
        connect(yt)
        yt.server.fail("start", response)
        with pytest.raises(PublishError) as info:
            run_upload(yt.uploader, self.request(video))
        error = info.value
        assert error.retryable is retryable
        assert error.message.startswith("YouTube upload failed.")
        assert phrase.replace("\n", " ") in error.message.replace("\n", " ")
        assert "HTTP" not in error.message.split("\n\n")[0]

    def test_forbidden_message_matches_the_spec(self, yt: Any, video: Path) -> None:
        connect(yt)
        yt.server.fail("start", FakeResponse(403, {"error": {"code": 403, "errors": [{"reason": "forbidden"}]}}))
        with pytest.raises(PublishError) as info:
            run_upload(yt.uploader, self.request(video))
        assert info.value.message == ("YouTube upload failed.\n\nThe connected account does not have permission to "
                                      "perform this action.\n\nReconnect your YouTube account and try again.")
        assert "HTTP 403" in (info.value.detail or "")  # technical detail kept for the log

    def test_thumbnail_failure_is_only_a_warning(self, yt: Any, video: Path, tmp_path: Path) -> None:
        connect(yt)
        thumb = tmp_path / "t.jpg"
        thumb.write_bytes(b"\xff\xd8\xff" + b"0" * 100)
        yt.server.fail("thumbnail", FakeResponse(403, {"error": {"code": 403}}))
        result, _ = run_upload(yt.uploader, self.request(video, thumbnail=thumb))
        assert result.remote_id == "yt-video-1" and "thumbnail" in result.warnings[0]

    def test_thumbnail_is_uploaded(self, yt: Any, video: Path, tmp_path: Path) -> None:
        connect(yt)
        thumb = tmp_path / "t.png"
        thumb.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)
        result, _ = run_upload(yt.uploader, self.request(video, thumbnail=thumb))
        call = yt.http.calls_to("thumbnails/set")[0]
        assert call.params["videoId"] == "yt-video-1" and call.headers["Content-Type"] == "image/png"
        assert result.warnings == []

    def test_missing_file_and_metadata_problems(self, yt: Any, tmp_path: Path) -> None:
        connect(yt)
        with pytest.raises(PublishError, match="no longer exists"):
            run_upload(yt.uploader, UploadRequest(video_path=tmp_path / "gone.mp4", title="x"))
        assert validate_metadata("", "", []) == ["The title is empty."]
        assert validate_metadata("ok", "x" * 5001, [])
        assert validate_metadata("ok", "", ["t" * 250, "u" * 250])
        assert validate_metadata("Fine title", "Fine description", ["a", "b c"]) == []

    def test_cancel_between_chunks(self, yt: Any, video: Path) -> None:
        from bottle_net.publisher.errors import UploadCancelled

        connect(yt)
        with pytest.raises(UploadCancelled):
            run_upload(yt.uploader, self.request(video), cancel=True)


# ------------------------------------------------------------------ Facebook


@pytest.fixture
def fb() -> Any:
    http = FakeHTTP()
    server = FakeFacebook(http)
    tokens = MemoryTokenStore()
    auth = FacebookAuth(fake_credentials(), tokens, http)
    return type("FB", (), {"http": http, "server": server, "tokens": tokens, "auth": auth,
                           "uploader": FacebookUploader(auth, http)})


def connect_fb(fb: Any, page: str = "111") -> None:
    fb.auth.complete("code", "http://localhost:8765/oauth/facebook/callback")
    fb.auth.select_page(page)


class TestFacebookAuth:
    def test_authorization_url(self, fb: Any) -> None:
        url = fb.auth.authorization_url("http://localhost:8765/oauth/facebook/callback", "st")
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert url.startswith("https://www.facebook.com/dialog/oauth?")
        assert query["scope"] == "pages_show_list,pages_read_engagement,pages_manage_posts,publish_video"
        assert query["state"] == "st" and "SECRET" not in url

    def test_pages_are_listed_without_tokens(self, fb: Any) -> None:
        fb.server.pages.append({"id": "222", "name": "Read-only Page", "access_token": "PAGE-TOKEN-222",
                                "tasks": ["ANALYZE"]})
        pages = fb.auth.complete("code", "http://localhost/cb")
        assert pages == [{"id": "111", "name": "My Facebook Page"}]  # no CREATE_CONTENT -> not offered
        assert "token" not in str(fb.auth.pending_pages()).lower()

    def test_select_page_stores_page_token(self, fb: Any) -> None:
        connect_fb(fb)
        assert fb.auth.page_credentials() == ("111", "PAGE-TOKEN-111")
        assert fb.auth.pending_pages() == []

    def test_no_pages(self, fb: Any) -> None:
        fb.server.pages.clear()
        with pytest.raises(PublisherError, match="Pages"):
            fb.auth.complete("code", "http://localhost/cb")

    def test_unknown_page_choice(self, fb: Any) -> None:
        fb.auth.complete("code", "http://localhost/cb")
        with pytest.raises(PublisherError):
            fb.auth.select_page("999")

    def test_disconnect(self, fb: Any) -> None:
        connect_fb(fb)
        fb.auth.disconnect()
        assert fb.server.revoked and not fb.auth.is_connected()


class TestFacebookUpload:
    def request(self, video: Path, **extra: Any) -> UploadRequest:
        return UploadRequest(video_path=video, title="Hello", description="World", **extra)

    def test_chunked_upload_phases(self, fb: Any, video: Path) -> None:
        connect_fb(fb)
        result, progress = run_upload(fb.uploader, self.request(video))
        phases = [c.data["upload_phase"] for c in fb.http.calls_to("graph-video")]
        assert phases == ["start"] + ["transfer"] * 4 + ["finish"]  # 3072 bytes in 1000-byte chunks
        assert fb.server.videos[0]["data"] == video.read_bytes()
        finish = fb.server.videos[0]["finish"]
        assert finish["title"] == "Hello" and finish["description"] == "World" and "published" not in finish
        assert result.remote_id == "991" and result.post_id == "111_991"
        assert result.url == "https://www.facebook.com/111/videos/991/"
        assert progress[-1] == (3072, 3072)
        # The page token is sent as form data, never in the URL.
        assert all("PAGE-TOKEN" not in c.url for c in fb.http.calls_to("graph-video"))

    def test_scheduled_publishing(self, fb: Any, video: Path) -> None:
        connect_fb(fb)
        when = datetime(2026, 9, 19, 13, 0, tzinfo=UTC)
        run_upload(fb.uploader, self.request(video, publish_at=when))
        finish = fb.server.videos[0]["finish"]
        assert finish["published"] == "false" and finish["scheduled_publish_time"] == str(int(when.timestamp()))

    def test_resume_from_saved_offsets(self, fb: Any, video: Path) -> None:
        connect_fb(fb)
        saved: list[dict[str, Any]] = []
        original = fb.server.videos_endpoint
        calls = {"n": 0}

        def flaky(call: Any) -> Any:
            if call.data["upload_phase"] == "transfer":
                calls["n"] += 1
                if calls["n"] == 2:
                    return requests_error("timeout")
            return original(call)

        fb.http.on("POST", r"https://graph-video\.facebook\.com/(\d+)/videos", flaky)
        with pytest.raises(PublishError) as info:
            run_upload(fb.uploader, self.request(video), sessions=saved)
        assert info.value.retryable and saved[-1]["start_offset"] == 1000
        result, _ = run_upload(fb.uploader, self.request(video), session=saved[-1])
        assert result.remote_id == "991" and len(fb.server.sessions) == 1
        assert fb.server.videos[0]["data"] == video.read_bytes()

    @pytest.mark.parametrize(("response", "retryable", "reconnect", "phrase"), [
        (FakeFacebook.error(190, "Error validating access token"), False, True, "expired or was revoked"),
        (FakeFacebook.error(200, "Permissions error", 403), False, True, "does not have permission"),
        (FakeFacebook.error(4, "Application request limit reached"), True, False, "limiting"),
        (FakeFacebook.error(2, "Service temporarily unavailable", 500, is_transient=True), True, False,
         "Temporary API error"),
        (FakeFacebook.error(100, "Unsupported post request", error_subcode=33), False, True, "could not be found"),
        (FakeFacebook.error(6000, "There was a problem uploading your video file"), False, False, "video file"),
        (FakeFacebook.error(100, "Invalid parameter"), False, False, "Facebook said: Invalid parameter"),
    ])
    def test_errors_are_friendly(self, fb: Any, video: Path, response: Any, retryable: bool, reconnect: bool,
                                 phrase: str) -> None:
        connect_fb(fb)
        fb.server.fail("start", response)
        with pytest.raises(PublishError) as info:
            run_upload(fb.uploader, self.request(video))
        assert info.value.retryable is retryable and info.value.reconnect is reconnect
        assert info.value.message.startswith("Facebook upload failed.") and phrase in info.value.message

    def test_not_connected(self, fb: Any, video: Path) -> None:
        with pytest.raises(PublishError, match="No Facebook Page is connected"):
            run_upload(fb.uploader, self.request(video))

    def test_hashtags(self) -> None:
        assert with_hashtags("Hi", ["video", "how to", "#tech"]) == "Hi\n\n#video #howto #tech"
        assert with_hashtags("", ["a"]) == "#a"
        assert with_hashtags("Hi", []) == "Hi"


def test_oauth_state_is_single_use_and_expires() -> None:
    now = [0.0]
    store = OAuthStateStore(clock=lambda: now[0])
    state = store.create("youtube", verifier="v")
    assert store.consume(state, "youtube") == {"verifier": "v"}
    with pytest.raises(PublisherError):
        store.consume(state, "youtube")
    other = store.create("facebook")
    with pytest.raises(PublisherError):
        store.consume(other, "youtube")  # wrong platform
    late = store.create("youtube")
    now[0] = 601
    with pytest.raises(PublisherError, match="expired"):
        store.consume(late, "youtube")


def test_pkce_challenge_matches_verifier() -> None:
    import base64
    import hashlib

    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected
