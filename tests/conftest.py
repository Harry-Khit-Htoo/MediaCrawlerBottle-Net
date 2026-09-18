"""Shared test fixtures.

No test touches the real TikTok or Facebook: yt-dlp is replaced by
:class:`FakeYDLFactory` and HTTP by :class:`FakeSession`.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from yt_dlp.networking import Response
from yt_dlp.networking.exceptions import HTTPError, TransportError
from yt_dlp.utils import DownloadError, ExtractorError

from bottle_net.config import Config

# ------------------------------------------------------------------ yt-dlp fake


@dataclass
class FakeVideo:
    """A video the fake yt-dlp knows about."""

    id: str
    title: str = "Test video"
    content: bytes = b"\x00" * 2048
    ext: str = "mp4"
    #: Exceptions raised by successive extract_info calls before succeeding.
    extract_errors: list[BaseException] = field(default_factory=list)
    #: Exceptions raised by successive download calls before succeeding.
    download_errors: list[BaseException] = field(default_factory=list)


class FakeYDL:
    """Implements the parts of ``yt_dlp.YoutubeDL`` Bottle Net uses."""

    def __init__(self, factory: FakeYDLFactory, params: dict[str, Any]) -> None:
        import http.cookiejar

        self.factory = factory
        self.params = params
        self.cookiejar = http.cookiejar.CookieJar()
        factory.clients.append(self)

    def __enter__(self) -> FakeYDL:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def extract_info(self, url: str, download: bool = False, process: bool = True) -> dict[str, Any]:
        self.factory.extract_calls.append(url)
        if url in self.factory.playlists:
            playlist = self.factory.playlists[url]
            if isinstance(playlist, list):  # successive results, the last one repeats
                playlist = playlist.pop(0) if len(playlist) > 1 else playlist[0]
            if isinstance(playlist, BaseException):
                raise playlist
            return {"_type": "playlist", "id": "user", "entries": playlist()}
        video = self.factory.videos.get(url)
        if video is None:
            raise DownloadError(f"ERROR: [TikTok] {url}: Video not available, status code 10204")
        if video.extract_errors:
            raise video.extract_errors.pop(0)
        return {
            "id": video.id,
            "title": video.title,
            "ext": video.ext,
            "filesize": len(video.content),
            "webpage_url": url,
            "uploader": "tester",
        }

    def process_ie_result(self, info: dict[str, Any], download: bool = True) -> dict[str, Any]:
        video = next(v for v in self.factory.videos.values() if v.id == info["id"])
        self.factory.download_calls.append(info["id"])
        if video.download_errors:
            raise video.download_errors.pop(0)
        home = Path(self.params["paths"]["home"])
        path = home / f"{info['bottle_net_filename']}.{info['ext']}"
        total = len(video.content)
        for hook in self.params.get("progress_hooks", []):
            hook({"status": "downloading", "downloaded_bytes": total // 2, "total_bytes": total,
                  "filename": f"{path}.part", "speed": 1_000_000.0, "eta": 1})
        path.write_bytes(video.content)
        for hook in self.params.get("progress_hooks", []):
            hook({"status": "finished", "downloaded_bytes": total, "total_bytes": total, "filename": str(path)})
        return {**info, "requested_downloads": [{"filepath": str(path)}]}


class FakeYDLFactory:
    """Callable replacing ``yt_dlp.YoutubeDL`` construction in tests."""

    def __init__(self, videos: list[FakeVideo] | None = None) -> None:
        self.videos: dict[str, FakeVideo] = {}
        for video in videos or []:
            self.add(f"https://www.tiktok.com/@user/video/{video.id}", video)
        self.playlists: dict[str, Any] = {}
        self.extract_calls: list[str] = []
        self.download_calls: list[str] = []
        self.options: list[dict[str, Any]] = []
        self.clients: list[FakeYDL] = []

    def add(self, url: str, video: FakeVideo) -> None:
        """Register *video* under its canonical *url*."""
        self.videos[url] = video

    def __call__(self, params: dict[str, Any]) -> FakeYDL:
        self.options.append(params)
        return FakeYDL(self, params)


# ---------------------------------------------------------------- requests fake


@dataclass
class FakeResponse:
    """Stand-in for :class:`requests.Response`."""

    text: str = ""
    status_code: int = 200
    url: str = ""


class FakeSession:
    """Returns canned responses (or raises canned exceptions) per URL."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        value = self.responses.get(url, FakeResponse(status_code=404, url=url))
        if isinstance(value, list):
            value = value.pop(0) if len(value) > 1 else value[0]
        if isinstance(value, BaseException):
            raise value
        if not value.url:
            value.url = url
        return value


# ------------------------------------------------------------------- helpers


def http_error(status: int) -> DownloadError:
    """A yt-dlp DownloadError wrapping an HTTP error with *status*."""
    response = Response(io.BytesIO(b""), "https://example.invalid", {}, status=status)
    cause = HTTPError(response)
    extractor_error = ExtractorError(f"Unable to download webpage: {cause}", cause=cause)
    return DownloadError(f"ERROR: [TikTok] 1: {extractor_error}", exc_info=(type(extractor_error), extractor_error, None))


def network_error() -> DownloadError:
    """A yt-dlp DownloadError caused by a connection failure."""
    cause = TransportError("[Errno 11001] getaddrinfo failed")
    return DownloadError(f"ERROR: Unable to download webpage: {cause}", exc_info=(type(cause), cause, None))


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """A config writing into a temporary directory, with no delays."""
    return Config(
        download_directory=tmp_path / "downloads",
        output_directory=tmp_path / "output",
        max_retries=2,
        timeout=5,
        request_delay=0,
    )


# ------------------------------------------------------------ publisher fixtures


@dataclass
class PublisherEnv:
    """A publisher app wired to fake Google/Meta servers and a fake clock."""

    app: Any
    http: Any
    youtube: Any
    facebook: Any
    clock: Any

    def connect_youtube(self) -> None:
        from urllib.parse import parse_qs, urlsplit

        from bottle_net.publisher.models import PublishPlatform

        url = self.app.connect(PublishPlatform.YOUTUBE)
        state = parse_qs(urlsplit(url).query)["state"][0]
        outcome = self.app.oauth_callback(PublishPlatform.YOUTUBE, {"code": "google-code", "state": state})
        assert outcome.ok, outcome.message

    def connect_facebook(self, page_id: str = "111") -> None:
        from urllib.parse import parse_qs, urlsplit

        from bottle_net.publisher.models import PublishPlatform

        url = self.app.connect(PublishPlatform.FACEBOOK)
        state = parse_qs(urlsplit(url).query)["state"][0]
        outcome = self.app.oauth_callback(PublishPlatform.FACEBOOK, {"code": "fb-code", "state": state})
        assert outcome.ok, outcome.message
        if self.app.facebook_auth.pending_pages():
            self.app.select_facebook_page(page_id)

    def add_video(self, name: str = "Morning.mp4", content: bytes = b"\x00\x00\x00\x18ftypmp42" + b"v" * 2500,
                  *, source: str = "import") -> Any:
        folder = self.app.paths.root / "incoming"
        folder.mkdir(exist_ok=True)
        path = folder / name
        path.write_bytes(content)
        return self.app.library.import_path(path, source=source)

    def tick(self) -> list[int]:
        return self.app.scheduler.tick()


@pytest.fixture
def pub(tmp_path: Path) -> Any:
    from publisher_fakes import FakeClock, FakeFacebook, FakeHTTP, FakeYouTube, fake_credentials

    from bottle_net.publisher.app import PublisherApp
    from bottle_net.publisher.models import PublishPlatform
    from bottle_net.publisher.paths import PublisherPaths
    from bottle_net.publisher.secrets import MemoryTokenStore

    http = FakeHTTP()
    youtube = FakeYouTube(http)
    facebook = FakeFacebook(http)
    clock = FakeClock()
    holder: dict[str, Any] = {}
    app = PublisherApp(PublisherPaths(tmp_path / "data"), token_store=MemoryTokenStore(),
                       credentials=fake_credentials(), http=http, clock=clock,
                       submit=lambda pj_id: holder["app"].worker.process(pj_id))
    holder["app"] = app
    app.uploaders[PublishPlatform.YOUTUBE].chunk_size = 1024  # several chunks even for tiny test videos
    yield PublisherEnv(app, http, youtube, facebook, clock)
    app.close()
