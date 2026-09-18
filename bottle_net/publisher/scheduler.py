"""The scheduler: starts uploads at the right time, even across restarts.

All schedule state lives in the database, so nothing is lost when Bottle
Net closes. On start the scheduler:

1. resumes uploads that were interrupted by the shutdown,
2. applies the *missed-upload policy* to uploads whose time passed while
   Bottle Net was not running (``hold`` = mark as missed and ask the user,
   ``run`` = publish now, ``skip`` = cancel), with a grace period so an
   upload that is only a few minutes late still runs normally,
3. then waits until the next upload is due, wakes up, and hands due uploads
   to a small pool of worker threads.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from bottle_net.publisher.models import Status, utcnow
from bottle_net.publisher.notifications import Notifier
from bottle_net.publisher.queue import UploadWorker
from bottle_net.publisher.settings import PublisherSettings
from bottle_net.publisher.storage import Database

logger = logging.getLogger(__name__)

DUE_STATUSES = (Status.SCHEDULED, Status.RETRYING)
#: Maximum time between checks, even when nothing seems due (seconds).
POLL_INTERVAL = 30.0


class Scheduler:
    """Background thread that dispatches due platform jobs."""

    def __init__(
        self,
        db: Database,
        worker: UploadWorker,
        settings: Callable[[], PublisherSettings],
        notifier: Notifier,
        *,
        clock: Callable[[], datetime] = utcnow,
        submit: Callable[[int], None] | None = None,
    ) -> None:
        self.db = db
        self.worker = worker
        self._settings = settings
        self.notifier = notifier
        self._clock = clock
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._executor: ThreadPoolExecutor | None = None
        #: Custom dispatcher (tests run uploads inline); default: thread pool.
        self._submit = submit

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self.recover()
        if self._submit is None:
            self._executor = ThreadPoolExecutor(max_workers=self._settings().workers, thread_name_prefix="upload")
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)

    def wake(self) -> None:
        """Re-check the schedule now (called when jobs change)."""
        self._wake.set()

    # ------------------------------------------------------------------ logic

    def recover(self) -> None:
        """After a restart: resume interrupted uploads and handle missed ones."""
        now = self._clock()
        for pj in self.db.platform_jobs_with_status([Status.UPLOADING, Status.QUEUED]):
            # Resumable uploads continue from the saved session instead of restarting.
            self.db.transition(pj.id, {Status.UPLOADING, Status.QUEUED}, status=Status.RETRYING, due_at=now,
                               last_error="Interrupted when Bottle Net closed; resuming the upload.")
            logger.info("Resuming interrupted upload #%s (%s)", pj.job_id, pj.platform.display_name)
        self.handle_missed(now)

    def handle_missed(self, now: datetime) -> None:
        settings = self._settings()
        cutoff = now - timedelta(minutes=settings.missed_grace_minutes)
        for pj in self.db.due_platform_jobs(cutoff, [Status.SCHEDULED]):
            if settings.missed_policy == "run":
                continue  # dispatched normally by tick()
            job = self.db.get_job(pj.job_id)
            when = (job.scheduled_at or pj.due_at or now).astimezone(settings.zone).strftime("%Y-%m-%d %H:%M")
            if settings.missed_policy == "skip":
                message = f"Skipped: Bottle Net was not running at the scheduled time ({when})."
                if self.db.transition(pj.id, {Status.SCHEDULED}, status=Status.CANCELLED, last_error=message):
                    self.notifier.missed(job, pj, message)
            else:
                message = (f"Bottle Net was not running at the scheduled time ({when}). "
                           "Choose Publish now, reschedule it, or cancel it.")
                if self.db.transition(pj.id, {Status.SCHEDULED}, status=Status.MISSED, last_error=message):
                    self.notifier.missed(job, pj, message)

    def tick(self, now: datetime | None = None) -> list[int]:
        """Dispatch every due platform job; return their IDs."""
        now = now or self._clock()
        self.handle_missed(now)
        dispatched = []
        for pj in self.db.due_platform_jobs(now, DUE_STATUSES):
            # The conditional update guarantees a job is dispatched only once.
            if self.db.transition(pj.id, DUE_STATUSES, status=Status.QUEUED):
                dispatched.append(pj.id)
                self._dispatch(pj.id)
        return dispatched

    def _dispatch(self, pj_id: int) -> None:
        if self._submit is not None:
            self._submit(pj_id)
        elif self._executor is not None:
            self._executor.submit(self._run, pj_id)
        else:
            self._run(pj_id)

    def _run(self, pj_id: int) -> None:
        try:
            self.worker.process(pj_id)
        except Exception:  # noqa: BLE001
            logger.exception("Upload worker crashed for platform job #%s", pj_id)
        finally:
            self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - keep scheduling even if one check fails
                logger.exception("Scheduler check failed")
            next_due = self.db.next_due_at(DUE_STATUSES)
            timeout = POLL_INTERVAL
            if next_due is not None:
                timeout = min(POLL_INTERVAL, max(0.2, (next_due - self._clock()).total_seconds()))
            self._wake.wait(timeout)
            self._wake.clear()
