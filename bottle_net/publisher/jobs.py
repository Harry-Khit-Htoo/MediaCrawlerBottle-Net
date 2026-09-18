"""Creating and managing upload jobs.

A job is created from the upload form; one platform job is created for each
selected platform. This module validates the input, converts the chosen
local date/time to UTC in the chosen timezone, renders template
placeholders, and protects against publishing the same video twice.
"""

from __future__ import annotations

import zoneinfo
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol

from bottle_net.publisher.errors import ConflictError, DuplicateError, ValidationError
from bottle_net.publisher.library import VideoLibrary
from bottle_net.publisher.models import (
    ACTIVE_STATUSES,
    Job,
    PublishPlatform,
    ScheduleMode,
    Status,
    utcnow,
)
from bottle_net.publisher.settings import PRIVACY_OPTIONS, PublisherSettings
from bottle_net.publisher.storage import Database
from bottle_net.publisher.templates import parse_tags, render
from bottle_net.publisher.youtube.uploader import validate_metadata

#: Facebook only accepts scheduled posts at least 10 minutes in the future.
FACEBOOK_MIN_SCHEDULE = timedelta(minutes=10)
#: A scheduled time must be at least this far in the future.
MIN_SCHEDULE_AHEAD = timedelta(seconds=30)
EDITABLE_STATUSES = frozenset({Status.SCHEDULED, Status.MISSED})
CANCELLABLE_STATUSES = frozenset({Status.SCHEDULED, Status.QUEUED, Status.RETRYING, Status.MISSED})
RETRYABLE_STATUSES = frozenset({Status.FAILED, Status.CANCELLED, Status.MISSED})


class Connectable(Protocol):
    def is_connected(self) -> bool: ...


def local_to_utc(day: str, clock_time: str, timezone: str) -> datetime:
    """Convert a date (YYYY-MM-DD) and time (HH:MM) in *timezone* to UTC."""
    try:
        zone = zoneinfo.ZoneInfo(timezone)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationError(f"Unknown timezone: {timezone}") from exc
    try:
        local = datetime.combine(date.fromisoformat(day), time.fromisoformat(clock_time), tzinfo=zone)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Choose a valid date (YYYY-MM-DD) and time (HH:MM).") from exc
    # Times skipped by a daylight-saving change do not exist.
    back = local.astimezone(UTC).astimezone(zone)
    if back.replace(tzinfo=None) != local.replace(tzinfo=None):
        raise ValidationError(f"{day} {clock_time} does not exist in {timezone} (daylight-saving change).")
    return local.astimezone(UTC)


class JobService:
    """All operations the GUI performs on jobs."""

    def __init__(
        self,
        db: Database,
        library: VideoLibrary,
        settings: Callable[[], PublisherSettings],
        accounts: Mapping[PublishPlatform, Connectable],
        *,
        on_change: Callable[[], None] = lambda: None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.db = db
        self.library = library
        self._settings = settings
        self._accounts = accounts
        self._on_change = on_change
        self._clock = clock

    # ------------------------------------------------------------------ create

    def create(self, data: Mapping[str, Any]) -> Job:
        """Create a job from the upload form (see the ``/api/jobs`` endpoint)."""
        settings = self._settings()
        now = self._clock()
        video = self.library.get(_int(data.get("video_id"), "video"))

        platforms = self._platforms(data.get("platforms"))
        for platform in platforms:
            if not self._accounts[platform].is_connected():
                raise ValidationError(f"Connect {platform.display_name} in Accounts before publishing to it.",
                                      code="not_connected")

        timezone = str(data.get("timezone") or settings.timezone)
        when = str(data.get("when") or "now")
        if when not in ("now", "schedule"):
            raise ValidationError("Choose 'Publish now' or 'Schedule'.")
        scheduled_at = local_to_utc(str(data.get("date", "")), str(data.get("time", "")), timezone) \
            if when == "schedule" else None
        mode = ScheduleMode.PLATFORM if (scheduled_at and data.get("platform_scheduling")) else ScheduleMode.LOCAL
        if scheduled_at is not None:
            if scheduled_at < now + MIN_SCHEDULE_AHEAD:
                raise ValidationError("Choose a time in the future, or use Publish now.")
            if mode is ScheduleMode.PLATFORM and PublishPlatform.FACEBOOK in platforms \
                    and scheduled_at < now + FACEBOOK_MIN_SCHEDULE:
                raise ValidationError("Facebook can only schedule posts at least 10 minutes ahead.")

        publish_time = (scheduled_at or now).astimezone(zoneinfo.ZoneInfo(timezone))
        title = render(str(data.get("title") or "").strip(), filename=video.filename, when=publish_time)
        description = render(str(data.get("description") or ""), filename=video.filename, when=publish_time)
        tags = [render(t, filename=video.filename, when=publish_time) for t in parse_tags(data.get("tags"))]
        self._validate_metadata(title, description, tags, platforms)

        privacy = str(data.get("privacy") or settings.youtube_privacy)
        if privacy not in PRIVACY_OPTIONS:
            raise ValidationError(f"Privacy must be one of: {', '.join(PRIVACY_OPTIONS)}.")
        if video.source == "download" and not data.get("rights_confirmed"):
            raise ValidationError("This video was downloaded from another site. Confirm that you own it or have "
                                  "permission to publish it.", code="rights_required")
        thumbnail = data.get("thumbnail") or None
        if thumbnail is not None and self.library.image_path(str(thumbnail)) is None:
            raise ValidationError("The selected thumbnail was not found. Choose it again.")

        publish_again = bool(data.get("publish_again"))
        if not publish_again:
            self._check_duplicates(video.sha256, platforms, scheduled_at)

        due_at = scheduled_at if (scheduled_at and mode is ScheduleMode.LOCAL) else now
        job = self.db.create_job(
            {"video_id": video.id, "video_name": video.filename, "video_sha256": video.sha256, "title": title,
             "description": description, "tags": tags, "thumbnail": thumbnail, "platforms": platforms,
             "scheduled_at": scheduled_at, "timezone": timezone, "schedule_mode": mode, "privacy": privacy,
             "made_for_kids": bool(data.get("made_for_kids"))},
            [{"platform": p, "status": Status.SCHEDULED, "due_at": due_at, "publish_again": publish_again}
             for p in platforms],
        )
        self._on_change()
        return job

    @staticmethod
    def _platforms(value: Any) -> list[PublishPlatform]:
        if not isinstance(value, list) or not value:
            raise ValidationError("Select at least one platform to publish to.")
        platforms = []
        for item in value:
            try:
                platform = PublishPlatform(str(item))
            except ValueError as exc:
                raise ValidationError(f"Unknown platform: {item}") from exc
            if platform not in platforms:
                platforms.append(platform)
        return platforms

    @staticmethod
    def _validate_metadata(title: str, description: str, tags: list[str], platforms: list[PublishPlatform]) -> None:
        if not title:
            raise ValidationError("Enter a title.")
        if PublishPlatform.YOUTUBE in platforms:
            problems = validate_metadata(title, description, tags)
            if problems:
                raise ValidationError(" ".join(problems), code="invalid_metadata")
        if PublishPlatform.FACEBOOK in platforms and len(title) > 255:
            raise ValidationError("The Facebook title is longer than 255 characters.", code="invalid_metadata")

    def _check_duplicates(self, sha256: str, platforms: list[PublishPlatform], scheduled_at: datetime | None) -> None:
        """Refuse to publish the same video to the same platform again unless asked explicitly."""
        problems: list[str] = []
        duplicated: list[str] = []
        for platform in platforms:
            for pj, job in self.db.platform_jobs_for_video(sha256, platform):
                if pj.status is Status.COMPLETED:
                    when = (pj.completed_at or job.created_at).strftime("%Y-%m-%d")
                    problems.append(f"Already published to {platform.display_name} ({when}).")
                elif pj.status in ACTIVE_STATUSES:
                    problems.append(f"Already scheduled for {platform.display_name} (upload #{job.id}).")
                else:
                    continue
                duplicated.append(platform.value)
                break
        if problems:
            raise DuplicateError(" ".join(problems) + " Use 'Publish again' to publish it another time.",
                                 platforms=duplicated)

    # ------------------------------------------------------------------ change

    def update(self, job_id: int, data: Mapping[str, Any]) -> Job:
        """Edit or reschedule a job that has not started yet."""
        job = self.db.get_job(job_id)
        if any(pj.status not in EDITABLE_STATUSES and pj.status is not Status.CANCELLED for pj in job.platform_jobs):
            raise ConflictError("This upload has already started or finished and can no longer be edited.")
        changes: dict[str, Any] = {}
        if "title" in data:
            changes["title"] = str(data["title"]).strip()
        if "description" in data:
            changes["description"] = str(data["description"])
        if "tags" in data:
            changes["tags"] = parse_tags(data["tags"])
        if "privacy" in data:
            if data["privacy"] not in PRIVACY_OPTIONS:
                raise ValidationError(f"Privacy must be one of: {', '.join(PRIVACY_OPTIONS)}.")
            changes["privacy"] = data["privacy"]
        self._validate_metadata(changes.get("title", job.title), changes.get("description", job.description),
                                changes.get("tags", job.tags), job.platforms)

        new_time: datetime | None = None
        if "date" in data or "time" in data:
            if job.scheduled_at is None:
                raise ConflictError("This upload was set to publish immediately and cannot be rescheduled.")
            local = job.scheduled_at.astimezone(zoneinfo.ZoneInfo(job.timezone))
            new_time = local_to_utc(str(data.get("date") or local.strftime("%Y-%m-%d")),
                                    str(data.get("time") or local.strftime("%H:%M")), job.timezone)
            if new_time < self._clock() + MIN_SCHEDULE_AHEAD:
                raise ValidationError("Choose a time in the future.")
            if job.schedule_mode is ScheduleMode.PLATFORM and PublishPlatform.FACEBOOK in job.platforms \
                    and new_time < self._clock() + FACEBOOK_MIN_SCHEDULE:
                raise ValidationError("Facebook can only schedule posts at least 10 minutes ahead.")
            changes["scheduled_at"] = new_time

        if changes:
            self.db.update_job(job_id, **changes)
        if new_time is not None and job.schedule_mode is ScheduleMode.LOCAL:
            for pj in job.platform_jobs:
                if pj.status in EDITABLE_STATUSES:
                    self.db.transition(pj.id, EDITABLE_STATUSES, status=Status.SCHEDULED, due_at=new_time,
                                       last_error=None)
        self._on_change()
        return self.db.get_job(job_id)

    def cancel_job(self, job_id: int) -> Job:
        job = self.db.get_job(job_id)
        for pj in job.platform_jobs:
            if pj.status in CANCELLABLE_STATUSES or pj.status is Status.UPLOADING:
                self.cancel_platform_job(pj.id)
        return self.db.get_job(job_id)

    def cancel_platform_job(self, pj_id: int) -> str:
        """Cancel one platform upload. Returns ``"cancelled"`` or ``"cancelling"`` (upload in progress)."""
        pj = self.db.get_platform_job(pj_id)
        if self.db.transition(pj_id, CANCELLABLE_STATUSES, status=Status.CANCELLED, last_error="Cancelled by you."):
            self._on_change()
            return "cancelled"
        if pj.status is Status.UPLOADING and self.db.transition(pj_id, {Status.UPLOADING}, cancel_requested=True):
            return "cancelling"
        raise ConflictError(f"This {pj.platform.display_name} upload is already {pj.status.value}.")

    def retry_platform_job(self, pj_id: int) -> Job:
        """Try a failed, cancelled or missed platform upload again, now."""
        pj = self.db.get_platform_job(pj_id)
        sha = self.db.job_sha256(pj.job_id)
        if sha and not pj.publish_again:
            for other, _ in self.db.platform_jobs_for_video(sha, pj.platform):
                if other.id != pj.id and other.status is Status.COMPLETED:
                    raise DuplicateError(f"This video was already published to {pj.platform.display_name} by "
                                         "another upload. Use 'Publish again' to publish it another time.",
                                         platforms=[pj.platform.value])
        if not self.db.transition(pj_id, RETRYABLE_STATUSES, status=Status.SCHEDULED, due_at=self._clock(),
                                  round_attempts=0, last_error=None, error_code=None, progress=0,
                                  cancel_requested=False):
            raise ConflictError(f"Only failed, cancelled or missed uploads can be retried "
                                f"(this one is {pj.status.value}).")
        self._on_change()
        return self.db.get_job(pj.job_id)

    def run_missed_now(self, job_id: int) -> Job:
        """Publish the missed platform uploads of a job now."""
        job = self.db.get_job(job_id)
        for pj in job.platform_jobs:
            if pj.status is Status.MISSED:
                self.db.transition(pj.id, {Status.MISSED}, status=Status.SCHEDULED, due_at=self._clock(),
                                   last_error=None)
        self._on_change()
        return self.db.get_job(job_id)

    def publish_again(self, pj_id: int) -> Job:
        """Explicitly publish an already-published video to the same platform again."""
        pj = self.db.get_platform_job(pj_id)
        if pj.status is not Status.COMPLETED:
            raise ConflictError("Only published uploads can be published again; use Retry for failed ones.")
        job = self.db.get_job(pj.job_id)
        return self.create({
            "video_id": job.video_id, "title": job.title, "description": job.description, "tags": job.tags,
            "thumbnail": job.thumbnail, "platforms": [pj.platform.value], "when": "now", "timezone": job.timezone,
            "privacy": job.privacy, "made_for_kids": job.made_for_kids, "publish_again": True,
            "rights_confirmed": True,
        })

    def has_active_jobs(self, video_id: int) -> bool:
        return any(job.video_id == video_id and any(pj.status in ACTIVE_STATUSES for pj in job.platform_jobs)
                   for job in self.db.list_jobs())


def _int(value: Any, what: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Choose a {what}.") from exc
