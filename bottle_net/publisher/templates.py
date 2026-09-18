"""Metadata templates: reusable title/description/tags with placeholders.

Placeholders: ``{filename}`` (file name without extension), ``{date}``
(YYYY-MM-DD) and ``{time}`` (HH:MM) of the publish time, in the job's
timezone. Unknown ``{...}`` text is left untouched.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

PLACEHOLDERS = ("{filename}", "{date}", "{time}")


def render(text: str, *, filename: str, when: datetime) -> str:
    """Replace the supported placeholders in *text*."""
    values = {
        "{filename}": Path(filename).stem,
        "{date}": when.strftime("%Y-%m-%d"),
        "{time}": when.strftime("%H:%M"),
    }
    for placeholder, value in values.items():
        text = text.replace(placeholder, value)
    return text


def parse_tags(value: object) -> list[str]:
    """Accept tags as a list or a comma-separated string; drop blanks and duplicates."""
    items = value if isinstance(value, list) else str(value or "").split(",")
    tags: list[str] = []
    for item in items:
        tag = " ".join(str(item).split()).lstrip("#")
        if tag and tag.lower() not in {t.lower() for t in tags}:
            tags.append(tag)
    return tags
