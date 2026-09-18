"""Publisher settings (stored in the database, edited on the Settings page)."""

from __future__ import annotations

import zoneinfo
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from bottle_net.publisher.errors import ValidationError
from bottle_net.publisher.retry import DEFAULT_DELAYS, DEFAULT_MAX_ATTEMPTS, RetryPolicy

MISSED_POLICIES = {
    "hold": "Ask me - mark the upload as missed and wait",
    "run": "Publish as soon as Bottle Net is running again",
    "skip": "Skip it - mark the upload as cancelled",
}
PRIVACY_OPTIONS = ("public", "unlisted", "private")


@dataclass
class PublisherSettings:
    """All user-configurable publisher settings, with their defaults."""

    timezone: str = "Asia/Bangkok"
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    retry_delays: list[int] = field(default_factory=lambda: list(DEFAULT_DELAYS))
    #: What to do with an upload whose time passed while Bottle Net was not running.
    missed_policy: str = "hold"
    #: Uploads up to this many minutes late still run normally.
    missed_grace_minutes: int = 10
    #: Where videos added through the GUI are stored ("" = the default folder).
    video_directory: str = ""
    notify_completed: bool = True
    notify_failed: bool = True
    browser_notifications: bool = False
    youtube_privacy: str = "public"
    youtube_category: str = "22"
    facebook_tags_as_hashtags: bool = True
    #: Parallel uploads (at most one per platform is sensible).
    workers: int = 2

    @property
    def retry_policy(self) -> RetryPolicy:
        return RetryPolicy(max_attempts=self.max_attempts, delays=tuple(self.retry_delays))

    @property
    def zone(self) -> zoneinfo.ZoneInfo:
        return zoneinfo.ZoneInfo(self.timezone)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PublisherSettings:
        """Build settings from stored values, ignoring unknown or invalid ones."""
        settings = cls()
        for key, value in data.items():
            try:
                settings.apply({key: value})
            except ValidationError:
                continue
        return settings

    def apply(self, changes: dict[str, Any]) -> None:
        """Validate and apply *changes*; raises ValidationError on bad input."""
        known = {f.name for f in fields(self)}
        for key, value in changes.items():
            if key not in known:
                raise ValidationError(f"Unknown setting: {key}")
            setattr(self, key, _validate(key, value))


def _validate(key: str, value: Any) -> Any:
    def need(condition: bool, message: str) -> None:
        if not condition:
            raise ValidationError(message)

    if key == "timezone":
        need(isinstance(value, str) and value in zoneinfo.available_timezones(), f"Unknown timezone: {value}")
    elif key == "max_attempts":
        need(isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 10,
             "Maximum attempts must be a whole number from 1 to 10.")
    elif key == "retry_delays":
        need(isinstance(value, list) and 0 < len(value) <= 10
             and all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 86400 for v in value),
             "Retry delays must be a list of 1-10 numbers of seconds (0 to 86400).")
    elif key == "missed_policy":
        need(value in MISSED_POLICIES, f"Missed-upload policy must be one of: {', '.join(MISSED_POLICIES)}.")
    elif key == "missed_grace_minutes":
        need(isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1440,
             "Grace period must be 0 to 1440 minutes.")
    elif key == "video_directory":
        need(isinstance(value, str) and len(value) < 1000, "Video directory must be a folder path.")
    elif key in ("notify_completed", "notify_failed", "browser_notifications", "facebook_tags_as_hashtags"):
        need(isinstance(value, bool), f"{key} must be true or false.")
    elif key == "youtube_privacy":
        need(value in PRIVACY_OPTIONS, f"Privacy must be one of: {', '.join(PRIVACY_OPTIONS)}.")
    elif key == "youtube_category":
        need(isinstance(value, str) and value.isdigit(), "YouTube category must be a numeric category ID.")
    elif key == "workers":
        need(isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 4,
             "Parallel uploads must be 1 to 4.")
    return value
