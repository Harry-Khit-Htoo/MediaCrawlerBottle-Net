"""Job creation, validation, duplicates, editing, templates, settings and library."""

from __future__ import annotations

import io
from datetime import UTC, datetime
from typing import Any

import pytest

from bottle_net.publisher.errors import ConflictError, DuplicateError, ValidationError
from bottle_net.publisher.jobs import local_to_utc
from bottle_net.publisher.models import PublishPlatform, ScheduleMode, Status
from bottle_net.publisher.settings import PublisherSettings
from bottle_net.publisher.templates import parse_tags, render

YT, FB = PublishPlatform.YOUTUBE, PublishPlatform.FACEBOOK


@pytest.fixture
def env(pub: Any) -> Any:
    pub.connect_youtube()
    pub.connect_facebook()
    pub.video = pub.add_video("Morning.mp4")
    return pub


def create(env: Any, **data: Any) -> Any:
    body = {"video_id": env.video.id, "title": "Title", "platforms": ["youtube", "facebook"], "when": "now"}
    return env.app.jobs.create({**body, **data})


class TestTime:
    def test_bangkok_to_utc(self) -> None:
        assert local_to_utc("2026-09-19", "20:00", "Asia/Bangkok") == datetime(2026, 9, 19, 13, 0, tzinfo=UTC)

    def test_daylight_saving_gap_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="does not exist"):
            local_to_utc("2026-03-08", "02:30", "America/New_York")

    @pytest.mark.parametrize(("day", "time", "zone"), [("2026-02-30", "10:00", "UTC"), ("x", "10:00", "UTC"),
                                                        ("2026-09-19", "25:00", "UTC"), ("2026-09-19", "10:00", "Mars/Base")])
    def test_invalid(self, day: str, time: str, zone: str) -> None:
        with pytest.raises(ValidationError):
            local_to_utc(day, time, zone)


class TestCreate:
    def test_publish_now_creates_one_platform_job_each(self, env: Any) -> None:
        job = create(env)
        assert [pj.platform for pj in job.platform_jobs] == [YT, FB]
        assert all(pj.status is Status.SCHEDULED and pj.due_at == env.clock.now for pj in job.platform_jobs)
        assert job.scheduled_at is None and job.timezone == "Asia/Bangkok"

    def test_schedule_and_placeholders(self, env: Any) -> None:
        job = create(env, when="schedule", date="2026-09-19", time="20:00", title="{filename} {date} {time}",
                     description="Posted {date}", tags="a, {filename}, a, #b")
        assert job.title == "Morning 2026-09-19 20:00"
        assert job.description == "Posted 2026-09-19"
        assert job.tags == ["a", "Morning", "b"]
        assert job.platform_jobs[0].due_at == datetime(2026, 9, 19, 13, 0, tzinfo=UTC)

    def test_platform_scheduling_uploads_now(self, env: Any) -> None:
        job = create(env, when="schedule", date="2026-09-19", time="20:00", platform_scheduling=True)
        assert job.schedule_mode is ScheduleMode.PLATFORM
        assert job.platform_jobs[0].due_at == env.clock.now

    def test_facebook_platform_scheduling_needs_10_minutes(self, env: Any) -> None:
        with pytest.raises(ValidationError, match="10 minutes"):
            create(env, when="schedule", date="2026-09-19", time="12:05", platform_scheduling=True)

    @pytest.mark.parametrize(("data", "message"), [
        ({"platforms": []}, "at least one platform"),
        ({"platforms": ["tiktok"]}, "Unknown platform"),
        ({"title": "  "}, "Enter a title"),
        ({"title": "<b>bad</b>"}, "< and >"),
        ({"title": "x" * 101, "platforms": ["youtube"]}, "longer than 100"),
        ({"when": "schedule", "date": "2026-09-19", "time": "11:00"}, "future"),
        ({"when": "later"}, "Publish now"),
        ({"privacy": "secret"}, "Privacy"),
        ({"thumbnail": "nope.jpg"}, "thumbnail"),
        ({"video_id": 999}, "not found"),
    ])
    def test_validation(self, env: Any, data: dict[str, Any], message: str) -> None:
        with pytest.raises(Exception, match=message):
            create(env, **data)

    def test_platform_must_be_connected(self, env: Any) -> None:
        env.app.disconnect(YT)
        with pytest.raises(ValidationError, match="Connect YouTube"):
            create(env, platforms=["youtube"])

    def test_downloaded_video_needs_rights_confirmation(self, env: Any) -> None:
        downloaded = env.add_video("Clip.mp4", b"downloaded" * 50, source="download")
        with pytest.raises(ValidationError, match="permission"):
            create(env, video_id=downloaded.id)
        assert create(env, video_id=downloaded.id, rights_confirmed=True)


class TestDuplicates:
    def test_completed_video_is_not_published_twice(self, env: Any) -> None:
        create(env, platforms=["youtube"])
        env.tick()
        with pytest.raises(DuplicateError, match="Already published to YouTube") as info:
            create(env, platforms=["youtube"])
        assert info.value.platforms == ["youtube"]
        # Other platforms are fine, and an explicit publish-again is allowed.
        assert create(env, platforms=["facebook"])
        assert create(env, platforms=["youtube"], publish_again=True)

    def test_scheduled_video_is_not_scheduled_twice(self, env: Any) -> None:
        create(env, when="schedule", date="2026-09-19", time="20:00")
        with pytest.raises(DuplicateError, match="Already scheduled"):
            create(env, when="schedule", date="2026-09-19", time="20:00")

    def test_same_file_imported_again_is_still_a_duplicate(self, env: Any) -> None:
        create(env, platforms=["youtube"])
        env.tick()
        copy = env.add_video("Morning copy.mp4", open(env.video.path, "rb").read())
        with pytest.raises(DuplicateError):
            create(env, video_id=copy.id, platforms=["youtube"])

    def test_cancelled_or_failed_jobs_do_not_block(self, env: Any) -> None:
        job = create(env, platforms=["youtube"], when="schedule", date="2026-09-19", time="20:00")
        env.app.jobs.cancel_job(job.id)
        assert create(env, platforms=["youtube"])

    def test_publish_again_action(self, env: Any) -> None:
        job = create(env, platforms=["youtube"])
        env.tick()
        again = env.app.jobs.publish_again(env.app.db.get_job(job.id).platform_jobs[0].id)
        assert again.platform_jobs[0].publish_again
        env.tick()
        assert len(env.youtube.videos) == 2


class TestChanges:
    def test_edit_and_reschedule(self, env: Any) -> None:
        job = create(env, when="schedule", date="2026-09-19", time="20:00")
        updated = env.app.jobs.update(job.id, {"title": "New", "tags": "x, y", "date": "2026-09-20", "time": "08:00"})
        assert updated.title == "New" and updated.tags == ["x", "y"]
        assert updated.scheduled_at == datetime(2026, 9, 20, 1, 0, tzinfo=UTC)
        assert all(pj.due_at == updated.scheduled_at for pj in updated.platform_jobs)

    def test_move_keeps_time(self, env: Any) -> None:
        job = create(env, when="schedule", date="2026-09-19", time="20:00")
        moved = env.app.jobs.update(job.id, {"date": "2026-09-25"})
        assert moved.scheduled_at == datetime(2026, 9, 25, 13, 0, tzinfo=UTC)

    def test_started_job_cannot_be_edited(self, env: Any) -> None:
        job = create(env)
        env.tick()
        with pytest.raises(ConflictError):
            env.app.jobs.update(job.id, {"title": "Too late"})

    def test_cannot_reschedule_into_the_past(self, env: Any) -> None:
        job = create(env, when="schedule", date="2026-09-19", time="20:00")
        with pytest.raises(ValidationError, match="future"):
            env.app.jobs.update(job.id, {"time": "09:00"})

    def test_cancel_and_retry(self, env: Any) -> None:
        job = create(env, when="schedule", date="2026-09-19", time="20:00", platforms=["youtube"])
        pj = job.platform_jobs[0]
        assert env.app.jobs.cancel_platform_job(pj.id) == "cancelled"
        with pytest.raises(ConflictError):
            env.app.jobs.cancel_platform_job(pj.id)
        env.app.jobs.retry_platform_job(pj.id)
        env.tick()
        assert env.app.db.get_platform_job(pj.id).status is Status.COMPLETED

    def test_video_with_active_jobs_cannot_be_deleted(self, env: Any) -> None:
        create(env, when="schedule", date="2026-09-19", time="20:00")
        with pytest.raises(ConflictError):
            env.app.library.delete(env.video.id, delete_file=True, has_active_jobs=env.app.jobs.has_active_jobs)


class TestTemplatesAndSettings:
    def test_render(self) -> None:
        when = datetime(2026, 9, 19, 20, 5)
        assert render("{filename} on {date} at {time} {other}", filename="My clip.mp4", when=when) == \
            "My clip on 2026-09-19 at 20:05 {other}"

    def test_parse_tags(self) -> None:
        assert parse_tags("video,  Tutorial , ,#tech, video") == ["video", "Tutorial", "tech"]
        assert parse_tags(["a b", "A B"]) == ["a b"]

    def test_template_storage(self, env: Any) -> None:
        saved = env.app.db.save_template(None, "Tutorial", "{filename}", "Learn more.", ["tutorial"])
        assert env.app.db.list_templates() == [saved]
        with pytest.raises(ValidationError, match="already exists"):
            env.app.db.save_template(None, "Tutorial", "", "", [])

    def test_settings_defaults_and_validation(self, env: Any) -> None:
        settings = PublisherSettings()
        assert settings.timezone == "Asia/Bangkok" and settings.max_attempts == 4
        assert settings.retry_delays == [60, 300, 900] and settings.missed_policy == "hold"
        env.app.update_settings({"timezone": "Europe/London", "max_attempts": 6})
        assert env.app.settings().timezone == "Europe/London"
        for bad in ({"timezone": "Nowhere/City"}, {"max_attempts": 0}, {"retry_delays": []},
                    {"missed_policy": "maybe"}, {"workers": True}, {"unknown": 1}):
            with pytest.raises(ValidationError):
                env.app.update_settings(bad)
        # Settings survive a restart (stored in the database).
        assert PublisherSettings.from_dict(env.app.db.all_settings()).max_attempts == 6


class TestLibrary:
    def test_import_stream_sanitises_name_and_hashes(self, env: Any) -> None:
        video = env.app.library.import_stream('My Video: Part 1/2?.mp4', io.BytesIO(b"abc" * 100), 300)
        assert video.filename == "My_Video_Part_1-2.mp4" and video.managed and len(video.sha256) == 64
        again = env.app.library.import_stream('My Video: Part 1/2?.mp4', io.BytesIO(b"abc" * 100), 300)
        assert again.filename == "My_Video_Part_1-2_2.mp4"  # never overwrites

    def test_rejects_non_videos_and_truncated_uploads(self, env: Any) -> None:
        with pytest.raises(ValidationError, match="supported video"):
            env.app.library.import_stream("notes.txt", io.BytesIO(b"x"), 1)
        with pytest.raises(ValidationError, match="interrupted"):
            env.app.library.import_stream("clip.mp4", io.BytesIO(b"x" * 10), 100)
        assert not list(env.app.library.video_dir().glob("*.part"))

    def test_images(self, env: Any) -> None:
        name = env.app.library.save_image(b"\x89PNG\r\n\x1a\n" + b"0" * 20)
        assert env.app.library.image_path(name).is_file()
        assert env.app.library.image_path("../publisher.db") is None
        with pytest.raises(ValidationError):
            env.app.library.save_image(b"GIF89a")

    def test_import_downloads_folder(self, env: Any, tmp_path: Any) -> None:
        folder = tmp_path / "downloads" / "tiktok" / "user"
        folder.mkdir(parents=True)
        (folder / "a_1.mp4").write_bytes(b"1" * 10)
        (folder / "b_2.mp4.part").write_bytes(b"2" * 10)
        added = env.app.library.import_folder(tmp_path / "downloads")
        assert [v.filename for v in added] == ["a_1.mp4"] and added[0].source == "download"
        assert env.app.library.import_folder(tmp_path / "downloads") == []

    def test_download_and_schedule_uses_existing_downloader(self, env: Any, tmp_path: Any) -> None:
        from conftest import FakeVideo, FakeYDLFactory

        from bottle_net.config import Config
        from bottle_net.downloaders import get_downloader
        from bottle_net.publisher.downloads import DownloadManager

        factory = FakeYDLFactory([FakeVideo("555", title="Dance")])
        config = Config(download_directory=tmp_path / "dl", output_directory=tmp_path / "out", request_delay=0)
        manager = DownloadManager(env.app.library, config=lambda: config, run_async=False,
                                  downloader_factory=lambda p, c: get_downloader(p, c, ydl_factory=factory,
                                                                                 sleep=lambda _: None, ffmpeg=False))
        task = manager.start("https://www.tiktok.com/@user/video/555")
        assert task["status"] == "completed", task
        video = env.app.library.get(task["video_id"])
        assert video.source == "download" and video.filename == "Dance_555.mp4"
        assert (tmp_path / "dl" / "tiktok" / "Dance_555.mp4").is_file()  # same place as the CLI
        failed = manager.start("https://www.tiktok.com/@user/video/404")
        assert failed["status"] == "failed" and "unavailable" in failed["error"].lower()
        with pytest.raises(ValidationError):
            manager.start("https://example.com/video")
