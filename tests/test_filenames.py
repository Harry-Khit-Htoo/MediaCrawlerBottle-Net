"""Central filename sanitising (Windows-safe names, lengths, extensions)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bottle_net.utils.filenames import (
    MAX_NAME_BYTES,
    WINDOWS_RESERVED_NAMES,
    safe_filename,
    sanitize_filename,
    truncate_utf8,
    unique_path,
)

WINDOWS_FORBIDDEN = set('<>:"/\\|?*')

NASTY_TITLES = [
    'My Video: Part 1/2?.mp4',
    '<script>alert("x")</script>',
    "a|b|c*?",
    "C:\\Windows\\System32",
    "tab\there\nnewline\x00null",
    "trailing dots...",
    "   ",
    "🎶 emoji title 🌐",
    "မြန်မာ ဗီဒီယို",
    "CON",
    "nul.txt",
    "COM1.mp4",
    "x" * 500,
    "သ" * 500,
]


class TestSanitize:
    def test_spec_example(self) -> None:
        assert sanitize_filename("My Video: Part 1/2?.mp4") == "My_Video_Part_1-2.mp4"

    @pytest.mark.parametrize("title", NASTY_TITLES)
    def test_results_are_valid_windows_names(self, title: str) -> None:
        for name in (safe_filename(title), sanitize_filename(title + ".mp4")):
            assert name
            assert not WINDOWS_FORBIDDEN & set(name)
            assert all(ord(ch) >= 32 for ch in name)
            assert not name.endswith((".", " "))
            assert name.split(".")[0].upper() not in WINDOWS_RESERVED_NAMES
            assert len(name.encode("utf-8")) <= MAX_NAME_BYTES

    @pytest.mark.parametrize("title", NASTY_TITLES)
    def test_files_can_really_be_created(self, tmp_path: Path, title: str) -> None:
        # Uses the real file system of the machine running the tests (Windows here).
        path = tmp_path / sanitize_filename(title + ".mp4")
        path.write_bytes(b"ok")
        assert path.read_bytes() == b"ok"
        assert path.suffix == ".mp4"

    def test_extension_is_preserved_and_lowercased(self) -> None:
        assert sanitize_filename("Clip.MP4") == "Clip.mp4"
        long_name = sanitize_filename("word " * 100 + ".webm", max_length=40)
        assert long_name.endswith(".webm") and len(long_name) <= 40

    def test_no_extension(self) -> None:
        assert sanitize_filename("just a title") == "just_a_title"
        assert sanitize_filename("") == "video"

    def test_whitespace_is_normalised(self) -> None:
        assert safe_filename("  lots   of \t space  ") == "lots_of_space"

    def test_reserved_names(self) -> None:
        assert safe_filename("CON") == "CON_"
        assert sanitize_filename("aux.txt") == "aux_.txt"

    def test_unicode_is_kept(self) -> None:
        assert safe_filename("မြန်မာ ဗီဒီယို") == "မြန်မာ_ဗီဒီယို"
        assert "🎶" in safe_filename("🎶 song")


def test_truncate_utf8_never_splits_characters() -> None:
    text = "သ" * 10  # 3 bytes each
    assert truncate_utf8(text, 10) == "သ" * 3
    assert truncate_utf8("abc", 10) == "abc"


def test_byte_limit_for_non_latin_titles() -> None:
    name = safe_filename("သ" * 500, max_length=500, max_bytes=200)
    assert len(name.encode("utf-8")) <= 200


def test_unique_path_prevents_overwriting(tmp_path: Path) -> None:
    target = tmp_path / "links.txt"
    assert unique_path(target) == target
    target.write_text("1")
    (tmp_path / "links_2.txt").write_text("2")
    assert unique_path(target) == tmp_path / "links_3.txt"
