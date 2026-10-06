"""Content-Type-first resource classification."""

from __future__ import annotations

import re

_JS_TYPES = {
    "application/ecmascript",
    "application/javascript",
    "application/x-ecmascript",
    "application/x-javascript",
    "text/ecmascript",
    "text/javascript",
    "text/javascript1.0",
    "text/javascript1.1",
    "text/javascript1.2",
    "text/javascript1.3",
    "text/javascript1.4",
    "text/javascript1.5",
    "text/jscript",
    "text/livescript",
    "text/x-ecmascript",
    "text/x-javascript",
}
_FONT_TYPES = {
    "application/font-sfnt",
    "application/font-woff",
    "application/vnd.ms-fontobject",
    "application/x-font-opentype",
    "application/x-font-ttf",
    "application/x-font-woff",
}

CATEGORY_LABELS = {
    "html": "HTML",
    "css": "CSS",
    "js": "JavaScript",
    "images": "Images",
    "svg": "SVG",
    "fonts": "Fonts",
    "videos": "Videos",
    "audio": "Audio",
    "json": "JSON",
    "wasm": "WASM",
    "other": "Other",
}

EXTENSIONS = {
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "text/css": ".css",
    "application/javascript": ".js",
    "text/javascript": ".js",
    "image/svg+xml": ".svg",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/avif": ".avif",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/x-icon": ".ico",
    "image/tiff": ".tiff",
    "font/woff": ".woff",
    "font/woff2": ".woff2",
    "font/ttf": ".ttf",
    "font/otf": ".otf",
    "application/font-woff": ".woff",
    "application/vnd.ms-fontobject": ".eot",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "application/json": ".json",
    "application/wasm": ".wasm",
    "text/plain": ".txt",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "application/pdf": ".pdf",
}


def normalize_mime(value: str | None) -> str:
    if not value:
        return "application/octet-stream"
    return value.split(";", 1)[0].strip().lower() or "application/octet-stream"


def classify_content_type(value: str | None) -> str:
    mime = normalize_mime(value)
    if mime in {"text/html", "application/xhtml+xml"}:
        return "html"
    if mime == "text/css":
        return "css"
    if mime in _JS_TYPES or mime.endswith("+javascript"):
        return "js"
    if mime == "image/svg+xml":
        return "svg"
    if mime.startswith("image/"):
        return "images"
    if mime.startswith("font/") or mime in _FONT_TYPES:
        return "fonts"
    if mime.startswith("video/"):
        return "videos"
    if mime.startswith("audio/"):
        return "audio"
    if mime == "application/json" or mime.endswith("+json") or re.fullmatch(r"text/(?:x-)?json", mime):
        return "json"
    if mime == "application/wasm":
        return "wasm"
    return "other"


def extension_for_mime(value: str | None, *, category: str | None = None) -> str:
    mime = normalize_mime(value)
    if mime in EXTENSIONS:
        return EXTENSIONS[mime]
    category = category or classify_content_type(mime)
    return {
        "html": ".html",
        "css": ".css",
        "js": ".js",
        "svg": ".svg",
        "images": ".img",
        "fonts": ".font",
        "videos": ".video",
        "audio": ".audio",
        "json": ".json",
        "wasm": ".wasm",
    }.get(category, ".bin")

