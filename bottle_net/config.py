"""Optional TOML configuration.

Bottle Net Tool works without any configuration. When a ``config.toml`` file
is found, its values override the defaults. Lookup order:

1. The path given with ``--config``.
2. The ``BOTTLE_NET_CONFIG`` environment variable.
3. ``config.toml`` in the current working directory.
4. The per-user config directory
   (``%APPDATA%\\bottle-net\\config.toml`` on Windows,
   ``$XDG_CONFIG_HOME/bottle-net/config.toml`` or
   ``~/.config/bottle-net/config.toml`` elsewhere).

Settings may be written at the top level of the file or inside a
``[bottle_net]`` table.
"""

from __future__ import annotations

import logging
import os
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bottle_net.errors import ConfigError
from bottle_net.utils.urls import Platform

logger = logging.getLogger(__name__)

CONFIG_FILENAME = "config.toml"
CONFIG_ENV_VAR = "BOTTLE_NET_CONFIG"
CONFIG_TABLE = "bottle_net"


@dataclass(frozen=True)
class Config:
    """Runtime settings for Bottle Net Tool."""

    download_directory: Path = Path("downloads")
    output_directory: Path = Path("output")
    max_retries: int = 3
    timeout: float = 30.0
    request_delay: float = 1.0
    source: Path | None = field(default=None, compare=False)

    def platform_download_dir(self, platform: Platform) -> Path:
        """Return the default download folder for *platform*."""
        return self.download_directory / platform.value


# name -> (accepted types, validator, description of valid values)
_SETTINGS: dict[str, tuple[tuple[type, ...], Any, str]] = {
    "download_directory": ((str,), lambda v: bool(v.strip()), "a non-empty path string"),
    "output_directory": ((str,), lambda v: bool(v.strip()), "a non-empty path string"),
    "max_retries": ((int,), lambda v: 0 <= v <= 10, "an integer from 0 to 10"),
    "timeout": ((int, float), lambda v: 1 <= v <= 600, "a number of seconds from 1 to 600"),
    "request_delay": ((int, float), lambda v: 0 <= v <= 60, "a number of seconds from 0 to 60"),
}


def user_config_path() -> Path:
    """Return the per-user configuration file location for this OS."""
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        base = Path(os.environ["APPDATA"])
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "bottle-net" / CONFIG_FILENAME


def find_config_file(explicit: Path | None = None, *, cwd: Path | None = None) -> Path | None:
    """Locate the configuration file to use, or ``None`` if there is none.

    An explicitly requested file (``--config`` or the environment variable)
    must exist; the implicit locations are optional.
    """
    requested = explicit or (Path(os.environ[CONFIG_ENV_VAR]) if os.environ.get(CONFIG_ENV_VAR) else None)
    if requested is not None:
        if not requested.is_file():
            raise ConfigError(f"Configuration file not found: {requested}")
        return requested

    for candidate in ((cwd or Path.cwd()) / CONFIG_FILENAME, user_config_path()):
        if candidate.is_file():
            return candidate
    return None


def parse_config(data: Mapping[str, Any], *, source: Path | None = None) -> Config:
    """Build a :class:`Config` from already-parsed TOML data."""
    table = data.get(CONFIG_TABLE)
    settings: Mapping[str, Any] = table if isinstance(table, Mapping) else data

    values: dict[str, Any] = {}
    for key, value in settings.items():
        if key not in _SETTINGS:
            if key != CONFIG_TABLE:
                logger.debug("Ignoring unknown configuration key %r", key)
            continue
        types, is_valid, expected = _SETTINGS[key]
        # bool is a subclass of int; reject it explicitly.
        if isinstance(value, bool) or not isinstance(value, types) or not is_valid(value):
            where = f" in {source}" if source else ""
            raise ConfigError(f"Invalid value for '{key}'{where}: expected {expected}, got {value!r}")
        values[key] = value

    for key in ("download_directory", "output_directory"):
        if key in values:
            values[key] = Path(values[key]).expanduser()
    if "timeout" in values:
        values["timeout"] = float(values["timeout"])
    if "request_delay" in values:
        values["request_delay"] = float(values["request_delay"])

    return Config(**values, source=source)


def load_config(explicit: Path | None = None, *, cwd: Path | None = None) -> Config:
    """Find and load the configuration, falling back to defaults."""
    path = find_config_file(explicit, cwd=cwd)
    if path is None:
        return Config()
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Could not parse {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read {path}: {exc.strerror or exc}") from exc
    logger.debug("Loaded configuration from %s", path)
    return parse_config(data, source=path)
