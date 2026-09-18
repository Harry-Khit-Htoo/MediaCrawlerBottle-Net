"""OAuth client credentials and token storage.

**Client credentials** (the app's OAuth client ID/secret from Google Cloud
Console and the Meta App Dashboard) are read from environment variables,
or from an optional ``.env`` file in the publisher data folder:

* ``BOTTLE_NET_YOUTUBE_CLIENT_ID`` / ``BOTTLE_NET_YOUTUBE_CLIENT_SECRET``
  (or ``BOTTLE_NET_YOUTUBE_CLIENT_SECRETS_FILE`` pointing at the JSON file
  Google provides for a "Desktop app" OAuth client)
* ``BOTTLE_NET_FACEBOOK_APP_ID`` / ``BOTTLE_NET_FACEBOOK_APP_SECRET``

They are never sent to the browser, printed or logged.

**User tokens** (access/refresh tokens obtained through OAuth) are stored
separately from the job database, in the operating system's credential
store via ``keyring`` (Windows Credential Manager, macOS Keychain, Linux
Secret Service). Only if no credential store is available, a fallback file
readable only by the current user is used. Passwords are never involved:
users sign in on Google's and Meta's own pages.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from bottle_net.publisher.models import PublishPlatform

logger = logging.getLogger(__name__)

KEYRING_SERVICE = "bottle-net-publisher"

ENV_YOUTUBE_ID = "BOTTLE_NET_YOUTUBE_CLIENT_ID"
ENV_YOUTUBE_SECRET = "BOTTLE_NET_YOUTUBE_CLIENT_SECRET"
ENV_YOUTUBE_FILE = "BOTTLE_NET_YOUTUBE_CLIENT_SECRETS_FILE"
ENV_FACEBOOK_ID = "BOTTLE_NET_FACEBOOK_APP_ID"
ENV_FACEBOOK_SECRET = "BOTTLE_NET_FACEBOOK_APP_SECRET"


@dataclass(frozen=True)
class OAuthClient:
    """An OAuth client registration. The secret never appears in ``repr``."""

    client_id: str
    client_secret: str = field(repr=False)
    source: str = "environment"


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a simple ``KEY=value`` file (``#`` comments, optional quotes)."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


class CredentialsProvider:
    """Finds the OAuth client credentials for each platform."""

    def __init__(self, env_file: Path | None = None, environ: dict[str, str] | None = None) -> None:
        self._env_file = env_file
        self._environ = environ

    def _values(self) -> dict[str, str]:
        values = read_env_file(self._env_file) if self._env_file else {}
        values.update(os.environ if self._environ is None else self._environ)
        return values

    def client(self, platform: PublishPlatform) -> OAuthClient | None:
        """The OAuth client for *platform*, or None if not configured."""
        values = self._values()
        if platform is PublishPlatform.YOUTUBE:
            client_id, secret = values.get(ENV_YOUTUBE_ID, ""), values.get(ENV_YOUTUBE_SECRET, "")
            if (not client_id or not secret) and values.get(ENV_YOUTUBE_FILE):
                return _client_from_google_json(Path(values[ENV_YOUTUBE_FILE]).expanduser())
        else:
            client_id, secret = values.get(ENV_FACEBOOK_ID, ""), values.get(ENV_FACEBOOK_SECRET, "")
        if client_id.strip() and secret.strip():
            return OAuthClient(client_id.strip(), secret.strip())
        return None

    def setup_hint(self, platform: PublishPlatform) -> str:
        """Explain how to configure *platform* (names only, never values)."""
        if platform is PublishPlatform.YOUTUBE:
            return (f"Set {ENV_YOUTUBE_ID} and {ENV_YOUTUBE_SECRET} (or {ENV_YOUTUBE_FILE}) "
                    "to the OAuth client of a Google Cloud project with the YouTube Data API enabled.")
        return (f"Set {ENV_FACEBOOK_ID} and {ENV_FACEBOOK_SECRET} to the App ID and App Secret "
                "of a Meta app with Facebook Login.")


def _client_from_google_json(path: Path) -> OAuthClient | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Could not read the Google client secrets file configured in %s", ENV_YOUTUBE_FILE)
        return None
    section = data.get("installed") or data.get("web") or {}
    if section.get("client_id") and section.get("client_secret"):
        return OAuthClient(section["client_id"], section["client_secret"], source="client secrets file")
    return None


# ------------------------------------------------------------------ token store


class TokenStore(Protocol):
    """Where OAuth tokens are kept."""

    description: str

    def get(self, platform: PublishPlatform) -> dict[str, Any] | None: ...

    def set(self, platform: PublishPlatform, data: dict[str, Any]) -> None: ...

    def delete(self, platform: PublishPlatform) -> None: ...


class MemoryTokenStore:
    """Keeps tokens in memory only (tests)."""

    description = "memory"

    def __init__(self) -> None:
        self._data: dict[PublishPlatform, dict[str, Any]] = {}

    def get(self, platform: PublishPlatform) -> dict[str, Any] | None:
        value = self._data.get(platform)
        return dict(value) if value else None

    def set(self, platform: PublishPlatform, data: dict[str, Any]) -> None:
        self._data[platform] = dict(data)

    def delete(self, platform: PublishPlatform) -> None:
        self._data.pop(platform, None)


class KeyringTokenStore:
    """Stores tokens in the operating system's credential store."""

    description = "operating system credential store"

    def __init__(self, service: str = KEYRING_SERVICE) -> None:
        import keyring

        self._keyring = keyring
        self._service = service

    def get(self, platform: PublishPlatform) -> dict[str, Any] | None:
        value = self._keyring.get_password(self._service, platform.value)
        return json.loads(value) if value else None

    def set(self, platform: PublishPlatform, data: dict[str, Any]) -> None:
        self._keyring.set_password(self._service, platform.value, json.dumps(data, separators=(",", ":")))

    def delete(self, platform: PublishPlatform) -> None:
        try:
            self._keyring.delete_password(self._service, platform.value)
        except Exception:  # noqa: BLE001 - nothing stored is fine
            pass


class FileTokenStore:
    """Fallback: a JSON file readable only by the current user."""

    description = "protected file"

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    def get(self, platform: PublishPlatform) -> dict[str, Any] | None:
        with self._lock:
            return self._read().get(platform.value)

    def set(self, platform: PublishPlatform, data: dict[str, Any]) -> None:
        with self._lock:
            current = self._read()
            current[platform.value] = data
            self._write(current)

    def delete(self, platform: PublishPlatform) -> None:
        with self._lock:
            current = self._read()
            if current.pop(platform.value, None) is not None:
                self._write(current)


def default_token_store(fallback_file: Path) -> TokenStore:
    """Use the OS credential store when one is available, else a protected file."""
    try:
        import keyring
        from keyring.backends import fail

        backend = keyring.get_keyring()
        if not isinstance(backend, fail.Keyring) and getattr(backend, "priority", 1) > 0:
            return KeyringTokenStore()
    except Exception:  # noqa: BLE001 - any keyring problem means: use the file
        logger.debug("No usable OS credential store; using a protected token file", exc_info=True)
    return FileTokenStore(fallback_file)
