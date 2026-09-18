"""End-to-end CLI tests: argument parsing, help, streams and every command (mocked network).

Convention under test: stdout carries only machine-readable data (crawled
URLs); everything meant for humans goes to stderr.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeResponse, FakeSession, FakeVideo, FakeYDLFactory, network_error

from bottle_net import __author__, __version__, commands
from bottle_net.cli import EXIT_FAILURE, EXIT_INTERRUPTED, EXIT_OK, EXIT_USAGE, build_parser, main
from bottle_net.config import CONFIG_ENV_VAR
from bottle_net.crawlers import FacebookCrawler, TikTokCrawler
from bottle_net.downloaders import get_downloader as real_get_downloader
from bottle_net.utils.urls import Platform

TIKTOK_URL = "https://www.tiktok.com/@user/video/{}"
PROFILE = "https://www.tiktok.com/@example"
CRAWLED = [f"https://www.tiktok.com/@example/video/{i}" for i in (1, 2, 3)]


@pytest.fixture(autouse=True)
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run every CLI test in an empty directory with no delays and no user config."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("COLUMNS", "120")
    (tmp_path / "config.toml").write_text("request_delay = 0\nmax_retries = 1\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def fake_ydl(monkeypatch: pytest.MonkeyPatch) -> FakeYDLFactory:
    """Route all downloads and TikTok crawls through a fake yt-dlp."""
    factory = FakeYDLFactory()

    def get_downloader(platform: Platform, config: Any, **kwargs: Any) -> Any:
        return real_get_downloader(platform, config, ydl_factory=factory, sleep=lambda _: None, ffmpeg=False)

    monkeypatch.setattr(commands, "get_downloader", get_downloader)
    monkeypatch.setattr(commands, "TikTokCrawler",
                        lambda config: TikTokCrawler(config, ydl_factory=factory, sleep=lambda _: None))
    return factory


@pytest.fixture
def profile(fake_ydl: FakeYDLFactory) -> FakeYDLFactory:
    """A TikTok account @example with three videos (one listed twice)."""
    fake_ydl.playlists[PROFILE] = lambda: iter([{"url": u} for u in [*CRAWLED[:2], CRAWLED[1], CRAWLED[2]]])
    return fake_ydl


@pytest.fixture
def terminal_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend stdout is an interactive terminal (not redirected)."""
    import sys

    monkeypatch.setattr(commands, "is_terminal", lambda stream: stream is sys.stdout)


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


def set_stdin(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(text.encode("utf-8")), encoding="utf-8"))


# ------------------------------------------------------------------- parsing


class TestParsing:
    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            (["tiktok", "crawl", "@user"], {"action": "crawl", "platform": Platform.TIKTOK, "target": "@user",
                                            "format": "txt", "output": None}),
            (["tiktok", "crawl", "@user", "--limit", "5", "-o", "x.txt"], {"limit": 5, "output": "x.txt"}),
            (["tiktok", "crawl", "@user", "--output", "-", "--format", "txt"], {"output": "-", "format": "txt"}),
            (["tiktok", "crawl", "@user", "-q"], {"quiet": True}),
            (["facebook", "crawl", "https://www.facebook.com/p"], {"action": "crawl", "platform": Platform.FACEBOOK}),
            (["tiktok", "download", "URL", "-d", "dir"], {"action": "download", "url": "URL", "dir": Path("dir")}),
            (["facebook", "download-list", "f.txt", "--failed-file", "bad.txt"],
             {"action": "download-list", "file": "f.txt", "failed_file": Path("bad.txt")}),
            (["tiktok", "download-list", "-", "--name", "my batch"], {"file": "-", "name": "my_batch"}),
            (["download", "URL"], {"action": "download", "platform": None, "url": "URL"}),
            (["download-list", "-"], {"action": "download-list", "platform": None, "file": "-"}),
            (["--debug", "tiktok", "crawl", "@u"], {"debug": True}),
            (["-q", "tiktok", "crawl", "@u"], {"quiet": True}),
            (["tiktok", "crawl", "@u", "--debug", "--no-color"], {"debug": True, "no_color": True}),
            (["--config", "c.toml", "download", "URL"], {"config": Path("c.toml")}),
        ],
    )
    def test_arguments(self, argv: list[str], expected: dict[str, Any]) -> None:
        args = build_parser().parse_args(argv)
        for key, value in expected.items():
            assert getattr(args, key) == value

    @pytest.mark.parametrize(
        "argv",
        [
            ["tiktok", "crawl"],
            ["tiktok", "crawl", "@u", "--limit", "0"],
            ["tiktok", "crawl", "@u", "--limit", "many"],
            ["tiktok", "crawl", "@u", "--format", "xml"],
            ["tiktok", "download-list", "f.txt", "--name", "///"],
            ["youtube", "download", "x"],
            ["tiktok", "fly"],
            ["download"],
            ["--bogus"],
        ],
    )
    def test_usage_errors_exit_2(self, argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as info:
            main(argv)
        assert info.value.code == EXIT_USAGE
        out, err = capsys.readouterr()
        assert out == ""
        assert err.startswith("[!] Error:")
        assert "-h' for help" in err


# ---------------------------------------------------------------------- help


class TestHelp:
    @pytest.mark.parametrize("flag", ["-h", "--help"])
    def test_main_help(self, flag: str, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as info:
            main([flag])
        assert info.value.code == 0
        out = capsys.readouterr().out
        for section in ("Usage:", "Platforms:", "Commands:", "Examples:", "Pipelines:", "Options:",
                        "download-list", "--version", "--quiet", "standard error"):
            assert section in out

    @pytest.mark.parametrize(
        ("argv", "must_contain"),
        [
            (["tiktok", "-h"], ["TikTok tools", "crawl", "download-list", "download-list -"]),
            (["facebook", "--help"], ["Facebook tools", "Page or profile"]),
            (["tiktok", "crawl", "-h"], ["bottle-net tiktok crawl <ACCOUNT> [OPTIONS]", "-o, --output FILE",
                                         "Save links to FILE", "-q, --quiet", "Output only video URLs",
                                         "--format FORMAT", "(default: txt)", "Show this help message",
                                         "crawl @username > videos.txt", "crawl @username --quiet > videos.txt",
                                         "| sort -u", "output/tiktok_<name>_links.txt"]),
            (["tiktok", "download", "-h"], ["URL", "--dir", "downloads/tiktok/", "--quiet"]),
            (["tiktok", "download-list", "-h"], ["<FILE | ->", "standard input", "--name", "failed_tiktok.txt",
                                                 "cat links.txt | bottle-net tiktok download-list -"]),
            (["facebook", "crawl", "-h"], ["PAGE_OR_PROFILE", "signed in", "facebook_<name>_links.txt"]),
            (["facebook", "download", "-h"], ["watch/?v="]),
            (["facebook", "download-list", "-h"], ["failed_facebook.txt"]),
            (["download", "-h"], ["detected", "automatically"]),
            (["download-list", "-h"], ["detected automatically", "failed_downloads.txt", "downloads/<platform>/<name>/"]),
        ],
    )
    def test_contextual_help(self, argv: list[str], must_contain: list[str], capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as info:
            main(argv)
        assert info.value.code == 0
        out = capsys.readouterr().out
        for text in must_contain:
            assert text in out

    @pytest.mark.parametrize("flag", ["-v", "--version"])
    def test_version(self, flag: str, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            main([flag])
        out = capsys.readouterr().out
        assert __version__ in out and __author__ in out

    def test_no_arguments_shows_banner_and_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        code, out, _ = run(capsys)
        assert code == EXIT_OK
        assert "BOTTLE NET TOOL" in out and __author__ in out and "Usage:" in out

    def test_platform_without_command_shows_platform_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        code, out, _ = run(capsys, "tiktok")
        assert code == EXIT_OK and "TikTok tools" in out


# --------------------------------------------------------------------- crawl


class TestCrawlOutput:
    def test_redirected_stdout_gets_only_urls(self, profile: FakeYDLFactory, workdir: Path,
                                              capsys: pytest.CaptureFixture[str]) -> None:
        # Under pytest, stdout is not a terminal - exactly like `> links.txt` or `| sort`.
        code, out, err = run(capsys, "tiktok", "crawl", "@example")
        assert code == EXIT_OK
        assert out.splitlines() == CRAWLED
        assert out == "".join(f"{u}\n" for u in CRAWLED)
        for status in ("TikTok Crawler", "Target: @example", "Starting crawler", "Discovering public videos",
                       "Progress:", "Videos found: 3", "Duplicates:   1", "Crawl completed",
                       "standard output (3 URLs)"):
            assert status in err
        assert "[+]" not in out and "Videos found" not in out and "✓" not in out
        assert not (workdir / "output").exists()  # nothing saved behind the user's back

    def test_quiet_prints_only_urls(self, profile: FakeYDLFactory, capsys: pytest.CaptureFixture[str]) -> None:
        code, out, err = run(capsys, "tiktok", "crawl", "@example", "--quiet")
        assert code == EXIT_OK
        assert out.splitlines() == CRAWLED
        assert err == ""

    def test_quiet_in_terminal_still_prints_urls(self, profile: FakeYDLFactory, terminal_stdout: None,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
        code, out, err = run(capsys, "tiktok", "crawl", "@example", "-q")
        assert code == EXIT_OK and out.splitlines() == CRAWLED and err == ""

    @pytest.mark.parametrize("flag", ["-o", "--output"])
    def test_output_file(self, flag: str, profile: FakeYDLFactory, workdir: Path,
                         capsys: pytest.CaptureFixture[str]) -> None:
        code, out, err = run(capsys, "tiktok", "crawl", "@example", flag, "example-links.txt", "--format", "txt")
        assert code == EXIT_OK
        assert out == ""
        assert (workdir / "example-links.txt").read_text(encoding="utf-8") == "".join(f"{u}\n" for u in CRAWLED)
        assert "Output:" in err and "example-links.txt" in err

    def test_output_file_is_replaced_with_notice(self, profile: FakeYDLFactory, workdir: Path,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
        (workdir / "links.txt").write_text("old\n", encoding="utf-8")
        code, _, err = run(capsys, "tiktok", "crawl", "@example", "-o", "links.txt")
        assert code == EXIT_OK and "existing file was replaced" in err
        assert "old" not in (workdir / "links.txt").read_text(encoding="utf-8")

    def test_output_dash_means_stdout(self, profile: FakeYDLFactory, terminal_stdout: None,
                                      capsys: pytest.CaptureFixture[str]) -> None:
        code, out, err = run(capsys, "tiktok", "crawl", "@example", "-o", "-")
        assert code == EXIT_OK and out.splitlines() == CRAWLED and "Videos found: 3" in err

    def test_terminal_default_saves_unique_file(self, profile: FakeYDLFactory, terminal_stdout: None,
                                                workdir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        code, out, err = run(capsys, "tiktok", "crawl", "@example")
        assert code == EXIT_OK and out == ""
        first = workdir / "output" / "tiktok_example_links.txt"
        assert first.read_text(encoding="utf-8").splitlines() == CRAWLED
        assert "tiktok_example_links.txt" in err and 'download-list "output' in err

        first.write_text("keep me\n", encoding="utf-8")
        code, _, err = run(capsys, "tiktok", "crawl", "@example")
        assert code == EXIT_OK
        assert first.read_text(encoding="utf-8") == "keep me\n"  # never overwritten
        second = workdir / "output" / "tiktok_example_links_2.txt"
        assert second.read_text(encoding="utf-8").splitlines() == CRAWLED
        assert "tiktok_example_links_2.txt" in err

    def test_closed_pipe_stops_quietly(self, profile: FakeYDLFactory, monkeypatch: pytest.MonkeyPatch,
                                       capsys: pytest.CaptureFixture[str]) -> None:
        class ClosedPipe(io.StringIO):
            """Like the stdout of `bottle-net ... | head -1` after head exits."""

            def write(self, text: str) -> int:
                raise BrokenPipeError

            def fileno(self) -> int:
                raise io.UnsupportedOperation

        monkeypatch.setattr("sys.stdout", ClosedPipe())
        code = main(["tiktok", "crawl", "@example", "-q"])
        assert code == EXIT_OK
        assert "Traceback" not in capsys.readouterr().err

    def test_no_videos_found(self, fake_ydl: FakeYDLFactory, workdir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        fake_ydl.playlists[PROFILE] = lambda: iter([])
        code, out, err = run(capsys, "tiktok", "crawl", "@example")
        assert code == EXIT_FAILURE and out == "" and "No video URLs to save" in err

    def test_invalid_account(self, capsys: pytest.CaptureFixture[str]) -> None:
        code, out, err = run(capsys, "tiktok", "crawl", "bad name!")
        assert code == EXIT_FAILURE and out == ""
        assert "not a valid TikTok username" in err
        assert "Video Crawler" not in err  # fails before any output or network access

    def test_crawl_output_works_with_download_list(self, profile: FakeYDLFactory, workdir: Path,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
        run(capsys, "tiktok", "crawl", "@example", "-o", "links.txt")
        for i in (1, 2, 3):
            profile.add(f"https://www.tiktok.com/@example/video/{i}", FakeVideo(str(i)))
        code, _, err = run(capsys, "tiktok", "download-list", "links.txt")
        assert code == EXIT_OK and "Successful:    3" in err
        assert len(list((workdir / "downloads" / "tiktok" / "links").glob("*.mp4"))) == 3

    def test_facebook_crawl(self, workdir: Path, monkeypatch: pytest.MonkeyPatch,
                            capsys: pytest.CaptureFixture[str]) -> None:
        page = "https://www.facebook.com/examplepage"
        session = FakeSession({
            f"{page}/videos": FakeResponse('<a href="/examplepage/videos/12345678901/">v</a>'),
            f"{page}/reels": FakeResponse('<a href="/reel/22345678901/">r</a>'),
        })
        monkeypatch.setattr(commands, "FacebookCrawler",
                            lambda config, **kw: FacebookCrawler(config, session=session, sleep=lambda _: None, **kw))
        code, out, err = run(capsys, "facebook", "crawl", page, "-o", "facebook-links.txt")
        assert code == EXIT_OK and out == ""
        assert (workdir / "facebook-links.txt").read_text(encoding="utf-8").splitlines() == [
            "https://www.facebook.com/watch/?v=12345678901",
            "https://www.facebook.com/reel/22345678901",
        ]
        assert "Videos found: 2" in err

    def test_facebook_login_wall(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        wall = FakeResponse("<html></html>", url="https://www.facebook.com/login/?next=x")
        session = FakeSession({"https://www.facebook.com/private/videos": wall, "https://www.facebook.com/private/reels": wall})
        monkeypatch.setattr(commands, "FacebookCrawler",
                            lambda config, **kw: FacebookCrawler(config, session=session, sleep=lambda _: None, **kw))
        code, out, err = run(capsys, "facebook", "crawl", "private")
        assert code == EXIT_FAILURE and out == "" and "requires signing in" in err


# ------------------------------------------------------------------ download


class TestDownloadCommand:
    def test_tiktok_download(self, fake_ydl: FakeYDLFactory, workdir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        fake_ydl.add(TIKTOK_URL.format(1), FakeVideo("1", title="Example video"))
        code, out, err = run(capsys, "tiktok", "download", TIKTOK_URL.format(1))
        assert code == EXIT_OK
        assert out == ""  # status is not data
        assert (workdir / "downloads" / "tiktok" / "Example_video_1.mp4").is_file()
        assert "Download completed" in err and "Title: Example video" in err and "100%" in err
        assert "File:  Example_video_1.mp4" in err and "Size:  2.0 KB" in err and "Downloading" in err
        assert "Saved:" in err

    def test_quiet_download(self, fake_ydl: FakeYDLFactory, capsys: pytest.CaptureFixture[str]) -> None:
        fake_ydl.add(TIKTOK_URL.format(1), FakeVideo("1"))
        code, out, err = run(capsys, "tiktok", "download", TIKTOK_URL.format(1), "-q")
        assert (code, out, err) == (EXIT_OK, "", "")

    def test_universal_download_detects_facebook(self, fake_ydl: FakeYDLFactory, workdir: Path,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
        fake_ydl.add("https://www.facebook.com/watch/?v=123456", FakeVideo("123456", title="FB clip"))
        code, _, err = run(capsys, "download", "https://m.facebook.com/watch?v=123456")
        assert code == EXIT_OK
        assert (workdir / "downloads" / "facebook" / "FB_clip_123456.mp4").is_file()
        assert "Facebook Downloader" in err

    def test_custom_directory(self, fake_ydl: FakeYDLFactory, workdir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        fake_ydl.add(TIKTOK_URL.format(2), FakeVideo("2"))
        code, _, _ = run(capsys, "download", TIKTOK_URL.format(2), "-d", "my/videos")
        assert code == EXIT_OK
        assert list((workdir / "my" / "videos").glob("*_2.mp4"))

    def test_unknown_url(self, capsys: pytest.CaptureFixture[str]) -> None:
        code, _, err = run(capsys, "download", "https://example.com/video")
        assert code == EXIT_FAILURE
        assert "[!] Unsupported platform." in err and "example.com" in err
        assert "Supported platforms:" in err and "TikTok" in err and "Facebook" in err

    @pytest.mark.parametrize("value", ["not-a-url", "", "ftp:/x"])
    def test_not_a_url(self, value: str, capsys: pytest.CaptureFixture[str]) -> None:
        code, out, err = run(capsys, "download", value)
        assert code == EXIT_FAILURE and out == ""
        assert "[!] URL is invalid." in err and "Supported platforms:" in err
        assert "Traceback" not in err

    def test_invalid_url(self, fake_ydl: FakeYDLFactory, capsys: pytest.CaptureFixture[str]) -> None:
        code, _, err = run(capsys, "tiktok", "download", "https://www.tiktok.com/@someone")
        assert code == EXIT_FAILURE
        assert "profile URL" in err and "bottle-net tiktok crawl" in err
        assert fake_ydl.extract_calls == []

    def test_unavailable_video(self, fake_ydl: FakeYDLFactory, capsys: pytest.CaptureFixture[str]) -> None:
        code, _, err = run(capsys, "tiktok", "download", TIKTOK_URL.format(404))
        assert code == EXIT_FAILURE
        assert "[!] Video unavailable." in err and "deleted" in err

    def test_errors_still_shown_when_quiet(self, fake_ydl: FakeYDLFactory, capsys: pytest.CaptureFixture[str]) -> None:
        code, out, err = run(capsys, "tiktok", "download", TIKTOK_URL.format(404), "--quiet")
        assert code == EXIT_FAILURE and out == ""
        assert "[!] Video unavailable." in err and "Downloader" not in err

    def test_already_downloaded(self, fake_ydl: FakeYDLFactory, capsys: pytest.CaptureFixture[str]) -> None:
        fake_ydl.add(TIKTOK_URL.format(3), FakeVideo("3"))
        run(capsys, "download", TIKTOK_URL.format(3))
        code, _, err = run(capsys, "download", TIKTOK_URL.format(3))
        assert code == EXIT_OK and "skipped" in err
        assert fake_ydl.download_calls == ["3"]


# ------------------------------------------------------------- download-list


def write_list(workdir: Path, name: str, lines: list[str]) -> Path:
    path = workdir / "output" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestDownloadListCommand:
    def test_batch_with_failures_duplicates_and_blanks(
        self, fake_ydl: FakeYDLFactory, workdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        for vid in ("1", "2"):
            fake_ydl.add(TIKTOK_URL.format(vid), FakeVideo(vid, title=f"Video {vid}"))
        path = write_list(workdir, "tiktok_example_links.txt", [
            "# my list",
            TIKTOK_URL.format(1),
            "",
            TIKTOK_URL.format(1) + "?lang=en",
            "not a url",
            TIKTOK_URL.format(404),
            TIKTOK_URL.format(2),
        ])
        code, out, err = run(capsys, "tiktok", "download-list", str(path))

        assert code == EXIT_FAILURE  # some downloads failed
        assert out == ""
        dest = workdir / "downloads" / "tiktok" / "example"
        assert sorted(p.name for p in dest.iterdir()) == ["Video_1_1.mp4", "Video_2_2.mp4"]
        assert "Ignored 1 duplicate" in err
        assert "[1/4]" in err and "[4/4]" in err
        assert "Skipping and continuing" in err
        assert "Successful:    2" in err and "Failed:        2" in err and "Skipped:       0" in err

        failed = workdir / "output" / "failed_tiktok.txt"
        lines = [ln for ln in failed.read_text(encoding="utf-8").splitlines() if ln and not ln.startswith("#")]
        assert lines == ["not a url", TIKTOK_URL.format(404)]

    def test_rerun_skips_and_clears_failures(
        self, fake_ydl: FakeYDLFactory, workdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_ydl.add(TIKTOK_URL.format(1), FakeVideo("1"))
        path = write_list(workdir, "videos.txt", [TIKTOK_URL.format(1)])
        failed = workdir / "output" / "failed_tiktok.txt"
        failed.write_text("# old\nhttps://old\n", encoding="utf-8")

        assert run(capsys, "tiktok", "download-list", str(path))[0] == EXIT_OK
        code, _, err = run(capsys, "tiktok", "download-list", str(path))
        assert code == EXIT_OK and "Skipped:       1" in err
        assert (workdir / "downloads" / "tiktok" / "videos").is_dir()
        assert "https://old" not in failed.read_text(encoding="utf-8")

    def test_facebook_batch_folder(self, fake_ydl: FakeYDLFactory, workdir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        fake_ydl.add("https://www.facebook.com/watch/?v=111111", FakeVideo("111111"))
        path = write_list(workdir, "facebook_examplepage_links.txt", ["https://www.facebook.com/watch/?v=111111"])
        code, _, err = run(capsys, "facebook", "download-list", str(path))
        assert code == EXIT_OK and "Batch completed" in err
        assert list((workdir / "downloads" / "facebook" / "examplepage").glob("*_111111.mp4"))

    def test_from_stdin(self, fake_ydl: FakeYDLFactory, workdir: Path, monkeypatch: pytest.MonkeyPatch,
                        capsys: pytest.CaptureFixture[str]) -> None:
        for vid in ("1", "2"):
            fake_ydl.add(TIKTOK_URL.format(vid), FakeVideo(vid))
        set_stdin(monkeypatch, f"{TIKTOK_URL.format(1)}\n\n{TIKTOK_URL.format(2)}\n{TIKTOK_URL.format(1)}\n")
        code, out, err = run(capsys, "tiktok", "download-list", "-")
        assert code == EXIT_OK and out == ""
        assert "URL list: standard input" in err and "Ignored 1 duplicate" in err
        # Folder named after the (single) TikTok account in the list.
        assert len(list((workdir / "downloads" / "tiktok" / "user").glob("*.mp4"))) == 2

    def test_stdin_with_name(self, fake_ydl: FakeYDLFactory, workdir: Path, monkeypatch: pytest.MonkeyPatch,
                             capsys: pytest.CaptureFixture[str]) -> None:
        fake_ydl.add(TIKTOK_URL.format(1), FakeVideo("1"))
        set_stdin(monkeypatch, TIKTOK_URL.format(1))
        assert run(capsys, "tiktok", "download-list", "-", "--name", "favourites")[0] == EXIT_OK
        assert list((workdir / "downloads" / "tiktok" / "favourites").glob("*.mp4"))

    def test_empty_stdin(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        set_stdin(monkeypatch, "\n# nothing\n")
        code, _, err = run(capsys, "tiktok", "download-list", "-")
        assert code == EXIT_FAILURE and "Standard input contains no URLs" in err

    def test_universal_mixed_list(self, fake_ydl: FakeYDLFactory, workdir: Path, monkeypatch: pytest.MonkeyPatch,
                                  capsys: pytest.CaptureFixture[str]) -> None:
        fake_ydl.add(TIKTOK_URL.format(1), FakeVideo("1"))
        fake_ydl.add("https://www.facebook.com/watch/?v=222222", FakeVideo("222222"))
        path = write_list(workdir, "mixed.txt", [
            TIKTOK_URL.format(1), "https://www.facebook.com/watch/?v=222222", "https://example.com/v/1",
        ])
        code, out, err = run(capsys, "download-list", str(path))
        assert code == EXIT_FAILURE and out == ""
        assert list((workdir / "downloads" / "tiktok" / "mixed").glob("*_1.mp4"))
        assert list((workdir / "downloads" / "facebook" / "mixed").glob("*_222222.mp4"))
        failed = (workdir / "output" / "failed_downloads.txt").read_text(encoding="utf-8")
        assert "https://example.com/v/1" in failed and '--name "mixed"' in failed

    def test_universal_from_stdin_with_dir(self, fake_ydl: FakeYDLFactory, workdir: Path,
                                           monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        fake_ydl.add(TIKTOK_URL.format(1), FakeVideo("1"))
        fake_ydl.add("https://www.facebook.com/watch/?v=222222", FakeVideo("222222"))
        set_stdin(monkeypatch, f"{TIKTOK_URL.format(1)}\nhttps://www.facebook.com/watch/?v=222222\n")
        assert run(capsys, "download-list", "-", "-d", "all")[0] == EXIT_OK
        assert len(list((workdir / "all").glob("*.mp4"))) == 2

    def test_quiet_batch_reports_only_errors(self, fake_ydl: FakeYDLFactory, workdir: Path,
                                             capsys: pytest.CaptureFixture[str]) -> None:
        fake_ydl.add(TIKTOK_URL.format(1), FakeVideo("1"))
        path = write_list(workdir, "videos.txt", [TIKTOK_URL.format(1), TIKTOK_URL.format(404)])
        code, out, err = run(capsys, "tiktok", "download-list", str(path), "-q")
        assert code == EXIT_FAILURE and out == ""
        assert "1 of 2 download(s) failed" in err
        assert "Batch Downloader" not in err and "[1/2]" not in err

    def test_empty_list(self, fake_ydl: FakeYDLFactory, workdir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        path = write_list(workdir, "empty.txt", ["", "# nothing here", ""])
        code, _, err = run(capsys, "tiktok", "download-list", str(path))
        assert code == EXIT_FAILURE and "contains no URLs" in err

    def test_missing_list(self, capsys: pytest.CaptureFixture[str]) -> None:
        code, _, err = run(capsys, "facebook", "download-list", "nope.txt")
        assert code == EXIT_FAILURE and "not found" in err


class TestInterruptionsAndNetworkFailures:
    def test_single_download_interrupted(self, fake_ydl: FakeYDLFactory, capsys: pytest.CaptureFixture[str]) -> None:
        fake_ydl.add(TIKTOK_URL.format(1), FakeVideo("1", download_errors=[KeyboardInterrupt()]))
        code, _, err = run(capsys, "tiktok", "download", TIKTOK_URL.format(1))
        assert code == EXIT_INTERRUPTED
        assert "interrupted" in err and "resume" in err

    def test_batch_interrupted_records_remaining(
        self, fake_ydl: FakeYDLFactory, workdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_ydl.add(TIKTOK_URL.format(1), FakeVideo("1"))
        fake_ydl.add(TIKTOK_URL.format(2), FakeVideo("2", download_errors=[KeyboardInterrupt()]))
        fake_ydl.add(TIKTOK_URL.format(3), FakeVideo("3"))
        path = workdir / "videos.txt"
        path.write_text("\n".join(TIKTOK_URL.format(i) for i in (1, 2, 3)), encoding="utf-8")
        code, _, err = run(capsys, "tiktok", "download-list", str(path))
        assert code == EXIT_INTERRUPTED
        assert "[!] Download interrupted by user." in err
        assert "Completed:     1" in err and "Remaining:     2" in err
        assert "Partial files have been handled safely" in err
        assert "--output-dir" in err and "Traceback" not in err
        failed = (workdir / "output" / "failed_tiktok.txt").read_text(encoding="utf-8")
        assert TIKTOK_URL.format(2) in failed and TIKTOK_URL.format(3) in failed
        assert TIKTOK_URL.format(1) + "\n" not in failed

    def test_network_failures_are_recorded(
        self, fake_ydl: FakeYDLFactory, workdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_ydl.add(TIKTOK_URL.format(1), FakeVideo("1", extract_errors=[network_error()] * 5))
        fake_ydl.add(TIKTOK_URL.format(2), FakeVideo("2"))
        path = workdir / "videos.txt"
        path.write_text(f"{TIKTOK_URL.format(1)}\n{TIKTOK_URL.format(2)}\n", encoding="utf-8")
        code, _, err = run(capsys, "tiktok", "download-list", str(path))
        assert code == EXIT_FAILURE
        assert "[!] Network error. Retrying in" in err  # max_retries = 1 in the test config
        assert "[!] 1 download(s) failed." in err and "Retry failed downloads with:" in err
        assert "--output-dir" in err and "videos" in err
        assert "Successful:    1" in err and "Failed:        1" in err
        failed = (workdir / "output" / "failed_tiktok.txt").read_text(encoding="utf-8")
        assert "# Network error" in failed and TIKTOK_URL.format(1) in failed

    def test_crawl_retries_flaky_profile(self, fake_ydl: FakeYDLFactory, capsys: pytest.CaptureFixture[str]) -> None:
        from yt_dlp.utils import DownloadError

        flaky = DownloadError("ERROR: [tiktok:user] example: Unable to extract secondary user ID")
        fake_ydl.playlists[PROFILE] = [flaky, lambda: iter([{"url": CRAWLED[0]}])]
        code, out, err = run(capsys, "tiktok", "crawl", "@example")
        assert code == EXIT_OK and out.splitlines() == CRAWLED[:1]
        assert "[!] TikTok did not return the profile data. Retrying in" in err


def test_invalid_config_is_reported(workdir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (workdir / "config.toml").write_text("max_retries = -4\n", encoding="utf-8")
    code, _, err = run(capsys, "download", TIKTOK_URL.format(1))
    assert code == EXIT_FAILURE and "max_retries" in err


class TestReleasePolish:
    def test_output_dir_alias(self) -> None:
        for flag in ("-d", "--output-dir", "--dir"):
            args = build_parser().parse_args(["tiktok", "download-list", "f.txt", flag, "x"])
            assert args.dir == Path("x")

    def test_facebook_notice(self, workdir: Path, monkeypatch: pytest.MonkeyPatch,
                             capsys: pytest.CaptureFixture[str]) -> None:
        page = "https://www.facebook.com/examplepage"
        session = FakeSession({f"{page}/videos": FakeResponse('<a href="/examplepage/videos/12345678901/">v</a>'),
                               f"{page}/reels": FakeResponse("")})
        monkeypatch.setattr(commands, "FacebookCrawler",
                            lambda config, **kw: FacebookCrawler(config, session=session, sleep=lambda _: None, **kw))
        code, _, err = run(capsys, "facebook", "crawl", page, "-q")
        assert code == EXIT_OK and err == ""  # quiet hides the notice too
        code, _, err = run(capsys, "facebook", "crawl", page, "-o", "fb.txt")
        assert "[!] Note:" in err and "first batch of public videos" in err
        assert "does not use login credentials or private APIs" in err

    def test_retry_without_flags_uses_original_folder(
        self, fake_ydl: FakeYDLFactory, workdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        flaky = FakeVideo("7", extract_errors=[network_error()] * 2)  # fails twice (max_retries=1), then works
        fake_ydl.add(TIKTOK_URL.format(7), flaky)
        path = write_list(workdir, "tiktok_example_links.txt", [TIKTOK_URL.format(7)])
        assert run(capsys, "tiktok", "download-list", str(path))[0] == EXIT_FAILURE
        code, _, err = run(capsys, "tiktok", "download-list", "output/failed_tiktok.txt")
        assert code == EXIT_OK and "recorded" in err
        assert list((workdir / "downloads" / "tiktok" / "example").glob("*_7.mp4"))
        assert not (workdir / "downloads" / "tiktok" / "failed_tiktok").exists()

    def test_universal_retry_keeps_batch_name(
        self, fake_ydl: FakeYDLFactory, workdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_ydl.add(TIKTOK_URL.format(8), FakeVideo("8", extract_errors=[network_error()] * 2))
        path = write_list(workdir, "mixed.txt", [TIKTOK_URL.format(8)])
        run(capsys, "download-list", str(path))
        assert run(capsys, "download-list", "output/failed_downloads.txt")[0] == EXIT_OK
        assert list((workdir / "downloads" / "tiktok" / "mixed").glob("*_8.mp4"))
