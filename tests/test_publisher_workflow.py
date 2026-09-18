"""End-to-end acceptance workflow of the Video Publisher (spec section 29).

Runs the real GUI server over HTTP, the real service layer, scheduler, upload
worker and the real YouTube/Facebook clients - only the Google/Meta servers
are replaced by in-memory fakes and time is controlled by a fake clock.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from publisher_fakes import SECRET_VALUES, FakeResponse

from bottle_net.publisher.server import PublisherServer


@pytest.fixture
def gui(pub: Any) -> Any:
    server = PublisherServer(pub.app, port=0)
    server.start()
    session = requests.Session()
    response = session.get(server.launch_url, allow_redirects=False, timeout=10)
    assert response.status_code == 303
    page = session.get(server.base_url + "/", timeout=10).text
    token = page.split('name="bn-token" content="')[1].split('"')[0]
    session.headers["X-Bottle-Net-Token"] = token
    pub.server = server
    pub.session = session
    pub.url = server.base_url
    yield pub
    server.stop()


def api(pub: Any, method: str, path: str, **kwargs: Any) -> Any:
    response = pub.session.request(method, pub.url + path, timeout=10, **kwargs)
    assert response.status_code < 400, response.text
    return response.json()


def oauth_round_trip(pub: Any, platform: str, code: str) -> requests.Response:
    """Click Connect, 'sign in' at the provider, and follow its redirect back to Bottle Net."""
    auth_url = api(pub, "POST", f"/api/accounts/{platform}/connect")["auth_url"]
    query = parse_qs(urlsplit(auth_url).query)
    redirect = query["redirect_uri"][0]
    # The browser comes back from Google/Meta without Bottle Net's cookie (SameSite=Strict).
    return requests.get(redirect, params={"code": code, "state": query["state"][0]}, timeout=10)


def schedule_both(pub: Any, video_id: int, time: str = "20:00") -> dict[str, Any]:
    return api(pub, "POST", "/api/jobs", json={
        "video_id": video_id, "title": "{filename} video", "description": "Evening upload",
        "tags": "video, tutorial", "platforms": ["youtube", "facebook"], "when": "schedule",
        "date": "2026-09-19", "time": time, "timezone": "Asia/Bangkok", "privacy": "public",
    })


def statuses(job: dict[str, Any]) -> dict[str, str]:
    return {pj["platform"]: pj["status"] for pj in job["platform_jobs"]}


def test_full_acceptance_workflow(gui: Any) -> None:
    pub = gui
    # 1-7: Accounts -> Connect YouTube -> Google OAuth -> back -> Connected.
    accounts = api(pub, "GET", "/api/accounts")
    assert [a["connected"] for a in accounts] == [False, False]
    auth_url = api(pub, "POST", "/api/accounts/youtube/connect")["auth_url"]
    assert auth_url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    back = oauth_round_trip(pub, "youtube", "google-code")
    assert back.status_code == 200 and "YouTube connected" in back.text
    youtube = api(pub, "GET", "/api/accounts")[0]
    assert youtube["connected"] and youtube["display_name"] == "creator@example.com"

    # 8-12: Connect Facebook -> Meta OAuth -> choose Page -> Connected.
    pub.facebook.pages.append({"id": "222", "name": "Second Page", "access_token": "PAGE-TOKEN-222",
                               "tasks": ["CREATE_CONTENT"]})
    auth_url = api(pub, "POST", "/api/accounts/facebook/connect")["auth_url"]
    assert auth_url.startswith("https://www.facebook.com/dialog/oauth?")
    back = oauth_round_trip(pub, "facebook", "fb-code")
    assert "Choose your Facebook Page" in back.text
    facebook = api(pub, "GET", "/api/accounts")[1]
    assert [p["name"] for p in facebook["pending_pages"]] == ["My Facebook Page", "Second Page"]
    accounts = api(pub, "POST", "/api/accounts/facebook/page", json={"page_id": "111"})
    assert accounts[1]["connected"] and accounts[1]["display_name"] == "My Facebook Page"

    # 13-19: Videos -> select video -> details -> YouTube + Facebook -> Schedule 20:00.
    upload = pub.session.post(pub.url + "/api/videos/upload", data=b"\x00\x00\x00\x18ftypmp42" + b"x" * 3000,
                              headers={"X-Filename": "Evening.mp4"}, timeout=10)
    assert upload.status_code == 201
    video = upload.json()
    job = schedule_both(pub, video["id"])
    assert job["title"] == "Evening video"
    assert job["scheduled_at"] == "2026-09-19T13:00:00+00:00"  # 20:00 in Bangkok

    # 20: the job appears in the Upload Queue.
    queue = api(pub, "GET", "/api/jobs")
    assert [j["id"] for j in queue] == [job["id"]]
    assert statuses(queue[0]) == {"youtube": "scheduled", "facebook": "scheduled"}

    # 21: the scheduler waits (12:00 Bangkok now) ...
    assert pub.tick() == []
    pub.clock.advance(hours=8, seconds=30)  # 12:00 -> 20:00:30 Bangkok
    # 22-25: ... then starts the job; both platforms upload and update independently.
    assert len(pub.tick()) == 2
    done = api(pub, "GET", f"/api/jobs/{job['id']}")
    assert statuses(done) == {"youtube": "completed", "facebook": "completed"}
    assert done["status"] == "completed"
    assert done["youtube_video_id"] == "yt-video-1"
    assert done["facebook_post_id"] == "111_991"
    assert pub.youtube.videos[0]["metadata"]["snippet"]["tags"] == ["video", "tutorial"]
    assert pub.facebook.videos[0]["finish"]["description"] == "Evening upload\n\n#video #tutorial"

    # 26: History records both results.
    history = api(pub, "GET", "/api/history")
    assert sorted((row["platform"], row["status"]) for row in history) == [
        ("facebook", "completed"), ("youtube", "completed")]
    assert {row["remote_id"] for row in history} == {"yt-video-1", "991"}

    # Notifications were created for the user.
    titles = [n["title"] for n in api(pub, "GET", "/api/notifications")]
    assert "Evening video published to YouTube" in titles and "Evening video published to Facebook" in titles

    # No token or secret ever reached the browser.
    everything = " ".join(pub.session.get(pub.url + path, timeout=10).text for path in (
        "/api/dashboard", "/api/accounts", "/api/settings", "/api/jobs", "/api/history", "/api/notifications",
        "/api/videos", f"/api/jobs/{job['id']}"))
    for secret in SECRET_VALUES + ["upload.fake/session"]:
        assert secret not in everything


def test_youtube_succeeds_facebook_fails_then_retry(gui: Any) -> None:
    pub = gui
    pub.connect_youtube()
    pub.connect_facebook()
    video = pub.add_video("Afternoon.mp4")
    job = schedule_both(pub, video.id, time="13:00")
    # A permanent Facebook error: missing permissions.
    pub.facebook.fail("finish", pub.facebook.error(200, "(#200) Permissions error", 403))
    pub.clock.advance(hours=1, seconds=10)
    pub.tick()

    result = api(pub, "GET", f"/api/jobs/{job['id']}")
    assert statuses(result) == {"youtube": "completed", "facebook": "failed"}
    assert result["status"] == "partial"  # not "failed" as a whole
    facebook = next(pj for pj in result["platform_jobs"] if pj["platform"] == "facebook")
    assert facebook["last_error"].startswith("Facebook upload failed.")
    assert "does not have permission" in facebook["last_error"]
    assert facebook["attempts"] == 1  # permanent errors are not retried automatically

    # Retry only Facebook.
    api(pub, "POST", f"/api/platform-jobs/{facebook['id']}/retry")
    pub.tick()
    result = api(pub, "GET", f"/api/jobs/{job['id']}")
    assert statuses(result) == {"youtube": "completed", "facebook": "completed"}
    assert len(pub.youtube.videos) == 1  # YouTube was not uploaded twice


def test_youtube_temporary_failure_is_retried(gui: Any) -> None:
    pub = gui
    pub.connect_youtube()
    video = pub.add_video("Morning.mp4")
    job = api(pub, "POST", "/api/jobs", json={
        "video_id": video.id, "title": "Morning", "platforms": ["youtube"], "when": "schedule",
        "date": "2026-09-19", "time": "13:00", "timezone": "Asia/Bangkok"})
    pub.youtube.fail("start", FakeResponse(503, {"error": {"code": 503, "message": "Backend Error"}}))
    pub.clock.advance(hours=1, seconds=5)
    pub.tick()

    pj = api(pub, "GET", f"/api/jobs/{job['id']}")["platform_jobs"][0]
    assert pj["status"] == "retrying"
    assert "temporary problem" in pj["last_error"]
    # Attempt 2 is due one minute later, not before.
    assert pub.tick() == []
    pub.clock.advance(seconds=61)
    pub.tick()
    pj = api(pub, "GET", f"/api/jobs/{job['id']}")["platform_jobs"][0]
    assert pj["status"] == "completed" and pj["attempts"] == 2 and pj["retry_count"] == 1
    history = api(pub, "GET", "/api/history?filter=youtube")
    assert history[0]["retry_count"] == 1


def test_duplicate_protection_over_http(gui: Any) -> None:
    pub = gui
    pub.connect_youtube()
    video = pub.add_video()
    body = {"video_id": video.id, "title": "Once", "platforms": ["youtube"], "when": "now"}
    first = api(pub, "POST", "/api/jobs", json=body)
    pub.tick()
    assert api(pub, "GET", f"/api/jobs/{first['id']}")["status"] == "completed"

    again = pub.session.post(pub.url + "/api/jobs", json=body, timeout=10)
    assert again.status_code == 409
    assert again.json()["code"] == "duplicate" and "Already published to YouTube" in again.json()["error"]

    explicit = api(pub, "POST", "/api/jobs", json={**body, "publish_again": True})
    pub.tick()
    assert api(pub, "GET", f"/api/jobs/{explicit['id']}")["status"] == "completed"
    assert len(pub.youtube.videos) == 2


def test_restart_recovery_resumes_scheduled_upload(pub: Any, tmp_path: Any) -> None:
    """Scheduled at 20:00, Bottle Net restarted at 19:30: the job is still uploaded at 20:00."""
    from publisher_fakes import fake_credentials

    from bottle_net.publisher.app import PublisherApp

    pub.connect_youtube()
    video = pub.add_video("Evening.mp4")
    job = pub.app.jobs.create({"video_id": video.id, "title": "Evening", "platforms": ["youtube"],
                               "when": "schedule", "date": "2026-09-19", "time": "20:00"})
    tokens = pub.app.tokens
    pub.app.close()

    # "Restart" at 19:30 with a new app instance on the same data folder.
    pub.clock.now = pub.clock.now.replace(hour=12, minute=30)
    holder: dict[str, Any] = {}
    app = PublisherApp(pub.app.paths, token_store=tokens, credentials=fake_credentials(), http=pub.http,
                       clock=pub.clock, submit=lambda i: holder["app"].worker.process(i))
    holder["app"] = app
    try:
        app.scheduler.recover()
        assert app.scheduler.tick() == []
        assert app.db.get_job(job.id).status == "scheduled"
        pub.clock.advance(minutes=30, seconds=1)
        assert len(app.scheduler.tick()) == 1
        assert app.db.get_job(job.id).status == "completed"
    finally:
        app.close()
