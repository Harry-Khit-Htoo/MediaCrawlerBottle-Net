"""Progress bars for crawling, single downloads and batch downloads.

Progress is drawn on the (stderr) status console and never touches stdout,
which is reserved for machine-readable data. Built on :mod:`rich.progress`. :class:`DownloadProgress` exposes
:meth:`DownloadProgress.hook`, which matches yt-dlp's ``progress_hooks``
callback signature, so the downloader can report bytes as they arrive.
"""

from __future__ import annotations

import time
from types import TracebackType
from typing import Any, Self

from rich.console import Console
from rich.progress import (
    Progress,
    ProgressColumn,
    Task,
    TaskID,
    TextColumn,
)
from rich.text import Text

BAR_WIDTH = 32


class BlockBarColumn(ProgressColumn):
    """A solid block progress bar (``█████░░░░``).

    When the total is unknown, a short block sweeps back and forth.
    """

    def __init__(self, width: int = BAR_WIDTH, style: str = "cyan", *, brackets: bool = False) -> None:
        super().__init__()
        self.width = width
        self.style = style
        self.brackets = brackets

    def render(self, task: Task) -> Text:
        bar = self._bar(task)
        return Text.assemble("[", bar, "]") if self.brackets or task.fields.get("brackets") else bar

    def _bar(self, task: Task) -> Text:
        if task.total is None or task.total <= 0:
            segment = 6
            span = self.width - segment
            pos = int(time.monotonic() * 12) % (2 * span) if span > 0 else 0
            start = pos if pos <= span else 2 * span - pos
            text = Text("░" * start, style="dim")
            text.append("█" * segment, style=self.style)
            text.append("░" * (self.width - start - segment), style="dim")
            return text
        filled = int(round(self.width * min(task.completed / task.total, 1.0)))
        text = Text("█" * filled, style="green" if task.finished else self.style)
        text.append("░" * (self.width - filled), style="dim")
        return text


class PercentColumn(ProgressColumn):
    """Percentage complete, blank when the total is unknown."""

    def render(self, task: Task) -> Text:
        if task.total is None or task.total <= 0:
            return Text("   ")
        return Text(f"{task.percentage:>3.0f}%")


class _ProgressBase:
    """Context-manager plumbing shared by the progress displays."""

    progress: Progress

    def __enter__(self) -> Self:
        self.progress.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.progress.stop()

    def print(self, *args: Any, **kwargs: Any) -> None:
        """Print above the live progress display."""
        self.progress.console.print(*args, **kwargs)


class CrawlProgress(_ProgressBase):
    """``Progress: ████ 100%  N found`` line shown while a crawler runs.

    The number of videos is unknown until the crawl ends, so the bar sweeps
    while searching and fills to 100% when the crawl is complete.
    """

    def __init__(self, console: Console, *, live: bool = True) -> None:
        self.progress = Progress(
            TextColumn("[bold]Progress:[/bold]"),
            BlockBarColumn(),
            PercentColumn(),
            TextColumn("{task.fields[found]} found"),
            console=console,
            disable=not live,
            # Never capture sys.stdout: it may be carrying the URL list.
            redirect_stdout=False,
        )
        self.task: TaskID = self.progress.add_task("", total=None, found=0)
        self.found = 0

    def advance(self, count: int = 1) -> None:
        """Record newly discovered videos."""
        self.found += count
        self.progress.update(self.task, found=self.found)

    def hide(self) -> None:
        """Remove the live counter (used when the crawl fails)."""
        self.progress.update(self.task, visible=False)

    def finish(self) -> None:
        """Mark the crawl as complete (fills the bar to 100%)."""
        total = max(self.found, 1)
        self.progress.update(self.task, total=total, completed=total, found=self.found)


class DownloadProgress(_ProgressBase):
    """Byte-level progress for downloads, with size and speed.

    For batch downloads (``batch_total``), an overall ``[████] 100%`` bar
    shows how many videos of the list have been processed.
    """

    def __init__(self, console: Console, *, batch_total: int | None = None) -> None:
        columns: list[ProgressColumn] = []
        if batch_total is not None:
            columns.append(TextColumn("{task.description}", style="bold"))
        columns += [BlockBarColumn(), PercentColumn(), TextColumn("{task.fields[detail]}")]
        self.progress = Progress(*columns, console=console, redirect_stdout=False)
        self.overall: TaskID | None = None
        self.batch_total = batch_total or 0
        if batch_total is not None:
            self.overall = self.progress.add_task(
                " " * len(f"[{batch_total}/{batch_total}]"), total=max(batch_total, 1),
                detail=f"0/{batch_total} videos", brackets=True,
            )
        self.file_task: TaskID | None = None
        self._current_file: str | None = None
        self._started_at: float | None = None
        self._bytes_done = 0.0
        #: Average speed (bytes/second) of the last finished file, if known.
        self.average_speed: float | None = None

    # ----------------------------------------------------------------- per file

    def start_file(self, description: str = "Download") -> None:
        """Begin tracking a new file (resets the byte-level bar)."""
        if self.file_task is not None:
            self.progress.remove_task(self.file_task)
        self.file_task = self.progress.add_task(description, total=None, detail="starting...")
        self._current_file = None
        self._started_at = None
        self._bytes_done = 0.0
        self.average_speed = None

    def hook(self, status: dict[str, Any]) -> None:
        """yt-dlp ``progress_hooks`` callback."""
        if self.file_task is None:
            self.start_file()
        assert self.file_task is not None
        state = status.get("status")
        filename = status.get("filename")
        if filename and filename != self._current_file:
            # A new stream (e.g. separate audio track) started; reset the bar.
            self._current_file = filename
            self.progress.reset(self.file_task, total=None, detail="")

        total = status.get("total_bytes") or status.get("total_bytes_estimate")
        done = status.get("downloaded_bytes") or 0
        if state == "downloading":
            if self._started_at is None:
                self._started_at = time.monotonic()
            self.progress.update(self.file_task, total=total, completed=done, detail=self._detail(done, total, status))
        elif state == "finished":
            size = total or done or 1
            self._bytes_done += size
            elapsed = status.get("elapsed")
            if self._started_at is not None:
                elapsed = time.monotonic() - self._started_at
            if elapsed and elapsed > 0 and self._started_at is not None:
                self.average_speed = self._bytes_done / elapsed
            self.progress.update(self.file_task, total=size, completed=size, detail=format_bytes(size))

    def finish_file(self, detail: str = "") -> None:
        """Mark the current file as complete."""
        if self.file_task is None:
            return
        task = self.progress.tasks[self._index(self.file_task)]
        total = task.total or task.completed or 1
        self.progress.update(self.file_task, total=total, completed=total, detail=detail or task.fields.get("detail", ""))

    def hide_file(self) -> None:
        """Hide the per-file bar (used when a download fails or is skipped)."""
        if self.file_task is not None:
            self.progress.update(self.file_task, visible=False)

    def advance_overall(self, done: int) -> None:
        """Update the overall batch counter to *done* processed items."""
        if self.overall is not None:
            self.progress.update(self.overall, completed=done, detail=f"{done}/{self.batch_total} videos")

    def stop(self) -> None:
        """Stop the live display (safe to call more than once)."""
        self.progress.stop()

    # ------------------------------------------------------------------ helpers

    def _index(self, task_id: TaskID) -> int:
        return [t.id for t in self.progress.tasks].index(task_id)

    @staticmethod
    def _detail(done: float, total: float | None, status: dict[str, Any]) -> str:
        parts = [f"{format_bytes(done)} / {format_bytes(total)}" if total else format_bytes(done)]
        speed = status.get("speed")
        if speed:
            parts.append(f"{format_bytes(speed)}/s")
        eta = status.get("eta")
        if eta and status.get("status") == "downloading":
            parts.append(f"ETA {format_duration(eta)}")
        return " | ".join(parts)


def format_bytes(size: float | None) -> str:
    """Format a byte count using decimal units, e.g. ``12.4 MB``."""
    if size is None:
        return "unknown"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1000 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1000
    return f"{value:.1f} TB"  # pragma: no cover


def format_duration(seconds: float) -> str:
    """Format seconds as ``m:ss`` or ``h:mm:ss``."""
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
