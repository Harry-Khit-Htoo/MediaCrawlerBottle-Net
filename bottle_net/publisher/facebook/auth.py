"""Facebook Login (Meta OAuth) and Page selection.

The user signs in on Facebook's own page and grants Page permissions.
Bottle Net exchanges the returned code for a long-lived user token, lists
the Pages the user can post to, and after the user picks one stores that
Page's access token. Videos are published to Pages only, never to personal
profiles.

The redirect URI (``http://localhost:<port>/oauth/facebook/callback``) must
be listed under "Valid OAuth Redirect URIs" in the Meta app settings.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from bottle_net.publisher.errors import NotConfiguredError, PublisherError, PublishError, friendly
from bottle_net.publisher.models import PublishPlatform
from bottle_net.publisher.platform import AccountInfo, json_body, response_detail, send
from bottle_net.publisher.secrets import CredentialsProvider, OAuthClient, TokenStore

logger = logging.getLogger(__name__)

GRAPH_VERSION_ENV = "BOTTLE_NET_FACEBOOK_GRAPH_VERSION"
SCOPES = ("pages_show_list", "pages_read_engagement", "pages_manage_posts", "publish_video")


def graph_version() -> str:
    """Graph API version path segment, e.g. ``"v23.0/"``.

    Empty by default, which uses the version configured for the Meta app.
    Set ``BOTTLE_NET_FACEBOOK_GRAPH_VERSION`` to pin one.
    """
    version = os.environ.get(GRAPH_VERSION_ENV, "").strip().strip("/")
    return f"{version}/" if version else ""


def graph_url(path: str = "") -> str:
    return f"https://graph.facebook.com/{graph_version()}{path}"


def dialog_url() -> str:
    return f"https://www.facebook.com/{graph_version()}dialog/oauth"


class FacebookAuth:
    """Connects a Facebook Page and supplies its Page access token."""

    platform = PublishPlatform.FACEBOOK

    def __init__(self, credentials: CredentialsProvider, tokens: TokenStore, http: Any,
                 clock: Callable[[], float] = time.time) -> None:
        self._credentials = credentials
        self._tokens = tokens
        self._http = http
        self._clock = clock
        self._lock = threading.Lock()
        #: Pages waiting for the user's choice (kept in memory only).
        self._pending: dict[str, Any] | None = None

    def client(self) -> OAuthClient:
        client = self._credentials.client(self.platform)
        if client is None:
            raise NotConfiguredError("Facebook is not set up yet. " + self._credentials.setup_hint(self.platform),
                                     code="not_configured")
        return client

    def is_configured(self) -> bool:
        return self._credentials.client(self.platform) is not None

    def is_connected(self) -> bool:
        data = self._tokens.get(self.platform)
        return bool(data and data.get("page_token") and data.get("page_id"))

    def authorization_url(self, redirect_uri: str, state: str) -> str:
        params = {
            "client_id": self.client().client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "response_type": "code",
            "scope": ",".join(SCOPES),
            "auth_type": "rerequest",
        }
        return f"{dialog_url()}?{urlencode(params)}"

    def complete(self, code: str, redirect_uri: str) -> list[dict[str, str]]:
        """Exchange the code for a long-lived user token and list the user's Pages."""
        client = self.client()
        response = send(self._http, "Facebook", "GET", graph_url("oauth/access_token"), params={
            "client_id": client.client_id, "client_secret": client.client_secret,
            "redirect_uri": redirect_uri, "code": code})
        short = json_body(response).get("access_token")
        if response.status_code != 200 or not short:
            logger.warning("Facebook code exchange failed: %s", response_detail(response))
            raise PublisherError("Facebook did not accept the sign-in. Please try connecting again.", code="oauth")

        response = send(self._http, "Facebook", "GET", graph_url("oauth/access_token"), params={
            "grant_type": "fb_exchange_token", "client_id": client.client_id,
            "client_secret": client.client_secret, "fb_exchange_token": short})
        body = json_body(response)
        user_token = body.get("access_token") or short
        expires_at = self._clock() + float(body.get("expires_in", 60 * 24 * 3600))

        response = send(self._http, "Facebook", "GET", graph_url("me/accounts"), params={
            "fields": "id,name,access_token,tasks", "limit": "100", "access_token": user_token})
        if response.status_code != 200:
            logger.warning("Listing Facebook Pages failed: %s", response_detail(response))
            raise PublisherError("Could not read your Facebook Pages. Connect again and allow access to your Pages.",
                                 code="oauth_scope")
        pages = {}
        for page in json_body(response).get("data", []):
            tasks = page.get("tasks")
            if page.get("access_token") and (tasks is None or "CREATE_CONTENT" in tasks):
                pages[str(page["id"])] = {"name": page.get("name", ""), "token": page["access_token"]}
        if not pages:
            raise PublisherError("No Facebook Pages you can publish to were found. Bottle Net publishes to "
                                 "Facebook Pages (not personal profiles); make sure you manage a Page and "
                                 "allowed access to it.", code="no_pages")
        with self._lock:
            self._pending = {"user_token": user_token, "user_expires_at": expires_at, "pages": pages}
        return self.pending_pages()

    def pending_pages(self) -> list[dict[str, str]]:
        """Pages waiting to be chosen (names and IDs only, never tokens)."""
        with self._lock:
            pending = self._pending
        if not pending:
            return []
        return [{"id": page_id, "name": page["name"]} for page_id, page in pending["pages"].items()]

    def select_page(self, page_id: str) -> AccountInfo:
        with self._lock:
            pending = self._pending
            if not pending or page_id not in pending["pages"]:
                raise PublisherError("That Page is not available. Connect Facebook again.", code="oauth_page")
            page = pending["pages"][page_id]
            self._tokens.set(self.platform, {
                "user_token": pending["user_token"], "user_expires_at": pending["user_expires_at"],
                "page_id": page_id, "page_token": page["token"],
            })
            self._pending = None
        return AccountInfo(display_name=page["name"], detail="Facebook Page", remote_id=page_id)

    def page_credentials(self) -> tuple[str, str]:
        """``(page_id, page_access_token)`` of the connected Page."""
        data = self._tokens.get(self.platform)
        if not data or not data.get("page_token") or not data.get("page_id"):
            raise PublishError(friendly("Facebook", "No Facebook Page is connected.",
                                        "Connect Facebook in Accounts and choose a Page."),
                               code="auth", reconnect=True)
        return str(data["page_id"]), str(data["page_token"])

    def disconnect(self) -> None:
        """Remove Bottle Net's permissions at Facebook (best effort) and forget the tokens."""
        data = self._tokens.get(self.platform)
        if data and data.get("user_token"):
            try:
                send(self._http, "Facebook", "DELETE", graph_url("me/permissions"),
                     params={"access_token": data["user_token"]}, timeout=15)
            except PublishError:
                logger.info("Could not revoke Facebook permissions; the token was removed locally anyway.")
        self._tokens.delete(self.platform)
        with self._lock:
            self._pending = None
