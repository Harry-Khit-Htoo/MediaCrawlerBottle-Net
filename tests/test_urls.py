"""URL validation, normalization, platform detection and de-duplication."""

from __future__ import annotations

import pytest

from bottle_net.errors import InvalidURLError, UnsupportedURLError
from bottle_net.utils.urls import (
    Platform,
    dedupe_urls,
    detect_platform,
    normalize_facebook_video_url,
    normalize_tiktok_video_url,
    normalize_video_url,
    parse_facebook_target,
    parse_tiktok_account,
    video_key,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.tiktok.com/@user/video/123", Platform.TIKTOK),
        ("https://vm.tiktok.com/ZMabc/", Platform.TIKTOK),
        ("tiktok.com/@user", Platform.TIKTOK),
        ("https://m.facebook.com/watch/?v=1", Platform.FACEBOOK),
        ("https://fb.watch/abc/", Platform.FACEBOOK),
        ("https://www.youtube.com/watch?v=1", None),
        ("https://nottiktok.com/@user/video/1", None),
        ("not a url", None),
        ("", None),
        ("ftp://www.tiktok.com/@user/video/1", None),
    ],
)
def test_detect_platform(url: str, expected: Platform | None) -> None:
    assert detect_platform(url) is expected


class TestTikTokVideoURLs:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://www.tiktok.com/@user.name/video/7123?is_from_webapp=1&lang=en",
             "https://www.tiktok.com/@user.name/video/7123"),
            ("http://tiktok.com/@user/video/7123/", "https://www.tiktok.com/@user/video/7123"),
            ("www.tiktok.com/@user/video/7123", "https://www.tiktok.com/@user/video/7123"),
            ("https://m.tiktok.com/v/7123.html", "https://www.tiktok.com/embed/v2/7123"),
            ("https://www.tiktok.com/embed/v2/7123", "https://www.tiktok.com/embed/v2/7123"),
            ("https://vm.tiktok.com/ZMabc", "https://vm.tiktok.com/ZMabc/"),
            ("https://www.tiktok.com/t/ZTabc/", "https://www.tiktok.com/t/ZTabc/"),
        ],
    )
    def test_valid(self, url: str, expected: str) -> None:
        assert normalize_tiktok_video_url(url) == expected

    def test_profile_url_is_rejected_with_hint(self) -> None:
        with pytest.raises(InvalidURLError) as info:
            normalize_tiktok_video_url("https://www.tiktok.com/@user")
        assert "crawl" in (info.value.hint or "")

    def test_photo_post_is_unsupported(self) -> None:
        with pytest.raises(UnsupportedURLError):
            normalize_tiktok_video_url("https://www.tiktok.com/@user/photo/7123")

    @pytest.mark.parametrize("url", ["https://www.tiktok.com/", "https://www.tiktok.com/explore", "garbage"])
    def test_invalid(self, url: str) -> None:
        with pytest.raises(InvalidURLError):
            normalize_tiktok_video_url(url)


class TestFacebookVideoURLs:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://www.facebook.com/watch/?v=123456", "https://www.facebook.com/watch/?v=123456"),
            ("https://www.facebook.com/watch?v=123456&ref=x", "https://www.facebook.com/watch/?v=123456"),
            ("https://m.facebook.com/video.php?v=123456", "https://www.facebook.com/watch/?v=123456"),
            ("https://www.facebook.com/NASA/videos/some-title/123456/", "https://www.facebook.com/watch/?v=123456"),
            ("https://www.facebook.com/NASA/videos/123456", "https://www.facebook.com/watch/?v=123456"),
            ("https://www.facebook.com/reel/123456?s=1", "https://www.facebook.com/reel/123456"),
            ("https://fb.watch/abcDEF/", "https://fb.watch/abcDEF/"),
            ("https://www.facebook.com/share/v/1AbC/", "https://www.facebook.com/share/v/1AbC/"),
            ("https://www.facebook.com/NASA/posts/pfbid0abc", "https://www.facebook.com/NASA/posts/pfbid0abc"),
        ],
    )
    def test_valid(self, url: str, expected: str) -> None:
        assert normalize_facebook_video_url(url) == expected

    def test_page_url_is_rejected_with_hint(self) -> None:
        with pytest.raises(InvalidURLError) as info:
            normalize_facebook_video_url("https://www.facebook.com/examplepage")
        assert "crawl" in (info.value.hint or "")

    @pytest.mark.parametrize("url", ["https://www.facebook.com/watch/?v=abc", "https://www.facebook.com/", "https://fb.watch/"])
    def test_invalid(self, url: str) -> None:
        with pytest.raises(InvalidURLError):
            normalize_facebook_video_url(url)


class TestNormalizeVideoURL:
    def test_wrong_platform_gives_hint(self) -> None:
        with pytest.raises(InvalidURLError) as info:
            normalize_video_url("https://www.facebook.com/watch/?v=123456", Platform.TIKTOK)
        assert info.value.hint == "Use: bottle-net facebook download <URL>"

    def test_unknown_site(self) -> None:
        with pytest.raises(UnsupportedURLError):
            normalize_video_url("https://example.com/video/1", Platform.TIKTOK)

    def test_malformed(self) -> None:
        with pytest.raises(InvalidURLError):
            normalize_video_url("definitely not a url", Platform.FACEBOOK)


class TestTikTokAccounts:
    @pytest.mark.parametrize(
        "value",
        ["@example", "example", " example ", "https://www.tiktok.com/@example",
         "https://www.tiktok.com/@example?lang=en", "tiktok.com/@example/"],
    )
    def test_valid(self, value: str) -> None:
        assert parse_tiktok_account(value) == "example"

    def test_dots_and_underscores(self) -> None:
        assert parse_tiktok_account("@my.user_1") == "my.user_1"

    @pytest.mark.parametrize("value", ["", "@", "bad name", "@bad!", "https://www.youtube.com/@example"])
    def test_invalid(self, value: str) -> None:
        with pytest.raises(InvalidURLError):
            parse_tiktok_account(value)

    def test_video_url_points_to_download(self) -> None:
        with pytest.raises(InvalidURLError) as info:
            parse_tiktok_account("https://www.tiktok.com/@example/video/123")
        assert "download" in (info.value.hint or "")


class TestFacebookTargets:
    @pytest.mark.parametrize(
        "value",
        ["examplepage", "@examplepage", "https://www.facebook.com/examplepage",
         "https://m.facebook.com/examplepage/videos/", "facebook.com/examplepage/reels"],
    )
    def test_named_page(self, value: str) -> None:
        target = parse_facebook_target(value)
        assert target.name == "examplepage"
        assert target.url == "https://www.facebook.com/examplepage"
        assert target.tab_url("videos") == "https://www.facebook.com/examplepage/videos"

    def test_numeric_profile(self) -> None:
        target = parse_facebook_target("https://www.facebook.com/profile.php?id=100012345")
        assert target.numeric_profile
        assert target.name == "100012345"
        assert target.tab_url("videos") == "https://www.facebook.com/profile.php?id=100012345&sk=videos"
        assert target.tab_url("reels") == "https://www.facebook.com/profile.php?id=100012345&sk=reels_tab"

    def test_video_url_points_to_download(self) -> None:
        with pytest.raises(InvalidURLError) as info:
            parse_facebook_target("https://www.facebook.com/watch/?v=123456")
        assert "download" in (info.value.hint or "")

    def test_groups_are_unsupported(self) -> None:
        with pytest.raises(UnsupportedURLError):
            parse_facebook_target("https://www.facebook.com/groups/12345")

    @pytest.mark.parametrize("value", ["", "https://www.facebook.com/", "bad page!", "https://twitter.com/x",
                                       "https://www.facebook.com/profile.php?id=abc"])
    def test_invalid(self, value: str) -> None:
        with pytest.raises(InvalidURLError):
            parse_facebook_target(value)


class TestDeduplication:
    def test_same_video_different_forms(self) -> None:
        assert video_key("https://www.tiktok.com/@a/video/1?x=1") == video_key("https://tiktok.com/@a/video/1/")
        assert video_key("https://www.facebook.com/watch/?v=123456") == video_key(
            "https://www.facebook.com/NASA/videos/title/123456/"
        )

    def test_dedupe_preserves_order_and_first_form(self) -> None:
        urls = [
            "https://www.tiktok.com/@a/video/2",
            "https://www.tiktok.com/@a/video/1",
            "https://www.tiktok.com/@a/video/2?lang=en",
            "  https://www.tiktok.com/@a/video/1  ",
            "https://www.facebook.com/watch/?v=123456",
            "https://www.facebook.com/reel/123456",
        ]
        assert dedupe_urls(urls) == [
            "https://www.tiktok.com/@a/video/2",
            "https://www.tiktok.com/@a/video/1",
            "https://www.facebook.com/watch/?v=123456",
        ]

    def test_unrecognised_urls_dedupe_by_text(self) -> None:
        assert dedupe_urls(["x", "x", "y/"]) == ["x", "y/"]
