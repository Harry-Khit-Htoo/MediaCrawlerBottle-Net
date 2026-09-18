"""Output streams: URL writers, format registry and where crawl results go."""

from __future__ import annotations

import errno
import io
from pathlib import Path

import pytest

from bottle_net.commands import choose_crawl_output
from bottle_net.utils.output import (
    DEFAULT_FORMAT,
    OUTPUT_FORMATS,
    OutputClosedError,
    TextURLWriter,
    get_format,
    is_terminal,
)


class TestTextWriter:
    def test_one_url_per_line(self) -> None:
        stream = io.StringIO()
        writer = TextURLWriter(stream)
        writer.write("https://a")
        writer.write("https://b")
        writer.close()
        assert stream.getvalue() == "https://a\nhttps://b\n"
        assert writer.count == 2

    @pytest.mark.parametrize("error", [BrokenPipeError(), OSError(errno.EINVAL, "Invalid argument")])
    def test_closed_pipe_is_reported(self, error: OSError) -> None:
        class Closed(io.StringIO):
            def write(self, text: str) -> int:
                raise error

        with pytest.raises(OutputClosedError):
            TextURLWriter(Closed()).write("https://a")

    def test_other_os_errors_propagate(self) -> None:
        class Full(io.StringIO):
            def write(self, text: str) -> int:
                raise OSError(errno.ENOSPC, "No space left on device")

        with pytest.raises(OSError, match="No space"):
            TextURLWriter(Full()).write("https://a")


class TestFormats:
    def test_txt_is_default_and_renders_lines(self) -> None:
        assert DEFAULT_FORMAT == "txt" and "txt" in OUTPUT_FORMATS
        fmt = get_format("txt")
        assert fmt.extension == ".txt"
        assert fmt.render(["https://a", " https://b ", ""]) == "https://a\nhttps://b\n"

    def test_unknown_format(self) -> None:
        with pytest.raises(ValueError):
            get_format("xml")


def test_is_terminal_handles_odd_streams() -> None:
    assert not is_terminal(io.StringIO())

    class Closed(io.StringIO):
        def isatty(self) -> bool:
            raise ValueError("I/O operation on closed file")

    assert not is_terminal(Closed())


@pytest.mark.parametrize(
    ("output", "quiet", "tty", "to_stdout", "path"),
    [
        (None, False, True, False, None),          # terminal: new file in output/
        (None, False, False, True, None),          # `> file` or `| cmd`
        (None, True, True, True, None),            # --quiet: URLs only, on stdout
        ("-", False, True, True, None),            # -o -
        ("links.txt", False, False, False, Path("links.txt")),  # -o wins over redirection
        ("links.txt", True, True, False, Path("links.txt")),
    ],
)
def test_choose_crawl_output(output: str | None, quiet: bool, tty: bool, to_stdout: bool, path: Path | None) -> None:
    result = choose_crawl_output(output, quiet=quiet, stdout_is_terminal=tty)
    assert result.to_stdout is to_stdout
    assert result.path == path
