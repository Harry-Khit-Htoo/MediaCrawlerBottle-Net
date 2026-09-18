"""File and directory helpers: URL lists, failed-URL records, download lookup.

Filename sanitising lives in :mod:`bottle_net.utils.filenames`.
"""

from __future__ import annotations

import codecs
import os
import re
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from bottle_net.errors import URLListError
from bottle_net.utils.filenames import safe_filename, unique_path
from bottle_net.utils.urls import Platform, video_key

# Suffixes added by the crawler: "_links", "_links_2", "_2026-09-18".
_GENERATED_SUFFIX = re.compile(r"(?:_links(?:_\d+)?|_\d{4}-\d{2}-\d{2})$")
_PLATFORM_PREFIX = re.compile("^(?:" + "|".join(p.value for p in Platform) + ")_(?=.)")
# Byte-order marks, longest first (UTF-32 LE starts with the UTF-16 LE mark).
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)
# Machine-readable header lines in failed-URL files, e.g.
# "# bottle-net-output-dir: C:\\Users\\me\\downloads\\tiktok\\example".
_DIRECTIVE = re.compile(r"^#\s*bottle-net-([a-z-]+):\s*(.+?)\s*$")
OUTPUT_DIR_DIRECTIVE = "output-dir"
NAME_DIRECTIVE = "name"


@dataclass(frozen=True)
class URLList:
    """The result of reading a URL list (from a file or standard input)."""

    #: The file read, or ``None`` for standard input.
    path: Path | None
    urls: list[str]
    duplicates: int
    blank_lines: int
    comment_lines: int
    #: ``# bottle-net-<key>: <value>`` header lines (written in failed-URL files).
    directives: dict[str, str] = field(default_factory=dict)

    @property
    def source(self) -> str:
        """Human-readable name of where the list came from."""
        return display_path(self.path) if self.path is not None else "standard input"


@dataclass(frozen=True)
class FailedURL:
    """A URL that could not be downloaded, with the reason why."""

    url: str
    reason: str


def ensure_directory(path: Path) -> Path:
    """Create *path* (and parents) if needed and return it."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except FileExistsError as exc:
        raise OSError(f"Cannot create folder {path}: a file with that name exists") from exc
    return path


def decode_text(data: bytes) -> str:
    """Decode text, honouring a byte-order mark if present (default UTF-8).

    Windows PowerShell 5.1 writes UTF-16 files when output is redirected
    with ``>``; those files are read transparently.
    """
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            return data.decode(encoding)
    return data.decode("utf-8")


def parse_url_list(text: str, *, path: Path | None = None) -> URLList:
    """Parse URL-list text: one URL per line.

    Blank lines and lines starting with ``#`` are ignored, and duplicate
    URLs (the same video under different URL forms) are removed while
    keeping the original order.
    """
    urls: list[str] = []
    seen: set[str] = set()
    directives: dict[str, str] = {}
    blank = comments = duplicates = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            blank += 1
            continue
        if line.startswith("#"):
            comments += 1
            if match := _DIRECTIVE.match(line):
                directives[match.group(1)] = match.group(2)
            continue
        key = video_key(line)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        urls.append(line)

    return URLList(
        path=path, urls=urls, duplicates=duplicates, blank_lines=blank, comment_lines=comments, directives=directives
    )


def read_url_list(path: Path) -> URLList:
    """Read a URL list file (see :func:`parse_url_list` for the format)."""
    if not path.exists():
        raise URLListError(f"URL list not found: {path}")
    if not path.is_file():
        raise URLListError(f"Not a file: {path}")
    try:
        text = decode_text(path.read_bytes())
    except UnicodeDecodeError as exc:
        raise URLListError(f"{path} is not a UTF-8 text file.") from exc
    except OSError as exc:
        raise URLListError(f"Could not read {path}: {exc.strerror or exc}") from exc
    return parse_url_list(text, path=path)


def read_url_stream(stream: BinaryIO) -> URLList:
    """Read a URL list from a binary stream such as ``sys.stdin.buffer``."""
    try:
        text = decode_text(stream.read())
    except UnicodeDecodeError as exc:
        raise URLListError("Standard input is not UTF-8 text.") from exc
    except OSError as exc:
        raise URLListError(f"Could not read standard input: {exc.strerror or exc}") from exc
    return parse_url_list(text)


def atomic_write_text(path: Path, text: str) -> Path:
    """Write *text* to *path* atomically (temporary file + rename)."""
    ensure_directory(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return path


def write_url_list(path: Path, urls: Iterable[str]) -> Path:
    """Write URLs to *path*, one per line (the format ``download-list`` reads)."""
    lines = [u.strip() for u in urls if u.strip()]
    return atomic_write_text(path, "".join(f"{u}\n" for u in lines))


def retry_command(
    path: Path,
    platform: Platform | None,
    *,
    destination: Path | None = None,
    name: str | None = None,
    multiline: bool = False,
) -> str:
    """Return the command that retries the URLs recorded in *path*.

    *platform* ``None`` means the universal ``bottle-net download-list``.
    With *multiline*, the folder option goes on a continuation line
    (``\\``, for POSIX shells).
    """
    prefix = f"bottle-net {platform.value}" if platform else "bottle-net"
    command = f'{prefix} download-list "{display_path(path)}"'
    option = ""
    if destination is not None:
        option = f'--output-dir "{display_path(destination)}"'
    elif name:
        option = f'--name "{name}"'
    if not option:
        return command
    return f"{command} \\\n  {option}" if multiline else f"{command} {option}"


def write_failed_urls(
    path: Path,
    failures: Sequence[FailedURL],
    *,
    platform: Platform | None,
    destination: Path | None = None,
    name: str | None = None,
) -> Path:
    """Record failed URLs so they can be retried with ``download-list``.

    Each URL is preceded by a ``#`` comment giving the reason; comment lines
    are ignored when the file is read back as a URL list. The header shows
    the exact retry command and records the original download folder (or
    batch name), so a retry never ends up in a new, unexpected folder.
    """
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    what = f"{platform.display_name} downloads" if platform else "downloads"
    lines = [
        f"# Bottle Net Tool - failed {what} ({stamp})",
        f"# Retry with: {retry_command(path, platform, destination=destination, name=name)}",
    ]
    if destination is not None:
        lines.append(f"# bottle-net-{OUTPUT_DIR_DIRECTIVE}: {destination.resolve()}")
    elif name:
        lines.append(f"# bottle-net-{NAME_DIRECTIVE}: {name}")
    lines.append("")
    for failure in failures:
        lines.append(f"# {failure.reason}")
        lines.append(failure.url)
    return atomic_write_text(path, "\n".join(lines) + "\n")


def default_crawl_output_path(output_dir: Path, platform: Platform, name: str, *, extension: str = ".txt") -> Path:
    """Return a new file path such as ``output/tiktok_example_links.txt``.

    An existing file is never overwritten: ``_links_2``, ``_links_3``, ...
    are tried until an unused name is found.
    """
    stem = f"{platform.value}_{safe_filename(name, max_length=60, fallback='account')}_links"
    return unique_path(output_dir / f"{stem}{extension}")


def batch_folder_name(list_path: Path, platform: Platform | None = None) -> str:
    """Derive a download sub-folder name from a URL list filename.

    ``tiktok_example_links.txt``, ``tiktok_example_links_2.txt`` and
    ``tiktok_example_2026-09-18.txt`` -> ``example``; ``videos.txt`` -> ``videos``.
    """
    stem = _GENERATED_SUFFIX.sub("", list_path.stem)
    stem = _PLATFORM_PREFIX.sub("", stem)
    fallback = platform.value if platform else "downloads"
    return safe_filename(stem, max_length=60, fallback=fallback)


def find_existing_download(directory: Path, video_id: str) -> Path | None:
    """Return a *completed* file in *directory* for *video_id*, if one exists.

    Downloads are named ``<title>_<id>.<ext>``. Anything with more than one
    extension after the ID is an unfinished yt-dlp file and is ignored:
    ``.mp4.part``, ``.mp4.part-Frag3``, ``.mp4.ytdl``, ``.temp.mp4`` and
    ``.f137.mp4`` (a stream waiting to be merged). Empty files are ignored
    too, so an interrupted download is never mistaken for a finished one.
    """
    if not video_id or not directory.is_dir():
        return None
    finished = re.compile(rf"_{re.escape(video_id)}\.[A-Za-z0-9]{{2,5}}$")
    for candidate in directory.glob(f"*_{glob_escape(video_id)}.*"):
        if (
            finished.search(candidate.name)
            and not candidate.name.lower().endswith((".part", ".ytdl", ".temp", ".tmp"))
            and candidate.is_file()
            and candidate.stat().st_size > 0
        ):
            return candidate
    return None


def glob_escape(text: str) -> str:
    """Escape glob metacharacters in *text*."""
    return re.sub(r"([*?\[\]])", r"[\1]", text)


def display_path(path: Path) -> str:
    """Return *path* relative to the working directory when possible."""
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)
