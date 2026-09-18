"""URL list reading/writing, failed-URL files, filenames and directories."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from bottle_net.errors import URLListError
from bottle_net.utils.files import (
    FailedURL,
    batch_folder_name,
    decode_text,
    default_crawl_output_path,
    ensure_directory,
    find_existing_download,
    read_url_list,
    read_url_stream,
    retry_command,
    safe_filename,
    unique_path,
    write_failed_urls,
    write_url_list,
)
from bottle_net.utils.urls import Platform


class TestReadURLList:
    def test_ignores_blanks_comments_and_duplicates(self, tmp_path: Path) -> None:
        path = tmp_path / "list.txt"
        path.write_text(
            "# my videos\n"
            "https://www.tiktok.com/@a/video/1\n"
            "\n"
            "   \n"
            "https://www.tiktok.com/@a/video/2\n"
            "https://www.tiktok.com/@a/video/1?lang=en\n"
            "  https://www.tiktok.com/@a/video/3  \n",
            encoding="utf-8",
        )
        result = read_url_list(path)
        assert result.urls == [
            "https://www.tiktok.com/@a/video/1",
            "https://www.tiktok.com/@a/video/2",
            "https://www.tiktok.com/@a/video/3",
        ]
        assert (result.duplicates, result.blank_lines, result.comment_lines) == (1, 2, 1)

    def test_handles_bom_and_crlf(self, tmp_path: Path) -> None:
        path = tmp_path / "list.txt"
        path.write_bytes("﻿https://www.tiktok.com/@a/video/1\r\nhttps://www.tiktok.com/@a/video/2\r\n".encode())
        assert read_url_list(path).urls == ["https://www.tiktok.com/@a/video/1", "https://www.tiktok.com/@a/video/2"]

    @pytest.mark.parametrize("encoding", ["utf-16", "utf-16-be", "utf-32", "utf-8-sig"])
    def test_powershell_encodings(self, tmp_path: Path, encoding: str) -> None:
        # Windows PowerShell 5.1 `>` redirection writes UTF-16 with a BOM.
        path = tmp_path / "ps.txt"
        text = "https://www.tiktok.com/@a/video/1\r\nhttps://www.tiktok.com/@a/video/2\r\n"
        data = text.encode(encoding)
        if encoding == "utf-16-be":
            data = b"\xfe\xff" + data  # the -be codec does not add a BOM itself
        path.write_bytes(data)
        assert read_url_list(path).urls == ["https://www.tiktok.com/@a/video/1", "https://www.tiktok.com/@a/video/2"]

    def test_decode_text_defaults_to_utf8(self) -> None:
        assert decode_text("\u1019\u103c\u1014\u103a\u1019\u102c".encode()) == "\u1019\u103c\u1014\u103a\u1019\u102c"

    def test_read_from_stream(self) -> None:
        stream = io.BytesIO(b"# piped\nhttps://www.tiktok.com/@a/video/1\nhttps://www.tiktok.com/@a/video/1\n\n")
        result = read_url_stream(stream)
        assert result.urls == ["https://www.tiktok.com/@a/video/1"]
        assert result.path is None and result.source == "standard input"
        assert result.duplicates == 1

    def test_invalid_stream(self) -> None:
        with pytest.raises(URLListError, match="Standard input"):
            read_url_stream(io.BytesIO(b"\xfa\x81\x00x"))

    def test_empty_file_gives_no_urls(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.txt"
        path.write_text("\n# nothing\n\n", encoding="utf-8")
        assert read_url_list(path).urls == []

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(URLListError, match="not found"):
            read_url_list(tmp_path / "missing.txt")

    def test_directory_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(URLListError, match="Not a file"):
            read_url_list(tmp_path)

    def test_binary_file_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "video.mp4"
        path.write_bytes(b"\xff\xfe\xfa\x00\x81")
        with pytest.raises(URLListError, match="UTF-8"):
            read_url_list(path)


class TestWriting:
    def test_write_url_list_round_trip_and_creates_dirs(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "dir" / "urls.txt"
        urls = ["https://www.tiktok.com/@a/video/1", " https://www.tiktok.com/@a/video/2 ", ""]
        write_url_list(path, urls)
        assert path.read_text(encoding="utf-8") == (
            "https://www.tiktok.com/@a/video/1\nhttps://www.tiktok.com/@a/video/2\n"
        )
        assert read_url_list(path).urls == ["https://www.tiktok.com/@a/video/1", "https://www.tiktok.com/@a/video/2"]
        assert not list(path.parent.glob("*.tmp")), "temporary file left behind"

    def test_failed_urls_file_is_reusable_as_a_list(self, tmp_path: Path) -> None:
        path = tmp_path / "output" / "failed_tiktok.txt"
        failures = [
            FailedURL("https://www.tiktok.com/@a/video/1", "Video is unavailable"),
            FailedURL("https://www.tiktok.com/@a/video/2", "Network error"),
        ]
        write_failed_urls(path, failures, platform=Platform.TIKTOK, destination=tmp_path / "downloads" / "x")
        text = path.read_text(encoding="utf-8")
        assert "# Video is unavailable" in text
        assert "download-list" in text and "-d" in text
        assert read_url_list(path).urls == [f.url for f in failures]


class TestNames:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Example video", "Example_video"),
            ('Bad <chars>: "a/b\\c" | what? *', "Bad_chars_a_b_c_what"),
            ("  spaced   out  ", "spaced_out"),
            ("Wait for it...", "Wait_for_it"),
            ("မြန်မာ ဗီဒီယို", "မြန်မာ_ဗီဒီယို"),
            ("", "video"),
            ("...", "video"),
            ("CON", "video"),
        ],
    )
    def test_safe_filename(self, title: str, expected: str) -> None:
        assert safe_filename(title) == expected

    def test_safe_filename_truncates(self) -> None:
        assert len(safe_filename("a" * 500, max_length=40)) == 40

    def test_default_crawl_output_path_never_overwrites(self, tmp_path: Path) -> None:
        first = default_crawl_output_path(tmp_path, Platform.TIKTOK, "example")
        assert first == tmp_path / "tiktok_example_links.txt"
        first.write_text("existing")
        second = default_crawl_output_path(tmp_path, Platform.TIKTOK, "example")
        assert second == tmp_path / "tiktok_example_links_2.txt"
        second.write_text("existing")
        assert default_crawl_output_path(tmp_path, Platform.TIKTOK, "example").name == "tiktok_example_links_3.txt"
        assert default_crawl_output_path(tmp_path, Platform.FACEBOOK, "page").name == "facebook_page_links.txt"

    def test_unique_path(self, tmp_path: Path) -> None:
        target = tmp_path / "links.txt"
        assert unique_path(target) == target
        target.write_text("x")
        assert unique_path(target) == tmp_path / "links_2.txt"

    def test_retry_command(self) -> None:
        assert retry_command(Path("f.txt"), Platform.TIKTOK) == 'bottle-net tiktok download-list "f.txt"'
        assert retry_command(Path("f.txt"), None, name="stdin") == 'bottle-net download-list "f.txt" --name "stdin"'
        assert retry_command(Path("f.txt"), Platform.FACEBOOK, destination=Path("d")) == (
            'bottle-net facebook download-list "f.txt" -d "d"'
        )

    @pytest.mark.parametrize(
        ("name", "platform", "expected"),
        [
            ("tiktok_example_2026-09-18.txt", Platform.TIKTOK, "example"),
            ("tiktok_example_links.txt", Platform.TIKTOK, "example"),
            ("tiktok_example_links_2.txt", Platform.TIKTOK, "example"),
            ("facebook_examplepage_links.txt", None, "examplepage"),
            ("my_links.txt", Platform.TIKTOK, "my"),
            ("tiktok_example.txt", Platform.TIKTOK, "example"),
            ("facebook_examplepage_2026-09-18.txt", Platform.FACEBOOK, "examplepage"),
            ("videos.txt", Platform.TIKTOK, "videos"),
            ("tiktok_.txt", Platform.TIKTOK, "tiktok"),
        ],
    )
    def test_batch_folder_name(self, name: str, platform: Platform | None, expected: str) -> None:
        assert batch_folder_name(Path("output") / name, platform) == expected


class TestDirectories:
    def test_ensure_directory_creates_parents(self, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b" / "c"
        assert ensure_directory(target) == target
        assert target.is_dir()
        ensure_directory(target)  # idempotent

    def test_ensure_directory_rejects_file(self, tmp_path: Path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        with pytest.raises(OSError):
            ensure_directory(blocker)

    def test_find_existing_download_ignores_partials(self, tmp_path: Path) -> None:
        (tmp_path / "Title_123.mp4.part").write_bytes(b"x")
        (tmp_path / "Other_1234.mp4").write_bytes(b"x")
        assert find_existing_download(tmp_path, "123") is None
        (tmp_path / "Title_123.mp4").write_bytes(b"data")
        assert find_existing_download(tmp_path, "123") == tmp_path / "Title_123.mp4"

    def test_find_existing_download_missing_dir(self, tmp_path: Path) -> None:
        assert find_existing_download(tmp_path / "nope", "1") is None
