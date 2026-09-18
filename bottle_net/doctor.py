"""``bottle-net doctor``: check that everything Bottle Net Tool needs is in place.

All checks are local: nothing is downloaded and TikTok/Facebook are not
contacted. Folders are tested for write access without being created.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from pathlib import Path

from rich.console import Console
from rich.markup import escape

from bottle_net import APP_NAME
from bottle_net.config import Config, find_config_file, load_config
from bottle_net.errors import ConfigError
from bottle_net.utils.files import display_path

MIN_PYTHON = (3, 11)
#: yt-dlp versions are dates; older releases often fail on changed sites.
YTDLP_MAX_AGE_DAYS = 90
LABEL_WIDTH = 13
RULE = "─" * 32

#: yt-dlp extractors each platform relies on.
PLATFORM_EXTRACTORS = {
    "TikTok": ("TikTok", "TikTokUser"),
    "Facebook": ("Facebook", "FacebookReel"),
}


class Status(StrEnum):
    """Result of one check."""

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


_SYMBOLS = {Status.OK: "[green]✓[/green]", Status.WARN: "[yellow]![/yellow]", Status.FAIL: "[red]✗[/red]"}


@dataclass
class Check:
    """One line of the doctor report."""

    name: str
    status: Status
    detail: str = ""
    #: Explanation printed below the line (for warnings and failures).
    advice: list[str] = field(default_factory=list)


def check_python(version: tuple[int, int, int] | None = None) -> Check:
    """Python must be 3.11 or newer."""
    major, minor, micro = version or sys.version_info[:3]
    text = f"{major}.{minor}.{micro}"
    if (major, minor) >= MIN_PYTHON:
        return Check("Python", Status.OK, text)
    return Check("Python", Status.FAIL, f"{text} (3.11 or newer is required)",
                 ["Install Python 3.11+ from https://www.python.org/downloads/"])


def check_ytdlp(today: date | None = None) -> Check:
    """yt-dlp must be importable; warn when it is old."""
    try:
        from yt_dlp.version import __version__ as version
    except ImportError:
        return Check("yt-dlp", Status.FAIL, "Not installed", ["Install it with: pip install yt-dlp"])
    try:
        released = date(*(int(part) for part in version.split(".")[:3]))
    except (TypeError, ValueError):
        return Check("yt-dlp", Status.OK, f"installed ({version})")
    age = ((today or date.today()) - released).days
    if age > YTDLP_MAX_AGE_DAYS:
        return Check("yt-dlp", Status.WARN, f"installed ({version}, {age} days old)", [
            "TikTok and Facebook change often; an old yt-dlp may fail to extract videos.",
            "Update it with: pip install --upgrade yt-dlp",
        ])
    return Check("yt-dlp", Status.OK, f"installed ({version})")


def check_ffmpeg(which: Callable[[str], str | None] = shutil.which) -> Check:
    """FFmpeg is optional: it enables merging separate video/audio streams."""
    if which("ffmpeg"):
        return Check("FFmpeg", Status.OK, "installed")
    return Check("FFmpeg", Status.WARN, "Not installed", [
        "Single-file downloads will still work when",
        "a combined audio/video stream is available.",
        "",
        "Install FFmpeg for best-quality stream merging.",
    ])


def check_config(explicit: Path | None = None) -> tuple[Check, Config]:
    """The configuration file (if any) must be valid."""
    try:
        path = find_config_file(explicit)
        config = load_config(explicit)
    except ConfigError as exc:
        return Check("Config", Status.FAIL, "invalid", [str(exc)]), Config()
    if path is None:
        return Check("Config", Status.OK, "valid (no config file; using defaults)"), config
    return Check("Config", Status.OK, f"valid ({display_path(path)})"), config


def check_writable(name: str, folder: Path) -> Check:
    """*folder* (or the nearest existing parent) must accept new files.

    A temporary file is created and removed immediately; the folder itself
    is not created.
    """
    existing = folder
    while not existing.exists() and existing.parent != existing:
        existing = existing.parent
    where = display_path(folder)
    if not existing.is_dir():
        return Check(name, Status.FAIL, f"not a folder: {display_path(existing)}",
                     ["Choose another location in config.toml."])
    try:
        with tempfile.TemporaryFile(dir=existing):
            pass
    except OSError as exc:
        return Check(name, Status.FAIL, f"not writable ({where})",
                     [f"{exc.strerror or exc}. Choose another location in config.toml or fix the permissions."])
    suffix = "" if folder.exists() else ", will be created"
    return Check(name, Status.OK, f"writable ({where}{suffix})")


def check_platforms() -> list[Check]:
    """yt-dlp must provide the extractors each platform relies on."""
    try:
        from yt_dlp.extractor import gen_extractor_classes

        available = {cls.ie_key() for cls in gen_extractor_classes()}
    except Exception:  # noqa: BLE001 - any failure means the platforms cannot work
        available = set()
    checks = []
    for platform, needed in PLATFORM_EXTRACTORS.items():
        missing = [key for key in needed if key not in available]
        if missing:
            checks.append(Check(platform, Status.FAIL, f"yt-dlp extractor missing: {', '.join(missing)}",
                                ["Update yt-dlp: pip install --upgrade yt-dlp"]))
        else:
            checks.append(Check(platform, Status.OK))
    return checks


def run_doctor(console: Console, *, config_path: Path | None = None) -> int:
    """Print the doctor report and return an exit code (1 if a check failed)."""
    config_check, config = check_config(config_path)
    system = [
        check_python(),
        check_ytdlp(),
        check_ffmpeg(),
        config_check,
        check_writable("Downloads", config.download_directory),
        check_writable("Output", config.output_directory),
    ]
    platforms = check_platforms()

    console.print(f"[bold]{APP_NAME} Doctor[/bold]")
    console.print(RULE, style="dim")
    console.print()
    _print_checks(console, system)
    console.print()
    console.print("Platform support:")
    _print_checks(console, platforms)
    console.print()
    console.print(RULE, style="dim")

    checks = system + platforms
    if any(check.status is Status.FAIL for check in checks):
        console.print("[red]Problems found.[/red] Fix the items marked ✗ above.")
        return 1
    if any(check.status is Status.WARN for check in checks):
        console.print("System ready. [dim](See the notes marked ! above.)[/dim]")
    else:
        console.print("System ready.")
    return 0


def _print_checks(console: Console, checks: list[Check]) -> None:
    for check in checks:
        detail = f" {escape(check.detail)}" if check.detail else ""
        console.print(f"{check.name:<{LABEL_WIDTH}}{_SYMBOLS[check.status]}{detail}")
        if check.advice:
            console.print()
            for line in check.advice:
                console.print(escape(line))
            console.print()
