"""Secrets never reach logs or user-facing messages."""

from __future__ import annotations

import logging

import pytest

from bottle_net.utils.logger import REDACTED, redact_secrets, setup_logging


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("Cookie: sessionid=abc123; csrftoken=zzz", "abc123"),
        ("{'Cookie': 'sid=SECRET', 'User-Agent': 'x'}", "SECRET"),
        ("Authorization: Bearer eyJhbGciOi", "eyJhbGciOi"),
        ("https://cdn.example/v.mp4?x-signature=S1GN&expires=1", "S1GN"),
        ("https://api.example/x?access_token=T0K3N", "T0K3N"),
        ("https://api.example/x?a=1&session_key=K3Y", "K3Y"),
        ("https://user:hunter2@example.com/path", "hunter2"),
        ("password=hunter2", "hunter2"),
    ],
)
def test_redact_secrets(text: str, secret: str) -> None:
    redacted = redact_secrets(text)
    assert secret not in redacted
    assert REDACTED in redacted


def test_normal_text_is_untouched() -> None:
    text = "https://www.tiktok.com/@user/video/123?lang=en downloaded 12.4 MB"
    assert redact_secrets(text) == text


def test_log_records_are_redacted(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "300")  # keep the log line on one row
    setup_logging(debug=True)
    logging.getLogger("bottle_net.test").debug("request to %s", "https://x/v.mp4?signature=S3CR3T")
    err = capsys.readouterr().err
    assert "S3CR3T" not in err and REDACTED in err
    setup_logging(debug=False)
