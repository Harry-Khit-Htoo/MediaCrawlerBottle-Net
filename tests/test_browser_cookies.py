"""Facebook login from an existing browser session (``--browser``).

No real browser data is used: a synthetic Firefox profile is created so that
yt-dlp's real cookie extraction can be exercised safely.
"""

from __future__ import annotations

import http.cookiejar
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeResponse, FakeSession, FakeVideo, FakeYDLFactory

from bottle_net import commands
from bottle_net.browser_cookies import (
    SUPPORTED_BROWSERS,
    BrowserCookieError,
    BrowserSpec,
    load_facebook_cookies,
    parse_browser,
)
from bottle_net.cli import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, main
from bottle_net.config import CONFIG_ENV_VAR, Config
from bottle_net.crawlers import FacebookCrawler
from bottle_net.downloaders import get_downloader as real_get_downloader
from bottle_net.errors import LoginRequiredError

SECRET_XS = "XS-SECRET-VALUE-123"
SECRET_GOOGLE = "GOOGLE-SID-SECRET"
PAGE = "https://www.facebook.com/examplepage"
FB_VIDEO = "https://www.facebook.com/watch/?v=123456"


def make_cookie(domain: str, name: str, value: str) -> http.cookiejar.Cookie:
    return http.cookiejar.Cookie(
        0, name, value, None, False, domain, True, domain.startswith("."), "/", True, True,
        int(time.time()) + 3600, False, None, None, {})


def jar(*cookies: http.cookiejar.Cookie) -> http.cookiejar.CookieJar:
    result = http.cookiejar.CookieJar()
    for cookie in cookies:
        result.set_cookie(cookie)
    return result


def signed_in_extractor(*extra: http.cookiejar.Cookie) -> Any:
    def extractor(browser: str, profile: str | None, logger: Any) -> http.cookiejar.CookieJar:
        return jar(make_cookie(".facebook.com", "c_user", "100001"), make_cookie(".facebook.com", "xs", SECRET_XS),
                   make_cookie(".google.com", "SID", SECRET_GOOGLE), *extra)
    return extractor


def firefox_profile(folder: Path, *, signed_in: bool = True) -> Path:
    """Create a minimal Firefox profile whose cookies.sqlite holds test cookies."""
    folder.mkdir(parents=True)
    db = sqlite3.connect(folder / "cookies.sqlite")
    db.execute("CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, originAttributes TEXT NOT NULL DEFAULT '', "
               "name TEXT, value TEXT, host TEXT, path TEXT, expiry INTEGER, isSecure INTEGER)")
    rows = [(".google.com", "SID", SECRET_GOOGLE), (".facebook.com", "datr", "datr-value")]
    if signed_in:
        rows += [(".facebook.com", "c_user", "100001"), (".facebook.com", "xs", SECRET_XS)]
    for host, name, value in rows:
        db.execute("INSERT INTO moz_cookies (originAttributes, name, value, host, path, expiry, isSecure) "
                   "VALUES ('', ?, ?, ?, '/', ?, 1)", (name, value, host, int(time.time()) + 3600))
    db.commit()
    db.close()
    return folder


# ------------------------------------------------------------------ parsing


class TestParseBrowser:
    @pytest.mark.parametrize("name", SUPPORTED_BROWSERS)
    def test_supported(self, name: str) -> None:
        assert parse_browser(name) == BrowserSpec(name)

    def test_case_and_profile(self) -> None:
        assert parse_browser(" Chrome:Profile 1 ") == BrowserSpec("chrome", "Profile 1")
        assert parse_browser("firefox:C:\\\\x").profile == "C:\\\\x"

    @pytest.mark.parametrize("value", ["netscape", "", "whale", ":profile"])
    def test_unsupported(self, value: str) -> None:
        with pytest.raises(ValueError, match="unsupported browser"):
            parse_browser(value)

    def test_all_requested_browsers_are_supported(self) -> None:
        assert set(SUPPORTED_BROWSERS) == {"chrome", "firefox", "edge", "brave", "chromium", "opera", "vivaldi",
                                           "safari"}


# ------------------------------------------------------------------ loading


class TestLoadCookies:
    def test_real_extraction_from_a_firefox_profile(self, tmp_path: Path) -> None:
        profile = firefox_profile(tmp_path / "ff-profile")
        cookies = load_facebook_cookies(parse_browser(f"firefox:{profile}"))
        assert {c.name for c in cookies} == {"c_user", "xs", "datr"}
        assert all(c.domain.endswith("facebook.com") for c in cookies)  # other sites' cookies are dropped

    def test_real_extraction_without_login(self, tmp_path: Path) -> None:
        profile = firefox_profile(tmp_path / "ff-profile", signed_in=False)
        with pytest.raises(BrowserCookieError, match="No signed-in Facebook session was found in Firefox"):
            load_facebook_cookies(parse_browser(f"firefox:{profile}"))

    def test_only_facebook_cookies_are_kept(self) -> None:
        extra = make_cookie("evilfacebook.com", "c_user", "x")
        cookies = load_facebook_cookies(BrowserSpec("chrome"), extractor=signed_in_extractor(extra))
        assert {(c.domain, c.name) for c in cookies} == {(".facebook.com", "c_user"), (".facebook.com", "xs")}

    def test_locked_database(self) -> None:
        def locked(*args: Any, **kwargs: Any) -> Any:
            raise PermissionError("Could not copy Chrome cookie database. See yt-dlp issue")

        with pytest.raises(BrowserCookieError) as info:
            load_facebook_cookies(BrowserSpec("chrome"), extractor=locked)
        assert "in use" in info.value.message and "Close Chrome completely" in info.value.hint

    def test_missing_profile(self) -> None:
        def missing(*args: Any, **kwargs: Any) -> Any:
            raise FileNotFoundError("could not find edge cookies database in C:/x")

        with pytest.raises(BrowserCookieError, match="No Edge cookie database was found"):
            load_facebook_cookies(BrowserSpec("edge"), extractor=missing)

    def test_decryption_problem_suggests_firefox(self) -> None:
        def undecryptable(browser: str, profile: str | None, logger: Any) -> http.cookiejar.CookieJar:
            logger.warning("failed to decrypt cookie (AES-GCM) because the MAC check failed")
            return jar(make_cookie(".facebook.com", "datr", "d"))

        with pytest.raises(BrowserCookieError) as info:
            load_facebook_cookies(BrowserSpec("chrome"), extractor=undecryptable)
        assert "--browser firefox" in info.value.hint

    def test_values_never_appear_in_errors_or_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger="bottle_net")
        load_facebook_cookies(BrowserSpec("chrome"), extractor=signed_in_extractor())

        def failing(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError(f"weird failure near {SECRET_XS}")

        with pytest.raises(BrowserCookieError) as info:
            load_facebook_cookies(BrowserSpec("chrome"), extractor=failing)
        text = " ".join(r.getMessage() for r in caplog.records) + info.value.message + (info.value.hint or "")
        assert SECRET_XS not in text and SECRET_GOOGLE not in text


# ---------------------------------------------------------- crawl/download


def test_crawler_sends_cookies_only_when_asked(tmp_path: Path) -> None:
    config = Config(request_delay=0, max_retries=0)
    body = FakeResponse('<a href="/examplepage/videos/12345678901/">v</a>')
    anonymous = FakeSession({f"{PAGE}/videos": body, f"{PAGE}/reels": FakeResponse("")})
    FacebookCrawler(config, session=anonymous).crawl(PAGE)
    assert all("cookies" not in kwargs for _, kwargs in anonymous.calls)

    cookies = load_facebook_cookies(BrowserSpec("chrome"), extractor=signed_in_extractor())
    signed_in = FakeSession({f"{PAGE}/videos": body, f"{PAGE}/reels": FakeResponse("")})
    FacebookCrawler(config, session=signed_in, cookies=cookies, browser_name="Chrome").crawl(PAGE)
    assert all(kwargs["cookies"] is cookies for _, kwargs in signed_in.calls)


def test_login_wall_messages() -> None:
    config = Config(request_delay=0, max_retries=0)
    wall = FakeResponse("", url="https://www.facebook.com/login/?next=x")
    session = FakeSession({f"{PAGE}/videos": wall, f"{PAGE}/reels": wall})
    with pytest.raises(LoginRequiredError) as anonymous:
        FacebookCrawler(config, session=session).crawl(PAGE)
    assert "--browser chrome" in anonymous.value.hint and "never asks for your password" in anonymous.value.hint
    cookies = load_facebook_cookies(BrowserSpec("firefox"), extractor=signed_in_extractor())
    with pytest.raises(LoginRequiredError) as signed_in:
        FacebookCrawler(config, session=session, cookies=cookies, browser_name="Firefox").crawl(PAGE)
    assert "using the login from Firefox" in signed_in.value.message
    assert "does not bypass access restrictions" in signed_in.value.hint


def test_downloader_gives_cookies_to_yt_dlp(tmp_path: Path) -> None:
    factory = FakeYDLFactory()
    factory.add(FB_VIDEO, FakeVideo("123456"))
    cookies = load_facebook_cookies(BrowserSpec("chrome"), extractor=signed_in_extractor())
    downloader = real_get_downloader(commands.Platform.FACEBOOK, Config(), ydl_factory=factory,
                                     sleep=lambda _: None, ffmpeg=False, cookies=cookies)
    downloader.download(FB_VIDEO, tmp_path)
    assert {c.name for c in factory.clients[-1].cookiejar} == {"c_user", "xs"}
    anonymous = real_get_downloader(commands.Platform.FACEBOOK, Config(), ydl_factory=factory,
                                    sleep=lambda _: None, ffmpeg=False)
    anonymous.download(FB_VIDEO, tmp_path / "other")
    assert list(factory.clients[-1].cookiejar) == []


# ---------------------------------------------------------------------- CLI


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("COLUMNS", "200")
    (tmp_path / "config.toml").write_text("request_delay = 0\nmax_retries = 0\n", encoding="utf-8")
    env = type("Env", (), {})()
    env.sessions = []
    env.factory = FakeYDLFactory()

    def crawler(config: Any, **kw: Any) -> FacebookCrawler:
        session = FakeSession({f"{PAGE}/videos": FakeResponse('<a href="/examplepage/videos/12345678901/">v</a>'),
                               f"{PAGE}/reels": FakeResponse("")})
        env.sessions.append(session)
        return FacebookCrawler(config, session=session, sleep=lambda _: None, **kw)

    monkeypatch.setattr(commands, "FacebookCrawler", crawler)
    monkeypatch.setattr(commands, "get_downloader", lambda p, c, **kw: real_get_downloader(
        p, c, ydl_factory=env.factory, sleep=lambda _: None, ffmpeg=False, **kw))
    env.profile = firefox_profile(tmp_path / "ff-profile")
    return env


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


def test_crawl_with_browser_session(cli_env: Any, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(capsys, "facebook", "crawl", PAGE, "--browser", f"firefox:{cli_env.profile}", "-q")
    assert code == EXIT_OK and out.strip() == "https://www.facebook.com/watch/?v=12345678901"
    assert err == ""  # --quiet: nothing else, and certainly no cookies
    code, out, err = run(capsys, "--debug", "facebook", "crawl", PAGE, "--browser", f"firefox:{cli_env.profile}",
                         "-o", "links.txt")
    assert code == EXIT_OK
    assert "Using your Facebook login from Firefox" in err and "sent only to facebook.com" in err
    assert "Signed in with your browser's Facebook session" in err
    for secret in (SECRET_XS, SECRET_GOOGLE):
        assert secret not in out and secret not in err
    sent = cli_env.sessions[-1].calls[0][1]["cookies"]
    assert {c.name for c in sent} == {"c_user", "xs", "datr"}


def test_download_with_browser_session(cli_env: Any, capsys: pytest.CaptureFixture[str]) -> None:
    cli_env.factory.add(FB_VIDEO, FakeVideo("123456", title="Members only"))
    code, out, err = run(capsys, "facebook", "download", FB_VIDEO, "--browser", f"firefox:{cli_env.profile}")
    assert code == EXIT_OK and "Download completed" in err and out == ""
    assert {c.name for c in cli_env.factory.clients[-1].cookiejar} == {"c_user", "xs", "datr"}
    assert SECRET_XS not in err


def test_login_required_hints(cli_env: Any, capsys: pytest.CaptureFixture[str]) -> None:
    from yt_dlp.utils import DownloadError

    cli_env.factory.add(FB_VIDEO, FakeVideo("123456", extract_errors=[
        DownloadError("ERROR: [facebook] 123456: You must log in to continue. Use --cookies")] * 2))
    code, _, err = run(capsys, "facebook", "download", FB_VIDEO)
    assert code == EXIT_FAILURE and "[!] Login is required." in err and "--browser chrome" in err
    code, _, err = run(capsys, "facebook", "download", FB_VIDEO, "--browser", f"firefox:{cli_env.profile}")
    assert code == EXIT_FAILURE and "signed in to Firefox" in err and "does not bypass" in err


def test_download_list_with_browser_repeats_option_in_retry(cli_env: Any, capsys: pytest.CaptureFixture[str],
                                                            tmp_path: Path) -> None:
    (tmp_path / "links.txt").write_text(FB_VIDEO + "\n", encoding="utf-8")
    code, _, err = run(capsys, "facebook", "download-list", "links.txt", "--browser", f"firefox:{cli_env.profile}")
    assert code == EXIT_FAILURE  # the fake has no such video
    assert f'--browser "firefox:{cli_env.profile}"' in err.replace(chr(10), "")


def test_browser_errors_are_friendly(cli_env: Any, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    empty = firefox_profile(tmp_path / "logged-out", signed_in=False)
    code, out, err = run(capsys, "facebook", "crawl", PAGE, "--browser", f"firefox:{empty}")
    assert code == EXIT_FAILURE and out == ""
    assert "[!] Could not use your browser's Facebook login." in err
    assert "log in" in err and "Traceback" not in err


def test_browser_option_is_facebook_only(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["tiktok", "crawl", "@user", "--browser", "chrome"])
    assert info.value.code == EXIT_USAGE
    with pytest.raises(SystemExit):
        main(["facebook", "-h"])
    out = capsys.readouterr().out
    for text in ("Facebook authentication", "does NOT ask for or store your Facebook password",
                 '--browser chrome', "Passwords are never requested.", "vivaldi", "safari",
                 "Authentication does not bypass private content or access restrictions."):
        assert text in out
