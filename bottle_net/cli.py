"""Command-line interface for Bottle Net Tool (``bottle-net``).

Command layout::

    bottle-net tiktok   crawl | download | download-list
    bottle-net facebook crawl | download | download-list
    bottle-net download <URL>          (platform detected automatically)
    bottle-net download-list <FILE|->  (platform detected per URL)

This module only parses arguments and dispatches; the work is done in
:mod:`bottle_net.commands`.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NoReturn

from rich.console import Console

from bottle_net import APP_NAME, __author__, __version__
from bottle_net.browser_cookies import SUPPORTED_BROWSERS, BrowserSpec, parse_browser
from bottle_net.config import load_config
from bottle_net.errors import BottleNetError
from bottle_net.utils.console import UI
from bottle_net.utils.filenames import safe_filename
from bottle_net.utils.logger import setup_logging
from bottle_net.utils.output import DEFAULT_FORMAT, OUTPUT_FORMATS
from bottle_net.utils.urls import Platform

PROG = "bottle-net"

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

MAIN_HELP = f"""\
{APP_NAME}

Download publicly accessible TikTok and Facebook videos.

Usage:
  {PROG} <platform> <command> [OPTIONS]
  {PROG} download <URL> [OPTIONS]
  {PROG} download-list <FILE | -> [OPTIONS]
  {PROG} doctor
  {PROG} gui

Platforms:
  tiktok         TikTok tools
  facebook       Facebook tools

Commands:
  crawl          Find public video URLs on an account/Page
  download       Download one video
  download-list  Download every video in a URL list (a file, or - for stdin)
  doctor         Check that Python, yt-dlp, FFmpeg and folders are ready
  gui            Open the Video Publisher: upload and schedule videos to
                 YouTube and Facebook Pages (official APIs, OAuth sign-in)

Examples:
  {PROG} doctor
  {PROG} tiktok crawl @username
  {PROG} tiktok crawl @username -o links.txt
  {PROG} tiktok download <URL>
  {PROG} tiktok download-list links.txt

  {PROG} facebook crawl https://www.facebook.com/examplepage
  {PROG} facebook download <URL>
  {PROG} facebook download-list links.txt

  {PROG} download <URL>                 Detects TikTok or Facebook automatically
  {PROG} download-list links.txt        Mixed TikTok and Facebook lists

Pipelines:
  {PROG} tiktok crawl @username > links.txt
  {PROG} tiktok crawl @username | sort -u > unique-links.txt
  cat links.txt | {PROG} tiktok download-list -

Options:
  -h, --help       Show this help message
  -v, --version    Show version
  -q, --quiet      Show only errors (crawl: print only the URLs)
  --config FILE    Use a specific config.toml file
  --debug          Show detailed diagnostic output
  --no-color       Disable colored output

Data (URL lists) is written to standard output; status messages and progress
bars go to standard error, so redirection and pipes stay clean.

Run '{PROG} <platform> -h' or '{PROG} <platform> <command> -h' for more help.

Only download content you have the right to download.
"""

PLATFORM_HELP = """\
{app} - {name} tools

Usage:
  {prog} {platform} <command> [OPTIONS]

Commands:
  crawl          Find public video URLs on a {name} {account_word}
  download       Download one {name} video
  download-list  Download every video in a URL list file

Examples:
{examples}

Options:
  -h, --help     Show this help message

Run '{prog} {platform} <command> -h' for details on a command.
"""

_PLATFORM_EXAMPLES = {
    Platform.TIKTOK: [
        "crawl @username",
        "crawl @username -o links.txt",
        "crawl @username > links.txt",
        'download "https://www.tiktok.com/@username/video/1234567890"',
        "download-list links.txt",
        "download-list -              (read URLs from standard input)",
    ],
    Platform.FACEBOOK: [
        "crawl https://www.facebook.com/examplepage",
        "crawl examplepage -o links.txt",
        'download "https://www.facebook.com/watch/?v=1234567890"',
        'download "https://www.facebook.com/reel/1234567890"',
        "download-list links.txt",
        "download-list -              (read URLs from standard input)",
    ],
}

_ACCOUNT_WORD = {Platform.TIKTOK: "account", Platform.FACEBOOK: "Page or profile"}


class HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Formats command help as ``Usage:`` / ``Arguments:`` / ``Options:``."""

    def __init__(self, prog: str, **kwargs: Any) -> None:
        super().__init__(prog, max_help_position=30, **kwargs)

    def add_usage(self, usage: str | None, actions: Any, groups: Any, prefix: str | None = None) -> None:
        super().add_usage(usage, actions, groups, prefix="Usage:\n  " if prefix is None else prefix)


class Parser(argparse.ArgumentParser):
    """ArgumentParser with friendlier errors and optional hand-written help."""

    def __init__(self, *args: Any, static_help: Callable[[], str] | None = None, **kwargs: Any) -> None:
        kwargs.setdefault("formatter_class", HelpFormatter)
        kwargs.setdefault("add_help", False)
        if sys.version_info >= (3, 14):
            kwargs.setdefault("color", False)
        super().__init__(*args, **kwargs)
        self._static_help = static_help
        self._positionals.title = "Arguments"
        self._optionals.title = "Options"

    def format_help(self) -> str:
        if self._static_help is not None:
            return self._static_help()
        return super().format_help()

    def error(self, message: str) -> NoReturn:
        sys.stderr.write(f"[!] Error: {message}\n\n")
        if self._static_help is None:
            sys.stderr.write(self.format_usage() + "\n")
        sys.stderr.write(f"Run '{self.prog} -h' for help.\n")
        raise SystemExit(EXIT_USAGE)


def _add_help(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-h", "--help", action="help", help="Show this help message")


def _add_quiet(parser: argparse.ArgumentParser, help_text: str = "Show only errors") -> None:
    parser.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS, help=help_text)


def _global_options(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    """Options accepted both before and after the command."""
    default: Any = argparse.SUPPRESS if suppress else None
    parser.add_argument("--config", metavar="FILE", type=Path, default=default,
                        help="Use a specific config.toml file")
    parser.add_argument("--debug", action="store_true", default=argparse.SUPPRESS if suppress else False,
                        help="Show detailed diagnostic output")
    parser.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS if suppress else False,
                        help="Disable colored output")
    if not suppress:
        parser.add_argument("-q", "--quiet", action="store_true", default=False, help=argparse.SUPPRESS)


FACEBOOK_AUTH_HELP = f"""\
Facebook authentication
-----------------------
Some Facebook pages/videos require you to be logged in.

Bottle Net Tool does NOT ask for or store your Facebook password.

Use an existing browser session:

  {PROG} facebook crawl "<PAGE_URL>" --browser chrome
  {PROG} facebook download "<VIDEO_URL>" --browser chrome
  {PROG} facebook download-list links.txt --browser chrome

Supported browsers:
{chr(10).join("  " + b for b in SUPPORTED_BROWSERS)}

For another browser profile use BROWSER:PROFILE, e.g. --browser "chrome:Profile 1".

Example:
  {PROG} facebook crawl "https://www.facebook.com/NASA" --browser chrome

The selected browser must already be logged in to Facebook. If its cookies
cannot be read, close the browser completely and try again (Chrome and Edge
lock their cookie file while running); Firefox usually works best.

Security:
  - Passwords are never requested.
  - Cookies are read locally from the selected browser (only facebook.com cookies).
  - Cookies/tokens are never printed, saved or uploaded; they are sent only to facebook.com.
  - Authentication does not bypass private content or access restrictions.
"""

BROWSER_OPTION_HELP = ("Use the Facebook login of this browser: " + ", ".join(SUPPORTED_BROWSERS)
                       + " (BROWSER:PROFILE for another profile). Your password is never asked for")
BROWSER_EPILOG = ("\n\nLogged-in content: add --browser chrome (or firefox, edge, ...) to use your browser's\n"
                  f"Facebook session. See '{PROG} facebook -h' for details and security notes.")


def _browser(value: str) -> BrowserSpec:
    try:
        return parse_browser(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _add_browser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--browser", metavar="BROWSER", type=_browser, help=BROWSER_OPTION_HELP)


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got '{value}'") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or greater")
    return number


def _folder_name(value: str) -> str:
    name = safe_filename(value, max_length=60, fallback="")
    if not name:
        raise argparse.ArgumentTypeError(f"'{value}' is not a usable folder name")
    return name


def _examples(lines: Sequence[str]) -> str:
    return "Examples:\n" + "\n".join(f"  {line}" for line in lines)


def _add_crawl(sub: Any, platform: Platform) -> None:
    name = platform.display_name
    value = platform.value
    if platform is Platform.TIKTOK:
        metavar, arg_help, target = "ACCOUNT", "TikTok username (@name or name) or profile URL", "@username"
        description = (
            "Find the public videos of a TikTok account and list their URLs, one per\n"
            "line. Only public accounts can be crawled."
        )
    else:
        metavar, arg_help, target = "PAGE_OR_PROFILE", "Facebook Page name or Page/profile URL", "examplepage"
        description = (
            "Find publicly accessible videos and reels on a Facebook Page or profile and\n"
            "list their URLs, one per line.\n\n"
            "Facebook shows only the first batch of videos to visitors who are not\n"
            "signed in. Bottle Net does not sign in or bypass login walls, so for Pages\n"
            "with many videos the list may be incomplete."
        )
    formats = ", ".join(OUTPUT_FORMATS)
    epilog = _examples(
        [
            f"{PROG} {value} crawl {target}",
            f"{PROG} {value} crawl {target} -o videos.txt",
            f"{PROG} {value} crawl {target} > videos.txt",
            f"{PROG} {value} crawl {target} --quiet > videos.txt",
            f"{PROG} {value} crawl {target} | sort -u > unique-links.txt",
        ]
    ) + (
        "\n\nWhere the links go:\n"
        "  -o FILE          Saved to FILE (replaced if it exists)\n"
        "  > FILE, | cmd    Written to standard output, one URL per line\n"
        "  --quiet          Written to standard output, with no status messages\n"
        f"  (no option)      In a terminal: saved to output/{value}_<name>_links.txt\n"
        "                   (never overwrites; adds _2, _3, ... if the name is taken)\n\n"
        "Status messages and progress always go to standard error, so redirected\n"
        "output contains only URLs.\n\n"
        f"Then download the videos with:\n  {PROG} {value} download-list videos.txt"
    )
    if platform is Platform.FACEBOOK:
        epilog += BROWSER_EPILOG
    parser = sub.add_parser(
        "crawl",
        help=f"Find public video URLs on a {name} {_ACCOUNT_WORD[platform]}",
        description=description,
        epilog=epilog,
        prog=f"{PROG} {value} crawl",
        usage=f"%(prog)s <{metavar}> [OPTIONS]",
    )
    parser.add_argument("target", metavar=metavar, help=arg_help)
    parser.add_argument("-o", "--output", metavar="FILE",
                        help="Save links to FILE ('-' for standard output)")
    _add_quiet(parser, "Output only video URLs (no status messages)")
    parser.add_argument("--format", metavar="FORMAT", choices=list(OUTPUT_FORMATS), default=DEFAULT_FORMAT,
                        help=f"Output format: {formats} (default: {DEFAULT_FORMAT})")
    parser.add_argument("--limit", metavar="N", type=_positive_int, help="Stop after finding N videos")
    if platform is Platform.FACEBOOK:
        _add_browser(parser)
    _add_help(parser)
    _global_options(parser, suppress=True)
    parser.set_defaults(action="crawl", platform=platform)


def _add_download(sub: Any, platform: Platform | None) -> None:
    if platform is None:
        name, prog = "TikTok or Facebook", f"{PROG} download"
        description = (
            "Download one video. The platform (TikTok or Facebook) is detected\n"
            "automatically from the URL."
        )
        examples = [
            f'{PROG} download "https://www.tiktok.com/@username/video/1234567890"',
            f'{PROG} download "https://www.facebook.com/watch/?v=1234567890"',
        ]
        default_dir = "downloads/<platform>/"
    else:
        name, prog = platform.display_name, f"{PROG} {platform.value} download"
        description = f"Download one publicly accessible {name} video." if platform is Platform.TIKTOK else (
            "Download one Facebook video: public videos, or videos your own account can\n"
            "see when you add --browser.")
        examples = [f'{PROG} {platform.value} download "{u}"' for u in (
            ["https://www.tiktok.com/@username/video/1234567890", "https://vm.tiktok.com/ZMabcdef/"]
            if platform is Platform.TIKTOK
            else ["https://www.facebook.com/watch/?v=1234567890", "https://www.facebook.com/reel/1234567890"]
        )]
        default_dir = f"downloads/{platform.value}/"
    parser = sub.add_parser(
        "download",
        help=f"Download one {name} video",
        description=description + "\n\nAlways put URLs in quotes: they often contain '?' or '&'.",
        epilog=_examples(examples) + (BROWSER_EPILOG if platform is Platform.FACEBOOK else ""),
        prog=prog,
        usage="%(prog)s <URL> [OPTIONS]",
    )
    parser.add_argument("url", metavar="URL", help=f"{name} video URL")
    parser.add_argument("-d", "--output-dir", "--dir", dest="dir", metavar="DIR", type=Path,
                        help=f"Save the video in DIR (default: {default_dir})")
    if platform is Platform.FACEBOOK:
        _add_browser(parser)
    _add_quiet(parser)
    _add_help(parser)
    _global_options(parser, suppress=True)
    parser.set_defaults(action="download", platform=platform)


def _add_download_list(sub: Any, platform: Platform | None) -> None:
    if platform is None:
        prog, what, default_dir, failed = f"{PROG} download-list", "TikTok and Facebook", "downloads/<platform>/<name>/", "downloads"
        extra = "\nThe platform of each URL is detected automatically, so one list may mix\nTikTok and Facebook videos.\n"
    else:
        value = platform.value
        prog, what, default_dir, failed = f"{PROG} {value} download-list", platform.display_name, f"downloads/{value}/<name>/", value
        extra = ""
    parser = sub.add_parser(
        "download-list",
        help="Download every video in a URL list",
        description=(
            f"Download every {what} video listed in FILE, one URL per line.\n"
            "Use '-' as FILE to read the URLs from standard input.\n"
            f"{extra}\n"
            "Blank lines, lines starting with '#' and duplicate URLs are ignored.\n"
            "Videos that are already downloaded are skipped. If a video fails, the\n"
            "download continues with the next one, and failed URLs are saved to a\n"
            "file you can retry later with this same command."
        ),
        epilog=_examples(
            [
                f"{prog} links.txt",
                f"{prog} links.txt -d my-videos/",
                f"cat links.txt | {prog} -",
                f"{prog} output/failed_{failed}.txt    (retry failures)",
            ]
        ) + (
            "\n\n<name> is taken from the list's filename (output/tiktok_example_links.txt\n"
            "-> example); for standard input, from the TikTok account, or 'stdin'."
        ) + (BROWSER_EPILOG if platform is Platform.FACEBOOK else ""),
        prog=prog,
        usage="%(prog)s <FILE | -> [OPTIONS]",
    )
    parser.add_argument("file", metavar="FILE", help="Text file with one video URL per line, or '-' for standard input")
    parser.add_argument("-d", "--output-dir", "--dir", dest="dir", metavar="DIR", type=Path,
                        help=f"Save videos in DIR (default: {default_dir})")
    parser.add_argument("--name", metavar="NAME", type=_folder_name,
                        help="Sub-folder name for this batch (default: from the list's filename)")
    parser.add_argument("--failed-file", metavar="FILE", type=Path,
                        help=f"Where to record failed URLs (default: output/failed_{failed}.txt)")
    if platform is Platform.FACEBOOK:
        _add_browser(parser)
    _add_quiet(parser)
    _add_help(parser)
    _global_options(parser, suppress=True)
    parser.set_defaults(action="download-list", platform=platform)


def _platform_help(platform: Platform) -> str:
    examples = "\n".join(f"  {PROG} {platform.value} {e}" for e in _PLATFORM_EXAMPLES[platform])
    text = PLATFORM_HELP.format(
        app=APP_NAME,
        name=platform.display_name,
        prog=PROG,
        platform=platform.value,
        account_word=_ACCOUNT_WORD[platform],
        examples=examples,
    )
    return text + "\n" + FACEBOOK_AUTH_HELP if platform is Platform.FACEBOOK else text


def build_parser() -> Parser:
    """Build the complete argument parser."""
    parser = Parser(prog=PROG, static_help=lambda: MAIN_HELP)
    _add_help(parser)
    parser.add_argument("-v", "--version", action="version", version=version_text())
    _global_options(parser, suppress=False)

    platforms = parser.add_subparsers(dest="command", metavar="<platform>", parser_class=Parser)
    for platform in Platform:
        platform_parser = platforms.add_parser(
            platform.value,
            help=f"{platform.display_name} tools",
            prog=f"{PROG} {platform.value}",
            static_help=lambda p=platform: _platform_help(p),
        )
        _add_help(platform_parser)
        platform_parser.set_defaults(platform=platform, action=None)
        commands = platform_parser.add_subparsers(dest="subcommand", metavar="<command>", parser_class=Parser)
        _add_crawl(commands, platform)
        _add_download(commands, platform)
        _add_download_list(commands, platform)

    _add_download(platforms, None)
    _add_download_list(platforms, None)
    _add_doctor(platforms)
    _add_publisher(platforms)
    return parser


def _add_publisher(sub: Any) -> None:
    """``bottle-net gui`` and ``bottle-net publisher ...`` (Video Publisher)."""
    data_help = "Publisher data folder (default: your user data folder)"
    gui = sub.add_parser(
        "gui",
        help="Open the Video Publisher (upload and schedule to YouTube and Facebook)",
        description=(
            "Open Bottle Net - Video Publisher in your web browser. It uploads and\n"
            "schedules videos to YouTube and Facebook Pages using their official APIs.\n\n"
            "Accounts are connected with Google's and Meta's own sign-in pages (OAuth);\n"
            "Bottle Net never asks for your password. The GUI runs only on this computer\n"
            "(127.0.0.1). Keep the command running for scheduled uploads."
        ),
        epilog=_examples([f"{PROG} gui", f"{PROG} gui --port 8766 --no-browser"]),
        prog=f"{PROG} gui",
        usage="%(prog)s [OPTIONS]",
    )
    gui.add_argument("--port", type=_positive_int, default=8765, metavar="PORT", help="Local port (default: 8765)")
    gui.add_argument("--no-browser", action="store_true", help="Do not open the browser automatically")
    gui.add_argument("--data-dir", metavar="DIR", help=data_help)
    _add_help(gui)
    _global_options(gui, suppress=True)
    gui.set_defaults(action="gui", platform=None)

    publisher = sub.add_parser(
        "publisher", help="Video Publisher tools for the terminal (optional)", prog=f"{PROG} publisher",
        description="Optional terminal commands for the Video Publisher. Most people use 'bottle-net gui'.",
        usage="%(prog)s <command> [OPTIONS]",
    )
    _add_help(publisher)
    publisher.set_defaults(action=None, platform=None)
    commands = publisher.add_subparsers(dest="publisher_command", metavar="<command>", parser_class=Parser)
    for name, action, text in (("jobs", "publisher-jobs", "List uploads and their status"),
                               ("run", "publisher-run", "Run the upload scheduler without the GUI")):
        command = commands.add_parser(name, help=text, description=text + ".", prog=f"{PROG} publisher {name}",
                                      usage="%(prog)s [OPTIONS]")
        command.add_argument("--data-dir", metavar="DIR", help=data_help)
        _add_help(command)
        _global_options(command, suppress=True)
        command.set_defaults(action=action, platform=None)


def _add_doctor(sub: Any) -> None:
    parser = sub.add_parser(
        "doctor",
        help="Check that everything Bottle Net Tool needs is ready",
        description=(
            "Check the Python version, yt-dlp, FFmpeg, the configuration file, and\n"
            "whether the download and output folders are writable.\n\n"
            "All checks are local: no videos are downloaded and TikTok/Facebook\n"
            "are not contacted. FFmpeg is optional."
        ),
        epilog=_examples([f"{PROG} doctor", f"{PROG} doctor --config my-config.toml"]),
        prog=f"{PROG} doctor",
        usage="%(prog)s [OPTIONS]",
    )
    _add_help(parser)
    _global_options(parser, suppress=True)
    parser.set_defaults(action="doctor", platform=None)


def version_text() -> str:
    """Return the ``--version`` output (the version lives in ``bottle_net/__init__.py``)."""
    return f"{APP_NAME} {__version__}\nAuthor: {__author__}"


def _configure_streams() -> None:
    """Prepare stdout/stderr for Unicode text and Unix-style line endings.

    Unicode output (box drawing, titles) must never crash on Windows, and
    data written to stdout uses ``\\n`` line endings on every platform so
    redirected URL lists match the files the tool writes itself.
    """
    for stream in (sys.stdout, sys.stderr):
        if not hasattr(stream, "reconfigure"):
            continue
        options: dict[str, str] = {"newline": "\n"} if stream is sys.stdout else {}
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if encoding != "utf8":
            options.update(encoding="utf-8", errors="replace")
        if options:
            try:
                stream.reconfigure(**options)
            except (ValueError, OSError):  # pragma: no cover - exotic streams
                pass


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``bottle-net`` command. Returns an exit code.

    Ctrl+C never produces a Python traceback: commands report what was
    finished, and anything else exits with code 130.
    """
    try:
        return _run(argv)
    except KeyboardInterrupt:
        sys.stderr.write("\n[!] Interrupted by user.\n")
        return EXIT_INTERRUPTED


def _run(argv: Sequence[str] | None) -> int:
    _configure_streams()
    args_list = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()

    if not args_list:
        UI(Console(highlight=False)).banner()  # help text belongs on stdout
        print(MAIN_HELP)
        return EXIT_OK

    args = parser.parse_args(args_list)
    no_color = bool(getattr(args, "no_color", False))
    # Human-readable output goes to stderr; stdout is reserved for data.
    ui = UI(quiet=bool(getattr(args, "quiet", False)), no_color=no_color)
    setup_logging(debug=bool(getattr(args, "debug", False)), console=Console(stderr=True, no_color=no_color))
    log = logging.getLogger("bottle_net.cli")

    action = getattr(args, "action", None)
    if action is None:
        # A platform was given without a command: show that platform's help.
        platform = getattr(args, "platform", None)
        print(_platform_help(platform) if platform else MAIN_HELP)
        return EXIT_OK

    if action == "doctor":
        # The doctor report is the command's output, so it goes to stdout.
        # It loads the configuration itself so that it can report problems.
        from bottle_net.doctor import run_doctor

        return run_doctor(Console(highlight=False, no_color=no_color), config_path=getattr(args, "config", None))

    if action in ("gui", "publisher-jobs", "publisher-run"):
        from bottle_net.publisher import commands as publisher_commands

        handler = {"gui": publisher_commands.run_gui, "publisher-jobs": publisher_commands.list_jobs,
                   "publisher-run": publisher_commands.run_scheduler}[action]
        return handler(args, ui)

    # Imported lazily so that `-h` stays fast.
    from bottle_net import commands

    try:
        config = load_config(getattr(args, "config", None))
        log.debug("Configuration: %s", config)
        handlers = {
            "crawl": commands.crawl,
            "download": commands.download,
            "download-list": commands.download_list,
        }
        return handlers[action](args, config, ui)
    except BottleNetError as exc:
        ui.failure(exc)
        if exc.detail:
            log.debug("Details: %s", exc.detail)
        return EXIT_FAILURE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
