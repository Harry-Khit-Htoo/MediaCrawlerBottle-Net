"""In-app notifications (shown as toasts in the GUI and on the dashboard)."""

from __future__ import annotations

from collections.abc import Callable

from bottle_net.publisher.models import Job, PlatformJob
from bottle_net.publisher.settings import PublisherSettings
from bottle_net.publisher.storage import Database


class Notifier:
    """Creates notifications, respecting the user's notification settings."""

    def __init__(self, db: Database, settings: Callable[[], PublisherSettings]) -> None:
        self.db = db
        self._settings = settings

    def info(self, title: str, message: str = "") -> None:
        self.db.add_notification("info", title, message)

    def completed(self, job: Job, pj: PlatformJob, warnings: list[str]) -> None:
        if not self._settings().notify_completed:
            return
        name = job.title or job.video_name
        message = "\n".join(warnings)
        self.db.add_notification("warning" if warnings else "success",
                                 f"{name} published to {pj.platform.display_name}", message,
                                 job_id=job.id, platform_job_id=pj.id)

    def failed(self, job: Job, pj: PlatformJob, message: str) -> None:
        if not self._settings().notify_failed:
            return
        self.db.add_notification("error", f"{pj.platform.display_name} upload failed: {job.title or job.video_name}",
                                 message, job_id=job.id, platform_job_id=pj.id)

    def missed(self, job: Job, pj: PlatformJob, message: str) -> None:
        self.db.add_notification("warning", f"Missed upload: {job.title or job.video_name}", message,
                                 job_id=job.id, platform_job_id=pj.id)
