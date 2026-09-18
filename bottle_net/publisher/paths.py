"""Where the publisher keeps its data (database, imported videos, logs)."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

DATA_DIR_ENV_VAR = "BOTTLE_NET_DATA_DIR"


def default_data_dir() -> Path:
    """Return the per-user data folder for the publisher.

    ``BOTTLE_NET_DATA_DIR`` overrides it. Otherwise:
    Windows ``%LOCALAPPDATA%\\bottle-net``, macOS
    ``~/Library/Application Support/bottle-net``, Linux
    ``$XDG_DATA_HOME/bottle-net`` (``~/.local/share/bottle-net``).
    """
    if os.environ.get(DATA_DIR_ENV_VAR):
        return Path(os.environ[DATA_DIR_ENV_VAR]).expanduser()
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "bottle-net"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "bottle-net"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "bottle-net"


@dataclass(frozen=True)
class PublisherPaths:
    """All publisher locations, derived from one root folder."""

    root: Path

    @property
    def database(self) -> Path:
        return self.root / "publisher.db"

    @property
    def videos(self) -> Path:
        """Default folder for videos added through the GUI."""
        return self.root / "videos"

    @property
    def thumbnails(self) -> Path:
        return self.root / "thumbnails"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def log_file(self) -> Path:
        return self.logs / "publisher.log"

    @property
    def token_file(self) -> Path:
        """Fallback token file, used only when no OS credential store exists."""
        return self.root / "credentials" / "tokens.json"

    @property
    def env_file(self) -> Path:
        """Optional file with OAuth client IDs/secrets (``KEY=value`` lines)."""
        return self.root / ".env"

    @property
    def lock_file(self) -> Path:
        return self.root / "publisher.lock"

    def ensure(self) -> PublisherPaths:
        """Create the folders the publisher writes to."""
        for folder in (self.root, self.videos, self.thumbnails, self.logs):
            folder.mkdir(parents=True, exist_ok=True)
        return self
