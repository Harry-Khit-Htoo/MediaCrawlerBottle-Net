"""GUI server over real HTTP: access control, API behaviour and security guarantees."""

from __future__ import annotations

import json
import logging
import re
import stat
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from publisher_fakes import SECRET_VALUES, FakeResponse

from bottle_net.publisher.models import PublishPlatform
from bottle_net.publisher.secrets import (
    CredentialsProvider,
    FileTokenStore,
    KeyringTokenStore,
    OAuthClient,
    default_token_store,
)
from bottle_net.publisher.server import WEB_DIR, PublisherServer

VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 8


@pytest.fixture
def srv(pub: Any) -> Any:
    server = PublisherServer(pub.app, port=0)
    server.start()
    session = requests.Session()
    session.get(server.launch_url, allow_redirects=False, timeout=10)
    page = session.get(server.base_url + "/", timeout=10).text
    session.headers["X-Bottle-Net-Token"] = re.search(r'name="bn-token" content="([^"]+)"', page).group(1)
    pub.server, pub.session, pub.url = server, session, server.base_url
    yield pub
    server.stop()


def call(srv: Any, method: str, path: str, **kwargs: Any) -> requests.Response:
    return srv.session.request(method, srv.url + path, timeout=10, **kwargs)


class TestAccessControl:
    def test_page_requires_the_launch_key(self, srv: Any) -> None:
        anonymous = requests.get(srv.url + "/", timeout=10)
        assert anonymous.status_code == 401 and "bottle-net gui" in anonymous.text
        assert requests.get(srv.url + "/?key=wrong", timeout=10).status_code == 401

    def test_launch_key_sets_a_strict_http_only_cookie(self, srv: Any) -> None:
        response = requests.get(srv.server.launch_url, allow_redirects=False, timeout=10)
        assert response.status_code == 303 and response.headers["Location"] == "/"
        cookie = response.headers["Set-Cookie"]
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie

    def test_api_requires_session_and_csrf_token(self, srv: Any) -> None:
        assert requests.get(srv.url + "/api/dashboard", timeout=10).status_code == 401
        no_token = requests.get(srv.url + "/api/dashboard", cookies=srv.session.cookies, timeout=10)
        assert no_token.status_code == 403 and no_token.json()["code"] == "csrf"
        assert call(srv, "GET", "/api/dashboard").status_code == 200

    def test_media_requires_session(self, srv: Any) -> None:
        video = srv.add_video()
        assert requests.get(f"{srv.url}/media/videos/{video.id}", timeout=10).status_code == 401

    def test_foreign_host_is_rejected(self, srv: Any) -> None:
        response = call(srv, "GET", "/api/dashboard", headers={"Host": "evil.example:80"})
        assert response.status_code == 403

    def test_security_headers(self, srv: Any) -> None:
        headers = call(srv, "GET", "/").headers
        assert "script-src 'self'" in headers["Content-Security-Policy"]
        assert headers["X-Frame-Options"] == "DENY" and headers["Referrer-Policy"] == "no-referrer"

    def test_static_files_cannot_escape_the_web_folder(self, srv: Any) -> None:
        assert call(srv, "GET", "/static/../server.py").status_code == 404
        assert call(srv, "GET", "/static/js/app.js").status_code == 200

    def test_server_binds_to_localhost_only(self, srv: Any) -> None:
        assert srv.server._httpd.server_address[0] == "127.0.0.1"


class TestApi:
    def test_upload_preview_and_range_requests(self, srv: Any) -> None:
        response = call(srv, "POST", "/api/videos/upload", data=VIDEO_BYTES, headers={"X-Filename": "My%20Clip.mp4"})
        assert response.status_code == 201
        video = response.json()
        assert video["filename"] == "My_Clip.mp4" and video["status"] == "not published"
        image = b"\xff\xd8\xff" + b"1" * 50
        updated = call(srv, "POST", f"/api/videos/{video['id']}/preview", data=image, headers={"X-Duration": "42.5"}).json()
        assert updated["duration"] == 42.5 and updated["thumbnail_url"].startswith("/media/images/")
        assert call(srv, "GET", updated["thumbnail_url"]).content == image
        part = call(srv, "GET", f"/media/videos/{video['id']}", headers={"Range": "bytes=10-19"})
        assert part.status_code == 206 and part.content == VIDEO_BYTES[10:20]
        assert part.headers["Content-Range"] == f"bytes 10-19/{len(VIDEO_BYTES)}"
        assert call(srv, "GET", f"/media/videos/{video['id']}").content == VIDEO_BYTES
        assert call(srv, "GET", "/api/videos?search=clip").json()[0]["id"] == video["id"]
        assert call(srv, "DELETE", f"/api/videos/{video['id']}?delete_file=1").json() == {"deleted": True}
        assert call(srv, "GET", "/api/videos").json() == []

    def test_rejects_non_video_uploads(self, srv: Any) -> None:
        response = call(srv, "POST", "/api/videos/upload", data=b"hello", headers={"X-Filename": "notes.txt"})
        assert response.status_code == 400 and "supported video" in response.json()["error"]

    def test_accounts_connect_and_disconnect(self, srv: Any) -> None:
        accounts = call(srv, "GET", "/api/accounts").json()
        assert [(a["platform"], a["connected"], a["configured"]) for a in accounts] == [
            ("youtube", False, True), ("facebook", False, True)]
        auth_url = call(srv, "POST", "/api/accounts/youtube/connect").json()["auth_url"]
        query = parse_qs(urlsplit(auth_url).query)
        assert query["redirect_uri"] == [f"http://127.0.0.1:{srv.server.port}/oauth/youtube/callback"]
        back = requests.get(query["redirect_uri"][0], params={"code": "c", "state": query["state"][0]}, timeout=10)
        assert back.status_code == 200 and "YouTube connected" in back.text
        # The same state cannot be replayed.
        replay = requests.get(query["redirect_uri"][0], params={"code": "c", "state": query["state"][0]}, timeout=10)
        assert replay.status_code == 400 and "invalid or was already used" in replay.text
        assert call(srv, "GET", "/api/accounts").json()[0]["connected"]
        accounts = call(srv, "POST", "/api/accounts/youtube/disconnect").json()
        assert not accounts[0]["connected"]

    def test_oauth_cancelled_or_forged(self, srv: Any) -> None:
        base = f"{srv.url}/oauth/facebook/callback"
        call(srv, "POST", "/api/accounts/facebook/connect")
        denied = requests.get(base, params={"error": "access_denied", "state": "x"}, timeout=10)
        assert denied.status_code == 400 and "You cancelled the sign-in" in denied.text
        forged = requests.get(base, params={"code": "c", "state": "forged"}, timeout=10)
        assert forged.status_code == 400
        assert not call(srv, "GET", "/api/accounts").json()[1]["connected"]

    def test_not_configured_platform(self, srv: Any) -> None:
        srv.app.credentials = CredentialsProvider(environ={})
        srv.app.youtube_auth._credentials = srv.app.credentials
        account = call(srv, "GET", "/api/accounts").json()[0]
        assert not account["configured"] and "BOTTLE_NET_YOUTUBE_CLIENT_ID" in account["setup_hint"]
        response = call(srv, "POST", "/api/accounts/youtube/connect")
        assert response.status_code == 400 and response.json()["code"] == "not_configured"

    def test_jobs_endpoints(self, srv: Any) -> None:
        srv.connect_youtube()
        video = srv.add_video()
        body = {"video_id": video.id, "title": "Clip", "platforms": ["youtube"], "when": "schedule",
                "date": "2026-09-19", "time": "20:00"}
        job = call(srv, "POST", "/api/jobs", json=body).json()
        assert job["status"] == "scheduled" and job["timezone"] == "Asia/Bangkok"
        edited = call(srv, "PATCH", f"/api/jobs/{job['id']}", json={"date": "2026-09-21"}).json()
        assert edited["scheduled_at"] == "2026-09-21T13:00:00+00:00"
        month = call(srv, "GET", "/api/jobs", params={"from": "2026-09-01T00:00:00+00:00",
                                                       "to": "2026-10-01T00:00:00+00:00"}).json()
        assert [j["id"] for j in month] == [job["id"]]
        pj = job["platform_jobs"][0]
        assert call(srv, "POST", f"/api/platform-jobs/{pj['id']}/cancel").json()["result"] == "cancelled"
        assert call(srv, "POST", f"/api/platform-jobs/{pj['id']}/retry").status_code == 200
        assert call(srv, "POST", f"/api/jobs/{job['id']}/cancel").json()["status"] == "cancelled"
        assert call(srv, "GET", "/api/history?filter=youtube").json()[0]["status"] == "cancelled"
        assert call(srv, "GET", "/api/history?filter=nonsense").status_code == 400

    def test_validation_errors_are_readable(self, srv: Any) -> None:
        srv.connect_youtube()
        video = srv.add_video()
        response = call(srv, "POST", "/api/jobs", json={"video_id": video.id, "title": "", "platforms": ["youtube"]})
        assert response.status_code == 400 and response.json()["error"] == "Enter a title."
        assert call(srv, "POST", "/api/jobs", data="not json").status_code == 400
        assert call(srv, "GET", "/api/nothing").status_code == 404

    def test_settings_templates_notifications(self, srv: Any) -> None:
        settings = call(srv, "GET", "/api/settings").json()
        assert settings["timezone"] == "Asia/Bangkok" and settings["credentials"]["youtube"]["configured"]
        assert call(srv, "PUT", "/api/settings", json={"timezone": "Asia/Yangon"}).json()["timezone"] == "Asia/Yangon"
        assert call(srv, "PUT", "/api/settings", json={"max_attempts": 99}).status_code == 400
        assert "Asia/Bangkok" in call(srv, "GET", "/api/timezones").json()
        template = call(srv, "POST", "/api/templates", json={"name": "Tutorial", "title": "{filename}",
                                                              "tags": "tutorial, tech"}).json()
        assert template["tags"] == ["tutorial", "tech"]
        assert call(srv, "PUT", f"/api/templates/{template['id']}", json={"name": "Tut"}).json()["name"] == "Tut"
        assert call(srv, "DELETE", f"/api/templates/{template['id']}").json() == {"deleted": True}
        srv.app.notifier.info("Hello")
        unread = call(srv, "GET", "/api/notifications?unread=1").json()
        call(srv, "POST", "/api/notifications/read", json={"ids": [unread[0]["id"]]})
        assert call(srv, "GET", "/api/notifications?unread=1").json() == []


class TestSecrets:
    def test_no_password_fields_anywhere_in_the_gui(self) -> None:
        for path in WEB_DIR.rglob("*"):
            if path.is_file():
                text = path.read_text(encoding="utf-8").lower()
                assert 'type="password"' not in text and "type='password'" not in text, path
                assert "client_secret" not in text, path

    def test_tokens_and_secrets_never_reach_the_browser_or_database(self, srv: Any) -> None:
        srv.connect_youtube()
        srv.connect_facebook()
        video = srv.add_video()
        call(srv, "POST", "/api/jobs", json={"video_id": video.id, "title": "T", "platforms": ["youtube", "facebook"]})
        srv.tick()
        pages = [call(srv, "GET", p).text for p in (
            "/", "/static/js/app.js", "/static/js/views.js", "/api/dashboard", "/api/accounts", "/api/settings",
            "/api/jobs", "/api/history", "/api/videos", "/api/notifications")]
        for text in pages:
            for secret in SECRET_VALUES + ["upload.fake/session"]:
                assert secret not in text
        database = Path(srv.app.paths.database).read_bytes()
        for secret in SECRET_VALUES:
            assert secret.encode() not in database

    def test_tokens_are_not_logged(self, srv: Any, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger="bottle_net")
        srv.connect_youtube()
        video = srv.add_video()
        srv.youtube.fail("start", FakeResponse(403, {"error": {"code": 403, "errors": [{"reason": "forbidden"}]}},
                                               url="https://www.googleapis.com/upload?access_token=ya29.ACCESS-1"))
        call(srv, "POST", "/api/jobs", json={"video_id": video.id, "title": "T", "platforms": ["youtube"]})
        srv.tick()
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "HTTP 403" in logged  # the technical cause is logged ...
        for secret in SECRET_VALUES:
            assert secret not in logged  # ... without any token or secret

    def test_oauth_client_repr_hides_secret(self) -> None:
        client = OAuthClient("id", "super-secret")
        assert "super-secret" not in repr(client)

    def test_credentials_from_env_file_and_google_json(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text('# comment\nBOTTLE_NET_FACEBOOK_APP_ID="42"\nexport BOTTLE_NET_FACEBOOK_APP_SECRET=abc\n')
        google = tmp_path / "client_secret.json"
        google.write_text(json.dumps({"installed": {"client_id": "gid", "client_secret": "gsec"}}))
        provider = CredentialsProvider(env, environ={"BOTTLE_NET_YOUTUBE_CLIENT_SECRETS_FILE": str(google)})
        assert provider.client(PublishPlatform.FACEBOOK) == OAuthClient("42", "abc")
        assert provider.client(PublishPlatform.YOUTUBE).client_id == "gid"
        assert "abc" not in provider.setup_hint(PublishPlatform.FACEBOOK)

    def test_file_token_store_is_private(self, tmp_path: Path) -> None:
        store = FileTokenStore(tmp_path / "credentials" / "tokens.json")
        store.set(PublishPlatform.YOUTUBE, {"refresh_token": "r"})
        assert store.get(PublishPlatform.YOUTUBE) == {"refresh_token": "r"}
        if sys.platform != "win32":
            assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
        store.delete(PublishPlatform.YOUTUBE)
        assert store.get(PublishPlatform.YOUTUBE) is None

    def test_keyring_is_preferred(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import keyring
        from keyring.backend import KeyringBackend
        from keyring.backends import fail

        class MemoryKeyring(KeyringBackend):
            priority = 10
            data: dict[tuple[str, str], str] = {}

            def get_password(self, service: str, username: str) -> str | None:
                return self.data.get((service, username))

            def set_password(self, service: str, username: str, password: str) -> None:
                self.data[(service, username)] = password

            def delete_password(self, service: str, username: str) -> None:
                self.data.pop((service, username), None)

        backend = MemoryKeyring()
        monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
        monkeypatch.setattr(keyring, "get_password", backend.get_password)
        monkeypatch.setattr(keyring, "set_password", backend.set_password)
        monkeypatch.setattr(keyring, "delete_password", backend.delete_password)
        store = default_token_store(tmp_path / "tokens.json")
        assert isinstance(store, KeyringTokenStore)
        store.set(PublishPlatform.FACEBOOK, {"page_token": "p"})
        assert backend.data[("bottle-net-publisher", "facebook")] == '{"page_token":"p"}'
        assert not (tmp_path / "tokens.json").exists()
        monkeypatch.setattr(keyring, "get_keyring", lambda: fail.Keyring())
        assert isinstance(default_token_store(tmp_path / "tokens.json"), FileTokenStore)


def test_port_cannot_be_taken_by_a_second_server(srv: Any) -> None:
    """On Windows SO_REUSEADDR would let a second program bind the same port."""
    from bottle_net.publisher.server import PublisherServer as Server

    with pytest.raises(OSError):
        Server(srv.app, port=srv.server.port).start()
