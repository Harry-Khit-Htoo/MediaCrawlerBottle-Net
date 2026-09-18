"""TikTok and Facebook crawlers with mocked yt-dlp / HTTP."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import requests
from conftest import FakeResponse, FakeSession, FakeYDLFactory, http_error, network_error
from yt_dlp.utils import DownloadError

from bottle_net.config import Config
from bottle_net.crawlers import FacebookCrawler, TikTokCrawler
from bottle_net.crawlers.facebook import REQUEST_HEADERS, extract_video_urls, looks_like_login_wall
from bottle_net.errors import (
    BlockedError,
    ExtractionError,
    InvalidURLError,
    LoginRequiredError,
    NetworkError,
    RateLimitedError,
)

PROFILE = "https://www.tiktok.com/@example"


def entries(*ids: str, fail_after: bool = False) -> Iterator[dict]:
    for vid in ids:
        yield {"_type": "url", "url": f"https://www.tiktok.com/@example/video/{vid}", "id": vid}
    if fail_after:
        raise DownloadError("ERROR: [tiktok:user] example: Unable to download API page: timed out")


# -------------------------------------------------------------------- TikTok


class TestTikTokCrawler:
    def make(self, config: Config, factory: FakeYDLFactory) -> TikTokCrawler:
        return TikTokCrawler(config, ydl_factory=factory, sleep=lambda _: None)

    def test_collects_unique_urls(self, config: Config) -> None:
        factory = FakeYDLFactory()
        factory.playlists[PROFILE] = lambda: iter([
            *entries("1", "2"),
            {"url": "https://www.tiktok.com/@example/video/2?lang=en"},  # duplicate
            {"url": "https://www.tiktok.com/@example/photo/9"},  # photo post: skipped
            {"id": "3"},  # URL built from the ID
            "garbage",
        ])
        found: list[str] = []
        result = self.make(config, factory).crawl("@example", on_found=found.append)
        assert result.urls == [f"https://www.tiktok.com/@example/video/{i}" for i in ("1", "2", "3")]
        assert found == result.urls
        assert result.complete and result.name == "example"
        opts = factory.options[0]
        assert opts["extract_flat"] == "in_playlist" and opts["cookiefile"] is None

    def test_accepts_profile_url(self, config: Config) -> None:
        factory = FakeYDLFactory()
        factory.playlists[PROFILE] = lambda: entries("1")
        assert self.make(config, factory).crawl("https://www.tiktok.com/@example?lang=en").urls

    def test_limit(self, config: Config) -> None:
        factory = FakeYDLFactory()
        factory.playlists[PROFILE] = lambda: entries("1", "2", "3", "4")
        result = self.make(config, factory).crawl("example", limit=2)
        assert len(result.urls) == 2
        assert any("limit" in note for note in result.notes)

    def test_partial_results_on_pagination_error(self, config: Config) -> None:
        factory = FakeYDLFactory()
        factory.playlists[PROFILE] = lambda: entries("1", "2", fail_after=True)
        result = self.make(config, factory).crawl("example")
        assert len(result.urls) == 2
        assert not result.complete
        assert any("stopped early" in note for note in result.notes)

    def test_empty_account(self, config: Config) -> None:
        factory = FakeYDLFactory()
        factory.playlists[PROFILE] = lambda: iter([])
        result = self.make(config, factory).crawl("example")
        assert result.urls == [] and result.notes

    def test_profile_failure_is_explained(self, config: Config) -> None:
        factory = FakeYDLFactory()
        factory.playlists[PROFILE] = DownloadError("ERROR: [tiktok:user] example: Unable to extract secondary user ID")
        with pytest.raises(ExtractionError, match="@example") as info:
            self.make(config, factory).crawl("example")
        assert info.value.hint

    def test_network_errors_are_retried(self, config: Config) -> None:
        factory = FakeYDLFactory()
        factory.playlists[PROFILE] = [network_error(), lambda: entries("1")]
        assert self.make(config, factory).crawl("example").urls
        assert factory.extract_calls == [PROFILE, PROFILE]

    def test_flaky_profile_page_is_retried(self, config: Config) -> None:
        factory = FakeYDLFactory()
        flaky = DownloadError("ERROR: [tiktok:user] example: Unable to extract secondary user ID")
        factory.playlists[PROFILE] = [flaky, flaky, lambda: entries("1")]
        retries: list[str] = []
        result = self.make(config, factory).crawl("example", on_retry=lambda n, err, d: retries.append(str(err)))
        assert result.urls and retries == ["TikTok did not return the profile data"] * 2

    def test_callback_errors_are_not_swallowed(self, config: Config) -> None:
        factory = FakeYDLFactory()
        factory.playlists[PROFILE] = lambda: entries("1", "2")

        def closed_pipe(url: str) -> None:
            raise BrokenPipeError

        with pytest.raises(BrokenPipeError):
            self.make(config, factory).crawl("example", on_found=closed_pipe)

    def test_rate_limit_error_surfaces(self, config: Config) -> None:
        factory = FakeYDLFactory()
        factory.playlists[PROFILE] = http_error(429)
        with pytest.raises(RateLimitedError):
            self.make(config, factory).crawl("example")

    def test_invalid_account_does_no_network(self, config: Config) -> None:
        factory = FakeYDLFactory()
        with pytest.raises(InvalidURLError):
            self.make(config, factory).crawl("not valid!")
        assert factory.extract_calls == []


# ------------------------------------------------------------------ Facebook

VIDEOS_HTML = """
<html><body>
<a href="https://www.facebook.com/examplepage/videos/launch-day/11111111111/">Launch</a>
<a href="/examplepage/videos/22222222222/">Second</a>
<script>{"url":"https:\\/\\/www.facebook.com\\/examplepage\\/videos\\/33333333333\\/","video_id":"44444444444"}</script>
<a href="https://www.facebook.com/otherpage/videos/99999999999/">Suggested from another Page</a>
<a href="https://www.facebook.com/watch/?v=55555555555&amp;ref=x">Watch</a>
<a href="https://www.facebook.com/examplepage/videos/11111111111/">Duplicate</a>
<script>{"page_info":{"has_next_page":true,"end_cursor":"abc"}}</script>
</body></html>
"""
REELS_HTML = '<a href="/reel/66666666666/">Reel</a><a href="/reel/22222222222/">Also a video</a>'
PAGE = "https://www.facebook.com/examplepage"


class TestFacebookExtraction:
    def test_extract_video_urls(self) -> None:
        urls = extract_video_urls(VIDEOS_HTML, owner="examplepage")
        assert urls == [
            "https://www.facebook.com/watch/?v=11111111111",
            "https://www.facebook.com/watch/?v=22222222222",
            "https://www.facebook.com/watch/?v=33333333333",
            "https://www.facebook.com/watch/?v=44444444444",
            "https://www.facebook.com/watch/?v=55555555555",
        ]

    def test_without_owner_filter_keeps_everything(self) -> None:
        assert "https://www.facebook.com/watch/?v=99999999999" in extract_video_urls(VIDEOS_HTML)

    def test_reels(self) -> None:
        assert extract_video_urls(REELS_HTML) == [
            "https://www.facebook.com/reel/66666666666",
            "https://www.facebook.com/reel/22222222222",
        ]

    def test_login_wall_detection(self) -> None:
        assert looks_like_login_wall("https://www.facebook.com/login/?next=x", "")
        assert looks_like_login_wall(PAGE, '<form id="login_form"></form>')
        assert not looks_like_login_wall(PAGE, VIDEOS_HTML)


class TestFacebookCrawler:
    def make(self, config: Config, session: FakeSession) -> FacebookCrawler:
        return FacebookCrawler(config, session=session, sleep=lambda _: None)

    def test_crawls_videos_and_reels_tabs(self, config: Config) -> None:
        session = FakeSession({f"{PAGE}/videos": FakeResponse(VIDEOS_HTML), f"{PAGE}/reels": FakeResponse(REELS_HTML)})
        found: list[str] = []
        result = self.make(config, session).crawl("https://www.facebook.com/examplepage", on_found=found.append)
        assert len(result.urls) == 6  # 5 videos + 1 new reel (the other reel is a duplicate ID)
        assert "https://www.facebook.com/reel/66666666666" in result.urls
        assert found == result.urls
        assert not result.complete  # has_next_page was true
        # Plain browser headers only; never cookies.
        for _, kwargs in session.calls:
            assert kwargs["headers"] == REQUEST_HEADERS
            assert "cookies" not in kwargs
            assert "Cookie" not in kwargs["headers"]

    def test_limit(self, config: Config) -> None:
        session = FakeSession({f"{PAGE}/videos": FakeResponse(VIDEOS_HTML), f"{PAGE}/reels": FakeResponse(REELS_HTML)})
        result = self.make(config, session).crawl("examplepage", limit=2)
        assert len(result.urls) == 2
        assert len(session.calls) == 1

    def test_login_wall_is_reported_not_bypassed(self, config: Config) -> None:
        wall = FakeResponse('<form id="login_form"></form>', url="https://www.facebook.com/login/?next=x")
        session = FakeSession({f"{PAGE}/videos": wall, f"{PAGE}/reels": wall})
        with pytest.raises(LoginRequiredError):
            self.make(config, session).crawl("examplepage")
        assert len(session.calls) == 2  # one request per tab, no retries or workarounds

    def test_one_tab_failing_gives_partial_result(self, config: Config) -> None:
        session = FakeSession({f"{PAGE}/videos": FakeResponse(VIDEOS_HTML), f"{PAGE}/reels": FakeResponse(status_code=500)})
        result = self.make(config, session).crawl("examplepage")
        assert len(result.urls) == 5
        assert any("could not be loaded" in note for note in result.notes)

    def test_page_not_found(self, config: Config) -> None:
        with pytest.raises(InvalidURLError, match="not found"):
            self.make(config, FakeSession({})).crawl("missingpage")

    @pytest.mark.parametrize(
        ("response", "expected"),
        [
            (FakeResponse(status_code=429), RateLimitedError),
            (FakeResponse(status_code=403), BlockedError),
            (FakeResponse(status_code=400), BlockedError),
            (requests.ConnectionError("boom"), NetworkError),
            (requests.Timeout("slow"), NetworkError),
        ],
    )
    def test_http_errors(self, config: Config, response: object, expected: type) -> None:
        session = FakeSession({f"{PAGE}/videos": [response], f"{PAGE}/reels": [response]})
        with pytest.raises(expected):
            self.make(config, session).crawl("examplepage")

    def test_transient_errors_are_retried(self, config: Config) -> None:
        session = FakeSession({
            f"{PAGE}/videos": [requests.ConnectionError("blip"), FakeResponse(VIDEOS_HTML)],
            f"{PAGE}/reels": FakeResponse(""),
        })
        result = self.make(config, session).crawl("examplepage")
        assert len(result.urls) == 5
        assert [url for url, _ in session.calls].count(f"{PAGE}/videos") == 2

    def test_no_videos(self, config: Config) -> None:
        session = FakeSession({f"{PAGE}/videos": FakeResponse("<html></html>"), f"{PAGE}/reels": FakeResponse("")})
        result = self.make(config, session).crawl("examplepage")
        assert result.urls == [] and result.notes
