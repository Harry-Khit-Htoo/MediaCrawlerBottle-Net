"""Wires the publisher together and offers the operations the GUI uses.

:class:`PublisherApp` is the service layer between the web UI and the
platform integrations: the UI never calls YouTube or Facebook directly.
Everything returned by the ``*_json`` helpers is safe to send to the
browser; tokens, client secrets and upload-session URLs never are.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from bottle_net import __version__
from bottle_net.publisher.downloads import DownloadManager
from bottle_net.publisher.errors import PublisherError, ValidationError
from bottle_net.publisher.facebook import FacebookAuth, FacebookUploader
from bottle_net.publisher.jobs import JobService
from bottle_net.publisher.library import VideoLibrary
from bottle_net.publisher.models import (
    ACTIVE_STATUSES,
    Job,
    PlatformJob,
    PublishPlatform,
    Status,
    Video,
    to_iso,
    utcnow,
)
from bottle_net.publisher.notifications import Notifier
from bottle_net.publisher.oauth import OAuthStateStore, pkce_pair
from bottle_net.publisher.paths import PublisherPaths, default_data_dir
from bottle_net.publisher.platform import Uploader
from bottle_net.publisher.queue import UploadWorker
from bottle_net.publisher.scheduler import Scheduler
from bottle_net.publisher.secrets import CredentialsProvider, TokenStore, default_token_store
from bottle_net.publisher.settings import MISSED_POLICIES, PublisherSettings
from bottle_net.publisher.storage import Database
from bottle_net.publisher.youtube import YouTubeAuth, YouTubeUploader
from bottle_net.utils.logger import RedactingFilter

logger = logging.getLogger(__name__)


class InstanceLock:
    """Ensures only one Bottle Net publisher runs the scheduler for a data folder."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: Any = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")  # noqa: SIM115 - kept open while locked
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self._fh.close()
        self._fh = None


def setup_file_logging(paths: PublisherPaths) -> None:
    """Write publisher diagnostics (secrets redacted) to ``logs/publisher.log``."""
    paths.logs.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(paths.log_file, maxBytes=2_000_000, backupCount=3,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    handler.setLevel(logging.INFO)
    root = logging.getLogger("bottle_net")
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)


@dataclass
class OAuthOutcome:
    """Result of an OAuth callback, rendered as a small page in the browser tab."""

    ok: bool
    title: str
    message: str


class PublisherApp:
    """The publisher's service layer."""

    def __init__(
        self,
        paths: PublisherPaths,
        *,
        token_store: TokenStore | None = None,
        credentials: CredentialsProvider | None = None,
        http: Any = None,
        clock: Callable[[], datetime] = utcnow,
        uploaders: dict[PublishPlatform, Uploader] | None = None,
        submit: Callable[[int], None] | None = None,
        downloads: DownloadManager | None = None,
    ) -> None:
        self.paths = paths.ensure()
        self.clock = clock
        self.db = Database(paths.database)
        self._settings_lock = threading.Lock()
        self._settings = PublisherSettings.from_dict(self.db.all_settings())
        self.tokens = token_store or default_token_store(paths.token_file)
        self.credentials = credentials or CredentialsProvider(paths.env_file)
        self.http = http or requests.Session()
        self.oauth_states = OAuthStateStore()
        self.youtube_auth = YouTubeAuth(self.credentials, self.tokens, self.http)
        self.facebook_auth = FacebookAuth(self.credentials, self.tokens, self.http)
        self.auths: dict[PublishPlatform, YouTubeAuth | FacebookAuth] = {
            PublishPlatform.YOUTUBE: self.youtube_auth, PublishPlatform.FACEBOOK: self.facebook_auth}
        self.library = VideoLibrary(self.db, paths, self.settings)
        self.notifier = Notifier(self.db, self.settings)
        self.uploaders: dict[PublishPlatform, Uploader] = uploaders or {
            PublishPlatform.YOUTUBE: YouTubeUploader(self.youtube_auth, self.http),
            PublishPlatform.FACEBOOK: FacebookUploader(self.facebook_auth, self.http),
        }
        self.worker = UploadWorker(self.db, self.library, self.uploaders, self.settings, self.notifier, clock=clock)
        self.scheduler = Scheduler(self.db, self.worker, self.settings, self.notifier, clock=clock, submit=submit)
        self.jobs = JobService(self.db, self.library, self.settings, self.auths,
                               on_change=self.scheduler.wake, clock=clock)
        self.downloads = downloads or DownloadManager(self.library)
        self.base_url = "http://127.0.0.1:8765"
        self._lock = InstanceLock(paths.lock_file)
        self.scheduler_running = False

    @classmethod
    def open(cls, data_dir: Path | None = None, **kwargs: Any) -> PublisherApp:
        return cls(PublisherPaths(data_dir or default_data_dir()), **kwargs)

    # ------------------------------------------------------------ lifecycle

    def start_scheduler(self) -> bool:
        """Start the scheduler unless another Bottle Net instance already runs it."""
        if not self._lock.acquire():
            logger.warning("Another Bottle Net publisher is running; this one will not run the scheduler.")
            return False
        self.scheduler.start()
        self.scheduler_running = True
        return True

    def close(self) -> None:
        if self.scheduler_running:
            self.scheduler.stop()
            self.scheduler_running = False
        self._lock.release()
        self.db.close()

    # ------------------------------------------------------------- settings

    def settings(self) -> PublisherSettings:
        with self._settings_lock:
            return self._settings

    def update_settings(self, changes: dict[str, Any]) -> PublisherSettings:
        with self._settings_lock:
            updated = PublisherSettings.from_dict(self._settings.to_dict())
            updated.apply(changes)
            self.db.set_settings({k: getattr(updated, k) for k in changes})
            self._settings = updated
        self.scheduler.wake()
        return updated

    def settings_json(self) -> dict[str, Any]:
        settings = self.settings()
        return {
            **settings.to_dict(),
            "missed_policies": MISSED_POLICIES,
            "effective_video_directory": str(self.library.video_dir()),
            "data_directory": str(self.paths.root),
            "log_file": str(self.paths.log_file),
            "token_storage": self.tokens.description,
            "credentials": {p.value: {"configured": self.auths[p].is_configured(),
                                      "setup_hint": self.credentials.setup_hint(p)} for p in PublishPlatform},
        }

    # ------------------------------------------------------------- accounts

    def redirect_uri(self, platform: PublishPlatform) -> str:
        port = self.base_url.rsplit(":", 1)[-1]
        host = "127.0.0.1" if platform is PublishPlatform.YOUTUBE else "localhost"
        return f"http://{host}:{port}/oauth/{platform.value}/callback"

    def accounts_json(self) -> list[dict[str, Any]]:
        result = []
        for platform in PublishPlatform:
            auth = self.auths[platform]
            account = self.db.get_account(platform)
            connected = bool(account) and auth.is_connected()
            entry: dict[str, Any] = {
                "platform": platform.value, "name": platform.display_name, "configured": auth.is_configured(),
                "connected": connected,
                "display_name": account["display_name"] if connected and account else None,
                "detail": account["detail"] if connected and account else None,
                "connected_at": account["connected_at"] if connected and account else None,
                "redirect_uri": self.redirect_uri(platform),
            }
            if not entry["configured"]:
                entry["setup_hint"] = self.credentials.setup_hint(platform)
            if platform is PublishPlatform.FACEBOOK:
                entry["pending_pages"] = self.facebook_auth.pending_pages()
            result.append(entry)
        return result

    def connect(self, platform: PublishPlatform) -> str:
        """Start the official OAuth flow; returns the URL to open in the browser."""
        redirect = self.redirect_uri(platform)
        if platform is PublishPlatform.YOUTUBE:
            self.youtube_auth.client()
            verifier, challenge = pkce_pair()
            state = self.oauth_states.create(platform.value, verifier=verifier, redirect=redirect)
            return self.youtube_auth.authorization_url(redirect, state, challenge)
        self.facebook_auth.client()
        state = self.oauth_states.create(platform.value, redirect=redirect)
        return self.facebook_auth.authorization_url(redirect, state)

    def oauth_callback(self, platform: PublishPlatform, params: dict[str, str]) -> OAuthOutcome:
        """Handle the browser's return from Google/Meta."""
        name = platform.display_name
        if params.get("error"):
            try:
                self.oauth_states.consume(params.get("state"), platform.value)
            except PublisherError:
                pass
            cancelled = params.get("error") in ("access_denied", "user_denied")
            return OAuthOutcome(False, f"{name} was not connected",
                                "You cancelled the sign-in." if cancelled else f"{name} reported an error during sign-in.")
        try:
            pending = self.oauth_states.consume(params.get("state"), platform.value)
            code = params.get("code", "")
            if not code:
                raise PublisherError("The sign-in did not return an authorization code. Try again.")
            if platform is PublishPlatform.YOUTUBE:
                info = self.youtube_auth.complete(code, pending["redirect"], pending["verifier"])
                self.db.set_account(platform, display_name=info.display_name, detail=info.detail,
                                    remote_id=info.remote_id)
                self.notifier.info("YouTube connected", info.display_name)
                return OAuthOutcome(True, "YouTube connected", f"Connected as {info.display_name}.")
            pages = self.facebook_auth.complete(code, pending["redirect"])
            if len(pages) == 1:
                self.select_facebook_page(pages[0]["id"])
                return OAuthOutcome(True, "Facebook connected", f"Connected to the Page {pages[0]['name']}.")
            return OAuthOutcome(True, "Choose your Facebook Page",
                                "Go back to Bottle Net and choose which Page to publish to.")
        except PublisherError as exc:
            return OAuthOutcome(False, f"{name} was not connected", exc.message)
        except Exception as exc:  # noqa: BLE001
            logger.warning("OAuth callback for %s failed: %s", name, getattr(exc, "detail", None) or repr(exc))
            return OAuthOutcome(False, f"{name} was not connected",
                                getattr(exc, "message", "An unexpected error occurred. Try again."))

    def select_facebook_page(self, page_id: str) -> None:
        info = self.facebook_auth.select_page(page_id)
        self.db.set_account(PublishPlatform.FACEBOOK, display_name=info.display_name, detail=info.detail,
                            remote_id=info.remote_id)
        self.notifier.info("Facebook connected", f"Page: {info.display_name}")

    def disconnect(self, platform: PublishPlatform) -> None:
        self.auths[platform].disconnect()
        self.db.delete_account(platform)

    # ----------------------------------------------------------------- views

    def local(self, value: datetime | None) -> datetime | None:
        return value.astimezone(self.settings().zone) if value else None

    def video_json(self, video: Video, jobs: list[Job] | None = None) -> dict[str, Any]:
        jobs = [j for j in (jobs if jobs is not None else self.db.list_jobs()) if j.video_id == video.id]
        pjs = [pj for job in jobs for pj in job.platform_jobs]
        published = sorted({pj.platform.value for pj in pjs if pj.status is Status.COMPLETED})
        if any(pj.status in ACTIVE_STATUSES for pj in pjs):
            status = "scheduled"
        elif published:
            status = "published"
        elif any(pj.status is Status.FAILED for pj in pjs):
            status = "failed"
        else:
            status = "not published"
        return {
            "id": video.id, "filename": video.filename, "size": video.size, "duration": video.duration,
            "created_at": to_iso(video.created_at), "source": video.source, "source_url": video.source_url,
            "exists": Path(video.path).is_file(), "managed": video.managed,
            "thumbnail_url": f"/media/images/{video.thumbnail}" if video.thumbnail else None,
            "preview_url": f"/media/videos/{video.id}", "status": status, "published_to": published,
            "job_count": len(jobs),
        }

    def videos_json(self, search: str = "", status: str = "") -> list[dict[str, Any]]:
        jobs = self.db.list_jobs()
        videos = [self.video_json(v, jobs) for v in self.library.videos(search)]
        return [v for v in videos if not status or v["status"] == status]

    @staticmethod
    def platform_job_json(pj: PlatformJob) -> dict[str, Any]:
        return {
            "id": pj.id, "platform": pj.platform.value, "status": pj.status.value, "progress": pj.progress,
            "attempts": pj.attempts, "retry_count": pj.retry_count, "last_error": pj.last_error,
            "error_code": pj.error_code, "due_at": to_iso(pj.due_at), "remote_id": pj.remote_id,
            "remote_post_id": pj.remote_post_id, "remote_url": pj.remote_url, "warnings": pj.warnings,
            "started_at": to_iso(pj.started_at), "completed_at": to_iso(pj.completed_at),
            "updated_at": to_iso(pj.updated_at), "cancel_requested": pj.cancel_requested,
            "publish_again": pj.publish_again,
        }

    def job_json(self, job: Job) -> dict[str, Any]:
        yt = job.platform_job(PublishPlatform.YOUTUBE)
        fb = job.platform_job(PublishPlatform.FACEBOOK)
        started = [pj.started_at for pj in job.platform_jobs if pj.started_at]
        completed = [pj.completed_at for pj in job.platform_jobs if pj.completed_at]
        return {
            "id": job.id, "video_id": job.video_id, "video": job.video_name, "title": job.title,
            "description": job.description, "tags": job.tags,
            "thumbnail_url": f"/media/images/{job.thumbnail}" if job.thumbnail else None,
            "platforms": [p.value for p in job.platforms], "scheduled_at": to_iso(job.scheduled_at),
            "timezone": job.timezone, "schedule_mode": job.schedule_mode.value, "privacy": job.privacy,
            "made_for_kids": job.made_for_kids, "status": job.status, "created_at": to_iso(job.created_at),
            "started_at": to_iso(min(started)) if started else None,
            "completed_at": to_iso(max(completed)) if completed and job.status == "completed" else None,
            "retry_count": sum(pj.retry_count for pj in job.platform_jobs),
            "last_error": next((pj.last_error for pj in job.platform_jobs if pj.status is Status.FAILED), None),
            "youtube_video_id": yt.remote_id if yt else None,
            "facebook_post_id": (fb.remote_post_id or fb.remote_id) if fb else None,
            "platform_jobs": [self.platform_job_json(pj) for pj in job.platform_jobs],
        }

    def queue_json(self) -> list[dict[str, Any]]:
        """Active uploads plus those that finished in the last 24 hours."""
        cutoff = self.clock() - timedelta(hours=24)
        jobs = []
        for job in self.db.list_jobs():
            active = any(pj.status in ACTIVE_STATUSES for pj in job.platform_jobs)
            recent = any((pj.updated_at or job.created_at) >= cutoff for pj in job.platform_jobs)
            if active or recent:
                jobs.append(self.job_json(job))
        jobs.sort(key=lambda j: (j["status"] not in ("uploading", "retrying"), j["scheduled_at"] or j["created_at"]))
        return jobs

    def jobs_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Jobs whose publish time falls in [start, end) (for the calendar)."""
        result = []
        for job in self.db.list_jobs():
            when = job.scheduled_at or job.created_at
            if start <= when < end:
                result.append(self.job_json(job))
        return sorted(result, key=lambda j: j["scheduled_at"] or j["created_at"])

    def history_json(self, filter_name: str = "all") -> list[dict[str, Any]]:
        platform, statuses = None, None
        if filter_name in ("youtube", "facebook"):
            platform = filter_name
        elif filter_name == "completed":
            statuses = [Status.COMPLETED]
        elif filter_name == "failed":
            statuses = [Status.FAILED]
        elif filter_name == "pending":
            statuses = list(ACTIVE_STATUSES)
        elif filter_name == "cancelled":
            statuses = [Status.CANCELLED]
        elif filter_name != "all":
            raise ValidationError(f"Unknown filter: {filter_name}")
        rows = []
        for pj, job in self.db.history(platform=platform, statuses=statuses):
            rows.append({**self.platform_job_json(pj), "job_id": job.id, "video": job.video_name, "title": job.title,
                         "scheduled_at": to_iso(job.scheduled_at), "created_at": to_iso(job.created_at)})
        return rows

    def dashboard_json(self) -> dict[str, Any]:
        settings = self.settings()
        zone = settings.zone
        now = self.clock()
        today = now.astimezone(zone).date()
        jobs = self.db.list_jobs()

        def is_today(value: datetime | None) -> bool:
            return value is not None and value.astimezone(zone).date() == today

        todays = [j for j in jobs if is_today(j.scheduled_at or j.created_at)]
        todays.sort(key=lambda j: j.scheduled_at or j.created_at)
        pjs = [pj for j in jobs for pj in j.platform_jobs]
        upcoming = sorted((j for j in jobs if j.scheduled_at and j.scheduled_at > now
                           and any(pj.status is Status.SCHEDULED for pj in j.platform_jobs)),
                          key=lambda j: j.scheduled_at or now)
        accounts = self.accounts_json()
        return {
            "version": __version__,
            "timezone": settings.timezone,
            "now": to_iso(now),
            "scheduler_running": self.scheduler_running,
            "accounts": accounts,
            "stats": {
                "scheduled_today": sum(1 for j in jobs if is_today(j.scheduled_at)),
                "uploaded_today": sum(1 for j in jobs if any(pj.status is Status.COMPLETED and is_today(pj.completed_at)
                                                             for pj in j.platform_jobs)),
                "pending": sum(1 for pj in pjs if pj.status in ACTIVE_STATUSES),
                "failed": sum(1 for pj in pjs if pj.status is Status.FAILED),
                "connected": sum(1 for a in accounts if a["connected"]),
            },
            "next": self.job_json(upcoming[0]) if upcoming else None,
            "today": [self.job_json(j) for j in todays],
            "unread_notifications": len(self.db.notifications(unread_only=True)),
        }
