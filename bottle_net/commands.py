"""Implementations of the CLI commands.

Each function takes the parsed arguments, the loaded :class:`Config` and a
:class:`UI`, performs the work via the crawler/downloader modules, renders
progress and results, and returns a process exit code.

Output streams follow the Unix convention: machine-readable data (the URL
list produced by ``crawl``) goes to **stdout**; banners, status lines,
progress bars and errors go to **stderr** through :class:`UI`.
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from bottle_net.config import Config
from bottle_net.crawlers import CrawlResult, FacebookCrawler, TikTokCrawler
from bottle_net.downloaders import (
    AutoDownloader,
    BatchCallbacks,
    BatchDownloader,
    BatchSummary,
    DownloadResult,
    DownloadStatus,
    VideoInfo,
    get_downloader,
    run_batch,
)
from bottle_net.errors import BottleNetError, InvalidURLError, UnsupportedURLError, URLListError
from bottle_net.utils.console import UI
from bottle_net.utils.files import (
    NAME_DIRECTIVE,
    OUTPUT_DIR_DIRECTIVE,
    URLList,
    atomic_write_text,
    batch_folder_name,
    default_crawl_output_path,
    display_path,
    read_url_list,
    read_url_stream,
    retry_command,
    write_failed_urls,
)
from bottle_net.utils.output import (
    STDIO_MARKER,
    OutputClosedError,
    URLWriter,
    get_format,
    is_terminal,
    silence_stdout,
)
from bottle_net.utils.progress import CrawlProgress, DownloadProgress, format_bytes
from bottle_net.utils.urls import (
    Platform,
    detect_platform,
    ensure_scheme,
    parse_facebook_target,
    parse_tiktok_account,
    tiktok_username_from_url,
)

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_INTERRUPTED = 130

SUPPORTED_PLATFORMS_HINT = "Supported platforms:\n  - TikTok\n  - Facebook"
PARTIAL_FILES_NOTE = (
    "Partial files have been handled safely: unfinished downloads are kept as\n"
    '".part" files, are never counted as completed, and resume on the next run.'
)
FACEBOOK_NOTE = [
    "Facebook may expose only the first batch of public videos",
    "without authentication. Results may therefore be incomplete.",
    "",
    "Bottle Net Tool does not use login credentials or private APIs.",
]


def _retry_line(attempt: int, error: BottleNetError, delay: float) -> str:
    return f"[yellow]\\[!][/yellow] {error.label}. Retrying in {delay:.0f}s (attempt {attempt})."


# ---------------------------------------------------------------------- crawl


@dataclass(frozen=True)
class CrawlOutput:
    """Where a crawl's URL list goes."""

    #: Write URLs to standard output as they are found.
    to_stdout: bool
    #: File named with ``-o FILE``. ``None`` (and not ``to_stdout``) means a
    #: new, uniquely named file in the output folder.
    path: Path | None = None


def choose_crawl_output(output: str | None, *, quiet: bool, stdout_is_terminal: bool) -> CrawlOutput:
    """Decide where crawl results are written.

    * ``-o FILE``: that file.  ``-o -``: standard output.
    * ``--quiet``, or stdout redirected/piped (``> file``, ``| sort``):
      standard output, so the data flows where the user sent it.
    * Otherwise (interactive terminal): a new file in the output folder.
    """
    if output == STDIO_MARKER:
        return CrawlOutput(to_stdout=True)
    if output:
        return CrawlOutput(to_stdout=False, path=Path(output))
    return CrawlOutput(to_stdout=quiet or not stdout_is_terminal)


def crawl(args: argparse.Namespace, config: Config, ui: UI) -> int:
    """``bottle-net <platform> crawl <TARGET>``."""
    platform: Platform = args.platform
    fmt = get_format(args.format)

    # Validate input before any network access; errors surface immediately.
    if platform is Platform.TIKTOK:
        name = parse_tiktok_account(args.target)
        target = f"@{name}"
        crawler: TikTokCrawler | FacebookCrawler = TikTokCrawler(config)
    else:
        page = parse_facebook_target(args.target)
        name, target = page.name, page.url
        crawler = FacebookCrawler(config)

    # Capture the real stdout now, before any live display is started.
    data_stream = sys.stdout
    stdout_tty = is_terminal(data_stream)
    output = choose_crawl_output(args.output, quiet=ui.quiet, stdout_is_terminal=stdout_tty)
    writer: URLWriter | None = fmt.stream_writer(data_stream) if output.to_stdout else None

    ui.header(f"{platform.display_name} Crawler")
    ui.field("Target", target)
    ui.blank()
    ui.info("Starting crawler")
    ui.info("Discovering public videos")
    ui.blank()

    def on_found(url: str) -> None:
        if writer is not None:
            writer.write(url)
        progress.advance()

    def on_retry(attempt: int, error: BottleNetError, delay: float) -> None:
        progress.print(_retry_line(attempt, error, delay))

    # When URLs are printed to the same terminal, a live bar would garble them.
    progress = CrawlProgress(ui.console, live=not (output.to_stdout and stdout_tty))
    interrupted = False
    with progress:
        try:
            result: CrawlResult = crawler.crawl(args.target, limit=args.limit, on_found=on_found, on_retry=on_retry)
        except KeyboardInterrupt:
            interrupted = True
            progress.hide()
        except OutputClosedError:
            # The reader of our stdout exited (e.g. `| head -5`): that is not an error.
            progress.hide()
            silence_stdout()
            logger.debug("Standard output was closed by the reader; stopping the crawl.")
            return EXIT_OK
        except BottleNetError:
            progress.hide()
            raise
        else:
            progress.finish()

    if writer is not None:
        writer.close()

    if interrupted:
        ui.blank()
        ui.warning("Crawl interrupted by user.")
        ui.blank()
        ui.field("Videos found", str(progress.found), width=13)
        if output.to_stdout and progress.found:
            ui.note(f"{progress.found} URL(s) had already been written to standard output.")
        else:
            ui.note("Nothing was saved.")
        return EXIT_INTERRUPTED

    ui.blank()
    ui.field("Videos found", str(len(result.urls)), width=13)
    ui.field("Duplicates", str(result.duplicates), width=13)
    for note in result.notes:
        ui.note(note)
    if platform is Platform.FACEBOOK:
        ui.blank()
        ui.notice("Note:", FACEBOOK_NOTE)

    if not result.urls:
        ui.blank()
        ui.warning("No video URLs to save.")
        return EXIT_FAILURE

    status = "Crawl completed" if result.complete else "Crawl completed (partial results)"
    if output.to_stdout:
        ui.blank()
        ui.success(status)
        ui.saved("Output:", f"standard output ({len(result.urls)} URLs)")
        return EXIT_OK

    path = output.path or default_crawl_output_path(config.output_directory, platform, name, extension=fmt.extension)
    replaced = output.path is not None and path.exists()
    try:
        atomic_write_text(path, fmt.render(result.urls))
    except OSError as exc:
        raise BottleNetError(
            f"Could not write {display_path(path)}: {exc.strerror or exc}",
            hint="Choose another location with -o FILE, or redirect the output: > links.txt",
            label="Could not save the URL list",
        ) from exc

    ui.blank()
    ui.success(status)
    ui.saved("Output:", display_path(path))
    if replaced:
        ui.note("The existing file was replaced.")
    ui.blank()
    ui.note(f'Next: bottle-net {platform.value} download-list "{display_path(path)}"')
    return EXIT_OK


# ------------------------------------------------------------------- download


def _shorten(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 3].rstrip() + "..."


def unknown_url_error(url: str) -> BottleNetError:
    """Explain why *url* cannot be handled by the universal commands."""
    text = url.strip()
    parts = urlsplit(ensure_scheme(text))
    if parts.scheme not in ("http", "https") or not parts.hostname or "." not in parts.hostname:
        return InvalidURLError(f"'{text}' is not a web address.", hint=SUPPORTED_PLATFORMS_HINT)
    return UnsupportedURLError(f"{parts.hostname} is not a supported website.", hint=SUPPORTED_PLATFORMS_HINT)


def download(args: argparse.Namespace, config: Config, ui: UI) -> int:
    """``bottle-net [<platform>] download <URL>``."""
    platform: Platform | None = args.platform or detect_platform(args.url)
    if platform is None:
        raise unknown_url_error(args.url)

    downloader = get_downloader(platform, config)
    # Validate before printing anything else so bad input fails fast.
    downloader.validate(args.url)
    dest_dir: Path = args.dir or config.platform_download_dir(platform)

    ui.header(f"{platform.display_name} Downloader")

    progress = DownloadProgress(ui.console)

    def on_info(info: VideoInfo) -> None:
        ui.field("Title", _shorten(info.title, 70), width=6)
        ui.field("File", info.filename or "?", width=6)
        ui.field("Size", format_bytes(info.filesize) if info.filesize else "unknown", width=6)
        ui.blank()
        if info.already_downloaded:
            return
        ui.console.print("[bold]Downloading[/bold]")
        progress.progress.start()
        progress.start_file()

    def on_retry(attempt: int, error: BottleNetError, delay: float) -> None:
        progress.print(_retry_line(attempt, error, delay))

    try:
        result = downloader.download(args.url, dest_dir, progress_hook=progress.hook, on_info=on_info, on_retry=on_retry)
        if result.status is DownloadStatus.DOWNLOADED:
            progress.finish_file()
        else:
            progress.hide_file()
    except KeyboardInterrupt:
        progress.stop()
        ui.err.print()
        ui.error("Download interrupted by user.")
        ui.err.print()
        ui.err.print(PARTIAL_FILES_NOTE)
        return EXIT_INTERRUPTED
    finally:
        progress.stop()

    if result.status is DownloadStatus.FAILED:
        assert result.error is not None
        ui.failure(result.error)
        return EXIT_FAILURE
    assert result.path is not None
    if result.status is DownloadStatus.SKIPPED:
        ui.success("Already downloaded - skipped")
    else:
        if progress.average_speed:
            ui.blank()
            ui.field("Speed", f"{format_bytes(progress.average_speed)}/s (average)")
        ui.blank()
        ui.success("Download completed")
    ui.saved("Saved:", display_path(result.path))
    return EXIT_OK


# -------------------------------------------------------------- download-list


def load_url_list(source: str, ui: UI) -> URLList:
    """Read a URL list from a file, or from standard input when *source* is ``-``."""
    if source != STDIO_MARKER:
        return read_url_list(Path(source))
    if is_terminal(sys.stdin):
        ui.note("Reading URLs from standard input. Finish with Ctrl+D (Windows: Ctrl+Z, then Enter).")
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is None:  # e.g. a StringIO substituted for stdin
        buffer = io.BytesIO(sys.stdin.read().encode("utf-8"))
    return read_url_stream(buffer)


def default_batch_name(url_list: URLList, platform: Platform | None) -> str:
    """Name of the sub-folder a batch is saved in (see ``--name``).

    Taken from the list's filename; for standard input, from the TikTok
    account when all URLs belong to one, otherwise ``stdin``.
    """
    if url_list.path is not None:
        return batch_folder_name(url_list.path, platform)
    users = {tiktok_username_from_url(url) for url in url_list.urls}
    if len(users) == 1 and (user := users.pop()):
        return user
    return "stdin"


def download_list(args: argparse.Namespace, config: Config, ui: UI) -> int:
    """``bottle-net [<platform>] download-list <FILE | ->``."""
    platform: Platform | None = args.platform  # None: detect the platform of each URL
    url_list = load_url_list(args.file, ui)
    if not url_list.urls:
        command = f"bottle-net {platform.value if platform else '<platform>'} crawl <ACCOUNT>"
        source = "Standard input" if url_list.path is None else url_list.source
        raise URLListError(
            f"{source} contains no URLs.",
            hint=f"Add one video URL per line, or create a list with: {command}",
        )

    # A failed-URL file remembers where its videos belong, so retrying it never
    # creates a new folder (an explicit --output-dir or --name still wins).
    recorded_dir = url_list.directives.get(OUTPUT_DIR_DIRECTIVE)
    recorded_name = url_list.directives.get(NAME_DIRECTIVE)
    output_dir: Path | None = args.dir
    if output_dir is None and args.name is None and recorded_dir:
        output_dir = Path(recorded_dir)
    name = args.name or (recorded_name if output_dir is None else None) or default_batch_name(url_list, platform)
    failed_file: Path = args.failed_file or config.output_directory / f"failed_{platform.value if platform else 'downloads'}.txt"

    downloader: BatchDownloader
    if platform is not None:
        downloader = get_downloader(platform, config)
        dest_dir: Path = output_dir or config.platform_download_dir(platform) / name
        save_to = display_path(dest_dir)
    else:
        downloader = AutoDownloader(config, lambda p: get_downloader(p, config), subfolder=None if output_dir else name)
        dest_dir = output_dir or config.download_directory
        save_to = display_path(dest_dir) if output_dir else display_path(dest_dir / "<platform>" / name)

    ui.header(f"{platform.display_name} Batch Downloader" if platform else "Batch Downloader")
    ui.field("URL list", url_list.source)
    ui.field("Total", str(len(url_list.urls)))
    if url_list.duplicates:
        ui.note(f"Ignored {url_list.duplicates} duplicate URL(s).")
    ui.field("Save to", save_to)
    if args.dir is None and args.name is None and (recorded_dir or recorded_name):
        ui.note("Using the download folder recorded in this failed-URL list.")
    ui.blank()

    total = len(url_list.urls)
    width = len(str(total))
    progress = DownloadProgress(ui.console, batch_total=total)

    def on_start(index: int, count: int, url: str) -> None:
        progress.print(f"[bold cyan]\\[{index:0{width}d}/{count}][/bold cyan] {url}")
        progress.start_file(f"[{index:0{width}d}/{count}]")

    def on_info(info: VideoInfo) -> None:
        size = f" ({format_bytes(info.filesize)})" if info.filesize else ""
        progress.print(f"  [dim]{_shorten(info.title, 70)}{size}[/dim]")

    def on_result(index: int, count: int, result: DownloadResult) -> None:
        if result.status is DownloadStatus.DOWNLOADED:
            progress.finish_file()
            assert result.path is not None
            progress.print(f"  [green]\\[✓][/green] Saved {result.path.name}")
        elif result.status is DownloadStatus.SKIPPED:
            progress.hide_file()
            progress.print("  [dim]\\[-] Already downloaded - skipped[/dim]")
        else:
            progress.hide_file()
            progress.print(f"  [red]\\[!] Failed:[/red] {result.reason}")
            progress.print("  [yellow]\\[!][/yellow] Skipping and continuing...")
        progress.advance_overall(index)

    def on_retry(attempt: int, error: BottleNetError, delay: float) -> None:
        progress.print("  " + _retry_line(attempt, error, delay))

    callbacks = BatchCallbacks(
        on_start=on_start, on_info=on_info, on_result=on_result, on_retry=on_retry, progress_hook=progress.hook
    )
    with progress:
        summary = run_batch(downloader, url_list.urls, dest_dir, callbacks=callbacks)

    # Retrying must put videos back in the same folder(s): name the exact
    # folder, or for the universal default layout, the batch name.
    retry_dir: Path | None = dest_dir if platform is not None else output_dir
    retry = RetryInfo(failed_file, platform, retry_dir, None if retry_dir else name)
    return _report_batch(ui, summary, retry)


@dataclass(frozen=True)
class RetryInfo:
    """Everything needed to write the failed-URL file and its retry command."""

    failed_file: Path
    platform: Platform | None
    destination: Path | None
    name: str | None

    def command(self, *, multiline: bool = False) -> str:
        return retry_command(
            self.failed_file, self.platform, destination=self.destination, name=self.name, multiline=multiline
        )


def _report_batch(ui: UI, summary: BatchSummary, retry: RetryInfo) -> int:
    # After Ctrl+C, URLs that were never attempted are "remaining", not failed.
    failed = summary.failed - len(summary.not_attempted) if summary.interrupted else summary.failed
    ui.blank()
    ui.counts([
        ("Successful", summary.completed, "green"),
        ("Failed", failed, "red" if failed else "dim"),
        ("Skipped", summary.skipped, "dim"),
    ])

    if summary.failures:
        write_failed_urls(
            retry.failed_file, summary.failures, platform=retry.platform,
            destination=retry.destination, name=retry.name,
        )
    elif retry.failed_file.exists():
        # Keep the failed-URL file in sync with the latest run.
        write_failed_urls(retry.failed_file, [], platform=retry.platform,
                          destination=retry.destination, name=retry.name)

    if summary.interrupted:
        return _report_interrupted(ui, summary, retry)

    if summary.aborted_reason:
        ui.blank()
        ui.warning(summary.aborted_reason)

    if summary.failures:
        ui.saved("Failed URLs:", display_path(retry.failed_file))
        ui.blank()
        ui.warning(f"{summary.failed} download(s) failed.")
        _print_retry_command(ui, "Retry failed downloads with:", retry)

    if summary.folders:
        ui.saved("Downloads saved to:", "\n  ".join(display_path(folder) for folder in summary.folders))
    ui.blank()

    if summary.failed:
        ui.warning(f"Batch completed with {summary.failed} failure(s).")
        if ui.quiet:
            ui.error(
                f"{summary.failed} of {summary.total} download(s) failed. "
                f"Failed URLs were saved to {display_path(retry.failed_file)}",
                hint=f"Retry with: {retry.command()}",
            )
        return EXIT_FAILURE
    ui.success("Batch completed")
    return EXIT_OK


def _report_interrupted(ui: UI, summary: BatchSummary, retry: RetryInfo) -> int:
    """Summary after Ctrl+C. Always shown (also with --quiet): the user asked to stop."""
    ui.err.print()
    ui.error("Download interrupted by user.")
    ui.err.print()
    width = len("Completed:") + 1
    ui.err.print(f"{'Completed:':<{width}} {summary.completed + summary.skipped:>4}")
    failed_now = summary.failed - len(summary.not_attempted)
    if failed_now:
        ui.err.print(f"{'Failed:':<{width}} {failed_now:>4}")
    ui.err.print(f"{'Remaining:':<{width}} {len(summary.not_attempted):>4}")
    ui.err.print()
    ui.err.print(PARTIAL_FILES_NOTE)
    if summary.failures:
        ui.err.print()
        ui.err.print(f"Unfinished URLs were saved to {display_path(retry.failed_file)}")
        ui.err.print()
        ui.err.print("Resume with:")
        ui.err.print()
        ui.err.print("  " + retry.command(multiline=os.name != "nt").replace("\n", "\n  "))
    return EXIT_INTERRUPTED


def _print_retry_command(ui: UI, title: str, retry: RetryInfo) -> None:
    # A "\" line continuation only works in POSIX shells; on Windows
    # (PowerShell / Command Prompt) the command is printed on one line.
    command = retry.command(multiline=os.name != "nt")
    ui.blank()
    ui.console.print(title)
    ui.blank()
    for line in command.splitlines():
        ui.console.print(f"  {line}", markup=False)
