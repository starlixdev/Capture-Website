"""Redaction helpers for logs and exported HTTP metadata."""

from __future__ import annotations

import re
from collections.abc import Iterable

from .config import redact_url

_SECRET_HEADER_NAMES = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
}
_JSON_SECRET = re.compile(
    r'(?i)("(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key|x-auth-token)"\s*:\s*")'
    r'((?:\\.|[^"\\])*)(")'
)
_LOG_SECRET = re.compile(
    r"(?im)\b(authorization|proxy-authorization|cookie|set-cookie|x-api-key|x-auth-token)"
    r"(\s*[:=]\s*)(?!\")([^\r\n]+)"
)
_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>]+")


def redact_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str | list[str]]:
    result: dict[str, str | list[str]] = {}
    for name, value in headers:
        clean = "[REDACTED]" if name.lower() in _SECRET_HEADER_NAMES else redact_text(value)
        if name in result:
            current = result[name]
            result[name] = [*current, clean] if isinstance(current, list) else [current, clean]
        else:
            result[name] = clean
    return result


def redact_text(text: str) -> str:
    text = _JSON_SECRET.sub(lambda match: match.group(1) + "[REDACTED]" + match.group(3), text)
    text = _LOG_SECRET.sub(lambda match: match.group(1) + match.group(2) + "[REDACTED]", text)

    def replace_url(match: re.Match[str]) -> str:
        redacted, _ = redact_url(match.group(0))
        return redacted

    return _URL_IN_TEXT.sub(replace_url, text)
