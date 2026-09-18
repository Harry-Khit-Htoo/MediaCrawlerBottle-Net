"""Local web server for the Video Publisher GUI (standard library only).

Security model (the server binds to 127.0.0.1 only):

* **Launch key** - ``bottle-net gui`` opens ``/?key=<random>``; the server
  answers with an HttpOnly, SameSite=Strict session cookie and redirects to
  ``/``. Without that cookie the GUI, API and media are not served, so other
  users or websites cannot use the publisher.
* **CSRF token** - every ``/api`` request must also send the
  ``X-Bottle-Net-Token`` header (the value is embedded in the GUI page).
* **Host check** - requests for any host other than ``127.0.0.1:<port>`` or
  ``localhost:<port>`` are refused (protects against DNS rebinding).
* **Content Security Policy** - only the app's own scripts and styles run.
* OAuth callbacks are protected by the single-use ``state`` value instead of
  the cookie (browsers do not send SameSite=Strict cookies on redirects
  from Google/Facebook).

No API response contains OAuth tokens, client secrets or upload URLs.
"""

from __future__ import annotations

import hmac
import html
import json
import logging
import mimetypes
import re
import secrets
import socket
import sys
import threading
import zoneinfo
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO, cast
from urllib.parse import parse_qs, unquote, urlsplit

from bottle_net.config import load_config
from bottle_net.publisher.app import PublisherApp
from bottle_net.publisher.errors import DuplicateError, PublisherError, ValidationError
from bottle_net.publisher.models import PublishPlatform
from bottle_net.utils.logger import redact_secrets

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).with_name("web")
COOKIE_NAME = "bn_session"
CSRF_HEADER = "X-Bottle-Net-Token"
MAX_JSON_BYTES = 1024 * 1024
CSP = ("default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self'; "
       "script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
       "frame-ancestors 'none'")
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "application/json; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)
    file: Path | None = None
    file_range: tuple[int, int] | None = None


def json_response(data: Any, status: int = 200) -> Response:
    return Response(status, json.dumps(data, ensure_ascii=False).encode("utf-8"))


def error_response(message: str, status: int, code: str = "error", **extra: Any) -> Response:
    return json_response({"error": message, "code": code, **extra}, status)


def html_page(title: str, message: str, *, ok: bool = True, status: int = 200) -> Response:
    icon = "✓" if ok else "!"
    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} - Bottle Net</title><link rel="stylesheet" href="/static/app.css"></head>
<body class="standalone"><main class="standalone-card" role="main">
<p class="standalone-icon {'ok' if ok else 'bad'}" aria-hidden="true">{icon}</p>
<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>
<p class="muted">You can close this tab and return to Bottle Net.</p></main></body></html>"""
    return Response(status, body.encode("utf-8"), "text/html; charset=utf-8")


class Request:
    """The parts of an HTTP request the API handlers need."""

    def __init__(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        self.handler = handler
        self.method = method
        parts = urlsplit(handler.path)
        self.path = unquote(parts.path)
        self.query = {k: v[-1] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        self.headers = handler.headers

    @property
    def length(self) -> int:
        try:
            return max(0, int(self.headers.get("Content-Length") or 0))
        except ValueError:
            return 0

    def json(self) -> dict[str, Any]:
        if self.length > MAX_JSON_BYTES:
            raise ValidationError("The request is too large.")
        raw = self.handler.rfile.read(self.length) if self.length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except ValueError as exc:
            raise ValidationError("The request body is not valid JSON.") from exc
        if not isinstance(data, dict):
            raise ValidationError("The request body must be a JSON object.")
        return data

    def raw(self, limit: int) -> bytes:
        if self.length > limit:
            raise ValidationError("The file is too large.")
        return self.handler.rfile.read(self.length) if self.length else b""


Handler = Callable[[Request, "re.Match[str]"], Response]


class PublisherServer:
    """Serves the GUI and the JSON API for one :class:`PublisherApp`."""

    def __init__(self, app: PublisherApp, *, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.app = app
        self.host = host
        self.port = port
        self.launch_key = secrets.token_urlsafe(24)
        self.csrf_token = secrets.token_urlsafe(32)
        self._sessions: set[str] = set()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.routes = self._build_routes()

    # -------------------------------------------------------------- lifecycle

    def start(self) -> int:
        server = self

        class _Handler(RequestHandler):
            publisher = server

        self._httpd = ExclusiveHTTPServer((self.host, self.port), _Handler)
        self.port = self._httpd.server_address[1]
        self.app.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="gui-server", daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def launch_url(self) -> str:
        """URL that signs the browser in (contains the launch key; keep private)."""
        return f"{self.base_url}/?key={self.launch_key}"

    # --------------------------------------------------------------- security

    def allowed_host(self, host: str | None) -> bool:
        return (host or "").lower() in {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def has_session(self, cookie_header: str | None) -> bool:
        if not cookie_header:
            return False
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header)
        except Exception:  # noqa: BLE001 - malformed cookies mean "no session"
            return False
        morsel = cookie.get(COOKIE_NAME)
        if morsel is None or not morsel.value:
            return False
        return any(hmac.compare_digest(morsel.value, s) for s in list(self._sessions))

    def new_session(self) -> str:
        token = secrets.token_urlsafe(32)
        self._sessions.add(token)
        return token

    # ----------------------------------------------------------------- routing

    def _build_routes(self) -> list[tuple[str, re.Pattern[str], Handler]]:
        api = Api(self)
        table: list[tuple[str, str, Handler]] = [
            ("GET", r"/api/dashboard", api.dashboard),
            ("GET", r"/api/accounts", api.accounts),
            ("POST", r"/api/accounts/(youtube|facebook)/connect", api.connect),
            ("POST", r"/api/accounts/(youtube|facebook)/disconnect", api.disconnect),
            ("POST", r"/api/accounts/facebook/page", api.select_page),
            ("GET", r"/api/videos", api.videos),
            ("POST", r"/api/videos/upload", api.upload_video),
            ("POST", r"/api/videos/import-downloads", api.import_downloads),
            ("GET", r"/api/videos/(\d+)", api.video),
            ("DELETE", r"/api/videos/(\d+)", api.delete_video),
            ("POST", r"/api/videos/(\d+)/preview", api.video_preview),
            ("POST", r"/api/images", api.upload_image),
            ("GET", r"/api/downloads", api.downloads),
            ("POST", r"/api/downloads", api.start_download),
            ("GET", r"/api/jobs", api.jobs),
            ("POST", r"/api/jobs", api.create_job),
            ("GET", r"/api/jobs/(\d+)", api.job),
            ("PATCH", r"/api/jobs/(\d+)", api.update_job),
            ("POST", r"/api/jobs/(\d+)/cancel", api.cancel_job),
            ("POST", r"/api/jobs/(\d+)/run-now", api.run_now),
            ("POST", r"/api/platform-jobs/(\d+)/retry", api.retry),
            ("POST", r"/api/platform-jobs/(\d+)/cancel", api.cancel_platform_job),
            ("POST", r"/api/platform-jobs/(\d+)/publish-again", api.publish_again),
            ("GET", r"/api/history", api.history),
            ("GET", r"/api/settings", api.settings),
            ("PUT", r"/api/settings", api.update_settings),
            ("GET", r"/api/timezones", api.timezones),
            ("GET", r"/api/templates", api.templates),
            ("POST", r"/api/templates", api.save_template),
            ("PUT", r"/api/templates/(\d+)", api.save_template),
            ("DELETE", r"/api/templates/(\d+)", api.delete_template),
            ("GET", r"/api/notifications", api.notifications),
            ("POST", r"/api/notifications/read", api.read_notifications),
        ]
        return [(method, re.compile(f"^{pattern}$"), handler) for method, pattern, handler in table]

    def dispatch(self, request: Request) -> Response:
        if not self.allowed_host(request.headers.get("Host")):
            return error_response("Forbidden host.", 403, "forbidden_host")
        path = request.path

        if path.startswith("/oauth/") and request.method == "GET":
            match = re.fullmatch(r"/oauth/(youtube|facebook)/callback", path)
            if not match:
                return error_response("Not found.", 404, "not_found")
            outcome = self.app.oauth_callback(PublishPlatform(match.group(1)), request.query)
            return html_page(outcome.title, outcome.message, ok=outcome.ok, status=200 if outcome.ok else 400)

        if path.startswith("/static/") and request.method == "GET":
            return self.static(path.removeprefix("/static/"))

        if path == "/" and request.method == "GET":
            key = request.query.get("key")
            if key and hmac.compare_digest(key, self.launch_key):
                session = self.new_session()
                return Response(303, b"", "text/plain", {
                    "Location": "/",
                    "Set-Cookie": f"{COOKIE_NAME}={session}; HttpOnly; SameSite=Strict; Path=/",
                })
            if not self.has_session(request.headers.get("Cookie")):
                return html_page("Open Bottle Net from the command line",
                                 "Run 'bottle-net gui' to open the Video Publisher. It opens this page "
                                 "with a private sign-in link.", ok=False, status=401)
            page = (WEB_DIR / "index.html").read_text(encoding="utf-8").replace("{{CSRF_TOKEN}}", self.csrf_token)
            return Response(200, page.encode("utf-8"), "text/html; charset=utf-8")

        if not self.has_session(request.headers.get("Cookie")):
            return error_response("Not signed in. Run 'bottle-net gui' to open Bottle Net.", 401, "unauthorized")

        if path.startswith("/media/") and request.method == "GET":
            return self.media(request)

        if path.startswith("/api/"):
            if not hmac.compare_digest(request.headers.get(CSRF_HEADER) or "", self.csrf_token):
                return error_response("Missing or invalid request token.", 403, "csrf")
            for method, pattern, handler in self.routes:
                match = pattern.match(path)
                if match and method == request.method:
                    return handler(request, match)
            return error_response("Not found.", 404, "not_found")
        return error_response("Not found.", 404, "not_found")

    def static(self, name: str) -> Response:
        path = (WEB_DIR / name).resolve()
        if WEB_DIR.resolve() not in path.parents or not path.is_file() or path.name == "index.html":
            return error_response("Not found.", 404, "not_found")
        types = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".svg": "image/svg+xml"}
        return Response(200, path.read_bytes(), types.get(path.suffix, "application/octet-stream"))

    def media(self, request: Request) -> Response:
        match = re.fullmatch(r"/media/videos/(\d+)", request.path)
        if match:
            video = self.app.library.get(int(match.group(1)), include_deleted=True)
            path = Path(video.path)
            if not path.is_file():
                return error_response("The video file no longer exists.", 404, "not_found")
            return file_response(path, request.headers.get("Range"))
        match = re.fullmatch(r"/media/images/([\w.-]+)", request.path)
        if match:
            image = self.app.library.image_path(match.group(1))
            if image:
                return file_response(image, None)
        return error_response("Not found.", 404, "not_found")


def file_response(path: Path, range_header: str | None) -> Response:
    """Serve a file, supporting single byte ranges (needed for video seeking)."""
    size = path.stat().st_size
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    headers = {"Accept-Ranges": "bytes"}
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", (range_header or "").strip())
    if match and (match.group(1) or match.group(2)):
        if match.group(1):
            start = int(match.group(1))
            end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
        else:
            start, end = max(0, size - int(match.group(2))), size - 1
        if start >= size or start > end:
            return Response(416, b"", content_type, {"Content-Range": f"bytes */{size}"})
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return Response(206, b"", content_type, headers, file=path, file_range=(start, end))
    return Response(200, b"", content_type, headers, file=path, file_range=(0, size - 1) if size else None)


class ExclusiveHTTPServer(ThreadingHTTPServer):
    """HTTP server that owns its port exclusively.

    ``HTTPServer`` enables ``SO_REUSEADDR``; on Windows that lets a second
    program bind the same port (and receive our requests). Disable it there
    and ask for exclusive use instead, so a second Bottle Net (or any other
    program) cannot take over the port.
    """

    daemon_threads = True
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self) -> None:
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class RequestHandler(BaseHTTPRequestHandler):
    publisher: PublisherServer
    server_version = "BottleNet"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        logger.debug("GUI %s", redact_secrets(format % args))

    def _handle(self, method: str) -> None:
        request = Request(self, method)
        try:
            response = self.publisher.dispatch(request)
        except DuplicateError as exc:
            response = error_response(exc.message, exc.status_code, exc.code, platforms=exc.platforms)
        except PublisherError as exc:
            response = error_response(exc.message, exc.status_code, exc.code)
        except Exception:  # noqa: BLE001 - never expose internals to the browser
            logger.exception("GUI request failed: %s %s", method, redact_secrets(request.path))
            response = error_response("Something went wrong in Bottle Net. Details were written to the log file.",
                                      500, "internal")
        self._send(response)

    def _send(self, response: Response) -> None:
        try:
            self.send_response(response.status)
            for name, value in {**SECURITY_HEADERS, **response.headers}.items():
                self.send_header(name, value)
            self.send_header("Content-Type", response.content_type)
            if response.file is not None:
                start, end = response.file_range or (0, -1)
                length = end - start + 1
                self.send_header("Content-Length", str(max(0, length)))
                self.end_headers()
                if length > 0:
                    with response.file.open("rb") as fh:
                        fh.seek(start)
                        remaining = length
                        while remaining > 0:
                            block = fh.read(min(256 * 1024, remaining))
                            if not block:
                                break
                            self.wfile.write(block)
                            remaining -= len(block)
            else:
                self.send_header("Content-Length", str(len(response.body)))
                self.end_headers()
                self.wfile.write(response.body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # the browser stopped reading (e.g. the video element seeked)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")


class Api:
    """JSON API handlers. They only call the service layer (PublisherApp)."""

    def __init__(self, server: PublisherServer) -> None:
        self.server = server

    @property
    def app(self) -> PublisherApp:
        return self.server.app

    # ------------------------------------------------------------- accounts

    def dashboard(self, req: Request, m: re.Match[str]) -> Response:
        return json_response(self.app.dashboard_json())

    def accounts(self, req: Request, m: re.Match[str]) -> Response:
        return json_response(self.app.accounts_json())

    def connect(self, req: Request, m: re.Match[str]) -> Response:
        return json_response({"auth_url": self.app.connect(PublishPlatform(m.group(1)))})

    def disconnect(self, req: Request, m: re.Match[str]) -> Response:
        self.app.disconnect(PublishPlatform(m.group(1)))
        return json_response(self.app.accounts_json())

    def select_page(self, req: Request, m: re.Match[str]) -> Response:
        self.app.select_facebook_page(str(req.json().get("page_id", "")))
        return json_response(self.app.accounts_json())

    # --------------------------------------------------------------- videos

    def videos(self, req: Request, m: re.Match[str]) -> Response:
        return json_response(self.app.videos_json(req.query.get("search", ""), req.query.get("status", "")))

    def video(self, req: Request, m: re.Match[str]) -> Response:
        return json_response(self.app.video_json(self.app.library.get(int(m.group(1)))))

    def upload_video(self, req: Request, m: re.Match[str]) -> Response:
        name = unquote(req.headers.get("X-Filename") or "")
        if not name:
            raise ValidationError("The file name is missing.")
        video = self.app.library.import_stream(name, cast(BinaryIO, req.handler.rfile), req.length)
        return json_response(self.app.video_json(video), 201)

    def video_preview(self, req: Request, m: re.Match[str]) -> Response:
        duration_header = req.headers.get("X-Duration")
        try:
            duration = float(duration_header) if duration_header else None
        except ValueError as exc:
            raise ValidationError("Invalid duration.") from exc
        image = req.raw(6 * 1024 * 1024) or None
        video = self.app.library.set_preview(int(m.group(1)), image=image, duration=duration)
        return json_response(self.app.video_json(video))

    def delete_video(self, req: Request, m: re.Match[str]) -> Response:
        self.app.library.delete(int(m.group(1)), delete_file=req.query.get("delete_file") == "1",
                                has_active_jobs=self.app.jobs.has_active_jobs)
        return json_response({"deleted": True})

    def import_downloads(self, req: Request, m: re.Match[str]) -> Response:
        folder = load_config().download_directory
        added = self.app.library.import_folder(folder)
        return json_response({"folder": str(folder.resolve()), "added": [self.app.video_json(v) for v in added]})

    def upload_image(self, req: Request, m: re.Match[str]) -> Response:
        return json_response({"name": self.app.library.save_image(req.raw(6 * 1024 * 1024))}, 201)

    def downloads(self, req: Request, m: re.Match[str]) -> Response:
        return json_response(self.app.downloads.tasks())

    def start_download(self, req: Request, m: re.Match[str]) -> Response:
        return json_response(self.app.downloads.start(str(req.json().get("url", ""))), 202)

    # ----------------------------------------------------------------- jobs

    def jobs(self, req: Request, m: re.Match[str]) -> Response:
        if req.query.get("from") and req.query.get("to"):
            try:
                start = datetime.fromisoformat(req.query["from"])
                end = datetime.fromisoformat(req.query["to"])
            except ValueError as exc:
                raise ValidationError("Invalid date range.") from exc
            if start.tzinfo is None or end.tzinfo is None:
                raise ValidationError("Dates must include a timezone.")
            return json_response(self.app.jobs_between(start, end))
        return json_response(self.app.queue_json())

    def job(self, req: Request, m: re.Match[str]) -> Response:
        return json_response(self.app.job_json(self.app.db.get_job(int(m.group(1)))))

    def create_job(self, req: Request, m: re.Match[str]) -> Response:
        return json_response(self.app.job_json(self.app.jobs.create(req.json())), 201)

    def update_job(self, req: Request, m: re.Match[str]) -> Response:
        return json_response(self.app.job_json(self.app.jobs.update(int(m.group(1)), req.json())))

    def cancel_job(self, req: Request, m: re.Match[str]) -> Response:
        return json_response(self.app.job_json(self.app.jobs.cancel_job(int(m.group(1)))))

    def run_now(self, req: Request, m: re.Match[str]) -> Response:
        return json_response(self.app.job_json(self.app.jobs.run_missed_now(int(m.group(1)))))

    def retry(self, req: Request, m: re.Match[str]) -> Response:
        return json_response(self.app.job_json(self.app.jobs.retry_platform_job(int(m.group(1)))))

    def cancel_platform_job(self, req: Request, m: re.Match[str]) -> Response:
        pj_id = int(m.group(1))
        result = self.app.jobs.cancel_platform_job(pj_id)
        job = self.app.db.get_job(self.app.db.get_platform_job(pj_id).job_id)
        return json_response({"result": result, "job": self.app.job_json(job)})

    def publish_again(self, req: Request, m: re.Match[str]) -> Response:
        return json_response(self.app.job_json(self.app.jobs.publish_again(int(m.group(1)))), 201)

    def history(self, req: Request, m: re.Match[str]) -> Response:
        return json_response(self.app.history_json(req.query.get("filter", "all")))

    # -------------------------------------------------------------- settings

    def settings(self, req: Request, m: re.Match[str]) -> Response:
        return json_response(self.app.settings_json())

    def update_settings(self, req: Request, m: re.Match[str]) -> Response:
        self.app.update_settings(req.json())
        return json_response(self.app.settings_json())

    def timezones(self, req: Request, m: re.Match[str]) -> Response:
        return json_response(sorted(zoneinfo.available_timezones()))

    def templates(self, req: Request, m: re.Match[str]) -> Response:
        return json_response(self.app.db.list_templates())

    def save_template(self, req: Request, m: re.Match[str]) -> Response:
        from bottle_net.publisher.templates import parse_tags

        data = req.json()
        name = str(data.get("name", "")).strip()
        if not name:
            raise ValidationError("Give the template a name.")
        template_id = int(m.group(1)) if m.groups() else None
        saved = self.app.db.save_template(template_id, name, str(data.get("title", "")),
                                          str(data.get("description", "")), parse_tags(data.get("tags")))
        return json_response(saved, 201 if template_id is None else 200)

    def delete_template(self, req: Request, m: re.Match[str]) -> Response:
        self.app.db.delete_template(int(m.group(1)))
        return json_response({"deleted": True})

    def notifications(self, req: Request, m: re.Match[str]) -> Response:
        return json_response(self.app.db.notifications(unread_only=req.query.get("unread") == "1"))

    def read_notifications(self, req: Request, m: re.Match[str]) -> Response:
        ids = req.json().get("ids")
        if ids is not None and not (isinstance(ids, list) and all(isinstance(i, int) for i in ids)):
            raise ValidationError("ids must be a list of numbers.")
        self.app.db.mark_notifications_read(ids)
        return json_response({"ok": True})

