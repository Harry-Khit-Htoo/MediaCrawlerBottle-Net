"""Scheduler, upload queue and retry policy (with scripted uploaders)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from publisher_fakes import StubUploader

from bottle_net.publisher.errors import PublishError, UploadCancelled
from bottle_net.publisher.models import PublishPlatform, Status
from bottle_net.publisher.retry import RetryPolicy

YT, FB = PublishPlatform.YOUTUBE, PublishPlatform.FACEBOOK
TEMPORARY = PublishError("YouTube upload failed.\n\nThe connection timed out.", retryable=True, code="timeout")
PERMANENT = PublishError("YouTube upload failed.\n\nThe connected account does not have permission.", code="forbidden")


@pytest.fixture
def env(pub: Any) -> Any:
    pub.connect_youtube()
    pub.connect_facebook()
    pub.yt = StubUploader(YT)
    pub.fb = StubUploader(FB)
    pub.app.uploaders[YT] = pub.yt
    pub.app.uploaders[FB] = pub.fb
    pub.video = pub.add_video()
    return pub


def schedule(env: Any, time: str, platforms: tuple[str, ...] = ("youtube",), **extra: Any) -> Any:
    return env.app.jobs.create({"video_id": env.video.id, "title": f"Video {time}", "platforms": list(platforms),
                                "when": "schedule", "date": "2026-09-19", "time": time, "publish_again": True,
                                **extra})


def pj_status(env: Any, job: Any, platform: PublishPlatform = YT) -> Status:
    return env.app.db.get_job(job.id).platform_job(platform).status


class TestScheduling:
    def test_future_job_waits(self, env: Any) -> None:
        job = schedule(env, "20:00")
        assert env.tick() == []
        env.clock.advance(hours=7, minutes=59)  # 19:59 Bangkok
        assert env.tick() == []
        assert pj_status(env, job) is Status.SCHEDULED
        assert env.yt.requests == []

    def test_due_job_runs(self, env: Any) -> None:
        job = schedule(env, "13:00")
        env.clock.advance(hours=1)
        assert len(env.tick()) == 1
        assert pj_status(env, job) is Status.COMPLETED
        assert env.yt.requests[0].title == "Video 13:00"

    def test_multiple_jobs_per_day_run_in_order(self, env: Any) -> None:
        jobs = [schedule(env, t) for t in ("20:00", "13:00", "15:30")]
        env.clock.advance(hours=1)  # 13:00
        env.tick()
        env.clock.advance(hours=2, minutes=31)  # 15:31 - the real scheduler wakes at 15:30
        env.tick()
        assert [pj_status(env, j) for j in jobs] == [Status.SCHEDULED, Status.COMPLETED, Status.COMPLETED]
        assert [r.title for r in env.yt.requests] == ["Video 13:00", "Video 15:30"]
        env.clock.advance(hours=4, minutes=30)  # 20:01
        env.tick()
        assert pj_status(env, jobs[0]) is Status.COMPLETED

    def test_multiple_jobs_at_the_same_time(self, env: Any) -> None:
        first = schedule(env, "14:00")
        second = schedule(env, "14:00", platforms=("youtube", "facebook"))
        env.clock.advance(hours=2)
        assert len(env.tick()) == 3
        assert pj_status(env, first) is Status.COMPLETED
        assert pj_status(env, second) is Status.COMPLETED and pj_status(env, second, FB) is Status.COMPLETED

    def test_a_job_is_dispatched_only_once(self, env: Any) -> None:
        dispatched: list[int] = []
        env.app.scheduler._submit = dispatched.append  # do not run uploads
        schedule(env, "13:00")
        env.clock.advance(hours=1)
        assert len(env.tick()) == 1
        assert env.tick() == []  # already queued
        assert len(dispatched) == 1

    def test_publish_now(self, env: Any) -> None:
        job = env.app.jobs.create({"video_id": env.video.id, "title": "Now", "platforms": ["facebook"],
                                   "when": "now", "publish_again": True})
        env.tick()
        assert pj_status(env, job, FB) is Status.COMPLETED


class TestRecoveryAndMissedJobs:
    def test_interrupted_upload_resumes_after_restart(self, env: Any) -> None:
        job = schedule(env, "13:00")
        pj = job.platform_job(YT)
        # Simulate a crash in the middle of an upload with a saved resumable session.
        env.app.db.update_platform_job(pj.id, status=Status.UPLOADING, attempts=1, round_attempts=1,
                                       session={"upload_url": "https://upload.example/abc", "size": 10})
        env.app.scheduler.recover()
        assert pj_status(env, job) is Status.RETRYING
        env.tick()
        assert pj_status(env, job) is Status.COMPLETED
        assert env.yt.sessions[0]["upload_url"] == "https://upload.example/abc"  # resumed, not restarted

    def test_missed_job_is_held_by_default(self, env: Any) -> None:
        job = schedule(env, "13:00")
        env.clock.advance(hours=5)  # Bottle Net was "offline" until 17:00
        env.app.scheduler.recover()
        assert pj_status(env, job) is Status.MISSED
        assert env.yt.requests == []
        pj = env.app.db.get_job(job.id).platform_job(YT)
        assert "was not running at the scheduled time (2026-09-19 13:00)" in pj.last_error
        assert any(n["title"].startswith("Missed upload") for n in env.app.db.notifications())
        # The user decides: publish now.
        env.app.jobs.run_missed_now(job.id)
        env.tick()
        assert pj_status(env, job) is Status.COMPLETED

    def test_missed_job_can_be_rescheduled(self, env: Any) -> None:
        job = schedule(env, "13:00")
        env.clock.advance(hours=5)
        env.app.scheduler.recover()
        env.app.jobs.update(job.id, {"date": "2026-09-19", "time": "21:00"})
        assert pj_status(env, job) is Status.SCHEDULED
        env.clock.advance(hours=4, seconds=1)
        env.tick()
        assert pj_status(env, job) is Status.COMPLETED

    def test_missed_policy_run(self, env: Any) -> None:
        env.app.update_settings({"missed_policy": "run"})
        job = schedule(env, "13:00")
        env.clock.advance(hours=5)
        env.app.scheduler.recover()
        env.tick()
        assert pj_status(env, job) is Status.COMPLETED

    def test_missed_policy_skip(self, env: Any) -> None:
        env.app.update_settings({"missed_policy": "skip"})
        job = schedule(env, "13:00")
        env.clock.advance(hours=5)
        env.tick()
        assert pj_status(env, job) is Status.CANCELLED
        assert env.yt.requests == []

    def test_slightly_late_job_still_runs(self, env: Any) -> None:
        job = schedule(env, "13:00")
        env.clock.advance(hours=1, minutes=9)  # within the 10-minute grace period
        env.app.scheduler.recover()
        env.tick()
        assert pj_status(env, job) is Status.COMPLETED


class TestRetries:
    def test_retry_schedule_1_5_15_minutes_then_fail(self, env: Any) -> None:
        env.yt.outcomes = [TEMPORARY] * 4
        job = schedule(env, "13:00")
        env.clock.advance(hours=1)
        env.tick()
        waits = []
        for _ in range(3):
            pj = env.app.db.get_job(job.id).platform_job(YT)
            assert pj.status is Status.RETRYING
            waits.append(round((pj.due_at - env.clock.now).total_seconds()))
            env.clock.now = pj.due_at - timedelta(seconds=1)
            assert env.tick() == []  # not early
            env.clock.advance(seconds=1)
            env.tick()
        assert waits == [60, 300, 900]
        pj = env.app.db.get_job(job.id).platform_job(YT)
        assert pj.status is Status.FAILED and pj.attempts == 4
        assert "timed out" in pj.last_error
        assert len(env.app.db.attempts_for(pj.id)) == 4

    def test_max_attempts_is_configurable(self, env: Any) -> None:
        env.app.update_settings({"max_attempts": 2, "retry_delays": [30]})
        env.yt.outcomes = [TEMPORARY, TEMPORARY]
        job = schedule(env, "13:00")
        env.clock.advance(hours=1)
        env.tick()
        env.clock.advance(seconds=30)
        env.tick()
        assert pj_status(env, job) is Status.FAILED

    def test_permanent_errors_are_not_retried(self, env: Any) -> None:
        env.yt.outcomes = [PERMANENT]
        job = schedule(env, "13:00")
        env.clock.advance(hours=1)
        env.tick()
        pj = env.app.db.get_job(job.id).platform_job(YT)
        assert pj.status is Status.FAILED and pj.attempts == 1 and pj.error_code == "forbidden"
        assert any(n["level"] == "error" for n in env.app.db.notifications())

    def test_success_after_temporary_failure(self, env: Any) -> None:
        env.yt.outcomes = [TEMPORARY, "ok"]
        job = schedule(env, "13:00")
        env.clock.advance(hours=1)
        env.tick()
        env.clock.advance(minutes=1)
        env.tick()
        pj = env.app.db.get_job(job.id).platform_job(YT)
        assert pj.status is Status.COMPLETED and pj.retry_count == 1 and pj.last_error is None

    def test_platforms_fail_and_retry_independently(self, env: Any) -> None:
        env.fb.outcomes = [PublishError("Facebook upload failed.\n\nTemporary API error", code="unexpected")]
        job = schedule(env, "13:00", platforms=("youtube", "facebook"))
        env.clock.advance(hours=1)
        env.tick()
        assert env.app.db.get_job(job.id).status == "partial"
        fb = env.app.db.get_job(job.id).platform_job(FB)
        env.app.jobs.retry_platform_job(fb.id)
        env.tick()
        assert env.app.db.get_job(job.id).status == "completed"
        assert len(env.yt.requests) == 1

    def test_unexpected_crash_becomes_a_failure(self, env: Any) -> None:
        env.yt.outcomes = [RuntimeError("bug")]
        job = schedule(env, "13:00")
        env.clock.advance(hours=1)
        env.tick()
        pj = env.app.db.get_job(job.id).platform_job(YT)
        assert pj.status is Status.FAILED and "unexpected error" in pj.last_error

    def test_policy_delays(self) -> None:
        policy = RetryPolicy(max_attempts=5, delays=(60, 300))
        assert [policy.delay_after(n) for n in range(1, 6)] == [60, 300, 300, 300, None]


class TestQueueStates:
    def test_cancel_scheduled(self, env: Any) -> None:
        job = schedule(env, "13:00")
        env.app.jobs.cancel_job(job.id)
        env.clock.advance(hours=1)
        env.tick()
        assert pj_status(env, job) is Status.CANCELLED
        assert env.yt.requests == []

    def test_cancel_while_uploading(self, env: Any) -> None:
        def slow(progress: Any, is_cancelled: Any, save_session: Any) -> Any:
            job_pj = env.app.db.platform_jobs_with_status([Status.UPLOADING])[0]
            assert env.app.jobs.cancel_platform_job(job_pj.id) == "cancelling"
            assert is_cancelled()
            return UploadCancelled()

        env.yt.outcomes = [slow]
        job = schedule(env, "13:00")
        env.clock.advance(hours=1)
        env.tick()
        pj = env.app.db.get_job(job.id).platform_job(YT)
        assert pj.status is Status.CANCELLED and pj.last_error == "Cancelled by you."

    def test_status_while_uploading(self, env: Any) -> None:
        seen: list[tuple[str, float]] = []

        def watch(progress: Any, is_cancelled: Any, save_session: Any) -> None:
            progress(40, 100)
            job = env.app.db.list_jobs()[0]
            pj = job.platform_job(YT)
            seen.append((job.status, pj.progress))

        env.yt.outcomes = [watch]
        schedule(env, "13:00")
        env.clock.advance(hours=1)
        env.tick()
        assert seen == [("uploading", 40.0)]

    def test_completed_upload_is_not_retried(self, env: Any) -> None:
        from bottle_net.publisher.errors import ConflictError

        job = schedule(env, "13:00")
        env.clock.advance(hours=1)
        env.tick()
        with pytest.raises(ConflictError):
            env.app.jobs.retry_platform_job(job.platform_job(YT).id)
