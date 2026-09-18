"""Centralised, cross-platform filename handling.

Every filename or folder name that Bottle Net Tool derives from outside
input (video titles, account names, list names) goes through this module, so
the rules live in one place:

1. Characters that are invalid on Windows, macOS or Linux are replaced:
   ``< > : " / \\ | ? *`` and control characters (``/`` and ``\\`` become
   ``-``, the rest are dropped).
2. Whitespace is normalised (runs of spaces become one ``_``).
3. Empty results fall back to a default name, and Windows reserved device
   names (``CON``, ``NUL``, ``COM1`` ...) are made safe.
4. Length is limited both in characters and in UTF-8 bytes, because
   non-Latin titles (Burmese, Thai ...) take 3 bytes per character and most
   file systems allow 255 bytes per name.
5. File extensions are preserved.
6. :func:`unique_path` avoids overwriting existing files.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

# Characters that are illegal in filenames on Windows (a superset of the
# macOS and Linux rules), plus ASCII control characters.
_SLASHES = re.compile(r"[/\\]+")
_ILLEGAL_CHARS = re.compile(r'[<>:"|?*\x00-\x1f\x7f]')
_ELLIPSIS = re.compile(r"\.{2,}|…")
_EXTENSION = re.compile(r"^\.[A-Za-z0-9]{1,8}$")

WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
)

#: Default maximum length of a name, in characters.
DEFAULT_MAX_LENGTH = 80
#: Most file systems (NTFS, ext4, APFS) allow 255 bytes/characters per name.
MAX_NAME_BYTES = 255


def truncate_utf8(text: str, max_bytes: int) -> str:
    """Shorten *text* so its UTF-8 encoding is at most *max_bytes* bytes.

    Never cuts a character in half.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def safe_filename(
    text: str,
    *,
    max_length: int = DEFAULT_MAX_LENGTH,
    max_bytes: int | None = None,
    fallback: str = "video",
) -> str:
    """Turn arbitrary text (e.g. a video title) into a safe filename *stem*.

    Non-ASCII letters (Burmese, Thai, emoji, ...) are kept; only characters
    that are illegal in filenames are removed. Whitespace becomes ``_``.
    Use :func:`sanitize_filename` for a complete name with an extension.
    """
    text = unicodedata.normalize("NFC", text or "")
    text = _SLASHES.sub("-", text)
    text = _ILLEGAL_CHARS.sub(" ", text)
    text = _ELLIPSIS.sub(" ", text)  # ellipses read badly in filenames
    text = re.sub(r"\s+", "_", text.strip())
    text = re.sub(r"_{2,}", "_", text)
    text = re.sub(r"_?-_?", "-", text)
    text = text[:max_length]
    if max_bytes is not None:
        text = truncate_utf8(text, max_bytes)
    # Windows does not allow names ending in a dot or space.
    text = text.strip("._ -")
    if not text:
        return fallback
    if text.split(".")[0].upper() in WINDOWS_RESERVED_NAMES:
        text = f"{text}_"
    return text


def sanitize_filename(
    name: str,
    *,
    max_length: int = DEFAULT_MAX_LENGTH,
    fallback: str = "video",
) -> str:
    """Make a complete filename safe while preserving its extension.

    ``"My Video: Part 1/2?.mp4"`` -> ``"My_Video_Part_1-2.mp4"``
    """
    stem, extension = name, ""
    dot = name.rfind(".")
    if dot > 0 and _EXTENSION.match(name[dot:].strip()):
        stem, extension = name[:dot], name[dot:].strip().lower()
    budget = max(1, max_length - len(extension))
    safe_stem = safe_filename(
        stem, max_length=budget, max_bytes=MAX_NAME_BYTES - len(extension.encode()), fallback=fallback
    )
    return f"{safe_stem}{extension}"


def unique_path(path: Path) -> Path:
    """Return *path*, or ``<stem>_2<suffix>``, ``<stem>_3<suffix>``... if taken."""
    candidate = path
    counter = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}_{counter}{path.suffix}")
        counter += 1
    return candidate
