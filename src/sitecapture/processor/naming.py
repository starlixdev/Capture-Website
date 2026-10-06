"""Deterministic Windows-safe local payload naming."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from .classifier import classify_content_type, extension_for_mime

_INVALID_WINDOWS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_TRAILING = re.compile(r"[ .]+$")
_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def safe_stem(value: str, max_length: int = 72) -> str:
    value = unicodedata.normalize("NFC", unquote(value))
    value = _INVALID_WINDOWS.sub("_", value)
    value = re.sub(r"\s+", "-", value).strip(" .-")
    value = _TRAILING.sub("", value)
    if not value:
        value = "resource"
    if value.upper().split(".", 1)[0] in _RESERVED:
        value = "_" + value
    if len(value) > max_length:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
        value = value[: max_length - 12].rstrip(" .-") + "--" + digest
    return value


def local_payload_path(
    url: str,
    content_type: str | None,
    sha256_hex: str,
    *,
    force_raw: bool = False,
) -> str:
    parsed = urlsplit(url)
    path = parsed.path
    basename = PurePosixPath(path).name if path and not path.endswith("/") else "index"
    original_stem = PurePosixPath(basename).stem or basename or "resource"
    host = safe_stem(parsed.hostname or "unknown-host", 32)
    stem = safe_stem(original_stem)
    url_identity = hashlib.sha256(url.encode("utf-8", "surrogatepass")).hexdigest()[:10]
    category = "other" if force_raw else classify_content_type(content_type)
    extension = ".raw" if force_raw else extension_for_mime(content_type, category=category)
    filename = f"{host}--{stem}--u{url_identity}--h{sha256_hex[:16]}{extension}"
    # The returned path is constructed from controlled category and one filename;
    # URL slashes and traversal components are never reused as path components.
    return f"{category}/{filename}"

