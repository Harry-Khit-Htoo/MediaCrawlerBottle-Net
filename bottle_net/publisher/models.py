"""Data model of the publisher: videos, upload jobs and platform jobs.

One :class:`Job` (what the user schedules: a video plus metadata) has one
:class:`PlatformJob` per selected platform. Platform jobs run, fail, retry
and complete independently, so "YouTube published, Facebook failed" is a
normal, representable state; the job's overall status is derived from them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class PublishPlatform(StrEnum):
    """Platforms videos can be published to."""

    YOUTUBE = "youtube"
    FACEBOOK = "facebook"

    @property
    def display_name(self) -> str:
        return {"youtube": "YouTube", "facebook": "Facebook"}[self.value]


class Status(StrEnum):
    """Status of one platform upload."""

    SCHEDULED = "scheduled"  # waiting for its time
    QUEUED = "queued"  # due, waiting for a free worker
    UPLOADING = "uploading"
    RETRYING = "retrying"  # a temporary error occurred; waiting to try again
    MISSED = "missed"  # Bottle Net was not running at the scheduled time
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: Statuses that still need work.
ACTIVE_STATUSES = frozenset({Status.SCHEDULED, Status.QUEUED, Status.UPLOADING, Status.RETRYING, Status.MISSED})
#: Final statuses.
FINAL_STATUSES = frozenset({Status.COMPLETED, Status.FAILED, Status.CANCELLED})


class ScheduleMode(StrEnum):
    """How a scheduled job is published."""

    #: Bottle Net uploads the video at the scheduled time (it must be running).
    LOCAL = "local"
    #: Upload immediately; YouTube/Facebook publish it at the scheduled time.
    PLATFORM = "platform"


def utcnow() -> datetime:
    """Current time in UTC (timezone-aware)."""
    return datetime.now(UTC)


def to_iso(value: datetime | None) -> str | None:
    """Store datetimes as sortable UTC ISO-8601 strings."""
    if value is None:
        return None
    return value.astimezone(UTC).isoformat(timespec="seconds")


def from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass
class Video:
    """A video in the library."""

    id: int
    path: str
    filename: str
    size: int
    duration: float | None
    sha256: str
    source: str  # "upload", "import" or "download"
    source_url: str | None
    thumbnail: str | None
    managed: bool  # True when the file lives in Bottle Net's video folder
    created_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> Video:
        return cls(
            id=row["id"], path=row["path"], filename=row["filename"], size=row["size"] or 0,
            duration=row["duration"], sha256=row["sha256"] or "", source=row["source"] or "upload",
            source_url=row["source_url"], thumbnail=row["thumbnail"], managed=bool(row["managed"]),
            created_at=from_iso(row["created_at"]) or utcnow(),
        )


@dataclass
class PlatformJob:
    """The upload of one job to one platform."""

    id: int
    job_id: int
    platform: PublishPlatform
    status: Status
    due_at: datetime | None
    attempts: int
    round_attempts: int
    last_error: str | None
    error_code: str | None
    progress: float
    remote_id: str | None
    remote_post_id: str | None
    remote_url: str | None
    session: dict[str, Any]
    warnings: list[str]
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime | None
    cancel_requested: bool
    publish_again: bool

    @classmethod
    def from_row(cls, row: Any) -> PlatformJob:
        return cls(
            id=row["id"], job_id=row["job_id"], platform=PublishPlatform(row["platform"]),
            status=Status(row["status"]), due_at=from_iso(row["due_at"]), attempts=row["attempts"],
            round_attempts=row["round_attempts"], last_error=row["last_error"], error_code=row["error_code"],
            progress=row["progress"] or 0.0, remote_id=row["remote_id"], remote_post_id=row["remote_post_id"],
            remote_url=row["remote_url"], session=json.loads(row["session"] or "{}"),
            warnings=json.loads(row["warnings"] or "[]"), started_at=from_iso(row["started_at"]),
            completed_at=from_iso(row["completed_at"]), updated_at=from_iso(row["updated_at"]),
            cancel_requested=bool(row["cancel_requested"]), publish_again=bool(row["publish_again"]),
        )

    @property
    def retry_count(self) -> int:
        """How many times the upload was retried (attempts after the first)."""
        return max(0, self.attempts - 1)


@dataclass
class Job:
    """What the user scheduled: one video, its metadata and target platforms."""

    id: int
    video_id: int
    video_name: str
    title: str
    description: str
    tags: list[str]
    thumbnail: str | None
    platforms: list[PublishPlatform]
    scheduled_at: datetime | None  # None means "publish now"
    timezone: str
    schedule_mode: ScheduleMode
    privacy: str
    made_for_kids: bool
    created_at: datetime
    updated_at: datetime
    platform_jobs: list[PlatformJob] = field(default_factory=list)

    @classmethod
    def from_row(cls, row: Any, platform_jobs: list[PlatformJob] | None = None) -> Job:
        return cls(
            id=row["id"], video_id=row["video_id"], video_name=row["video_name"], title=row["title"],
            description=row["description"] or "", tags=json.loads(row["tags"] or "[]"),
            thumbnail=row["thumbnail"], platforms=[PublishPlatform(p) for p in json.loads(row["platforms"])],
            scheduled_at=from_iso(row["scheduled_at"]), timezone=row["timezone"],
            schedule_mode=ScheduleMode(row["schedule_mode"]), privacy=row["privacy"],
            made_for_kids=bool(row["made_for_kids"]), created_at=from_iso(row["created_at"]) or utcnow(),
            updated_at=from_iso(row["updated_at"]) or utcnow(), platform_jobs=platform_jobs or [],
        )

    @property
    def status(self) -> str:
        """Overall status, derived from the platform jobs.

        ``partial`` means some platforms were published and others failed.
        """
        statuses = {pj.status for pj in self.platform_jobs}
        if not statuses:
            return "unknown"
        if statuses & {Status.UPLOADING, Status.QUEUED}:
            return "uploading"
        if Status.RETRYING in statuses:
            return "retrying"
        if Status.MISSED in statuses:
            return "missed"
        if Status.SCHEDULED in statuses:
            return "scheduled"
        if statuses == {Status.COMPLETED}:
            return "completed"
        if statuses == {Status.CANCELLED}:
            return "cancelled"
        if Status.COMPLETED in statuses and Status.FAILED in statuses:
            return "partial"
        if Status.FAILED in statuses:
            return "failed"
        return "completed" if Status.COMPLETED in statuses else "cancelled"

    def platform_job(self, platform: PublishPlatform) -> PlatformJob | None:
        return next((pj for pj in self.platform_jobs if pj.platform is platform), None)
