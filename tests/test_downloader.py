"""Error classification, retry logic, single and batch downloads (mocked)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeVideo, FakeYDLFactory, http_error, network_error
from yt_dlp.utils import DownloadError

from bottle_net.config import Config
from bottle_net.downloaders import (
    AutoDownloader,
    BatchCallbacks,
    DownloadStatus,
    FacebookDownloader,
    TikTokDownloader,
    get_downloader,
    retry_call,
    run_batch,
)
from bottle_net.downloaders.common import MAX_PATH_LENGTH, VideoInfo
from bottle_net.errors import (
    BlockedError,
    ExtractionError,
    LoginRequiredError,
    NetworkError,
    PrivateContentError,
    RateLimitedError,
    StorageError,
    VideoUnavailableError,
)
from bottle_net.ytdlp import base_options, classify_error, format_selector

VIDEO_URL = "https://www.tiktok.com/@user/video/{}"


def make_downloader(config: Config, factory: FakeYDLFactory, sleeps: list[float]) -> TikTokDownloader:
    return TikTokDownloader(config, ydl_factory=factory, sleep=sleeps.append, ffmpeg=False)


# ------------------------------------------------------------ classification


class TestClassifyError:
    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (http_error(429), RateLimitedError),
            (http_error(404), VideoUnavailableError),
            (http_error(410), VideoUnavailableError),
            (http_error(503), NetworkError),
            (network_error(), NetworkError),
            (DownloadError("ERROR: [TikTok] 1: Video not available, status code 10204"), VideoUnavailableError),
            (DownloadError("ERROR: [TikTok] 1: This video has been removed"), VideoUnavailableError),
            (DownloadError("ERROR: [TikTok] 1: Your IP address is blocked from accessing this post"),
             VideoUnavailableError),
            (DownloadError("ERROR: [facebook] 1: This video is private"), PrivateContentError),
            (DownloadError("ERROR: [facebook] 1: You must log in to continue. Use --cookies"), LoginRequiredError),
            (DownloadError("ERROR: Please solve the CAPTCHA"), BlockedError),
            (DownloadError("ERROR: This video is DRM protected"), BlockedError),
            (DownloadError("ERROR: Read timed out."), NetworkError),
            (DownloadError("ERROR: unable to open for writing: [Errno 2] No such file or directory"), StorageError),
            (DownloadError("ERROR: [TikTok] 1: Unable to extract universal data; please report this issue"),
             ExtractionError),
            (TimeoutError("timed out"), NetworkError),
        ],
    )
    def test_mapping(self, error: BaseException, expected: type) -> None:
        assert type(classify_error(error)) is expected

    def test_retryable_flags(self) -> None:
        assert classify_error(network_error()).retryable
        assert classify_error(http_error(429)).retryable
        assert not classify_error(http_error(404)).retryable

    def test_boilerplate_is_removed(self) -> None:
        message = str(classify_error(DownloadError("ERROR: [TikTok] 1: Weird thing; please report this issue on GitHub")))
        assert "please report" not in message.lower()
        assert "Weird thing" in message


# ------------------------------------------------------------------ retries


class TestRetryCall:
    def test_retries_transient_errors_then_succeeds(self) -> None:
        sleeps: list[float] = []
        attempts = iter([network_error(), network_error(), "ok"])

        def flaky() -> str:
            value = next(attempts)
            if isinstance(value, BaseException):
                raise value
            return value

        retried: list[int] = []
        result = retry_call(flaky, retries=3, base_delay=1, sleep=sleeps.append,
                            on_retry=lambda n, err, delay: retried.append(n))
        assert result == "ok"
        assert sleeps == [1, 2]  # exponential backoff
        assert retried == [1, 2]

    def test_gives_up_after_max_retries(self) -> None:
        sleeps: list[float] = []

        def always_fails() -> None:
            raise network_error()

        with pytest.raises(NetworkError):
            retry_call(always_fails, retries=2, base_delay=1, sleep=sleeps.append)
        assert len(sleeps) == 2

    def test_permanent_errors_are_not_retried(self) -> None:
        sleeps: list[float] = []
        calls = 0

        def gone() -> None:
            nonlocal calls
            calls += 1
            raise http_error(404)

        with pytest.raises(VideoUnavailableError):
            retry_call(gone, retries=5, sleep=sleeps.append)
        assert calls == 1 and sleeps == []

    def test_rate_limits_back_off_longer(self) -> None:
        sleeps: list[float] = []
        attempts = iter([http_error(429), "ok"])

        def limited() -> str:
            value = next(attempts)
            if isinstance(value, BaseException):
                raise value
            return value

        retry_call(limited, retries=1, base_delay=2, sleep=sleeps.append)
        assert sleeps == [10]

    def test_zero_retries(self) -> None:
        with pytest.raises(NetworkError):
            retry_call(lambda: (_ for _ in ()).throw(network_error()), retries=0, sleep=lambda _: None)

    def test_keyboard_interrupt_propagates(self) -> None:
        def interrupted() -> None:
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            retry_call(interrupted, retries=3, sleep=lambda _: None)


# --------------------------------------------------------- single downloads


class TestSingleDownload:
    def test_downloads_file_with_progress(self, config: Config, tmp_path: Path) -> None:
        factory = FakeYDLFactory([FakeVideo("111", title="My First Video #fun #viral")])
        sleeps: list[float] = []
        events: list[dict] = []
        infos: list[VideoInfo] = []
        dest = tmp_path / "out" / "tiktok"

        result = make_downloader(config, factory, sleeps).download(
            VIDEO_URL.format(111) + "?lang=en", dest, progress_hook=events.append, on_info=infos.append
        )

        assert result.status is DownloadStatus.DOWNLOADED
        assert result.path == dest / "My_First_Video_111.mp4"
        assert result.path.read_bytes() == b"\x00" * 2048
        assert [e["status"] for e in events] == ["downloading", "finished"]
        assert infos[0].title == "My First Video" and infos[0].filesize == 2048
        # The canonical URL (query string removed) is what gets requested.
        assert factory.extract_calls == [VIDEO_URL.format(111)]

    def test_output_directory_is_created(self, config: Config, tmp_path: Path) -> None:
        dest = tmp_path / "does" / "not" / "exist"
        factory = FakeYDLFactory([FakeVideo("1")])
        make_downloader(config, factory, []).download(VIDEO_URL.format(1), dest)
        assert dest.is_dir()

    def test_invalid_url_fails_without_network(self, config: Config, tmp_path: Path) -> None:
        factory = FakeYDLFactory()
        result = make_downloader(config, factory, []).download("https://www.tiktok.com/@user", tmp_path)
        assert result.status is DownloadStatus.FAILED
        assert "profile URL" in result.reason
        assert factory.extract_calls == []

    def test_unavailable_video(self, config: Config, tmp_path: Path) -> None:
        result = make_downloader(config, FakeYDLFactory(), []).download(VIDEO_URL.format(999), tmp_path)
        assert result.status is DownloadStatus.FAILED
        assert isinstance(result.error, VideoUnavailableError)

    def test_retries_network_error_during_download(self, config: Config, tmp_path: Path) -> None:
        video = FakeVideo("5", download_errors=[network_error()])
        factory = FakeYDLFactory([video])
        sleeps: list[float] = []
        result = make_downloader(config, factory, sleeps).download(VIDEO_URL.format(5), tmp_path)
        assert result.status is DownloadStatus.DOWNLOADED
        assert factory.download_calls == ["5", "5"]
        assert len(sleeps) == 1

    def test_network_failure_after_retries(self, config: Config, tmp_path: Path) -> None:
        video = FakeVideo("6", extract_errors=[network_error()] * 5)
        result = make_downloader(config, FakeYDLFactory([video]), []).download(VIDEO_URL.format(6), tmp_path)
        assert result.status is DownloadStatus.FAILED
        assert isinstance(result.error, NetworkError)

    def test_skips_already_downloaded(self, config: Config, tmp_path: Path) -> None:
        (tmp_path / "Old_name_7.mp4").write_bytes(b"done")
        factory = FakeYDLFactory([FakeVideo("7")])
        result = make_downloader(config, factory, []).download(VIDEO_URL.format(7), tmp_path)
        assert result.status is DownloadStatus.SKIPPED
        assert result.path == tmp_path / "Old_name_7.mp4"
        assert factory.download_calls == []

    def test_long_titles_respect_path_limit(self, config: Config, tmp_path: Path) -> None:
        dest = tmp_path / ("deep" * 30)
        factory = FakeYDLFactory([FakeVideo("8", title="word " * 100)])
        result = make_downloader(config, factory, []).download(VIDEO_URL.format(8), dest)
        assert result.path is not None
        assert len(str(result.path.resolve())) <= MAX_PATH_LENGTH

    def test_facebook_title_cleanup(self, config: Config) -> None:
        downloader = FacebookDownloader(config, ffmpeg=False)
        assert downloader.clean_title("10M views · 240K reactions | Come watch a spacewalk!") == "Come watch a spacewalk!"
        assert downloader.clean_title("Plain title") == "Plain title"


def test_options_never_include_credentials(config: Config) -> None:
    opts = base_options(config)
    assert opts["cookiefile"] is None and opts["usenetrc"] is False
    assert "cookiesfrombrowser" not in opts and "username" not in opts and "password" not in opts
    assert "impersonate" not in opts


def test_format_selection_depends_on_ffmpeg() -> None:
    assert format_selector(True) == "bv*+ba/b"
    assert "acodec!=none" in format_selector(False)


# ------------------------------------------------------------------ batches


class TestBatch:
    def test_continues_after_failures_and_records_them(self, config: Config, tmp_path: Path) -> None:
        factory = FakeYDLFactory([FakeVideo("1"), FakeVideo("3")])
        urls = [VIDEO_URL.format(1), "not a url", VIDEO_URL.format(2), VIDEO_URL.format(3)]
        started: list[int] = []
        summary = run_batch(
            make_downloader(config, factory, []), urls, tmp_path,
            callbacks=BatchCallbacks(on_start=lambda i, n, u: started.append(i)), delay=0,
        )
        assert started == [1, 2, 3, 4]
        assert (summary.completed, summary.failed, summary.skipped) == (2, 2, 0)
        assert [f.url for f in summary.failures] == ["not a url", VIDEO_URL.format(2)]
        assert len(list(tmp_path.glob("*.mp4"))) == 2

    def test_skipped_counts(self, config: Config, tmp_path: Path) -> None:
        (tmp_path / "x_1.mp4").write_bytes(b"x")
        factory = FakeYDLFactory([FakeVideo("1"), FakeVideo("2")])
        summary = run_batch(make_downloader(config, factory, []), [VIDEO_URL.format(1), VIDEO_URL.format(2)],
                            tmp_path, delay=0)
        assert (summary.completed, summary.skipped, summary.failed) == (1, 1, 0)

    def test_polite_delay_between_downloads(self, config: Config, tmp_path: Path) -> None:
        factory = FakeYDLFactory([FakeVideo("1"), FakeVideo("2")])
        sleeps: list[float] = []
        run_batch(make_downloader(config, factory, []), [VIDEO_URL.format(1), VIDEO_URL.format(2)], tmp_path,
                  delay=1.5, sleep=sleeps.append)
        assert sleeps == [1.5]  # between the two, not after the last

    def test_stops_when_platform_keeps_rate_limiting(self, config: Config, tmp_path: Path) -> None:
        config = Config(download_directory=config.download_directory, max_retries=0, request_delay=0)
        videos = [FakeVideo(str(i), extract_errors=[http_error(429)]) for i in range(1, 6)]
        factory = FakeYDLFactory(videos)
        urls = [VIDEO_URL.format(i) for i in range(1, 6)]
        summary = run_batch(make_downloader(config, factory, []), urls, tmp_path, delay=0)
        assert summary.aborted_reason
        assert len(summary.results) == 3
        assert summary.failed == 5
        assert [f.url for f in summary.failures] == urls

    def test_interrupt_records_unfinished_urls(self, config: Config, tmp_path: Path) -> None:
        video = FakeVideo("2", download_errors=[KeyboardInterrupt()])
        factory = FakeYDLFactory([FakeVideo("1"), video, FakeVideo("3")])
        urls = [VIDEO_URL.format(i) for i in (1, 2, 3)]
        summary = run_batch(make_downloader(config, factory, []), urls, tmp_path, delay=0)
        assert summary.interrupted
        assert summary.completed == 1
        assert [f.url for f in summary.failures] == urls[1:]


def test_retry_call_also_retry() -> None:
    sleeps: list[float] = []
    attempts = iter([DownloadError("ERROR: Unable to extract secondary user ID"), "ok"])

    def flaky() -> str:
        value = next(attempts)
        if isinstance(value, BaseException):
            raise value
        return value

    with pytest.raises(ExtractionError):
        retry_call(lambda: (_ for _ in ()).throw(DownloadError("ERROR: Unable to extract x")), retries=2,
                   sleep=sleeps.append)
    assert sleeps == []  # extraction errors are permanent by default
    assert retry_call(flaky, retries=2, base_delay=1, sleep=sleeps.append, also_retry=(ExtractionError,)) == "ok"
    assert sleeps == [1]


class TestAutoDownloader:
    def make(self, config: Config, factory: FakeYDLFactory, subfolder: str | None) -> AutoDownloader:
        return AutoDownloader(
            config,
            lambda platform: get_downloader(platform, config, ydl_factory=factory, sleep=lambda _: None, ffmpeg=False),
            subfolder=subfolder,
        )

    def test_routes_by_platform_into_platform_folders(self, config: Config, tmp_path: Path) -> None:
        factory = FakeYDLFactory([FakeVideo("1")])
        factory.add("https://www.facebook.com/watch/?v=222222", FakeVideo("222222"))
        auto = self.make(config, factory, "batch")
        urls = [VIDEO_URL.format(1), "https://fb.com/watch/?v=222222", "https://example.com/x"]
        summary = run_batch(auto, urls, tmp_path, delay=0)
        assert (summary.completed, summary.failed) == (2, 1)
        assert summary.folders == sorted([tmp_path / "facebook" / "batch", tmp_path / "tiktok" / "batch"])
        assert "Unsupported" in summary.failures[0].reason

    def test_without_subfolder_uses_dest_dir(self, config: Config, tmp_path: Path) -> None:
        auto = self.make(config, FakeYDLFactory([FakeVideo("1")]), None)
        result = auto.download(VIDEO_URL.format(1), tmp_path)
        assert result.path is not None and result.path.parent == tmp_path
