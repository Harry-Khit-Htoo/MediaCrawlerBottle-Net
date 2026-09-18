"""Runs a single platform upload: progress, cancellation, retries, results.

Each platform job is processed independently, so a failed Facebook upload
never marks the YouTube upload of the same job as failed (and vice versa),
and each can be retried on its own.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path

from bottle_net.publisher.errors import PublishError, UploadCancelled, friendly
from bottle_net.publisher.facebook.uploader import with_hashtags
from bottle_net.publisher.library import VideoLibrary
from bottle_net.publisher.models import Job, PlatformJob, PublishPlatform, ScheduleMode, Status, Video, utcnow
from bottle_net.publisher.notifications import Notifier
from bottle_net.publisher.platform import Uploader, UploadRequest
from bottle_net.publisher.settings import PublisherSettings
from bottle_net.publisher.storage import Database

logger = logging.getLogger(__name__)

#: Write progress to the database at most this often (seconds).
PROGRESS_INTERVAL = 0.5


class UploadWorker:
    """Executes queued platform jobs."""

    def __init__(
        self,
        db: Database,
        library: VideoLibrary,
        uploaders: Mapping[PublishPlatform, Uploader],
        settings: Callable[[], PublisherSettings],
        notifier: Notifier,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.db = db
        self.library = library
        self.uploaders = uploaders
        self._settings = settings
        self.notifier = notifier
        self._clock = clock

    def build_request(self, job: Job, video: Video, platform: PublishPlatform) -> UploadRequest:
        settings = self._settings()
        description = job.description
        if platform is PublishPlatform.FACEBOOK and settings.facebook_tags_as_hashtags:
            description = with_hashtags(description, job.tags)
        return UploadRequest(
            video_path=Path(video.path), title=job.title, description=description, tags=list(job.tags),
            thumbnail=self.library.image_path(job.thumbnail), privacy=job.privacy,
            made_for_kids=job.made_for_kids, category=settings.youtube_category,
            publish_at=job.scheduled_at if job.schedule_mode is ScheduleMode.PLATFORM else None,
        )

    def process(self, pj_id: int) -> None:
        """Upload one queued platform job (called by the scheduler's workers)."""
        pj = self.db.get_platform_job(pj_id)
        if pj.status is not Status.QUEUED:
            return
        job = self.db.get_job(pj.job_id)
        now = self._clock()
        if not self.db.transition(pj_id, {Status.QUEUED}, status=Status.UPLOADING, attempts=pj.attempts + 1,
                                  round_attempts=pj.round_attempts + 1, started_at=pj.started_at or now,
                                  cancel_requested=False, error_code=None):
            return
        pj = self.db.get_platform_job(pj_id)
        attempt_id = self.db.start_attempt(pj_id, pj.attempts)
        logger.info("Uploading job #%s to %s (attempt %s)", job.id, pj.platform.display_name, pj.attempts)

        try:
            video = self.library.get(job.video_id, include_deleted=True)
            result = self.uploaders[pj.platform].upload(
                self.build_request(job, video, pj.platform),
                progress=self._progress_writer(pj_id),
                is_cancelled=lambda: self.db.get_platform_job(pj_id).cancel_requested,
                session=pj.session,
                save_session=lambda state: self.db.update_platform_job(pj_id, session=state),
            )
        except UploadCancelled:
            self.db.update_platform_job(pj_id, status=Status.CANCELLED, last_error="Cancelled by you.",
                                        cancel_requested=False, session={})
            self.db.finish_attempt(attempt_id, "cancelled")
            return
        except PublishError as exc:
            self._failed(job, self.db.get_platform_job(pj_id), exc, attempt_id)
            return
        except Exception as exc:  # noqa: BLE001 - never let one upload crash the scheduler
            logger.exception("Unexpected error while uploading job #%s to %s", job.id, pj.platform.display_name)
            error = PublishError(friendly(pj.platform.display_name, "An unexpected error occurred in Bottle Net.",
                                          "Retry the upload. If it keeps failing, check the log file."),
                                 code="internal", detail=repr(exc))
            self._failed(job, self.db.get_platform_job(pj_id), error, attempt_id)
            return

        self.db.update_platform_job(
            pj_id, status=Status.COMPLETED, progress=100, remote_id=result.remote_id,
            remote_post_id=result.post_id, remote_url=result.url, warnings=result.warnings,
            completed_at=self._clock(), last_error=None, error_code=None, session={},
        )
        self.db.finish_attempt(attempt_id, "completed")
        logger.info("Job #%s published to %s as %s", job.id, pj.platform.display_name, result.remote_id)
        self.notifier.completed(job, self.db.get_platform_job(pj_id), result.warnings)

    def _progress_writer(self, pj_id: int) -> Callable[[int, int], None]:
        last = {"time": 0.0, "value": -1.0}

        def progress(done: int, total: int) -> None:
            value = round(100.0 * done / total, 1) if total else 0.0
            now = time.monotonic()
            if value >= 100 or value - last["value"] >= 1 or now - last["time"] >= PROGRESS_INTERVAL:
                last.update(time=now, value=value)
                self.db.update_platform_job(pj_id, progress=value)

        return progress

    def _failed(self, job: Job, pj: PlatformJob, error: PublishError, attempt_id: int) -> None:
        # The technical detail goes to the (redacted) log file only.
        logger.warning("Upload of job #%s to %s failed (attempt %s, %s): %s", job.id, pj.platform.display_name,
                       pj.attempts, error.code, error.detail or error.message.splitlines()[0])
        self.db.finish_attempt(attempt_id, "failed", error.message)
        delay = self._settings().retry_policy.delay_after(pj.round_attempts) if error.retryable else None
        if delay is not None:
            self.db.update_platform_job(pj.id, status=Status.RETRYING, due_at=self._clock() + timedelta(seconds=delay),
                                        last_error=error.message, error_code=error.code)
            return
        self.db.update_platform_job(pj.id, status=Status.FAILED, last_error=error.message, error_code=error.code)
        self.notifier.failed(job, pj, error.message)
