"""Configuration loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from bottle_net.config import CONFIG_ENV_VAR, Config, load_config, parse_config
from bottle_net.errors import ConfigError
from bottle_net.utils.urls import Platform


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Never pick up the developer's real config files."""
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


def test_defaults_without_config_file(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path)
    assert config == Config()
    assert config.platform_download_dir(Platform.TIKTOK) == Path("downloads") / "tiktok"
    assert config.source is None


def test_loads_config_from_working_directory(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        'download_directory = "media"\noutput_directory = "lists"\nmax_retries = 5\ntimeout = 10\n'
        "request_delay = 0.5\n",
        encoding="utf-8",
    )
    config = load_config(cwd=tmp_path)
    assert config.download_directory == Path("media")
    assert config.output_directory == Path("lists")
    assert (config.max_retries, config.timeout, config.request_delay) == (5, 10.0, 0.5)
    assert config.source == tmp_path / "config.toml"


def test_table_form_and_unknown_keys(tmp_path: Path) -> None:
    config = parse_config({"title": "some other app", "bottle_net": {"max_retries": 1, "surprise": True}})
    assert config.max_retries == 1


def test_explicit_path_and_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "custom.toml"
    path.write_text("max_retries = 7\n", encoding="utf-8")
    assert load_config(path, cwd=tmp_path).max_retries == 7
    monkeypatch.setenv(CONFIG_ENV_VAR, str(path))
    assert load_config(cwd=tmp_path).max_retries == 7


def test_explicit_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.toml", cwd=tmp_path)


@pytest.mark.parametrize(
    "content",
    ['max_retries = "3"', "max_retries = 99", "max_retries = true", "timeout = 0", 'download_directory = ""',
     "request_delay = -1"],
)
def test_invalid_values(tmp_path: Path, content: str) -> None:
    (tmp_path / "config.toml").write_text(content + "\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="Invalid value"):
        load_config(cwd=tmp_path)


def test_invalid_toml(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text("max_retries = [\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="Could not parse"):
        load_config(cwd=tmp_path)
