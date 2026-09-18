"""Use the Facebook login of a browser the user is already signed in to.

Some Facebook Pages and videos are only visible to signed-in users. With
``--browser chrome`` (or firefox, edge, ...), Bottle Net reads that
browser's cookies **locally** with yt-dlp's cookie extraction, keeps only
the ``facebook.com`` cookies, and sends them only to ``facebook.com`` - the
same thing the browser itself does.

Guarantees:

* Passwords are never requested or read.
* Cookie values are never printed, logged, written to disk or sent anywhere
  except to facebook.com over HTTPS.
* Only content the signed-in account is allowed to see becomes accessible;
  private content and access restrictions are not bypassed.
"""

from __future__ import annotations

import http.cookiejar
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from bottle_net.errors import BottleNetError

logger = logging.getLogger(__name__)

#: Browsers yt-dlp can read cookies from (the names users type).
SUPPORTED_BROWSERS = ("chrome", "firefox", "edge", "brave", "chromium", "opera", "vivaldi", "safari")
BROWSER_NAMES = {"chrome": "Chrome", "firefox": "Firefox", "edge": "Edge", "brave": "Brave",
                 "chromium": "Chromium", "opera": "Opera", "vivaldi": "Vivaldi", "safari": "Safari"}
FACEBOOK_DOMAINS = ("facebook.com",)
#: Present only when a Facebook account is signed in.
FACEBOOK_LOGIN_COOKIES = ("c_user", "xs")

Extractor = Callable[..., http.cookiejar.CookieJar]


class BrowserCookieError(BottleNetError):
    """The browser's Facebook login could not be used."""

    label = "Could not use your browser's Facebook login"


@dataclass(frozen=True)
class BrowserSpec:
    """A browser, optionally with a profile name or folder (``chrome:Profile 1``)."""

    browser: str
    profile: str | None = None

    @property
    def display_name(self) -> str:
        name = BROWSER_NAMES[self.browser]
        return f"{name} ({self.profile})" if self.profile else name


def parse_browser(value: str) -> BrowserSpec:
    """Parse ``BROWSER`` or ``BROWSER:PROFILE``; raises ValueError for unknown browsers."""
    name, _, profile = value.strip().partition(":")
    name = name.strip().lower()
    if name not in SUPPORTED_BROWSERS:
        raise ValueError(f"unsupported browser '{name}' (choose from {', '.join(SUPPORTED_BROWSERS)})")
    return BrowserSpec(name, profile.strip() or None)


def _domain_matches(domain: str, domains: Iterable[str]) -> bool:
    domain = domain.lstrip(".").lower()
    return any(domain == d or domain.endswith("." + d) for d in domains)


class _QuietLogger:
    """Receives yt-dlp's cookie-extraction messages (which never contain values)."""

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def debug(self, message: str, only_once: bool = False) -> None:
        logger.debug("cookies: %s", message)

    def info(self, message: str) -> None:
        logger.debug("cookies: %s", message)

    def warning(self, message: str, only_once: bool = False) -> None:
        self.warnings.append(message)
        logger.debug("cookies warning: %s", message)

    def error(self, message: str) -> None:
        self.warnings.append(message)
        logger.debug("cookies error: %s", message)


def _yt_dlp_extractor(browser: str, profile: str | None, logger: Any) -> http.cookiejar.CookieJar:
    from yt_dlp.cookies import extract_cookies_from_browser

    return extract_cookies_from_browser(browser, profile, logger=logger)


def load_facebook_cookies(spec: BrowserSpec, *, extractor: Extractor = _yt_dlp_extractor) -> http.cookiejar.CookieJar:
    """Return a cookie jar holding only the Facebook cookies of *spec*'s browser.

    Raises :class:`BrowserCookieError` with a readable explanation if the
    cookies cannot be read or the browser is not signed in to Facebook.
    """
    name = spec.display_name
    quiet = _QuietLogger()
    try:
        source = extractor(spec.browser, spec.profile, logger=quiet)
    except Exception as exc:  # noqa: BLE001 - every failure gets a friendly message
        raise _extraction_error(spec, str(exc)) from None

    jar = http.cookiejar.CookieJar()
    for cookie in source:
        if _domain_matches(cookie.domain, FACEBOOK_DOMAINS):
            jar.set_cookie(cookie)
    names = {cookie.name for cookie in jar}
    logger.debug("Using %d Facebook cookies from %s (values are never shown)", len(names), name)
    if not all(required in names for required in FACEBOOK_LOGIN_COOKIES):
        decrypt_problem = any("decrypt" in w.lower() for w in quiet.warnings)
        raise BrowserCookieError(
            f"No signed-in Facebook session was found in {name}.",
            hint=(f"Open facebook.com in {BROWSER_NAMES[spec.browser]} and log in, then run the command again."
                  + (f"\n{BROWSER_NAMES[spec.browser]} protects its cookies in a way other programs cannot always "
                     "read (common with Chrome on Windows). If this keeps happening, log in to Facebook in "
                     "Firefox and use --browser firefox." if decrypt_problem else "")),
        )
    return jar


def _extraction_error(spec: BrowserSpec, message: str) -> BrowserCookieError:
    name = BROWSER_NAMES[spec.browser]
    lower = message.lower()
    if "could not copy" in lower or "database is locked" in lower or "permission denied" in lower:
        return BrowserCookieError(f"{name}'s cookie database is in use and could not be read.",
                                  hint=f"Close {name} completely (all windows), then run the command again.")
    if "could not find" in lower or "no such file" in lower or "not found" in lower:
        where = f" (profile '{spec.profile}')" if spec.profile else ""
        return BrowserCookieError(f"No {name} cookie database was found{where}.",
                                  hint=f"Check that {name} is installed and has been used on this computer. "
                                       "For another profile use --browser BROWSER:PROFILE.")
    if "unsupported platform" in lower or "only supported on" in lower:
        return BrowserCookieError(f"Reading {name} cookies is not supported on this operating system.")
    return BrowserCookieError(f"The cookies of {name} could not be read.",
                              hint="Run with --debug for technical details (cookie values are never shown).")
