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
import sys
from dataclasses import dataclass
from pathlib import Path

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
from bottle_net.errors import BottleNetError, URLListError
from bottle_net.utils.console import UI
from bottle_net.utils.files import (
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
    parse_facebook_target,
    parse_tiktok_account,
    tiktok_profile_url,
    tiktok_username_from_url,
)

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_INTERRUPTED = 130


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
        target_url = tiktok_profile_url(name)
        crawler: TikTokCrawler | FacebookCrawler = TikTokCrawler(config)
    else:
        page = parse_facebook_target(args.target)
        name, target_url = page.name, page.url
        crawler = FacebookCrawler(config)

    # Capture the real stdout now, before any live display is started.
    data_stream = sys.stdout
    stdout_tty = is_terminal(data_stream)
    output = choose_crawl_output(args.output, quiet=ui.quiet, stdout_is_terminal=stdout_tty)
    writer: URLWriter | None = fmt.stream_writer(data_stream) if output.to_stdout else None

    ui.header(f"{platform.display_name} Video Crawler")
    ui.field("Target", target_url)
    if output.to_stdout:
        ui.field("Output", "standard output")
    elif output.path is not None:
        ui.field("Output", display_path(output.path))
    ui.console.print()
    ui.info(f"Crawling {'@' + name if platform is Platform.TIKTOK else name}...")
    ui.console.print()

    def on_found(url: str) -> None:
        if writer is not None:
            writer.write(url)
        progress.advance()

    def on_retry(attempt: int, error: BottleNetError, delay: float) -> None:
        progress.print(f"[yellow]\\[!][/yellow] {error} - retrying in {delay:.0f}s (attempt {attempt})")

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
        ui.console.print()
        if output.to_stdout and progress.found:
            ui.warning(f"Crawl interrupted by user after {progress.found} URL(s) were written.")
        else:
            ui.warning("Crawl interrupted by user. Nothing was saved.")
        return EXIT_INTERRUPTED

    ui.console.print()
    ui.info(f"Videos found: {len(result.urls)}")
    for note in result.notes:
        ui.note(note)

    if not result.urls:
        ui.console.print()
        ui.warning("No video URLs to save.")
        return EXIT_FAILURE

    status = "Crawl completed" if result.complete else "Crawl completed (partial results)"
    if output.to_stdout:
        ui.success(status)
        ui.note(f"{len(result.urls)} URL(s) written to standard output.")
        return EXIT_OK

    path = output.path or default_crawl_output_path(config.output_directory, platform, name, extension=fmt.extension)
    replaced = output.path is not None and path.exists()
    try:
        atomic_write_text(path, fmt.render(result.urls))
    except OSError as exc:
        raise BottleNetError(
            f"Could not save the URL list to {display_path(path)}: {exc.strerror or exc}",
            hint="Choose another location with -o FILE, or redirect the output: > links.txt",
        ) from exc

    ui.console.print()
    ui.success(status)
    ui.saved("Saved:", display_path(path))
    if replaced:
        ui.note("The existing file was replaced.")
    ui.console.print()
    ui.note(f'Next: bottle-net {platform.value} download-list "{display_path(path)}"')
    return EXIT_OK


# ------------------------------------------------------------------- download


def _print_info(ui: UI, info: VideoInfo) -> None:
    ui.field("Title", _shorten(info.title, 70))
    if info.uploader:
        ui.field("Uploader", info.uploader)
    ui.field("Size", format_bytes(info.filesize) if info.filesize else "unknown")
    ui.console.print()


def _shorten(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 3].rstrip() + "..."


def download(args: argparse.Namespace, config: Config, ui: UI) -> int:
    """``bottle-net [<platform>] download <URL>``."""
    platform: Platform | None = args.platform or detect_platform(args.url)
    if platform is None:
        ui.err.print("[bold red]\\[!][/bold red] Unsupported or unknown URL.")
        ui.err.print()
        ui.err.print("Supported platforms:")
        for supported in Platform:
            ui.err.print(f"  - {supported.display_name}")
        return EXIT_FAILURE

    downloader = get_downloader(platform, config)
    # Validate before printing anything else so bad input fails fast.
    downloader.validate(args.url)
    dest_dir: Path = args.dir or config.platform_download_dir(platform)

    ui.header(f"{platform.display_name} Downloader")
    ui.info(f"Processing {platform.display_name} video")
    ui.console.print()

    progress = DownloadProgress(ui.console)
    started = False

    def on_info(info: VideoInfo) -> None:
        nonlocal started
        _print_info(ui, info)
        ui.console.print("[bold]Downloading:[/bold]")
        progress.progress.start()
        progress.start_file()
        started = True

    def on_retry(attempt: int, error: BottleNetError, delay: float) -> None:
        ui.console.print(f"[yellow]\\[!][/yellow] {error} - retrying in {delay:.0f}s (attempt {attempt})")

    try:
        result = downloader.download(args.url, dest_dir, progress_hook=progress.hook, on_info=on_info, on_retry=on_retry)
        if result.status is DownloadStatus.DOWNLOADED:
            progress.finish_file()
        else:
            progress.hide_file()
    except KeyboardInterrupt:
        progress.progress.stop()
        ui.console.print()
        ui.warning("Download interrupted. Run the same command again to resume.")
        return EXIT_INTERRUPTED
    finally:
        if started:
            progress.progress.stop()

    return _report_single(ui, result)


def _report_single(ui: UI, result: DownloadResult) -> int:
    ui.console.print()
    if result.status is DownloadStatus.FAILED:
        error = result.error
        ui.error("Download failed", reason=result.reason, hint=error.hint if error else None)
        return EXIT_FAILURE
    assert result.path is not None
    if result.status is DownloadStatus.SKIPPED:
        ui.success("Already downloaded - skipped")
    else:
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

    name = args.name or default_batch_name(url_list, platform)
    failed_file: Path = args.failed_file or config.output_directory / f"failed_{platform.value if platform else 'downloads'}.txt"

    downloader: BatchDownloader
    if platform is not None:
        downloader = get_downloader(platform, config)
        dest_dir: Path = args.dir or config.platform_download_dir(platform) / name
        save_to = display_path(dest_dir)
    else:
        auto = AutoDownloader(config, lambda p: get_downloader(p, config), subfolder=None if args.dir else name)
        downloader = auto
        dest_dir = args.dir or config.download_directory
        save_to = display_path(dest_dir) if args.dir else display_path(dest_dir / "<platform>" / name)

    ui.header(f"{platform.display_name} Batch Downloader" if platform else "Batch Downloader")
    ui.field("URL list", url_list.source)
    ui.field("Videos", str(len(url_list.urls)))
    if url_list.duplicates:
        ui.note(f"Ignored {url_list.duplicates} duplicate URL(s).")
    ui.field("Save to", save_to)
    ui.console.print()

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
        progress.print(f"  [yellow]\\[!][/yellow] {error} - retrying in {delay:.0f}s (attempt {attempt})")

    callbacks = BatchCallbacks(
        on_start=on_start, on_info=on_info, on_result=on_result, on_retry=on_retry, progress_hook=progress.hook
    )
    with progress:
        summary = run_batch(downloader, url_list.urls, dest_dir, callbacks=callbacks)

    # Retrying must put videos back in the same folder(s): name the exact
    # folder, or for the universal default layout, the batch name.
    retry_dir: Path | None = dest_dir if platform is not None else args.dir
    return _report_batch(ui, summary, failed_file, platform, destination=retry_dir,
                         name=None if retry_dir else name)


def _report_batch(
    ui: UI,
    summary: BatchSummary,
    failed_file: Path,
    platform: Platform | None,
    *,
    destination: Path | None = None,
    name: str | None = None,
) -> int:
    if summary.interrupted:
        ui.console.print()
        ui.warning("Interrupted by user. Unfinished URLs are recorded below so you can resume.")
    if summary.aborted_reason:
        ui.console.print()
        ui.warning(summary.aborted_reason)

    ui.summary(
        "Download Summary",
        [
            ("Successful", summary.completed, "green"),
            ("Failed", summary.failed, "red" if summary.failed else "dim"),
            ("Skipped", summary.skipped, "dim"),
        ],
    )

    retry = retry_command(failed_file, platform, destination=destination, name=name)
    if summary.failures:
        write_failed_urls(failed_file, summary.failures, platform=platform, destination=destination, name=name)
        ui.saved("Failed URLs:", display_path(failed_file))
        ui.note(f"Retry them with: {retry}")
    elif failed_file.exists():
        # Keep the failed-URL file in sync with the latest run.
        write_failed_urls(failed_file, [], platform=platform, destination=destination, name=name)

    if summary.folders:
        ui.console.print()
        ui.success("Downloads saved to:")
        for folder in summary.folders:
            ui.console.print(f"  {display_path(folder)}")
    ui.console.print()

    if summary.interrupted:
        return EXIT_INTERRUPTED
    if summary.failed:
        ui.warning(f"Finished with {summary.failed} failure(s).")
        if ui.quiet:
            ui.error(
                f"{summary.failed} of {summary.total} download(s) failed. "
                f"Failed URLs were saved to {display_path(failed_file)}",
                hint=f"Retry with: {retry}",
            )
        return EXIT_FAILURE
    ui.success("Finished")
    return EXIT_OK
