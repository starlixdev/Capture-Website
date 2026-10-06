"""Validated configuration and safe URL/output naming."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .errors import ValidationError


APP_NAME = "CaptureWebsite"
_LOG = logging.getLogger(__name__)
_SETTINGS_FILENAME = "settings.json"
BrowserSessionMode = Literal["isolated", "persistent_chrome", "persistent_edge"]
_BROWSER_SESSION_MODES: tuple[BrowserSessionMode, ...] = ("isolated", "persistent_chrome", "persistent_edge")


def application_data_dir() -> Path:
    """Return a writable per-user application data directory."""
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA")
        base = Path(root) if root else Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        root = os.environ.get("XDG_DATA_HOME")
        base = Path(root) if root else Path.home() / ".local" / "share"
    return base / APP_NAME


def default_output_dir() -> Path:
    """Return and create CaptureWebsite's application-owned output directory."""
    path = application_data_dir() / "output"
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _load_settings() -> dict[str, Any]:
    settings_path = application_data_dir() / _SETTINGS_FILENAME
    try:
        raw = settings_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except UnicodeError as exc:
        _LOG.warning("Could not decode CaptureWebsite settings from %s: %s", settings_path, exc)
        return {}
    except OSError as exc:
        _LOG.warning("Could not read CaptureWebsite settings from %s: %s", settings_path, exc)
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _LOG.warning("Could not parse CaptureWebsite settings from %s: %s", settings_path, exc)
        return {}
    if not isinstance(data, dict):
        _LOG.warning("CaptureWebsite settings in %s are not a JSON object; using defaults.", settings_path)
        return {}
    return data


def _save_settings(updates: dict[str, Any]) -> None:
    app_dir = application_data_dir()
    app_dir.mkdir(parents=True, exist_ok=True)
    settings_path = app_dir / _SETTINGS_FILENAME
    temp_path = settings_path.with_suffix(settings_path.suffix + ".tmp")
    payload = _load_settings()
    payload.update(updates)
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp_path, settings_path)


def load_output_preference() -> Path:
    """Load the GUI's persisted output folder, falling back to the app-owned default."""
    fallback = default_output_dir()
    value = _load_settings().get("outputFolder")
    if isinstance(value, str) and value.strip():
        try:
            return Path(value).expanduser().resolve()
        except (OSError, ValueError):
            pass
    return fallback


def save_output_preference(path: Path | str) -> Path:
    """Persist the GUI output folder atomically and return its normalized path."""
    resolved = Path(path).expanduser().resolve()
    _save_settings({"outputFolder": str(resolved)})
    return resolved


def load_browser_session_preference() -> BrowserSessionMode:
    """Load the GUI's selected browser-session mode."""
    value = _load_settings().get("browserSession")
    if value in _BROWSER_SESSION_MODES:
        return value
    return "isolated"


def save_browser_session_preference(mode: BrowserSessionMode) -> BrowserSessionMode:
    """Persist the selected browser-session mode without discarding other settings."""
    if mode not in _BROWSER_SESSION_MODES:
        raise ValueError(f"Unsupported browser session mode: {mode}")
    _save_settings({"browserSession": mode})
    return mode


def browser_profile_dir(mode: BrowserSessionMode) -> Path:
    """Return CaptureWebsite's dedicated persistent browser profile directory."""
    if mode == "persistent_chrome":
        name = "chrome"
    elif mode == "persistent_edge":
        name = "edge"
    else:
        raise ValueError("Isolated mode does not use a persistent browser profile directory.")
    path = application_data_dir() / "browser-profiles" / name
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()

_SENSITIVE_QUERY_NAMES = re.compile(
    r"^(?:access[_-]?token|api[_-]?key|auth|authorization|code|credential|jwt|"
    r"key|password|secret|session|signature|sig|token)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CaptureProfile:
    name: Literal["desktop", "mobile"]
    viewport: dict[str, object]
    mobile_device: str | None = None
    user_agent: str | None = None


DESKTOP_PROFILE = CaptureProfile(
    name="desktop",
    viewport={"width": 1920, "height": 1080, "deviceScaleFactor": 1},
)
MOBILE_PROFILE = CaptureProfile(
    name="mobile",
    viewport={"width": 393, "height": 851, "deviceScaleFactor": 3, "isMobile": True},
    mobile_device="Pixel 5",
    user_agent=(
        "Mozilla/5.0 (Linux; Android 13; Pixel 5) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/152.0.0.0 Mobile Safari/537.36"
    ),
)


def validate_url(value: str, *, allow_private: bool = False) -> str:
    value = value.strip()
    if not value or any(ord(char) < 32 for char in value):
        raise ValidationError("URL is empty or contains control characters.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValidationError(f"Invalid URL: {exc}") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValidationError("Only http:// and https:// URLs are supported.")
    if not parsed.hostname:
        raise ValidationError("The URL must contain a hostname.")
    if parsed.username is not None or parsed.password is not None:
        raise ValidationError("Credentials in URLs are not accepted.")
    if port is not None and not 1 <= port <= 65535:
        raise ValidationError("URL port must be between 1 and 65535.")
    if not allow_private and port is not None:
        expected = 443 if parsed.scheme.lower() == "https" else 80
        if port != expected:
            raise ValidationError(
                "Non-standard ports are blocked by default; use --allow-private only for a target you control."
            )

    hostname = parsed.hostname.rstrip(".").lower()
    if not allow_private:
        if hostname == "localhost" or hostname.endswith(".localhost"):
            raise ValidationError(
                "Local/private targets are blocked by default; use --allow-private only for a target you control."
            )
        try:
            address = ipaddress.ip_address(hostname.strip("[]"))
        except ValueError:
            address = None
        if address and not address.is_global:
            raise ValidationError(
                "Local/private IP targets are blocked by default; use --allow-private only for a target you control."
            )

    try:
        ascii_host = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValidationError("The URL hostname is not valid IDNA.") from exc
    netloc = ascii_host
    if ":" in ascii_host and not ascii_host.startswith("["):
        netloc = f"[{ascii_host}]"
    if port is not None:
        netloc += f":{port}"
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, parsed.fragment))


def redact_url(value: str) -> tuple[str, bool]:
    """Redact common secret-bearing query fields from exported metadata."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value, False
    changed = False
    pairs: list[tuple[str, str]] = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        if _SENSITIVE_QUERY_NAMES.match(key):
            item = "[REDACTED]"
            changed = True
        pairs.append((key, item))
    fragment = parsed.fragment
    if "=" in fragment:
        fragment_pairs: list[tuple[str, str]] = []
        fragment_changed = False
        for key, item in parse_qsl(fragment, keep_blank_values=True):
            if _SENSITIVE_QUERY_NAMES.match(key):
                item = "[REDACTED]"
                fragment_changed = True
            fragment_pairs.append((key, item))
        if fragment_changed:
            fragment = urlencode(fragment_pairs)
            changed = True
    if not changed:
        return value, False
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(pairs), fragment)), True


def site_slug(url: str) -> str:
    host = (urlsplit(url).hostname or "site").lower().rstrip(".")
    labels = [label for label in host.split(".") if label]
    if labels and labels[0] == "www":
        labels.pop(0)
    if len(labels) > 1:
        labels = labels[:-1]
    raw = "-".join(labels) or "site"
    raw = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    return (slug or "site")[:64]


@dataclass
class CaptureConfig:
    url: str
    output_dir: Path = field(default_factory=lambda: default_output_dir())
    full: bool = False
    desktop: bool = True
    mobile: bool = False
    timeout: int = 120
    scroll: bool = True
    screenshots: bool = True
    visible_browser: bool = False
    allow_private: bool = False
    browser_channel: Literal["auto", "msedge", "chrome"] = "auto"
    browser_session: BrowserSessionMode = "isolated"

    def __post_init__(self) -> None:
        self.url = validate_url(self.url, allow_private=self.allow_private)
        self.output_dir = Path(self.output_dir).expanduser().resolve()
        if not self.desktop and not self.mobile:
            raise ValidationError("At least one capture profile must be selected.")
        if not 10 <= self.timeout <= 3600:
            raise ValidationError("--timeout must be between 10 and 3600 seconds.")
        if self.browser_channel not in {"auto", "msedge", "chrome"}:
            raise ValidationError("Browser must be auto, msedge, or chrome.")
        if self.browser_session not in _BROWSER_SESSION_MODES:
            raise ValidationError("Browser session must be isolated, persistent_chrome, or persistent_edge.")
        expected_channel = {
            "persistent_chrome": "chrome",
            "persistent_edge": "msedge",
        }.get(self.browser_session)
        if expected_channel and self.browser_channel not in {"auto", expected_channel}:
            raise ValidationError(
                f"{self.browser_session.replace('_', ' ')} requires browser channel {expected_channel}."
            )


    @property
    def effective_browser_channel(self) -> Literal["auto", "msedge", "chrome"]:
        if self.browser_session == "persistent_chrome":
            return "chrome"
        if self.browser_session == "persistent_edge":
            return "msedge"
        return self.browser_channel

    @property
    def persistent_profile_dir(self) -> Path | None:
        if self.browser_session == "isolated":
            return None
        return browser_profile_dir(self.browser_session)

    @property
    def profiles(self) -> tuple[CaptureProfile, ...]:
        selected: list[CaptureProfile] = []
        if self.desktop:
            selected.append(DESKTOP_PROFILE)
        if self.mobile:
            selected.append(MOBILE_PROFILE)
        return tuple(selected)

    @property
    def slug(self) -> str:
        return site_slug(self.url)

    def check_output_writable(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        probe = self.output_dir / f".sitecapture-write-{os.getpid()}"
        try:
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as exc:
            raise ValidationError(f"Output directory is not writable: {self.output_dir}") from exc
