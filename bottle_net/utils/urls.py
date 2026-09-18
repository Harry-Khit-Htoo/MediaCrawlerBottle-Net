"""URL parsing, validation, normalization and de-duplication.

Everything here is pure string processing - no network access - so it is
fast and easy to test. Normalization turns the many URL variants the
platforms use into one canonical form so duplicates can be detected.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qs, urlsplit

from bottle_net.errors import InvalidURLError, UnsupportedURLError


class Platform(StrEnum):
    """Supported platforms."""

    TIKTOK = "tiktok"
    FACEBOOK = "facebook"

    @property
    def display_name(self) -> str:
        """Human-readable platform name."""
        return {"tiktok": "TikTok", "facebook": "Facebook"}[self.value]


TIKTOK_DOMAINS = ("tiktok.com",)
FACEBOOK_DOMAINS = ("facebook.com", "fb.watch", "fb.com")

TIKTOK_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.]{1,30}$")
FACEBOOK_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

_TIKTOK_VIDEO_PATTERNS = (
    re.compile(r"^/@(?P<user>[\w.-]+)/video/(?P<id>\d+)/?$"),
    re.compile(r"^/(?:embed/v2|embed|v)/(?P<id>\d+)(?:\.html)?/?$"),
)
_TIKTOK_SHORT_HOSTS = ("vm.tiktok.com", "vt.tiktok.com")
_TIKTOK_SHORT_PATH = re.compile(r"^/t/[\w-]+/?$")
_TIKTOK_PHOTO_PATH = re.compile(r"^/@[\w.-]+/photo/\d+/?$")
_TIKTOK_PROFILE_PATH = re.compile(r"^/@(?P<user>[\w.-]+)/?$")

_FB_VIDEOS_PATH = re.compile(r"^/(?:[^/]+/)?videos/(?:[^/]+/)*?(?P<id>\d{5,})/?$")
_FB_REEL_PATH = re.compile(r"^/reels?/(?P<id>\d{5,})/?$")
_FB_WATCH_PATH = re.compile(r"^/(?:watch(?:/live)?|video\.php)/?$")
_FB_SHARE_PATH = re.compile(r"^/share/(?:v|r)/[\w-]+/?$")
_FB_POST_PATH = re.compile(r"^/(?:[^/]+/posts/[^/]+|permalink\.php|story\.php)/?$")

# First path segments that are Facebook features rather than Page names.
_FB_RESERVED = frozenset(
    {
        "watch", "reel", "reels", "video.php", "share", "story.php", "permalink.php",
        "groups", "events", "login", "login.php", "photo", "photo.php", "photos",
        "marketplace", "gaming", "help", "settings", "hashtag", "search", "stories",
        "home.php", "messages", "notifications", "friends", "pages", "plugins",
    }
)
# Trailing Page tabs that may be included in a Page URL.
_FB_PAGE_TABS = frozenset({"videos", "reels", "about", "posts", "photos", "live_videos", "featured"})


@dataclass(frozen=True)
class FacebookTarget:
    """A public Facebook Page or profile to crawl."""

    name: str
    url: str
    numeric_profile: bool = False

    def tab_url(self, tab: str) -> str:
        """Return the URL of a Page tab such as ``videos`` or ``reels``."""
        if self.numeric_profile:
            sk = {"videos": "videos", "reels": "reels_tab"}.get(tab, tab)
            return f"{self.url}&sk={sk}"
        return f"{self.url}/{tab}"


def ensure_scheme(value: str) -> str:
    """Add ``https://`` to host-like input such as ``tiktok.com/@user``."""
    value = value.strip()
    if "://" not in value and re.match(r"^(?:[\w-]+\.)+[a-z]{2,}(?:[/?#]|$)", value, re.IGNORECASE):
        return "https://" + value
    return value


def _split(url: str) -> tuple[str, str, str]:
    """Return ``(host, path, query)`` of an http(s) URL, or raise InvalidURLError."""
    text = ensure_scheme(url)
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise InvalidURLError(f"Not a valid web URL: {url.strip() or '(empty)'}")
    return parts.hostname.lower(), parts.path or "/", parts.query


def _host_matches(host: str, domains: Iterable[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def detect_platform(url: str) -> Platform | None:
    """Return the platform a URL belongs to, or ``None`` if unknown."""
    try:
        host, _, _ = _split(url)
    except InvalidURLError:
        return None
    if _host_matches(host, TIKTOK_DOMAINS):
        return Platform.TIKTOK
    if _host_matches(host, FACEBOOK_DOMAINS):
        return Platform.FACEBOOK
    return None


# --------------------------------------------------------------------------- TikTok


def normalize_tiktok_video_url(url: str) -> str:
    """Validate a TikTok video URL and return its canonical form.

    Short links (``vm.tiktok.com/...``) are returned unchanged because they
    can only be resolved over the network.
    """
    host, path, _ = _split(url)
    if not _host_matches(host, TIKTOK_DOMAINS):
        raise InvalidURLError(f"Not a TikTok URL: {url.strip()}")

    if host in _TIKTOK_SHORT_HOSTS and len(path.strip("/")) > 0:
        return f"https://{host}/{path.strip('/')}/"
    if _TIKTOK_SHORT_PATH.match(path):
        return f"https://www.tiktok.com/{path.strip('/')}/"

    for pattern in _TIKTOK_VIDEO_PATTERNS:
        match = pattern.match(path)
        if match:
            user = match.groupdict().get("user")
            vid = match.group("id")
            if user:
                return f"https://www.tiktok.com/@{user}/video/{vid}"
            return f"https://www.tiktok.com/embed/v2/{vid}"

    if _TIKTOK_PHOTO_PATH.match(path):
        raise UnsupportedURLError(
            "This is a TikTok photo post (slideshow), not a video.",
        )
    if _TIKTOK_PROFILE_PATH.match(path):
        raise InvalidURLError(
            "This is a TikTok profile URL, not a video URL.",
            hint="To collect all videos from an account, use: bottle-net tiktok crawl <ACCOUNT>",
        )
    raise InvalidURLError(f"Not a recognised TikTok video URL: {url.strip()}")


def parse_tiktok_account(value: str) -> str:
    """Extract a TikTok username from ``@name``, ``name`` or a profile URL."""
    text = value.strip()
    if not text:
        raise InvalidURLError("No TikTok account given.")

    if "tiktok.com" in text.lower() or "://" in text:
        host, path, _ = _split(text)
        if not _host_matches(host, TIKTOK_DOMAINS):
            raise InvalidURLError(f"Not a TikTok URL: {text}")
        match = _TIKTOK_PROFILE_PATH.match(path)
        if not match:
            if any(p.match(path) for p in _TIKTOK_VIDEO_PATTERNS):
                raise InvalidURLError(
                    "This is a TikTok video URL, not a profile URL.",
                    hint="To download a single video, use: bottle-net tiktok download <URL>",
                )
            raise InvalidURLError(f"Not a TikTok profile URL: {text}")
        text = match.group("user")

    username = text.removeprefix("@")
    if not TIKTOK_USERNAME_RE.match(username):
        raise InvalidURLError(
            f"'{value.strip()}' is not a valid TikTok username.",
            hint="Usernames contain only letters, numbers, underscores and periods (e.g. @example).",
        )
    return username


def tiktok_profile_url(username: str) -> str:
    """Return the public profile URL for a TikTok username."""
    return f"https://www.tiktok.com/@{username}"


def tiktok_username_from_url(url: str) -> str | None:
    """Return the ``@username`` part of a TikTok video URL, if it has one."""
    try:
        host, path, _ = _split(url)
    except InvalidURLError:
        return None
    if not _host_matches(host, TIKTOK_DOMAINS):
        return None
    match = _TIKTOK_VIDEO_PATTERNS[0].match(path)
    return match.group("user") if match else None


# ------------------------------------------------------------------------- Facebook


def _facebook_video_id(path: str, query: str) -> tuple[str, str] | None:
    """Return ``(kind, id)`` for Facebook video/reel paths, if identifiable."""
    if _FB_WATCH_PATH.match(path):
        vid = parse_qs(query).get("v", [""])[0]
        return ("video", vid) if vid.isdigit() else None
    if match := _FB_REEL_PATH.match(path):
        return "reel", match.group("id")
    if match := _FB_VIDEOS_PATH.match(path):
        return "video", match.group("id")
    return None


def facebook_video_url(video_id: str, *, reel: bool = False) -> str:
    """Return the canonical URL for a Facebook video or reel ID."""
    if reel:
        return f"https://www.facebook.com/reel/{video_id}"
    return f"https://www.facebook.com/watch/?v={video_id}"


def normalize_facebook_video_url(url: str) -> str:
    """Validate a Facebook video URL and return its canonical form."""
    host, path, query = _split(url)
    if not _host_matches(host, FACEBOOK_DOMAINS):
        raise InvalidURLError(f"Not a Facebook URL: {url.strip()}")

    if host == "fb.watch":
        code = path.strip("/")
        if not code:
            raise InvalidURLError(f"Incomplete fb.watch link: {url.strip()}")
        return f"https://fb.watch/{code}/"

    ident = _facebook_video_id(path, query)
    if ident:
        kind, vid = ident
        return facebook_video_url(vid, reel=kind == "reel")

    if _FB_SHARE_PATH.match(path):
        return f"https://www.facebook.com/{path.strip('/')}/"
    if _FB_POST_PATH.match(path):
        rest = f"?{query}" if query else ""
        return f"https://www.facebook.com{path}{rest}"

    segments = [s for s in path.split("/") if s]
    if segments and segments[0].lower() not in _FB_RESERVED and len(segments) <= 2:
        raise InvalidURLError(
            "This looks like a Facebook Page or profile URL, not a video URL.",
            hint="To collect all public videos from a Page, use: bottle-net facebook crawl <PAGE_OR_PROFILE>",
        )
    raise InvalidURLError(f"Not a recognised Facebook video URL: {url.strip()}")


def parse_facebook_target(value: str) -> FacebookTarget:
    """Parse a Facebook Page/profile name or URL into a :class:`FacebookTarget`."""
    text = value.strip()
    if not text:
        raise InvalidURLError("No Facebook Page or profile given.")

    if "facebook.com" in text.lower() or "fb.com" in text.lower() or "://" in text:
        host, path, query = _split(text)
        if not _host_matches(host, FACEBOOK_DOMAINS) or host == "fb.watch":
            raise InvalidURLError(f"Not a Facebook Page or profile URL: {text}")

        if _facebook_video_id(path, query) or _FB_SHARE_PATH.match(path):
            raise InvalidURLError(
                "This is a Facebook video URL, not a Page or profile URL.",
                hint="To download a single video, use: bottle-net facebook download <URL>",
            )

        if path.rstrip("/") == "/profile.php":
            profile_id = parse_qs(query).get("id", [""])[0]
            if not profile_id.isdigit():
                raise InvalidURLError(f"Profile URL is missing a numeric id: {text}")
            return FacebookTarget(
                name=profile_id,
                url=f"https://www.facebook.com/profile.php?id={profile_id}",
                numeric_profile=True,
            )

        segments = [s for s in path.split("/") if s]
        if not segments:
            raise InvalidURLError("The URL does not name a Facebook Page or profile.")
        if segments[0].lower() in _FB_RESERVED:
            raise UnsupportedURLError(
                f"'/{segments[0]}' URLs are not supported for crawling.",
                hint="Crawling supports public Facebook Pages and profiles, e.g. https://www.facebook.com/examplepage",
            )
        if len(segments) > 1 and segments[1].lower() not in _FB_PAGE_TABS:
            raise InvalidURLError(f"Not a Facebook Page or profile URL: {text}")
        text = segments[0]

    name = text.removeprefix("@")
    if not FACEBOOK_SLUG_RE.match(name):
        raise InvalidURLError(f"'{value.strip()}' is not a valid Facebook Page name.")
    return FacebookTarget(name=name, url=f"https://www.facebook.com/{name}")


# -------------------------------------------------------------------------- Generic


def normalize_video_url(url: str, platform: Platform) -> str:
    """Validate *url* as a video URL for *platform* and return its canonical form."""
    detected = detect_platform(url)
    if detected is None:
        _split(url)  # raises InvalidURLError for malformed input
        raise UnsupportedURLError(f"Not a {platform.display_name} URL: {url.strip()}")
    if detected is not platform:
        raise InvalidURLError(
            f"This is a {detected.display_name} URL, not a {platform.display_name} URL.",
            hint=f"Use: bottle-net {detected.value} download <URL>",
        )
    if platform is Platform.TIKTOK:
        return normalize_tiktok_video_url(url)
    return normalize_facebook_video_url(url)


def video_key(url: str) -> str:
    """Return a key identifying the video behind *url*, for de-duplication.

    URLs pointing to the same video (different query strings, hosts or
    formats) produce the same key. Unrecognised URLs fall back to the
    trimmed URL itself.
    """
    platform = detect_platform(url)
    try:
        if platform is Platform.TIKTOK:
            canonical = normalize_tiktok_video_url(url)
            match = re.search(r"/(?:video|v2)/(\d+)$", canonical)
            return f"tiktok:{match.group(1)}" if match else canonical
        if platform is Platform.FACEBOOK:
            host, path, query = _split(url)
            ident = _facebook_video_id(path, query)
            return f"facebook:{ident[1]}" if ident else normalize_facebook_video_url(url)
    except (InvalidURLError, UnsupportedURLError):
        pass
    return url.strip().rstrip("/")


def dedupe_urls(urls: Iterable[str]) -> list[str]:
    """Remove duplicates (by :func:`video_key`) while preserving order."""
    seen: set[str] = set()
    unique: list[str] = []
    for url in urls:
        key = video_key(url)
        if key not in seen:
            seen.add(key)
            unique.append(url.strip())
    return unique
