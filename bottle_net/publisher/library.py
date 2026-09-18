"""The video library: videos that can be published.

Videos arrive three ways:

* uploaded through the GUI (drag & drop or file picker) - stored in the
  video folder (``managed``),
* imported in place from disk (e.g. the Bottle Net ``downloads`` folder),
* downloaded with the existing Bottle Net downloader (see ``downloads``).

Each file's SHA-256 is recorded so that publishing the same video twice to
a platform can be detected, even if it was imported again under another
name.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from bottle_net.publisher.errors import ConflictError, NotFoundError, ValidationError
from bottle_net.publisher.models import Video
from bottle_net.publisher.paths import PublisherPaths
from bottle_net.publisher.settings import PublisherSettings
from bottle_net.publisher.storage import Database
from bottle_net.utils.filenames import sanitize_filename, unique_path

VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".wmv", ".3gp", ".mpeg", ".mpg"})
MAX_VIDEO_BYTES = 64 * 1024**3
MAX_IMAGE_BYTES = 5 * 1024 * 1024
_CHUNK = 1024 * 1024


def image_extension(data: bytes) -> str:
    """Return ``.jpg``/``.png`` for a JPEG/PNG image, or raise ValidationError."""
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    raise ValidationError("Thumbnails must be JPEG or PNG images.")


def mp4_duration(path: Path) -> float | None:
    """Read the duration of an MP4/MOV file from its ``moov/mvhd`` header (no FFmpeg needed).

    Returns None for other formats or unreadable files. The browser fills in
    the duration for formats it can play; this also covers codecs it cannot
    (e.g. H.265 on many Windows systems).
    """
    if path.suffix.lower() not in (".mp4", ".m4v", ".mov", ".3gp"):
        return None
    try:
        with path.open("rb") as fh:
            return _find_mvhd(fh, 0, path.stat().st_size)
    except (OSError, ValueError, ZeroDivisionError):
        return None


def _find_mvhd(fh: BinaryIO, start: int, end: int) -> float | None:
    offset = start
    while offset + 8 <= end:
        fh.seek(offset)
        header = fh.read(8)
        if len(header) < 8:
            return None
        size, kind = int.from_bytes(header[:4], "big"), header[4:]
        header_size = 8
        if size == 1:
            size = int.from_bytes(fh.read(8), "big")
            header_size = 16
        elif size == 0:
            size = end - offset
        if size < header_size:
            return None
        if kind == b"moov":
            return _find_mvhd(fh, offset + header_size, offset + size)
        if kind == b"mvhd":
            data = fh.read(32)
            if data[0] == 1:  # version 1: 64-bit times
                timescale = int.from_bytes(data[20:24], "big")
                duration = int.from_bytes(data[24:32], "big")
            else:
                timescale = int.from_bytes(data[12:16], "big")
                duration = int.from_bytes(data[16:20], "big")
            return round(duration / timescale, 3) if timescale else None
        offset += size
    return None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


class VideoLibrary:
    """Adds, finds and removes library videos."""

    def __init__(self, db: Database, paths: PublisherPaths, settings: Callable[[], PublisherSettings]) -> None:
        self.db = db
        self.paths = paths
        self._settings = settings

    def video_dir(self) -> Path:
        configured = self._settings().video_directory.strip()
        folder = Path(configured).expanduser() if configured else self.paths.videos
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def get(self, video_id: int, *, include_deleted: bool = False) -> Video:
        return self.db.get_video(video_id, include_deleted=include_deleted)

    def videos(self, search: str = "") -> list[Video]:
        return self.db.list_videos(search.strip())

    def import_stream(self, filename: str, stream: BinaryIO, length: int) -> Video:
        """Store an uploaded video in the video folder and add it to the library."""
        # sanitize_filename turns "/" and "\" into "-", so no path can escape the folder.
        name = sanitize_filename(filename or "video.mp4", max_length=120)
        if Path(name).suffix.lower() not in VIDEO_EXTENSIONS:
            raise ValidationError(f"'{filename}' is not a supported video file "
                                  f"({', '.join(sorted(VIDEO_EXTENSIONS))}).")
        if length <= 0:
            raise ValidationError("The video file is empty.")
        if length > MAX_VIDEO_BYTES:
            raise ValidationError("The video file is too large.")
        destination = unique_path(self.video_dir() / name)
        partial = destination.with_name(destination.name + ".part")
        digest = hashlib.sha256()
        received = 0
        try:
            with partial.open("wb") as fh:
                while received < length:
                    block = stream.read(min(_CHUNK, length - received))
                    if not block:
                        break
                    fh.write(block)
                    digest.update(block)
                    received += len(block)
            if received != length:
                raise ValidationError("The upload was interrupted before the whole file arrived. Try again.")
            partial.replace(destination)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        return self.db.add_video(path=destination, filename=destination.name, size=received,
                                 sha256=digest.hexdigest(), source="upload", managed=True,
                                 duration=mp4_duration(destination))

    def import_path(self, path: Path, *, source: str = "import", source_url: str | None = None) -> Video:
        """Add an existing file (left where it is) to the library."""
        path = path.resolve()
        if not path.is_file():
            raise NotFoundError(f"File not found: {path}")
        if path.suffix.lower() not in VIDEO_EXTENSIONS:
            raise ValidationError(f"'{path.name}' is not a supported video file.")
        existing = self.db.find_video_by_path(path)
        if existing:
            return existing
        return self.db.add_video(path=path, filename=path.name, size=path.stat().st_size,
                                 sha256=file_sha256(path), source=source, source_url=source_url, managed=False,
                                 duration=mp4_duration(path))

    def import_folder(self, folder: Path) -> list[Video]:
        """Import every finished video file under *folder* (e.g. Bottle Net's downloads)."""
        if not folder.is_dir():
            return []
        added = []
        for path in sorted(folder.rglob("*")):
            if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS and path.stat().st_size > 0:
                known = self.db.find_video_by_path(path.resolve())
                if known is None:
                    added.append(self.import_path(path, source="download"))
        return added

    def set_preview(self, video_id: int, *, image: bytes | None = None, duration: float | None = None) -> Video:
        """Store the library thumbnail and duration (captured in the browser)."""
        video = self.get(video_id)
        fields: dict[str, object] = {}
        if duration is not None:
            if not 0 <= duration < 7 * 24 * 3600:
                raise ValidationError("Invalid video duration.")
            fields["duration"] = float(duration)
        if image is not None:
            fields["thumbnail"] = self.save_image(image, prefix=f"video{video.id}")
        if fields:
            self.db.update_video(video.id, **fields)
        return self.get(video_id)

    def save_image(self, data: bytes, *, prefix: str = "thumb") -> str:
        """Save a JPEG/PNG image to the thumbnail folder; return its file name."""
        if len(data) > MAX_IMAGE_BYTES:
            raise ValidationError("Thumbnails must be smaller than 5 MB.")
        name = f"{prefix}_{secrets.token_hex(8)}{image_extension(data)}"
        self.paths.thumbnails.mkdir(parents=True, exist_ok=True)
        (self.paths.thumbnails / name).write_bytes(data)
        return name

    def image_path(self, name: str | None) -> Path | None:
        """Resolve a stored thumbnail name, refusing anything outside the folder."""
        if not name or Path(name).name != name:
            return None
        path = self.paths.thumbnails / name
        return path if path.is_file() else None

    def delete(self, video_id: int, *, delete_file: bool, has_active_jobs: Callable[[int], bool]) -> None:
        """Remove a video from the library (and its file, if Bottle Net owns it)."""
        video = self.get(video_id)
        if has_active_jobs(video_id):
            raise ConflictError("This video has scheduled uploads. Cancel them first, then delete the video.")
        self.db.update_video(video_id, deleted=True)
        if delete_file and video.managed:
            Path(video.path).unlink(missing_ok=True)
