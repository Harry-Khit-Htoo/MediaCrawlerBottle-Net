"""``bottle-net doctor`` checks and report."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from bottle_net import doctor
from bottle_net.cli import main
from bottle_net.config import CONFIG_ENV_VAR
from bottle_net.doctor import (
    Status,
    check_config,
    check_ffmpeg,
    check_platforms,
    check_python,
    check_writable,
    check_ytdlp,
)


@pytest.fixture(autouse=True)
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path


def test_python_version() -> None:
    assert check_python((3, 11, 0)).status is Status.OK
    old = check_python((3, 10, 12))
    assert old.status is Status.FAIL and "3.11" in old.detail


def test_ytdlp_age() -> None:
    fresh = check_ytdlp(today=date(2000, 1, 1))  # installed version is newer than this date
    assert fresh.status is Status.OK and "installed" in fresh.detail
    stale = check_ytdlp(today=date(2100, 1, 1))
    assert stale.status is Status.WARN
    assert any("pip install --upgrade yt-dlp" in line for line in stale.advice)


def test_ffmpeg_is_optional() -> None:
    assert check_ffmpeg(which=lambda name: "/usr/bin/ffmpeg").status is Status.OK
    missing = check_ffmpeg(which=lambda name: None)
    assert missing.status is Status.WARN and missing.detail == "Not installed"
    assert "Single-file downloads will still work when" in missing.advice
    assert "Install FFmpeg for best-quality stream merging." in missing.advice


def test_config_checks(workdir: Path) -> None:
    ok, config = check_config()
    assert ok.status is Status.OK and "defaults" in ok.detail
    (workdir / "config.toml").write_text('download_directory = "media"\n', encoding="utf-8")
    ok, config = check_config()
    assert ok.status is Status.OK and config.download_directory == Path("media")
    (workdir / "config.toml").write_text("max_retries = -1\n", encoding="utf-8")
    bad, _ = check_config()
    assert bad.status is Status.FAIL and "max_retries" in bad.advice[0]


def test_writable_does_not_create_folders(workdir: Path) -> None:
    target = workdir / "not" / "yet" / "there"
    result = check_writable("Downloads", target)
    assert result.status is Status.OK and "will be created" in result.detail
    assert not (workdir / "not").exists()
    assert list(workdir.iterdir()) == []  # the probe file was removed


def test_writable_rejects_a_file(workdir: Path) -> None:
    (workdir / "blocker").write_text("x")
    assert check_writable("Output", workdir / "blocker" / "sub").status is Status.FAIL


def test_platform_extractors_present() -> None:
    assert [(c.name, c.status) for c in check_platforms()] == [("TikTok", Status.OK), ("Facebook", Status.OK)]


def test_doctor_command_report(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    for line in ("Bottle Net Tool Doctor", "Python", "yt-dlp", "FFmpeg       ! Not installed", "Config       ✓ valid",
                 "Downloads    ✓ writable", "Output       ✓ writable", "Platform support:", "TikTok       ✓",
                 "Facebook     ✓", "System ready."):
        assert line in out


def test_doctor_reports_invalid_config(workdir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (workdir / "config.toml").write_text("timeout = 0\n", encoding="utf-8")
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "Config       ✗ invalid" in out and "Problems found." in out


def test_version_output(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert capsys.readouterr().out == "Bottle Net Tool 1.0.0\nAuthor: Aung Khit Htoo\n"


def test_version_is_defined_once() -> None:
    import tomllib

    import bottle_net

    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert "version" not in pyproject["project"] and "version" in pyproject["project"]["dynamic"]
    assert pyproject["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "bottle_net.__version__"}
    assert bottle_net.__version__ == "1.0.0"
