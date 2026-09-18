"""Machine-readable output: URL lists written to files or standard output.

Bottle Net Tool keeps two kinds of output strictly apart:

* **stdout** carries *data* only (e.g. discovered video URLs), so commands
  can be redirected (``> links.txt``) or piped (``| sort -u``).
* **stderr** carries everything meant for humans: banners, status lines,
  progress bars, warnings and errors.

Only the ``txt`` format (one URL per line) exists today. Formats are looked
up in :data:`OUTPUT_FORMATS`, so ``json`` or ``csv`` can be added later
without touching the commands.
"""

from __future__ import annotations

import errno
import os
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol, TextIO

#: ``-o -`` / ``download-list -`` mean standard output / standard input.
STDIO_MARKER = "-"
DEFAULT_FORMAT = "txt"


class OutputClosedError(Exception):
    """The program reading our standard output went away (e.g. ``| head -5``)."""


class URLWriter(Protocol):
    """Writes URLs to a stream as they are discovered."""

    def write(self, url: str) -> None:
        """Write one URL."""

    def close(self) -> None:
        """Finish the output (formats with a footer write it here)."""


class TextURLWriter:
    """``txt`` format: one URL per line, flushed immediately for pipelines."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self.count = 0

    def write(self, url: str) -> None:
        try:
            self._stream.write(f"{url}\n")
            self._stream.flush()
        except BrokenPipeError as exc:
            raise OutputClosedError from exc
        except OSError as exc:
            # Windows reports a closed pipe as EINVAL rather than EPIPE.
            if exc.errno in (errno.EPIPE, errno.EINVAL):
                raise OutputClosedError from exc
            raise
        self.count += 1

    def close(self) -> None:
        try:
            self._stream.flush()
        except (BrokenPipeError, OSError):
            pass


@dataclass(frozen=True)
class OutputFormat:
    """A supported output format."""

    name: str
    extension: str
    render: Callable[[Iterable[str]], str]
    stream_writer: Callable[[TextIO], URLWriter]


def _render_txt(urls: Iterable[str]) -> str:
    return "".join(f"{url.strip()}\n" for url in urls if url.strip())


OUTPUT_FORMATS: dict[str, OutputFormat] = {
    "txt": OutputFormat("txt", ".txt", _render_txt, TextURLWriter),
}


def get_format(name: str) -> OutputFormat:
    """Return the :class:`OutputFormat` called *name*."""
    try:
        return OUTPUT_FORMATS[name]
    except KeyError:
        raise ValueError(f"Unknown output format: {name}") from None


def silence_stdout() -> None:
    """Point stdout at the null device after the reader closed the pipe.

    Without this, Python reports a ``BrokenPipeError`` while flushing
    stdout at exit (see the ``signal`` module documentation, "Note on
    SIGPIPE").
    """
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        pass


def is_terminal(stream: TextIO) -> bool:
    """Return True if *stream* is an interactive terminal."""
    try:
        return stream.isatty()
    except (AttributeError, ValueError, OSError):
        return False
