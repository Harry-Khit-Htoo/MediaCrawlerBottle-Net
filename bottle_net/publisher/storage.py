"""SQLite persistence for the publisher.

The database holds videos, jobs, platform jobs, upload attempts, settings,
templates, notifications and *non-secret* account details (display names).
OAuth tokens are never stored here; see :mod:`bottle_net.publisher.secrets`.

All access goes through one connection guarded by a lock, so the web server
threads, the scheduler and the upload workers can share it safely. Claiming
a due job is a single conditional ``UPDATE``, so a job can never be picked
up twice, even by two processes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from bottle_net.publisher.errors import NotFoundError
from bottle_net.publisher.models import Job, PlatformJob, PublishPlatform, Status, Video, to_iso, utcnow

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    filename TEXT NOT NULL,
    size INTEGER,
    duration REAL,
    sha256 TEXT,
    source TEXT,
    source_url TEXT,
    thumbnail TEXT,
    managed INTEGER NOT NULL DEFAULT 0,
    deleted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER NOT NULL REFERENCES videos(id),
    video_name TEXT NOT NULL,
    video_sha256 TEXT,
    title TEXT NOT NULL,
    description TEXT,
    tags TEXT,
    thumbnail TEXT,
    platforms TEXT NOT NULL,
    scheduled_at TEXT,
    timezone TEXT NOT NULL,
    schedule_mode TEXT NOT NULL,
    privacy TEXT NOT NULL,
    made_for_kids INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS platform_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    platform TEXT NOT NULL,
    status TEXT NOT NULL,
    due_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    round_attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    error_code TEXT,
    progress REAL NOT NULL DEFAULT 0,
    remote_id TEXT,
    remote_post_id TEXT,
    remote_url TEXT,
    session TEXT,
    warnings TEXT,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    publish_again INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_platform_jobs_due ON platform_jobs(status, due_at);

CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_job_id INTEGER NOT NULL REFERENCES platform_jobs(id),
    attempt INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT,
    job_id INTEGER,
    platform_job_id INTEGER,
    created_at TEXT NOT NULL,
    read INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS accounts (
    platform TEXT PRIMARY KEY,
    display_name TEXT,
    detail TEXT,
    remote_id TEXT,
    connected_at TEXT NOT NULL
);
"""

_PJ_FIELDS = frozenset({
    "status", "due_at", "attempts", "round_attempts", "last_error", "error_code", "progress", "remote_id",
    "remote_post_id", "remote_url", "session", "warnings", "started_at", "completed_at", "cancel_requested",
})
_JOB_FIELDS = frozenset({
    "title", "description", "tags", "thumbnail", "scheduled_at", "timezone", "schedule_mode", "privacy",
    "made_for_kids",
})
_VIDEO_FIELDS = frozenset({"duration", "thumbnail", "deleted", "size", "sha256"})


def _encode(value: Any) -> Any:
    if isinstance(value, datetime):
        return to_iso(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    if isinstance(value, bool):
        return int(value)
    return value


class Database:
    """Repository for all publisher data."""

    def __init__(self, path: Path | str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if str(path) != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run several statements atomically."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)))

    def _execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def _update(self, table: str, row_id: int, allowed: frozenset[str], fields: dict[str, Any]) -> None:
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Cannot update {table}.{sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = [_encode(value) for value in fields.values()]
        if table in ("platform_jobs", "jobs"):
            assignments += ", updated_at = ?"
            values.append(to_iso(utcnow()))
        self._execute(f"UPDATE {table} SET {assignments} WHERE id = ?", [*values, row_id])

    # ------------------------------------------------------------------- videos

    def add_video(
        self, *, path: Path, filename: str, size: int, sha256: str, source: str,
        source_url: str | None = None, managed: bool = False, duration: float | None = None,
    ) -> Video:
        cursor = self._execute(
            "INSERT INTO videos(path, filename, size, sha256, source, source_url, managed, duration, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(path), filename, size, sha256, source, source_url, int(managed), duration, to_iso(utcnow())),
        )
        return self.get_video(int(cursor.lastrowid or 0))

    def get_video(self, video_id: int, *, include_deleted: bool = False) -> Video:
        rows = self._query("SELECT * FROM videos WHERE id = ?" + ("" if include_deleted else " AND deleted = 0"),
                           (video_id,))
        if not rows:
            raise NotFoundError(f"Video #{video_id} was not found.")
        return Video.from_row(rows[0])

    def find_video_by_path(self, path: Path) -> Video | None:
        rows = self._query("SELECT * FROM videos WHERE path = ? AND deleted = 0", (str(path),))
        return Video.from_row(rows[0]) if rows else None

    def list_videos(self, search: str = "") -> list[Video]:
        sql = "SELECT * FROM videos WHERE deleted = 0"
        params: list[Any] = []
        if search:
            sql += " AND filename LIKE ? ESCAPE '\\'"
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{escaped}%")
        return [Video.from_row(row) for row in self._query(sql + " ORDER BY created_at DESC, id DESC", params)]

    def update_video(self, video_id: int, **fields: Any) -> None:
        self._update("videos", video_id, _VIDEO_FIELDS, fields)

    # --------------------------------------------------------------------- jobs

    def create_job(self, job: dict[str, Any], platform_rows: list[dict[str, Any]]) -> Job:
        """Insert a job and its platform jobs atomically."""
        now = to_iso(utcnow())
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO jobs(video_id, video_name, video_sha256, title, description, tags, thumbnail,"
                " platforms, scheduled_at, timezone, schedule_mode, privacy, made_for_kids, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job["video_id"], job["video_name"], job["video_sha256"], job["title"], job["description"],
                 json.dumps(job["tags"]), job["thumbnail"], json.dumps([p.value for p in job["platforms"]]),
                 to_iso(job["scheduled_at"]), job["timezone"], job["schedule_mode"].value, job["privacy"],
                 int(job["made_for_kids"]), now, now),
            )
            job_id = int(cursor.lastrowid or 0)
            for row in platform_rows:
                conn.execute(
                    "INSERT INTO platform_jobs(job_id, platform, status, due_at, publish_again, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (job_id, row["platform"].value, row["status"].value, to_iso(row["due_at"]),
                     int(row.get("publish_again", False)), now),
                )
        return self.get_job(job_id)

    def get_job(self, job_id: int) -> Job:
        rows = self._query("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if not rows:
            raise NotFoundError(f"Job #{job_id} was not found.")
        return Job.from_row(rows[0], self.platform_jobs_for(job_id))

    def list_jobs(self, *, job_ids: Iterable[int] | None = None) -> list[Job]:
        """All jobs (newest first), with their platform jobs."""
        if job_ids is not None:
            ids = list(job_ids)
            if not ids:
                return []
            rows = self._query(f"SELECT * FROM jobs WHERE id IN ({','.join('?' * len(ids))}) ORDER BY id DESC", ids)
        else:
            rows = self._query("SELECT * FROM jobs ORDER BY id DESC")
        by_job: dict[int, list[PlatformJob]] = {}
        for pj_row in self._query("SELECT * FROM platform_jobs ORDER BY id"):
            by_job.setdefault(pj_row["job_id"], []).append(PlatformJob.from_row(pj_row))
        return [Job.from_row(row, by_job.get(row["id"], [])) for row in rows]

    def update_job(self, job_id: int, **fields: Any) -> None:
        self._update("jobs", job_id, _JOB_FIELDS, fields)

    def job_sha256(self, job_id: int) -> str | None:
        rows = self._query("SELECT video_sha256 FROM jobs WHERE id = ?", (job_id,))
        return rows[0]["video_sha256"] if rows else None

    # ------------------------------------------------------------ platform jobs

    def platform_jobs_for(self, job_id: int) -> list[PlatformJob]:
        return [PlatformJob.from_row(r) for r in self._query(
            "SELECT * FROM platform_jobs WHERE job_id = ? ORDER BY id", (job_id,))]

    def get_platform_job(self, pj_id: int) -> PlatformJob:
        rows = self._query("SELECT * FROM platform_jobs WHERE id = ?", (pj_id,))
        if not rows:
            raise NotFoundError(f"Platform job #{pj_id} was not found.")
        return PlatformJob.from_row(rows[0])

    def update_platform_job(self, pj_id: int, **fields: Any) -> None:
        self._update("platform_jobs", pj_id, _PJ_FIELDS, fields)

    def transition(self, pj_id: int, from_statuses: Iterable[Status], **fields: Any) -> bool:
        """Update a platform job only if it is still in one of *from_statuses*.

        Returns False when another thread/process changed it first. This is
        what makes claiming jobs and cancelling safe against races.
        """
        unknown = set(fields) - _PJ_FIELDS
        if unknown:
            raise ValueError(f"Cannot update platform_jobs.{sorted(unknown)}")
        statuses = [s.value for s in from_statuses]
        assignments = ", ".join(f"{name} = ?" for name in fields) + ", updated_at = ?"
        values = [_encode(v) for v in fields.values()] + [to_iso(utcnow())]
        cursor = self._execute(
            f"UPDATE platform_jobs SET {assignments} WHERE id = ? AND status IN ({','.join('?' * len(statuses))})",
            [*values, pj_id, *statuses],
        )
        return cursor.rowcount == 1

    def platform_jobs_with_status(self, statuses: Iterable[Status]) -> list[PlatformJob]:
        values = [s.value for s in statuses]
        return [PlatformJob.from_row(r) for r in self._query(
            f"SELECT * FROM platform_jobs WHERE status IN ({','.join('?' * len(values))}) ORDER BY due_at, id",
            values)]

    def due_platform_jobs(self, now: datetime, statuses: Iterable[Status]) -> list[PlatformJob]:
        values = [s.value for s in statuses]
        return [PlatformJob.from_row(r) for r in self._query(
            f"SELECT * FROM platform_jobs WHERE status IN ({','.join('?' * len(values))})"
            " AND due_at <= ? ORDER BY due_at, id", [*values, to_iso(now)])]

    def next_due_at(self, statuses: Iterable[Status]) -> datetime | None:
        values = [s.value for s in statuses]
        rows = self._query(
            f"SELECT MIN(due_at) AS due FROM platform_jobs WHERE status IN ({','.join('?' * len(values))})", values)
        value = rows[0]["due"] if rows else None
        return datetime.fromisoformat(value) if value else None

    def platform_jobs_for_video(self, sha256: str, platform: PublishPlatform) -> list[tuple[PlatformJob, Job]]:
        """Every platform job that published (or will publish) the same video file."""
        rows = self._query(
            "SELECT pj.id AS pj_id, j.id AS job_id FROM platform_jobs pj JOIN jobs j ON j.id = pj.job_id"
            " WHERE j.video_sha256 = ? AND pj.platform = ?", (sha256, platform.value))
        result = []
        for row in rows:
            job = self.get_job(row["job_id"])
            pj = next(p for p in job.platform_jobs if p.id == row["pj_id"])
            result.append((pj, job))
        return result

    def history(self, *, platform: str | None = None, statuses: Iterable[Status] | None = None,
                limit: int = 500) -> list[tuple[PlatformJob, Job]]:
        sql = "SELECT pj.id AS pj_id, pj.job_id AS job_id FROM platform_jobs pj WHERE 1 = 1"
        params: list[Any] = []
        if platform:
            sql += " AND pj.platform = ?"
            params.append(platform)
        if statuses is not None:
            values = [s.value for s in statuses]
            sql += f" AND pj.status IN ({','.join('?' * len(values))})"
            params += values
        sql += " ORDER BY COALESCE(pj.completed_at, pj.updated_at) DESC, pj.id DESC LIMIT ?"
        params.append(limit)
        rows = self._query(sql, params)
        jobs = {job.id: job for job in self.list_jobs(job_ids={row["job_id"] for row in rows})}
        return [(next(p for p in jobs[row["job_id"]].platform_jobs if p.id == row["pj_id"]), jobs[row["job_id"]])
                for row in rows]

    # ----------------------------------------------------------------- attempts

    def start_attempt(self, pj_id: int, attempt: int) -> int:
        cursor = self._execute(
            "INSERT INTO attempts(platform_job_id, attempt, started_at) VALUES (?, ?, ?)",
            (pj_id, attempt, to_iso(utcnow())))
        return int(cursor.lastrowid or 0)

    def finish_attempt(self, attempt_id: int, outcome: str, error: str | None = None) -> None:
        self._execute("UPDATE attempts SET finished_at = ?, outcome = ?, error = ? WHERE id = ?",
                      (to_iso(utcnow()), outcome, error, attempt_id))

    def attempts_for(self, pj_id: int) -> list[sqlite3.Row]:
        return self._query("SELECT * FROM attempts WHERE platform_job_id = ? ORDER BY id", (pj_id,))

    # ----------------------------------------------------------------- settings

    def all_settings(self) -> dict[str, Any]:
        return {row["key"]: json.loads(row["value"]) for row in self._query("SELECT key, value FROM settings")}

    def set_settings(self, values: dict[str, Any]) -> None:
        with self.transaction() as conn:
            for key, value in values.items():
                conn.execute("INSERT INTO settings(key, value) VALUES (?, ?)"
                             " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)))

    # ---------------------------------------------------------------- templates

    def list_templates(self) -> list[dict[str, Any]]:
        return [self._template(row) for row in self._query("SELECT * FROM templates ORDER BY name COLLATE NOCASE")]

    def get_template(self, template_id: int) -> dict[str, Any]:
        rows = self._query("SELECT * FROM templates WHERE id = ?", (template_id,))
        if not rows:
            raise NotFoundError(f"Template #{template_id} was not found.")
        return self._template(rows[0])

    def save_template(self, template_id: int | None, name: str, title: str, description: str,
                      tags: list[str]) -> dict[str, Any]:
        try:
            if template_id is None:
                cursor = self._execute(
                    "INSERT INTO templates(name, title, description, tags, created_at) VALUES (?, ?, ?, ?, ?)",
                    (name, title, description, json.dumps(tags), to_iso(utcnow())))
                template_id = int(cursor.lastrowid or 0)
            else:
                self.get_template(template_id)
                self._execute("UPDATE templates SET name = ?, title = ?, description = ?, tags = ? WHERE id = ?",
                              (name, title, description, json.dumps(tags), template_id))
        except sqlite3.IntegrityError as exc:
            from bottle_net.publisher.errors import ValidationError

            raise ValidationError(f"A template named '{name}' already exists.") from exc
        return self.get_template(template_id)

    def delete_template(self, template_id: int) -> None:
        self.get_template(template_id)
        self._execute("DELETE FROM templates WHERE id = ?", (template_id,))

    @staticmethod
    def _template(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "name": row["name"], "title": row["title"], "description": row["description"],
                "tags": json.loads(row["tags"])}

    # ------------------------------------------------------------ notifications

    def add_notification(self, level: str, title: str, message: str = "", *, job_id: int | None = None,
                         platform_job_id: int | None = None) -> None:
        self._execute(
            "INSERT INTO notifications(level, title, message, job_id, platform_job_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)", (level, title, message, job_id, platform_job_id, to_iso(utcnow())))

    def notifications(self, *, unread_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM notifications" + (" WHERE read = 0" if unread_only else "")
        rows = self._query(sql + " ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(row) | {"read": bool(row["read"])} for row in rows]

    def mark_notifications_read(self, ids: Iterable[int] | None = None) -> None:
        if ids is None:
            self._execute("UPDATE notifications SET read = 1")
            return
        values = list(ids)
        if values:
            self._execute(f"UPDATE notifications SET read = 1 WHERE id IN ({','.join('?' * len(values))})", values)

    # ----------------------------------------------------------------- accounts

    def get_account(self, platform: PublishPlatform) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM accounts WHERE platform = ?", (platform.value,))
        return dict(rows[0]) if rows else None

    def set_account(self, platform: PublishPlatform, *, display_name: str, detail: str = "",
                    remote_id: str = "") -> None:
        self._execute(
            "INSERT INTO accounts(platform, display_name, detail, remote_id, connected_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(platform) DO UPDATE SET display_name = excluded.display_name, detail = excluded.detail,"
            " remote_id = excluded.remote_id, connected_at = excluded.connected_at",
            (platform.value, display_name, detail, remote_id, to_iso(utcnow())))

    def delete_account(self, platform: PublishPlatform) -> None:
        self._execute("DELETE FROM accounts WHERE platform = ?", (platform.value,))
