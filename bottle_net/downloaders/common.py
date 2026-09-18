"""Shared download machinery: retries, single downloads and batch runs.

Platform modules subclass :class:`VideoDownloader`; :func:`run_batch`
drives any downloader over a list of URLs, continuing past failures and
reporting events through :class:`BatchCallbacks` so that presentation stays
in the CLI layer.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Protocol, TypeVar

from bottle_net.config import Config
from bottle_net.errors import BottleNetError, ExtractionError, RateLimitedError, StorageError
from bottle_net.utils.filenames import MAX_NAME_BYTES, safe_filename
from bottle_net.utils.files import FailedURL, ensure_directory, find_existing_download
from bottle_net.utils.urls import Platform, normalize_video_url
from bottle_net.ytdlp import YDLFactory, base_options, classify_error, default_factory, format_selector, has_ffmpeg

logger = logging.getLogger(__name__)

T = TypeVar("T")
ProgressHook = Callable[[dict[str, Any]], None]

#: Field injected into the yt-dlp info dict to control the output filename.
FILENAME_FIELD = "bottle_net_filename"
#: Stop a batch after this many consecutive rate-limit failures.
RATE_LIMIT_ABORT_THRESHOLD = 3
#: Keep full paths below the classic Windows MAX_PATH (260) limit.
MAX_PATH_LENGTH = 250
#: Longest title portion of a filename.
MAX_TITLE_LENGTH = 80
#: Room reserved for yt-dlp's temporary suffixes such as ".part-Frag123".
_SUFFIX_RESERVE = 16


def retry_call(
    func: Callable[[], T],
    *,
    retries: int,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, BottleNetError, float], None] | None = None,
    also_retry: tuple[type[BottleNetError], ...] = (),
) -> T:
    """Call *func*, retrying transient failures with exponential backoff.

    Exceptions are classified with :func:`bottle_net.ytdlp.classify_error`.
    Only errors marked ``retryable`` (network errors, rate limits), plus any
    types listed in *also_retry*, are retried, up to *retries* extra
    attempts; rate limits wait longer. Permanent errors (deleted/private
    video, ...) are raised immediately.
    """
    attempt = 0
    while True:
        try:
            return func()
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 - classified below
            error = classify_error(exc)
            if not (error.retryable or isinstance(error, also_retry)) or attempt >= retries:
                raise error from exc
            attempt += 1
            multiplier = 5 if isinstance(error, RateLimitedError) else 1
            delay = min(base_delay * multiplier * 2 ** (attempt - 1), max_delay)
            logger.debug("Attempt %d failed (%s); retrying in %.1fs", attempt, error, delay)
            if on_retry:
                on_retry(attempt, error, delay)
            sleep(delay)


@dataclass(frozen=True)
class VideoInfo:
    """Metadata about a video, as reported by the platform."""

    id: str
    title: str
    url: str
    uploader: str | None = None
    filesize: int | None = None
    ext: str = "mp4"
    duration: float | None = None
    #: Name of the file the video is (or will be) saved as.
    filename: str | None = None
    #: True when a completed copy already exists (the download is skipped).
    already_downloaded: bool = False


class DownloadStatus(StrEnum):
    """Outcome of a single download attempt."""

    DOWNLOADED = "downloaded"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass
class DownloadResult:
    """The outcome of downloading one URL."""

    url: str
    status: DownloadStatus
    path: Path | None = None
    info: VideoInfo | None = None
    error: BottleNetError | None = None

    @property
    def reason(self) -> str:
        """Human-readable failure reason (empty on success)."""
        return self.error.summary if self.error else ""


class VideoDownloader:
    """Download public videos for one platform using yt-dlp."""

    platform: ClassVar[Platform]

    def __init__(
        self,
        config: Config,
        *,
        ydl_factory: YDLFactory = default_factory,
        sleep: Callable[[float], None] = time.sleep,
        ffmpeg: bool | None = None,
    ) -> None:
        self.config = config
        self._factory = ydl_factory
        self._sleep = sleep
        self._ffmpeg = has_ffmpeg() if ffmpeg is None else ffmpeg

    # ------------------------------------------------------------- extension

    def validate(self, url: str) -> str:
        """Validate *url* and return its canonical form."""
        return normalize_video_url(url, self.platform)

    def clean_title(self, title: str) -> str:
        """Return a tidier title for filenames (platforms may override)."""
        return title

    # ------------------------------------------------------------ operations

    def options(self, dest_dir: Path, hooks: Sequence[ProgressHook] = ()) -> dict[str, Any]:
        """Build yt-dlp options for downloading into *dest_dir*."""
        opts = base_options(self.config)
        opts.update(
            {
                "format": format_selector(self._ffmpeg),
                "paths": {"home": str(dest_dir)},
                "outtmpl": {"default": f"%({FILENAME_FIELD})s.%(ext)s"},
                "progress_hooks": list(hooks),
            }
        )
        if self._ffmpeg:
            opts["merge_output_format"] = "mp4"
        return opts

    def _retry(self, func: Callable[[], T], on_retry: Callable[[int, BottleNetError, float], None] | None) -> T:
        return retry_call(func, retries=self.config.max_retries, sleep=self._sleep, on_retry=on_retry)

    def fetch_info(self, url: str, *, on_retry: Callable[[int, BottleNetError, float], None] | None = None) -> VideoInfo:
        """Look up a video's public metadata without downloading it."""
        canonical = self.validate(url)
        with self._factory(base_options(self.config) | {"format": format_selector(self._ffmpeg)}) as ydl:
            raw = self._retry(lambda: ydl.extract_info(canonical, download=False), on_retry)
        return self._to_info(raw, canonical)

    def download(
        self,
        url: str,
        dest_dir: Path,
        *,
        progress_hook: ProgressHook | None = None,
        on_info: Callable[[VideoInfo], None] | None = None,
        on_retry: Callable[[int, BottleNetError, float], None] | None = None,
    ) -> DownloadResult:
        """Download one video into *dest_dir*.

        Never raises :class:`BottleNetError`; failures are returned as a
        :class:`DownloadResult` with status ``FAILED``. ``KeyboardInterrupt``
        propagates so the caller can stop cleanly (partial files are kept and
        resumed on the next run).
        """
        try:
            canonical = self.validate(url)
        except BottleNetError as exc:
            return DownloadResult(url=url, status=DownloadStatus.FAILED, error=exc)

        try:
            ensure_directory(dest_dir)
            hooks = [progress_hook] if progress_hook else []
            with self._factory(self.options(dest_dir, hooks)) as ydl:
                raw = self._retry(lambda: ydl.extract_info(canonical, download=False), on_retry)
                if not isinstance(raw, dict) or raw.get("_type") in ("playlist", "multi_video"):
                    raise ExtractionError("The URL did not resolve to a single video.")
                info = self._to_info(raw, canonical)

                existing = find_existing_download(dest_dir, info.id)
                if existing:
                    info = replace(info, filename=existing.name, already_downloaded=True)
                    if on_info:
                        on_info(info)
                    return DownloadResult(url=url, status=DownloadStatus.SKIPPED, path=existing, info=info)

                stem = self.filename_stem(info, dest_dir)
                info = replace(info, filename=f"{stem}.{info.ext}")
                if on_info:
                    on_info(info)
                raw[FILENAME_FIELD] = stem
                result = self._retry(lambda: ydl.process_ie_result(raw, download=True), on_retry)
        except BottleNetError as exc:
            return DownloadResult(url=url, status=DownloadStatus.FAILED, error=exc)
        except OSError as exc:
            error = StorageError(f"Could not write to {dest_dir}: {exc.strerror or exc}")
            return DownloadResult(url=url, status=DownloadStatus.FAILED, error=error)

        path = self._downloaded_path(result, dest_dir, info)
        if path is None:
            return DownloadResult(
                url=url,
                status=DownloadStatus.FAILED,
                info=info,
                error=ExtractionError("The download finished but no file was written."),
            )
        return DownloadResult(url=url, status=DownloadStatus.DOWNLOADED, path=path, info=info)

    def filename_stem(self, info: VideoInfo, dest_dir: Path) -> str:
        """Return the output filename (without extension): ``<title>_<id>``.

        The title is sanitised by :func:`bottle_net.utils.filenames.safe_filename`
        and shortened when needed so that the full path stays within
        :data:`MAX_PATH_LENGTH` characters and the name within the 255-byte
        limit of common file systems (including yt-dlp's temporary suffixes).
        """
        suffix = f"_{info.id}.{info.ext}"
        budget = MAX_PATH_LENGTH - len(str(dest_dir.resolve())) - len(suffix) - _SUFFIX_RESERVE
        byte_budget = MAX_NAME_BYTES - len(suffix.encode("utf-8")) - _SUFFIX_RESERVE
        title = safe_filename(info.title, max_length=max(8, min(MAX_TITLE_LENGTH, budget)), max_bytes=byte_budget)
        return f"{title}_{info.id}"

    # --------------------------------------------------------------- helpers

    def _to_info(self, raw: dict[str, Any], url: str) -> VideoInfo:
        video_id = str(raw.get("id") or "").strip()
        if not video_id:
            raise ExtractionError("The platform response did not include a video ID.")
        filesize = raw.get("filesize") or raw.get("filesize_approx")
        if not filesize and raw.get("requested_formats"):
            sizes = [f.get("filesize") or f.get("filesize_approx") for f in raw["requested_formats"]]
            filesize = sum(sizes) if all(sizes) else None
        title = str(raw.get("title") or raw.get("description") or f"{self.platform.display_name} video {video_id}")
        return VideoInfo(
            id=video_id,
            title=" ".join(self.clean_title(title).split()),
            url=str(raw.get("webpage_url") or url),
            uploader=raw.get("uploader") or raw.get("channel"),
            filesize=int(filesize) if filesize else None,
            ext=str(raw.get("ext") or "mp4"),
            duration=raw.get("duration"),
        )

    @staticmethod
    def _downloaded_path(result: Any, dest_dir: Path, info: VideoInfo) -> Path | None:
        if isinstance(result, dict):
            for item in result.get("requested_downloads") or []:
                filepath = item.get("filepath") or item.get("filename")
                if filepath and Path(filepath).is_file():
                    return Path(filepath)
        return find_existing_download(dest_dir, info.id)


# ------------------------------------------------------------------ batches


class BatchDownloader(Protocol):
    """Anything :func:`run_batch` can drive: a platform or auto-detecting downloader."""

    config: Config

    def download(
        self,
        url: str,
        dest_dir: Path,
        *,
        progress_hook: ProgressHook | None = None,
        on_info: Callable[[VideoInfo], None] | None = None,
        on_retry: Callable[[int, BottleNetError, float], None] | None = None,
    ) -> DownloadResult:
        """Download *url* into *dest_dir* (see :meth:`VideoDownloader.download`)."""
        ...


@dataclass
class BatchCallbacks:
    """Optional hooks the batch runner calls to report progress."""

    on_start: Callable[[int, int, str], None] | None = None
    on_info: Callable[[VideoInfo], None] | None = None
    on_result: Callable[[int, int, DownloadResult], None] | None = None
    on_retry: Callable[[int, BottleNetError, float], None] | None = None
    progress_hook: ProgressHook | None = None


@dataclass
class BatchSummary:
    """Totals for a batch download."""

    total: int
    destination: Path
    results: list[DownloadResult] = field(default_factory=list)
    not_attempted: list[FailedURL] = field(default_factory=list)
    aborted_reason: str | None = None
    interrupted: bool = False

    def _count(self, status: DownloadStatus) -> int:
        return sum(1 for r in self.results if r.status is status)

    @property
    def completed(self) -> int:
        """Number of videos downloaded in this run."""
        return self._count(DownloadStatus.DOWNLOADED)

    @property
    def skipped(self) -> int:
        """Number of videos skipped because they were already downloaded."""
        return self._count(DownloadStatus.SKIPPED)

    @property
    def failed(self) -> int:
        """Number of URLs that failed or were not attempted."""
        return self._count(DownloadStatus.FAILED) + len(self.not_attempted)

    @property
    def folders(self) -> list[Path]:
        """Folders that contain this batch's downloaded or skipped videos."""
        return sorted({r.path.parent for r in self.results if r.path is not None})

    @property
    def failures(self) -> list[FailedURL]:
        """Failed and not-attempted URLs, with reasons, for the failed-URL file."""
        failed = [FailedURL(r.url, r.reason) for r in self.results if r.status is DownloadStatus.FAILED]
        return failed + self.not_attempted


def run_batch(
    downloader: BatchDownloader,
    urls: Sequence[str],
    dest_dir: Path,
    *,
    callbacks: BatchCallbacks | None = None,
    delay: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> BatchSummary:
    """Download every URL in *urls* into *dest_dir*.

    One failure never stops the batch. The run stops early only if the
    platform keeps rate limiting (to respect its limits) or on Ctrl+C; any
    URLs not attempted are included in :attr:`BatchSummary.failures` so they
    can be retried later.
    """
    cb = callbacks or BatchCallbacks()
    summary = BatchSummary(total=len(urls), destination=dest_dir)
    wait = downloader.config.request_delay if delay is None else delay
    consecutive_rate_limits = 0

    try:
        for index, url in enumerate(urls, start=1):
            if cb.on_start:
                cb.on_start(index, len(urls), url)
            result = downloader.download(
                url, dest_dir, progress_hook=cb.progress_hook, on_info=cb.on_info, on_retry=cb.on_retry
            )
            summary.results.append(result)
            if cb.on_result:
                cb.on_result(index, len(urls), result)

            if isinstance(result.error, RateLimitedError):
                consecutive_rate_limits += 1
                if consecutive_rate_limits >= RATE_LIMIT_ABORT_THRESHOLD:
                    summary.aborted_reason = (
                        f"Stopped after {consecutive_rate_limits} consecutive rate-limit errors "
                        "to respect the platform's limits."
                    )
                    reason = "Not attempted: stopped because the platform was rate limiting"
                    summary.not_attempted = [FailedURL(u, reason) for u in urls[index:]]
                    break
            else:
                consecutive_rate_limits = 0

            # Be polite between network requests (no need after a skip or a local failure).
            if index < len(urls) and wait > 0 and result.info is not None and result.status is not DownloadStatus.SKIPPED:
                sleep(wait)
    except KeyboardInterrupt:
        summary.interrupted = True
        processed = len(summary.results)
        reason = "Not attempted: download was interrupted"
        summary.not_attempted = [FailedURL(u, reason) for u in urls[processed:]]

    return summary
