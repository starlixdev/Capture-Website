"""Hardened native browser capture backed by Playwright and Edge/Chrome.

Isolated captures use a fresh non-persistent context. Optional persistent captures use
a CaptureWebsite-owned browser profile under the application's per-user data directory;
the user's normal Chrome/Edge profile is never opened or copied. Network routing rejects
non-HTTP schemes, non-standard ports, and local/private destinations unless the CLI-only
expert override is enabled.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import ipaddress
import json
import os
import re
import selectors
import shutil
import socket
import socketserver
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import Browser, BrowserContext, Error as PlaywrightError, Page, Request, Response, Route
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import WebSocketRoute
from playwright.sync_api import sync_playwright
from warcio.archiveiterator import ArchiveIterator
from warcio.statusandheaders import StatusAndHeaders
from warcio.timeutils import iso_date_to_timestamp
from warcio.warcwriter import WARCWriter

from sitecapture.config import BrowserSessionMode, CaptureConfig, CaptureProfile, browser_profile_dir, redact_url
from sitecapture.errors import CaptureCancelled, CaptureError, PrerequisiteError, ValidationError
from sitecapture.security import redact_text
from sitecapture.version import __version__

ProgressCallback = Callable[[str, str], None]

_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_SECRET_HEADERS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
}
_LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
_EDGE_PATHS = (
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
    / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
    / "Microsoft/Edge/Application/msedge.exe",
    _LOCALAPPDATA / "Microsoft/Edge/Application/msedge.exe",
)
_CHROME_PATHS = (
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
    / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
    / "Google/Chrome/Application/chrome.exe",
    _LOCALAPPDATA / "Google/Chrome/Application/chrome.exe",
)
_MAX_CAPTURED_REQUESTS = 10_000
_MAX_CAPTURED_RESPONSES = 10_000
_MAX_RESPONSE_BODY = 128 * 1024 * 1024
_MAX_ARCHIVE_BODY_BYTES = 2 * 1024 * 1024 * 1024


def _browser_spec(channel: str) -> tuple[str, tuple[Path, ...]]:
    if channel == "msedge":
        return "Microsoft Edge", _EDGE_PATHS
    if channel == "chrome":
        return "Google Chrome", _CHROME_PATHS
    raise ValueError(f"Unsupported browser channel: {channel}")


def _persistent_channel(mode: BrowserSessionMode) -> str:
    if mode == "persistent_chrome":
        return "chrome"
    if mode == "persistent_edge":
        return "msedge"
    raise ValueError("Isolated mode does not have a persistent browser channel.")


def _persistent_launch_args() -> list[str]:
    # Keep these profiles independent from the user's personal browser while leaving
    # ordinary website login behavior intact. Browser account sync/extensions are not
    # needed for CaptureWebsite-owned sessions.
    return [
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-sync",
        "--no-default-browser-check",
        "--no-first-run",
    ]


def open_persistent_browser_session(
    mode: BrowserSessionMode,
    *,
    progress: ProgressCallback | None = None,
) -> None:
    """Open a visible CaptureWebsite-owned Chrome/Edge profile for manual login.

    The call returns after the user closes the browser window. It never attaches to or
    copies the user's normal browser profile.
    """
    if mode == "isolated":
        raise ValidationError("Choose Persistent Chrome or Persistent Edge before opening a session.")
    channel = _persistent_channel(mode)
    browser_name, paths = _browser_spec(channel)
    if not any(path.is_file() for path in paths):
        raise PrerequisiteError(f"{browser_name} was not found. Install a current stable browser first.")
    profile_dir = browser_profile_dir(mode)
    if progress:
        progress("session", f"Opening the CaptureWebsite {browser_name} session")
    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                channel=channel,
                headless=False,
                no_viewport=True,
                accept_downloads=False,
                args=_persistent_launch_args(),
            )
            try:
                if not context.pages:
                    context.new_page()
                # Keep dispatching Playwright events until the user closes the browser.
                while True:
                    pages = context.pages
                    if not pages:
                        break
                    try:
                        pages[0].wait_for_timeout(400)
                    except PlaywrightError:
                        break
            finally:
                try:
                    context.close()
                except PlaywrightError:
                    pass
    except PlaywrightError as exc:
        message = redact_text(str(exc))
        if "user data directory is already in use" in message.lower() or "processsingleton" in message.lower():
            raise CaptureError(
                f"The CaptureWebsite {browser_name} session is already open. Close it before opening or capturing with this profile."
            ) from exc
        raise CaptureError(f"Could not open the CaptureWebsite {browser_name} session: {message}") from exc


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _cdxj_url_key(url: str) -> str:
    """Create a deterministic SURT-style URL key without network lookups."""
    try:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme.lower() not in {"http", "https"} or not hostname:
            return url
        try:
            ipaddress.ip_address(hostname.strip("[]"))
            host_key = hostname
        except ValueError:
            host_key = ",".join(reversed(hostname.split(".")))
        port = parsed.port
        expected = 443 if parsed.scheme.lower() == "https" else 80
        if port and port != expected:
            host_key += f":{port}"
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        return f"{host_key}){path}"
    except ValueError:
        return url


def _write_cdxj(warc_path: Path, destination: Path) -> None:
    """Write the CDXJ random-access index required by WACZ 1.2."""
    lines: list[str] = []
    with warc_path.open("rb") as stream:
        iterator = ArchiveIterator(stream)
        for record in iterator:
            if record.rec_type not in {"response", "revisit", "resource"}:
                continue
            url = record.rec_headers.get_header("WARC-Target-URI")
            date = record.rec_headers.get_header("WARC-Date")
            if not url or not date:
                continue
            if record.http_headers:
                mime = record.http_headers.get_header("Content-Type") or "application/octet-stream"
                mime = mime.split(";", 1)[0].strip().lower()
                status = record.http_headers.get_statuscode()
            else:
                mime = record.rec_headers.get_header("Content-Type") or "application/octet-stream"
                status = None
            value: dict[str, str] = {
                "url": url,
                "mime": mime,
                "length": str(iterator.get_record_length()),
                "offset": str(iterator.get_record_offset()),
                "filename": warc_path.name,
            }
            digest = record.rec_headers.get_header("WARC-Payload-Digest")
            if digest:
                value["digest"] = digest
            if status:
                value["status"] = status
            line = (
                f"{_cdxj_url_key(url)} {iso_date_to_timestamp(date)} "
                f"{json.dumps(value, ensure_ascii=False, sort_keys=True)}"
            )
            lines.append(line)
    if not lines:
        raise CaptureError("The WARC contained no indexable response or resource records.")
    destination.write_text("\n".join(sorted(lines)) + "\n", encoding="utf-8", newline="\n")


def validate_native_wacz(path: Path) -> None:
    """Validate the WACZ 1.2 package structure and every declared resource."""
    try:
        with zipfile.ZipFile(path, "r") as archive:
            infos = archive.infolist()
            names = {info.filename for info in infos}
            if len(names) != len(infos):
                raise CaptureError("Generated WACZ contains duplicate ZIP member names.")
            required = {
                "archive/data.warc.gz",
                "indexes/index.cdx",
                "pages/pages.jsonl",
                "datapackage.json",
                "datapackage-digest.json",
            }
            missing = sorted(required - names)
            if missing:
                raise CaptureError(f"Generated WACZ is missing required members: {', '.join(missing)}")
            for info in infos:
                name = info.filename
                parts = Path(name.replace("/", os.sep)).parts
                if not name or "\\" in name or name.startswith("/") or ".." in parts or ":" in parts[0]:
                    raise CaptureError(f"Generated WACZ contains an unsafe member path: {name!r}")
            if archive.getinfo("archive/data.warc.gz").compress_type != zipfile.ZIP_STORED:
                raise CaptureError("Generated WACZ recompressed its gzipped WARC member.")

            package_bytes = archive.read("datapackage.json")
            package = json.loads(package_bytes)
            if package.get("profile") != "wacz" or package.get("waczVersion") != "1.2.0":
                raise CaptureError("Generated WACZ has an unexpected data-package profile or version.")
            declared = {item["path"]: item for item in package.get("resources", [])}
            actual_resources = names - {"datapackage.json", "datapackage-digest.json"}
            if set(declared) != actual_resources:
                raise CaptureError("Generated WACZ resource manifest does not exactly match its members.")
            for name in required - {"datapackage.json", "datapackage-digest.json"}:
                item = declared.get(name)
                if not item:
                    raise CaptureError(f"Generated WACZ does not declare {name} in datapackage.json.")
                content = archive.read(name)
                if item.get("bytes") != len(content) or item.get("hash") != f"sha256:{_sha256_bytes(content)}":
                    raise CaptureError(f"Generated WACZ fixity validation failed for {name}.")

            digest = json.loads(archive.read("datapackage-digest.json"))
            expected = f"sha256:{_sha256_bytes(package_bytes)}"
            if digest != {"path": "datapackage.json", "hash": expected}:
                raise CaptureError("Generated WACZ datapackage digest validation failed.")
            index_lines = archive.read("indexes/index.cdx").decode("utf-8").splitlines()
            if not index_lines:
                raise CaptureError("Generated WACZ has an empty CDXJ index.")
            for line in index_lines:
                parts = line.split(" ", 2)
                if len(parts) != 3 or not re.fullmatch(r"\d{14}", parts[1]):
                    raise CaptureError("Generated WACZ has a malformed CDXJ index.")
                value = json.loads(parts[2])
                if not {"url", "length", "offset", "filename"}.issubset(value):
                    raise CaptureError("Generated WACZ CDXJ entry is missing required lookup fields.")
            if archive.testzip() is not None:
                raise CaptureError("Generated WACZ failed ZIP CRC validation.")
    except (KeyError, json.JSONDecodeError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        raise CaptureError(f"Generated WACZ validation failed: {exc}") from exc


def _safe_header_pairs(headers: dict[str, str]) -> list[tuple[str, str]]:
    """Return archive-safe headers, excluding credentials and HTTP encoding framing."""
    result: list[tuple[str, str]] = []
    for raw_name, raw_value in headers.items():
        name = str(raw_name).strip()
        lowered = name.lower()
        if (
            not _HEADER_NAME.fullmatch(name)
            or lowered in _SECRET_HEADERS
            or lowered in {"content-length", "content-encoding", "transfer-encoding"}
        ):
            continue
        value = re.sub(r"[\x00-\x1f\x7f]+", " ", redact_text(str(raw_value))).strip()
        result.append((name, value[:16_384]))
    return result


def _request_target(url: str) -> str:
    parsed = urlsplit(redact_url(url)[0])
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    return target


def assert_safe_network_url(url: str, *, allow_private: bool = False) -> None:
    """Reject SSRF-style destinations before Chromium is allowed to request them."""
    try:
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise ValidationError(f"Blocked malformed request URL: {exc}") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValidationError("Blocked a browser request using an unsupported URL scheme.")
    if not allow_private and port not in {80, 443}:
        raise ValidationError(f"Blocked non-standard network port {port}.")
    if allow_private:
        return

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValidationError("Blocked a browser request to localhost.")
    try:
        literal = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise ValidationError("Blocked a browser request to a local/private IP address.")
        return

    _resolve_network_target(hostname, port, allow_private=False)


def _resolve_network_target(
    hostname: str,
    port: int,
    *,
    allow_private: bool,
) -> list[tuple[int, int, int, str, tuple[Any, ...]]]:
    """Resolve once, validate every result, and return socket addresses for pinned connection."""
    try:
        addresses = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValidationError(f"DNS resolution failed for {hostname}.") from exc
    if not addresses:
        raise ValidationError(f"DNS returned no address for {hostname}.")
    resolved = {ipaddress.ip_address(item[4][0].split("%", 1)[0]) for item in addresses}
    if not allow_private and any(not address.is_global for address in resolved):
        raise ValidationError(f"Blocked {hostname} because DNS resolved to a local/private address.")
    return addresses


def _connect_addresses(
    addresses: list[tuple[int, int, int, str, tuple[Any, ...]]],
    hostname: str,
    port: int,
) -> socket.socket:
    last_error: OSError | None = None
    for family, socktype, protocol, _canonical, sockaddr in addresses:
        connection = socket.socket(family, socktype, protocol)
        connection.settimeout(20)
        try:
            connection.connect(sockaddr)
            return connection
        except OSError as exc:
            last_error = exc
            connection.close()
    raise OSError(f"Could not connect to the validated address for {hostname}:{port}") from last_error


class _UnapprovedDestination(ValidationError):
    pass


class _SafeProxyServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = False
    block_on_close = True
    allow_reuse_address = False

    def __init__(
        self,
        *,
        allow_private: bool,
        security_log: Callable[[str, str, str | None], None],
    ) -> None:
        self.allow_private = allow_private
        self.security_log = security_log
        self._allowed: dict[tuple[str, int], list[tuple[int, int, int, str, tuple[Any, ...]]]] = {}
        self._allowed_lock = threading.Lock()
        self._stopping = threading.Event()
        super().__init__(("127.0.0.1", 0), _SafeProxyHandler, bind_and_activate=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def allow_url(self, url: str) -> None:
        assert_safe_network_url(url, allow_private=self.allow_private)
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        addresses = _resolve_network_target(hostname, port, allow_private=self.allow_private)
        with self._allowed_lock:
            self._allowed[(hostname, port)] = addresses

    def connect_allowed(self, hostname: str, port: int) -> socket.socket:
        key = (hostname.lower().rstrip("."), port)
        with self._allowed_lock:
            addresses = self._allowed.get(key)
        if not addresses:
            raise _UnapprovedDestination(
                f"Blocked a browser background destination not initiated by the captured page: {hostname}:{port}."
            )
        return _connect_addresses(addresses, hostname, port)

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    def stop(self) -> None:
        self._stopping.set()
        self.shutdown()
        self.server_close()


class _SafeProxyHandler(socketserver.BaseRequestHandler):
    server: _SafeProxyServer

    def handle(self) -> None:
        self.request.settimeout(30)
        remote: socket.socket | None = None
        target_url: str | None = None
        try:
            header, remainder = self._read_header(self.request)
            first_line, *header_lines = header.decode("iso-8859-1").split("\r\n")
            method, target, version = first_line.split(" ", 2)
            if method.upper() == "CONNECT":
                parsed = urlsplit("//" + target)
                hostname = parsed.hostname
                port = parsed.port or 443
                target_url = f"https://{target}/"
                if not hostname:
                    raise ValidationError("Proxy blocked a malformed CONNECT destination.")
                remote = self.server.connect_allowed(hostname, port)
                self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if remainder:
                    remote.sendall(remainder)
                self._tunnel(self.request, remote)
                return

            parsed = urlsplit(target)
            target_url = target
            hostname = parsed.hostname
            if not hostname:
                raise ValidationError("Proxy blocked an HTTP request without a hostname.")
            port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
            remote = self.server.connect_allowed(hostname, port)
            origin_target = parsed.path or "/"
            if parsed.query:
                origin_target += "?" + parsed.query
            safe_headers = [
                line
                for line in header_lines
                if line
                and not line.lower().startswith("proxy-connection:")
                and not line.lower().startswith("connection:")
                and not line.lower().startswith("proxy-authorization:")
            ]
            outbound = (
                f"{method} {origin_target} {version}\r\n"
                + "\r\n".join(safe_headers)
                + "\r\nConnection: close\r\n\r\n"
            ).encode("iso-8859-1")
            remote.sendall(outbound + remainder)
            self._tunnel(self.request, remote)
        except _UnapprovedDestination as exc:
            self.server.security_log("background_request_blocked", str(exc), target_url)
            self._reply_error(403, "Blocked by CaptureWebsite security policy")
        except ValidationError as exc:
            self.server.security_log("blocked_request", str(exc), target_url)
            self._reply_error(403, "Blocked by CaptureWebsite security policy")
        except (OSError, UnicodeError, ValueError) as exc:
            self.server.security_log("proxy_error", str(exc), target_url)
            self._reply_error(502, "CaptureWebsite secure proxy error")
        finally:
            if remote is not None:
                remote.close()

    @staticmethod
    def _read_header(connection: socket.socket) -> tuple[bytes, bytes]:
        content = bytearray()
        while b"\r\n\r\n" not in content:
            chunk = connection.recv(8192)
            if not chunk:
                raise OSError("Proxy client closed before sending complete headers.")
            content.extend(chunk)
            if len(content) > 64 * 1024:
                raise OSError("Proxy request headers exceeded the 64 KiB safety limit.")
        header, remainder = bytes(content).split(b"\r\n\r\n", 1)
        return header, remainder

    def _tunnel(self, left: socket.socket, right: socket.socket) -> None:
        selector = selectors.DefaultSelector()
        selector.register(left, selectors.EVENT_READ, right)
        selector.register(right, selectors.EVENT_READ, left)
        idle_deadline = time.monotonic() + 60.0
        try:
            while not self.server.stopping:
                remaining = idle_deadline - time.monotonic()
                if remaining <= 0:
                    return
                events = selector.select(timeout=min(0.5, remaining))
                if not events:
                    continue
                idle_deadline = time.monotonic() + 60.0
                for key, _mask in events:
                    source = key.fileobj
                    destination = key.data
                    data = source.recv(64 * 1024)
                    if not data:
                        return
                    destination.sendall(data)
        finally:
            selector.close()

    def _reply_error(self, status: int, message: str) -> None:
        try:
            body = message.encode("utf-8")
            response = (
                f"HTTP/1.1 {status} {message}\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii") + body
            self.request.sendall(response)
        except OSError:
            pass


@dataclass(frozen=True)
class NativeArtifact:
    profile: CaptureProfile
    collection: str
    wacz_path: Path
    collection_dir: Path
    stdout_log: Path
    command: list[str]


class _ArchiveRecorder:
    def __init__(self, warc_path: Path, event_log: Path, profile: CaptureProfile) -> None:
        self.warc_path = warc_path
        self.event_log = event_log
        self.profile = profile
        self.request_ids: dict[int, str] = {}
        self.requests_seen: set[int] = set()
        self.responses_seen: set[int] = set()
        self.response_count = 0
        self.body_bytes = 0
        self._lock = threading.RLock()
        self._warc_stream = warc_path.open("wb")
        self._writer = WARCWriter(self._warc_stream, gzip=True)
        self._log_stream = event_log.open("w", encoding="utf-8", newline="\n")
        warcinfo = self._writer.create_warcinfo_record(
            "sitecapture.warc.gz",
            {
                "software": f"CaptureWebsite/{__version__} Playwright",
                "format": "WARC File Format 1.1",
                "conformsTo": "https://iipc.github.io/warc-specifications/specifications/warc-format/warc-1.1/",
                "isPartOf": f"CaptureWebsite native {profile.name} capture",
                "description": "Credential-free, browser-observed capture. Browser-decoded response entities are stored.",
            },
        )
        self._writer.write_record(warcinfo)

    def log(self, level: str, kind: str, message: str, **details: object) -> None:
        value = {
            "timestamp": _utc_now(),
            "logLevel": level,
            "kind": kind,
            "message": redact_text(message),
            "captureProfile": self.profile.name,
            **details,
        }
        with self._lock:
            self._log_stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
            self._log_stream.flush()

    def record_request(self, request: Request) -> None:
        key = id(request)
        with self._lock:
            if key in self.requests_seen:
                return
            self.requests_seen.add(key)

        safe_url = redact_url(request.url)[0]
        try:
            headers = _safe_header_pairs(request.all_headers())
        except PlaywrightError:
            headers = []
        http = StatusAndHeaders(
            f"{request.method.upper()} {_request_target(request.url)} HTTP/1.1",
            headers,
            protocol="HTTP/1.1",
        )
        with self._lock:
            record = self._writer.create_warc_record(
                safe_url,
                "request",
                payload=BytesIO(b""),
                length=0,
                http_headers=http,
                warc_headers_dict={"WARC-Date": _utc_now()},
            )
            self._writer.write_record(record)
            record_id = record.rec_headers.get_header("WARC-Record-ID")
            if record_id:
                self.request_ids[key] = record_id

    def record_response(self, request: Request) -> None:
        key = id(request)
        with self._lock:
            if key in self.responses_seen or self.response_count >= _MAX_CAPTURED_RESPONSES:
                return
            self.responses_seen.add(key)

        response = request.response()
        if response is None:
            return
        with self._lock:
            self.response_count += 1

        body = b""
        truncated: str | None = None
        try:
            declared = int(response.header_value("content-length") or 0)
        except (TypeError, ValueError, PlaywrightError):
            declared = 0
        if declared > _MAX_RESPONSE_BODY:
            truncated = "declared response exceeds the 128 MiB per-resource safety limit"
        else:
            with self._lock:
                archive_limit_reached = self.body_bytes >= _MAX_ARCHIVE_BODY_BYTES
            if archive_limit_reached:
                truncated = "capture reached the 2 GiB decoded-body safety limit"
            else:
                try:
                    candidate = response.body()
                    if len(candidate) > _MAX_RESPONSE_BODY:
                        truncated = "response exceeds the 128 MiB per-resource safety limit"
                    else:
                        with self._lock:
                            if self.body_bytes + len(candidate) > _MAX_ARCHIVE_BODY_BYTES:
                                truncated = "capture reached the 2 GiB decoded-body safety limit"
                            else:
                                body = candidate
                                self.body_bytes += len(body)
                except PlaywrightError as exc:
                    truncated = f"browser body unavailable: {exc}"

        try:
            headers = _safe_header_pairs(response.all_headers())
        except PlaywrightError:
            headers = []
        headers.append(("Content-Length", str(len(body))))
        headers.append(("X-SiteCapture-Body-Representation", "decoded-by-browser"))
        http = StatusAndHeaders(
            f"{response.status} {response.status_text or ''}".strip(),
            headers,
            protocol="HTTP/1.1",
        )
        warc_headers = {"WARC-Date": _utc_now()}
        with self._lock:
            request_id = self.request_ids.get(key)
        if request_id:
            warc_headers["WARC-Concurrent-To"] = request_id
        if truncated:
            warc_headers["WARC-Truncated"] = "length"
            self.log("warn", "resource_limit", truncated, url=redact_url(request.url)[0])
        with self._lock:
            record = self._writer.create_warc_record(
                redact_url(response.url)[0],
                "response",
                payload=BytesIO(body),
                length=len(body),
                http_headers=http,
                warc_headers_dict=warc_headers,
            )
            self._writer.write_record(record)

    def record_screenshot(self, target_url: str, kind: str, content: bytes) -> None:
        with self._lock:
            record = self._writer.create_warc_record(
                f"urn:{kind}:{redact_url(target_url)[0]}",
                "resource",
                payload=BytesIO(content),
                length=len(content),
                warc_content_type="image/png",
                warc_headers_dict={"WARC-Date": _utc_now()},
            )
            self._writer.write_record(record)

    def close(self) -> None:
        with self._lock:
            self._log_stream.close()
            self._warc_stream.close()


class NativeBrowserRunner:
    engine_name = "CaptureWebsite Native Browser"

    def __init__(self, config: CaptureConfig) -> None:
        self.config = config
        self.playwright_version = importlib.metadata.version("playwright")
        self.browser_name = ""
        self.browser_version = ""
        self.channel = ""

    def preflight(self, progress: ProgressCallback) -> None:
        session_label = "isolated" if self.config.browser_session == "isolated" else "persistent"
        progress("prerequisites", f"Checking the {session_label} native browser runtime")
        self.config.check_output_writable()
        requested_channel = self.config.effective_browser_channel
        candidates: list[tuple[str, str, tuple[Path, ...]]] = []
        if requested_channel in {"auto", "msedge"}:
            candidates.append(("msedge", "Microsoft Edge", _EDGE_PATHS))
        if requested_channel in {"auto", "chrome"}:
            candidates.append(("chrome", "Google Chrome", _CHROME_PATHS))
        for channel, name, paths in candidates:
            if any(path.is_file() for path in paths):
                self.channel = channel
                self.browser_name = name
                return
        requested = "Microsoft Edge or Google Chrome" if requested_channel == "auto" else requested_channel
        raise PrerequisiteError(
            f"{requested} was not found. Install a current stable Edge or Chrome browser; Docker is not required."
        )

    def _launch_browser_context(
        self,
        playwright: Any,
        profile: CaptureProfile,
        proxy_url: str,
    ) -> tuple[Browser | None, BrowserContext]:
        capture_args = [
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-default-apps",
            "--disable-domain-reliability",
            "--disable-extensions",
            "--disable-notifications",
            "--disable-quic",
            "--disable-sync",
            "--metrics-recording-only",
            "--no-default-browser-check",
            "--no-first-run",
            "--proxy-bypass-list=<-loopback>",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        ]
        context_options: dict[str, Any] = {
            "accept_downloads": False,
            "service_workers": "block",
            "ignore_https_errors": False,
            "java_script_enabled": True,
            "viewport": {
                "width": int(profile.viewport["width"]),
                "height": int(profile.viewport["height"]),
            },
            "device_scale_factor": float(profile.viewport.get("deviceScaleFactor", 1)),
            "is_mobile": bool(profile.viewport.get("isMobile", False)),
            "has_touch": bool(profile.viewport.get("isMobile", False)),
            "proxy": {"server": proxy_url, "bypass": ""},
        }
        if profile.user_agent:
            context_options["user_agent"] = profile.user_agent

        if self.config.browser_session != "isolated":
            profile_dir = self.config.persistent_profile_dir
            if profile_dir is None:
                raise CaptureError("Persistent browser session did not resolve to a profile directory.")
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                channel=self.channel,
                headless=not self.config.visible_browser,
                args=capture_args,
                **context_options,
            )
            context_browser = context.browser
            if context_browser is not None:
                self.browser_version = context_browser.version
            return None, context

        browser = playwright.chromium.launch(
            channel=self.channel,
            headless=not self.config.visible_browser,
            args=capture_args,
        )
        self.browser_version = browser.version
        return browser, browser.new_context(**context_options)

    def _navigate_initial(
        self,
        page: Page,
        cancel_event: threading.Event,
        recorder: _ArchiveRecorder,
    ) -> tuple[Response | None, bool]:
        """Navigate while keeping cancellation responsive after the document commits."""
        self._check_cancel(cancel_event)
        deadline = time.monotonic() + self.config.timeout
        try:
            response = page.goto(
                self.config.url,
                wait_until="commit",
                timeout=self.config.timeout * 1000,
            )
        except PlaywrightTimeoutError:
            recorder.log("warn", "timeout", "Initial navigation timed out; preserving the partial capture.")
            return None, False
        except PlaywrightError:
            self._check_cancel(cancel_event)
            if page.is_closed() and recorder.response_count > 0:
                recorder.log(
                    "warn",
                    "page_closed",
                    "The site or operator closed the capture page; preserving responses already captured.",
                    url=redact_url(self.config.url)[0],
                )
                return None, True
            raise

        while True:
            self._check_cancel(cancel_event)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                recorder.log("warn", "timeout", "Initial navigation timed out; preserving the partial capture.")
                return None, False
            try:
                page.wait_for_load_state("load", timeout=max(1, min(250, int(remaining * 1000))))
                return response, False
            except PlaywrightTimeoutError:
                continue
            except PlaywrightError:
                self._check_cancel(cancel_event)
                if page.is_closed() and recorder.response_count > 0:
                    recorder.log(
                        "warn",
                        "page_closed",
                        "The site or operator closed the capture page; preserving responses already captured.",
                        url=redact_url(self.config.url)[0],
                    )
                    return None, True
                raise

    def capture_profile(
        self,
        profile: CaptureProfile,
        capture_root: Path,
        collection: str,
        _owned_process_name: str,
        cancel_event: threading.Event,
        progress: ProgressCallback,
    ) -> NativeArtifact:
        capture_root.mkdir(parents=True, exist_ok=False)
        collection_dir = capture_root / "collections" / collection
        archive_dir = collection_dir / "archive"
        pages_dir = collection_dir / "pages"
        logs_dir = collection_dir / "logs"
        archive_dir.mkdir(parents=True)
        pages_dir.mkdir()
        logs_dir.mkdir()
        warc_path = archive_dir / "data.warc.gz"
        event_log = logs_dir / "events.jsonl"
        stdout_log = capture_root / f"native-{profile.name}.stdout.log"
        persistent = self.config.browser_session != "isolated"
        session_description = (
            f"CaptureWebsite-owned persistent {self.browser_name} profile"
            if persistent
            else "temporary isolated context; no user profile loaded"
        )
        stdout_log.write_text(
            "CaptureWebsite native capture\n"
            "Docker: not used\n"
            f"Browser channel: {self.channel}\n"
            f"Browser session: {self.config.browser_session}\n"
            f"Profile isolation: {session_description}\n"
            f"Browser visibility: {'visible compatibility mode' if self.config.visible_browser else 'headless'}\n",
            encoding="utf-8",
            newline="\n",
        )
        if self.config.visible_browser:
            progress(
                "capture",
                f"Capturing the {profile.name} profile in the {session_description}; "
                "keep the window open and complete any site verification",
            )
        else:
            progress("capture", f"Capturing the {profile.name} profile in the {session_description}")
        recorder = _ArchiveRecorder(warc_path, event_log, profile)

        def security_log(kind: str, message: str, url: str | None) -> None:
            details: dict[str, object] = {}
            if url:
                details["url"] = redact_url(url)[0]
            level = "info" if kind == "background_request_blocked" else "warn"
            recorder.log(level, kind, message, **details)

        proxy_server = _SafeProxyServer(
            allow_private=self.config.allow_private,
            security_log=security_log,
        )
        proxy_thread = threading.Thread(
            target=proxy_server.serve_forever,
            name=f"sitecapture-secure-proxy-{profile.name}",
            daemon=False,
        )
        proxy_started = False
        title = ""
        final_url = self.config.url
        browser: Browser | None = None
        context: BrowserContext | None = None
        try:
            proxy_thread.start()
            proxy_started = True
            proxy_server.allow_url(self.config.url)
            with sync_playwright() as playwright:
                browser, context = self._launch_browser_context(playwright, profile, proxy_server.url)
                context.set_default_timeout(min(self.config.timeout * 1000, 60_000))
                context.set_default_navigation_timeout(self.config.timeout * 1000)
                inflight: set[int] = set()
                main_page: Page | None = None
                routed_requests = 0
                main_document = {"status": None, "cloudflare_challenge": False}

                def on_request(request: Request) -> None:
                    try:
                        proxy_server.allow_url(request.url)
                    except ValidationError as exc:
                        recorder.log("warn", "blocked_request", str(exc), url=redact_url(request.url)[0])
                    inflight.add(id(request))
                    recorder.record_request(request)

                def on_finished(request: Request) -> None:
                    try:
                        recorder.record_response(request)
                    finally:
                        inflight.discard(id(request))

                def on_failed(request: Request) -> None:
                    inflight.discard(id(request))
                    failure = request.failure or "browser request failed"
                    recorder.log(
                        "warn",
                        "connection_failure",
                        failure,
                        url=redact_url(request.url)[0],
                    )

                def on_response(response: Response) -> None:
                    if (
                        main_page is not None
                        and response.request.is_navigation_request()
                        and response.frame == main_page.main_frame
                    ):
                        main_document["status"] = response.status
                        try:
                            main_document["cloudflare_challenge"] = (
                                response.header_value("cf-mitigated") or ""
                            ).lower() == "challenge"
                        except PlaywrightError:
                            main_document["cloudflare_challenge"] = False
                    if not 300 <= response.status < 400:
                        return
                    try:
                        location = response.header_value("location")
                    except PlaywrightError:
                        location = None
                    if not location:
                        return
                    destination = urljoin(response.url, location)
                    try:
                        proxy_server.allow_url(destination)
                    except ValidationError as exc:
                        recorder.log(
                            "warn",
                            "blocked_redirect",
                            str(exc),
                            url=redact_url(destination)[0],
                        )

                def route_request(route: Route, request: Request) -> None:
                    nonlocal routed_requests
                    if cancel_event.is_set():
                        route.abort("aborted")
                        return
                    routed_requests += 1
                    if routed_requests > _MAX_CAPTURED_REQUESTS:
                        recorder.log(
                            "warn",
                            "resource_limit",
                            "Request blocked after the 10,000-request safety limit was reached.",
                            url=redact_url(request.url)[0],
                        )
                        route.abort("blockedbyclient")
                        return
                    try:
                        proxy_server.allow_url(request.url)
                    except ValidationError as exc:
                        recorder.log("warn", "blocked_request", str(exc), url=redact_url(request.url)[0])
                        route.abort("blockedbyclient")
                        return
                    route.continue_()

                def block_websocket(websocket: WebSocketRoute) -> None:
                    recorder.log(
                        "warn",
                        "websocket_blocked",
                        "WebSocket blocked because its traffic cannot be safely archived or inspected.",
                        url=redact_url(websocket.url)[0],
                    )
                    websocket.close(code=1008, reason="Blocked by CaptureWebsite security policy")

                def on_page(new_page: Page) -> None:
                    if main_page is not None and new_page != main_page:
                        recorder.log("warn", "popup_blocked", "An unsolicited popup was closed.")
                        try:
                            new_page.close()
                        except PlaywrightError:
                            pass

                context.route("**/*", route_request)
                context.route_web_socket("**", block_websocket)
                context.on("request", on_request)
                context.on("requestfinished", on_finished)
                context.on("requestfailed", on_failed)
                context.on("response", on_response)
                context.on("page", on_page)
                main_page = context.new_page()
                if persistent:
                    for existing_page in list(context.pages):
                        if existing_page != main_page:
                            try:
                                existing_page.close()
                            except PlaywrightError:
                                pass
                main_page.on("dialog", lambda dialog: dialog.dismiss())
                main_page.on("download", lambda download: download.cancel())
                response, page_closed_during_navigation = self._navigate_initial(
                    main_page, cancel_event, recorder
                )
                if not page_closed_during_navigation and response is not None and response.status >= 400:
                    if main_document["cloudflare_challenge"]:
                        progress(
                            "verification",
                            "The site requested automatic verification; waiting for it to complete safely",
                        )
                        challenge_deadline = time.monotonic() + min(
                            120.0 if self.config.visible_browser else 30.0,
                            max(30.0 if self.config.visible_browser else 8.0, self.config.timeout / 2),
                        )
                        while (
                            time.monotonic() < challenge_deadline
                            and main_document["cloudflare_challenge"]
                        ):
                            self._check_cancel(cancel_event)
                            main_page.wait_for_timeout(250)
                    if main_document["status"] is not None and int(main_document["status"]) < 400:
                        recorder.log(
                            "info",
                            "site_verification_completed",
                            "The site's automatic verification completed successfully.",
                            url=redact_url(main_page.url)[0],
                        )
                    elif main_document["cloudflare_challenge"]:
                        raise CaptureError(
                            "The site kept an anti-bot verification active (HTTP 403). "
                            "Enable 'Compatibility: open in visible browser' and complete the verification in the "
                            "capture window; CaptureWebsite does not bypass the protection."
                        )
                    else:
                        status = main_document["status"] or response.status
                        raise CaptureError(f"The requested page returned HTTP {status}.")
                if not page_closed_during_navigation:
                    self._wait_for_idle(main_page, inflight, cancel_event, 4.0 if self.config.full else 2.0)
                    self._check_cancel(cancel_event)
                    final_url = main_page.url
                    title = main_page.title()
                    if self.config.screenshots:
                        recorder.record_screenshot(final_url, "view", main_page.screenshot(type="png"))
                    if self.config.scroll:
                        progress("behavior", f"Safely scrolling the {profile.name} page to activate lazy resources")
                        self._safe_scroll(main_page, cancel_event, 30.0 if self.config.full else 15.0)
                        self._wait_for_idle(main_page, inflight, cancel_event, 4.0 if self.config.full else 2.0)
                    if self.config.screenshots:
                        recorder.record_screenshot(
                            final_url,
                            "fullPageFinal",
                            main_page.screenshot(type="png", full_page=True),
                        )
                self._check_cancel(cancel_event)
                context.close()
                context = None
                if browser is not None:
                    browser.close()
                    browser = None
        except CaptureCancelled:
            raise
        except PlaywrightError as exc:
            if cancel_event.is_set():
                self._check_cancel(cancel_event)
            message = redact_text(str(exc))
            if persistent and (
                "user data directory is already in use" in message.lower()
                or "processsingleton" in message.lower()
            ):
                raise CaptureError(
                    f"The CaptureWebsite {self.browser_name} persistent session is already open. "
                    "Close its browser window before starting a capture with this session."
                ) from exc
            raise CaptureError(f"Native browser capture failed: {message}") from exc
        finally:
            if context is not None:
                try:
                    context.close()
                except PlaywrightError:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except PlaywrightError:
                    pass
            try:
                if proxy_started:
                    proxy_server.stop()
                    proxy_thread.join(timeout=5)
                else:
                    proxy_server.server_close()
            finally:
                recorder.close()

        pages = {
            "format": "json-pages-1.0",
            "id": collection,
            "title": title,
            "url": redact_url(final_url)[0],
            "ts": _utc_now(),
        }
        (pages_dir / "pages.jsonl").write_text(
            json.dumps({"format": "json-pages-1.0", "id": "pages", "title": collection})
            + "\n"
            + json.dumps(pages, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        wacz_path = collection_dir / f"{collection}.wacz"
        self._build_wacz(warc_path, pages_dir / "pages.jsonl", wacz_path, collection, final_url)
        validate_native_wacz(wacz_path)
        return NativeArtifact(
            profile=profile,
            collection=collection,
            wacz_path=wacz_path,
            collection_dir=collection_dir,
            stdout_log=stdout_log,
            command=[
                "sitecapture-native",
                f"--browser={self.channel}",
                (
                    f"--persistent-profile={self.config.browser_session.removeprefix('persistent_')}"
                    if persistent
                    else "--temporary-profile"
                ),
                *(
                    ["--block-private-network"]
                    if not self.config.allow_private
                    else ["--allow-private"]
                ),
                "--pinned-public-proxy",
                "--block-downloads",
                "--block-service-workers",
                *(["--visible-browser"] if self.config.visible_browser else []),
            ],
        )

    def _build_wacz(
        self,
        warc_path: Path,
        pages_path: Path,
        destination: Path,
        collection: str,
        final_url: str,
    ) -> None:
        index_path = warc_path.parent.parent / "indexes" / "index.cdx"
        index_path.parent.mkdir()
        _write_cdxj(warc_path, index_path)

        resources: list[dict[str, object]] = []
        members = (
            ("data.warc.gz", "archive/data.warc.gz", warc_path),
            ("index.cdx", "indexes/index.cdx", index_path),
            ("pages.jsonl", "pages/pages.jsonl", pages_path),
        )
        for name, member, source in members:
            resources.append(
                {
                    "name": name,
                    "path": member,
                    "hash": f"sha256:{_sha256(source)}",
                    "bytes": source.stat().st_size,
                }
            )
        created = _utc_now()
        safe_final_url = redact_url(final_url)[0]
        datapackage = {
            "profile": "wacz",
            "waczVersion": "1.2.0",
            "title": collection,
            "description": "Credential-redacted public web capture created by CaptureWebsite.",
            "created": created,
            "modified": created,
            "software": f"CaptureWebsite/{__version__} Playwright/{self.playwright_version} {self.browser_name}/{self.browser_version}",
            "home": {"url": safe_final_url, "ts": created},
            "resources": resources,
        }
        datapackage_bytes = (
            json.dumps(datapackage, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        digest_bytes = (
            json.dumps(
                {
                    "path": "datapackage.json",
                    "hash": f"sha256:{_sha256_bytes(datapackage_bytes)}",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        temporary = destination.with_suffix(".wacz.tmp")
        with zipfile.ZipFile(temporary, "w", allowZip64=True) as archive:
            archive.writestr("datapackage.json", datapackage_bytes, compress_type=zipfile.ZIP_DEFLATED)
            archive.writestr(
                "datapackage-digest.json", digest_bytes, compress_type=zipfile.ZIP_DEFLATED
            )
            archive.write(warc_path, "archive/data.warc.gz", compress_type=zipfile.ZIP_STORED)
            archive.write(index_path, "indexes/index.cdx", compress_type=zipfile.ZIP_DEFLATED)
            archive.write(pages_path, "pages/pages.jsonl", compress_type=zipfile.ZIP_DEFLATED)
        temporary.replace(destination)

    def _check_cancel(self, cancel_event: threading.Event) -> None:
        if not cancel_event.is_set():
            return
        if self.config.browser_session == "isolated":
            message = "Capture cancelled. The temporary isolated browser context was closed."
        else:
            message = (
                "Capture cancelled. The persistent browser context was closed; "
                "the CaptureWebsite-owned profile was retained."
            )
        raise CaptureCancelled(message)

    def _wait_for_idle(
        self,
        page: Page,
        inflight: set[int],
        cancel_event: threading.Event,
        idle_seconds: float,
    ) -> None:
        deadline = time.monotonic() + min(20.0, max(5.0, self.config.timeout / 3))
        idle_since: float | None = None
        while time.monotonic() < deadline:
            self._check_cancel(cancel_event)
            page.wait_for_timeout(100)
            if inflight:
                idle_since = None
            elif idle_since is None:
                idle_since = time.monotonic()
            elif time.monotonic() - idle_since >= idle_seconds:
                return

    def _safe_scroll(self, page: Page, cancel_event: threading.Event, max_seconds: float) -> None:
        deadline = time.monotonic() + min(max_seconds, self.config.timeout / 2)
        previous = -1
        stable = 0
        while time.monotonic() < deadline and stable < 3:
            self._check_cancel(cancel_event)
            state = page.evaluate(
                """() => {
                    const root = document.scrollingElement || document.documentElement;
                    const step = Math.max(320, Math.floor(window.innerHeight * 0.8));
                    root.scrollTo({top: Math.min(root.scrollTop + step, 100000), behavior: 'auto'});
                    return {top: root.scrollTop, height: Math.min(root.scrollHeight, 100000)};
                }"""
            )
            position = int(state["top"])
            height = int(state["height"])
            stable = stable + 1 if position == previous or position + int(profile_height(page)) >= height else 0
            previous = position
            page.wait_for_timeout(350)
        page.evaluate("() => (document.scrollingElement || document.documentElement).scrollTo(0, 0)")


def profile_height(page: Page) -> int:
    return int(page.evaluate("() => window.innerHeight"))


def command_for_manifest(command: list[str]) -> list[str]:
    return [redact_text(value) for value in command]
