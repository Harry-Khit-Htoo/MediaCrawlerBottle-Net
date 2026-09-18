"""Google OAuth 2.0 for YouTube (installed-app flow with PKCE and a loopback redirect).

The user signs in on Google's own page. Bottle Net receives an authorization
code at ``http://127.0.0.1:<port>/oauth/youtube/callback`` and exchanges it
for tokens, which are kept in the token store (never in the database, the UI
or the logs).
"""

from __future__ import annotations

import base64
import json
import logging
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

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
SCOPES = (UPLOAD_SCOPE, "https://www.googleapis.com/auth/youtube.readonly", "openid", "email")
#: Refresh the access token when it expires within this many seconds.
REFRESH_MARGIN = 300


def _email_from_id_token(id_token: str | None) -> str:
    """Read the email claim of an ID token received directly from Google's token endpoint.

    The token comes straight from Google over TLS in the code exchange, so
    (per Google's documentation) its signature does not need to be verified
    to display the account name.
    """
    if not id_token or id_token.count(".") != 2:
        return ""
    payload = id_token.split(".")[1]
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, UnicodeDecodeError):
        return ""
    return str(data.get("email", "")) if isinstance(data, dict) else ""


class YouTubeAuth:
    """Connects a YouTube channel and supplies fresh access tokens."""

    platform = PublishPlatform.YOUTUBE

    def __init__(self, credentials: CredentialsProvider, tokens: TokenStore, http: Any,
                 clock: Callable[[], float] = time.time) -> None:
        self._credentials = credentials
        self._tokens = tokens
        self._http = http
        self._clock = clock
        self._lock = threading.Lock()

    def client(self) -> OAuthClient:
        client = self._credentials.client(self.platform)
        if client is None:
            raise NotConfiguredError("YouTube is not set up yet. " + self._credentials.setup_hint(self.platform),
                                     code="not_configured")
        return client

    def is_configured(self) -> bool:
        return self._credentials.client(self.platform) is not None

    def is_connected(self) -> bool:
        data = self._tokens.get(self.platform)
        return bool(data and data.get("refresh_token"))

    def authorization_url(self, redirect_uri: str, state: str, code_challenge: str) -> str:
        """The Google sign-in page the user's browser is sent to."""
        params = {
            "client_id": self.client().client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",  # ask for a refresh token
            "prompt": "consent select_account",
            "include_granted_scopes": "true",
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def complete(self, code: str, redirect_uri: str, code_verifier: str) -> AccountInfo:
        """Exchange the authorization code for tokens and look up the channel."""
        client = self.client()
        response = send(self._http, "YouTube", "POST", TOKEN_URL, data={
            "code": code, "client_id": client.client_id, "client_secret": client.client_secret,
            "redirect_uri": redirect_uri, "grant_type": "authorization_code", "code_verifier": code_verifier,
        })
        data = json_body(response)
        if response.status_code != 200 or "access_token" not in data:
            logger.warning("Google token exchange failed: %s", response_detail(response))
            raise PublisherError("Google did not accept the sign-in. Please try connecting again.", code="oauth")
        granted = set(str(data.get("scope", "")).split())
        if UPLOAD_SCOPE not in granted:
            raise PublisherError("Bottle Net needs permission to upload videos to YouTube. Connect again and "
                                 "allow all requested permissions.", code="oauth_scope")
        if not data.get("refresh_token"):
            raise PublisherError("Google did not grant long-term access. Remove Bottle Net from "
                                 "https://myaccount.google.com/permissions and connect again.", code="oauth")
        self._store(data, refresh_token=data["refresh_token"])
        email = _email_from_id_token(data.get("id_token"))
        channel = self._channel()
        if channel is None:
            self._tokens.delete(self.platform)
            raise PublisherError("This Google account has no YouTube channel. Create a channel on YouTube, "
                                 "then connect again.", code="no_channel")
        title = channel.get("snippet", {}).get("title", "")
        return AccountInfo(display_name=email or title, detail=title, remote_id=str(channel.get("id", "")))

    def _store(self, data: dict[str, Any], *, refresh_token: str) -> None:
        self._tokens.set(self.platform, {
            "access_token": data["access_token"],
            "refresh_token": refresh_token,
            "expires_at": self._clock() + float(data.get("expires_in", 3600)),
            "scope": data.get("scope", ""),
        })

    def _channel(self) -> dict[str, Any] | None:
        response = send(self._http, "YouTube", "GET", CHANNELS_URL, params={"part": "snippet", "mine": "true"},
                        headers=self.auth_header())
        items = json_body(response).get("items") if response.status_code == 200 else None
        return items[0] if items else None

    def access_token(self, *, force_refresh: bool = False) -> str:
        """A valid access token, refreshed with the refresh token when needed."""
        with self._lock:
            data = self._tokens.get(self.platform)
            if not data or not data.get("refresh_token"):
                raise self._reconnect_error("YouTube is not connected.")
            if not force_refresh and data.get("access_token") and data.get("expires_at", 0) - self._clock() > REFRESH_MARGIN:
                return str(data["access_token"])
            client = self.client()
            response = send(self._http, "YouTube", "POST", TOKEN_URL, data={
                "client_id": client.client_id, "client_secret": client.client_secret,
                "refresh_token": data["refresh_token"], "grant_type": "refresh_token",
            })
            body = json_body(response)
            if response.status_code == 200 and body.get("access_token"):
                self._store(body, refresh_token=body.get("refresh_token") or data["refresh_token"])
                return str(body["access_token"])
            if response.status_code >= 500 or response.status_code == 429:
                raise PublishError(friendly("YouTube", "Google's sign-in service had a temporary problem.",
                                            "Bottle Net will try again automatically."),
                                   retryable=True, code="server_error", detail=response_detail(response))
            raise self._reconnect_error("Your YouTube connection has expired or was revoked.",
                                        detail=response_detail(response))

    def auth_header(self, *, force_refresh: bool = False) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token(force_refresh=force_refresh)}"}

    def disconnect(self) -> None:
        """Revoke Bottle Net's access at Google (best effort) and forget the tokens."""
        data = self._tokens.get(self.platform)
        if data and data.get("refresh_token"):
            try:
                send(self._http, "YouTube", "POST", REVOKE_URL, data={"token": data["refresh_token"]}, timeout=15)
            except PublishError:
                logger.info("Could not revoke the YouTube token; it was removed locally anyway.")
        self._tokens.delete(self.platform)

    @staticmethod
    def _reconnect_error(explanation: str, detail: str | None = None) -> PublishError:
        return PublishError(friendly("YouTube", explanation, "Reconnect your YouTube account in Accounts and try again."),
                            code="auth", detail=detail, reconnect=True)
