"""Streaming WACZ/WARC response extraction using warcio."""

from __future__ import annotations

import gzip
import hashlib
import os
import re
import shutil
import stat
import tempfile
import zipfile
import zlib
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import urljoin

import brotli
from warcio.archiveiterator import ArchiveIterator

from sitecapture.config import CaptureProfile, redact_url
from sitecapture.errors import ArchiveError
from sitecapture.processor.classifier import classify_content_type, normalize_mime
from sitecapture.processor.naming import local_payload_path
from sitecapture.security import redact_headers, redact_text

ProgressCallback = Callable[[str, str], None]
_WARC_SUFFIX = re.compile(r"\.warc(?:\.gz)?$", re.IGNORECASE)
_MAX_WACZ_MEMBERS = 100_000
_MAX_WACZ_UNCOMPRESSED = 64 * 1024 * 1024 * 1024

def validate_wacz_member(info: zipfile.ZipInfo) -> PurePosixPath:
    name = info.filename
    if not name or "\\" in name or "\x00" in name:
        raise ArchiveError(f"Unsafe WACZ member name: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ArchiveError(f"Unsafe WACZ member path: {name!r}")
    if ":" in path.parts[0]:
        raise ArchiveError(f"Drive-qualified WACZ member is not allowed: {name!r}")
    mode = info.external_attr >> 16
    if mode and stat.S_ISLNK(mode):
        raise ArchiveError(f"Symlink WACZ member is not allowed: {name!r}")
    return path


def safe_output_path(root: Path, relative: str) -> Path:
    base = root.resolve()
    target = (base / Path(*PurePosixPath(relative).parts)).resolve()
    if target != base and base not in target.parents:
        raise ArchiveError(f"Output path escaped capture directory: {relative!r}")
    return target


def _headers(record: Any) -> list[tuple[str, str]]:
    if not record.http_headers:
        return []
    return [(str(name), str(value)) for name, value in record.http_headers.headers]


def _status(record: Any) -> int | None:
    if not record.http_headers:
        return None
    value = str(record.http_headers.statusline or "").split(" ", 1)[0]
    try:
        return int(value)
    except ValueError:
        return None


def _request_method(record: Any) -> str | None:
    if not record.http_headers:
        return None
    value = str(record.http_headers.statusline or "").split(" ", 1)[0]
    return value.upper() if re.fullmatch(r"[A-Za-z]+", value) else None


def _normalize_warc_digest(value: str | None) -> str | None:
    if not value or ":" not in value:
        return None
    algorithm, digest = value.split(":", 1)
    algorithm = algorithm.lower()
    if algorithm not in {"sha1", "sha256"}:
        return None
    digest = digest.strip()
    if not re.fullmatch(r"[0-9A-Za-z+/=_-]+", digest):
        return None
    return f"{algorithm}:{digest.upper()}"


def _decode_one(source: Path, destination: Path, encoding: str) -> None:
    encoding = encoding.strip().lower()
    if encoding in {"", "identity"}:
        shutil.copyfile(source, destination)
        return
    if encoding in {"gzip", "x-gzip"}:
        with gzip.open(source, "rb") as decoded, destination.open("wb") as output:
            shutil.copyfileobj(decoded, output, length=1024 * 1024)
        return
    if encoding == "deflate":
        def inflate(wbits: int) -> None:
            decoder = zlib.decompressobj(wbits)
            with source.open("rb") as input_stream, destination.open("wb") as output:
                while True:
                    chunk = input_stream.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(decoder.decompress(chunk))
                output.write(decoder.flush())

        try:
            inflate(zlib.MAX_WBITS)
        except zlib.error:
            inflate(-zlib.MAX_WBITS)
        return
    if encoding == "br":
        decoder = brotli.Decompressor()
        with source.open("rb") as input_stream, destination.open("wb") as output:
            while True:
                chunk = input_stream.read(1024 * 1024)
                if not chunk:
                    break
                output.write(decoder.process(chunk))
            output.write(decoder.process(b""))
        if not decoder.is_finished():
            raise ValueError("Incomplete Brotli stream")
        return
    raise ValueError(f"Unsupported Content-Encoding: {encoding}")


def decode_http_entity(raw_path: Path, content_encoding: str | None, temp_dir: Path) -> tuple[Path, bool]:
    encodings = [item.strip() for item in (content_encoding or "").split(",") if item.strip()]
    if not encodings or all(item.lower() == "identity" for item in encodings):
        return raw_path, True
    current = raw_path
    made: list[Path] = []
    try:
        for index, encoding in enumerate(reversed(encodings)):
            next_path = temp_dir / f"decode-{os.getpid()}-{id(raw_path)}-{index}.tmp"
            try:
                _decode_one(current, next_path, encoding)
            except Exception:
                next_path.unlink(missing_ok=True)
                raise
            made.append(next_path)
            if current != raw_path:
                current.unlink(missing_ok=True)
            current = next_path
        return current, True
    except (gzip.BadGzipFile, EOFError, zlib.error, brotli.error, ValueError):
        for path in made:
            path.unlink(missing_ok=True)
        return raw_path, False


@dataclass
class ExtractionResult:
    resources: list[dict[str, Any]] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)
    responses: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)


class WaczExtractor:
    def __init__(self, result_root: Path, progress: ProgressCallback | None = None) -> None:
        self.result_root = result_root.resolve()
        self.progress = progress or (lambda _phase, _message: None)
        self.result = ExtractionResult()
        self._payloads: dict[str, str] = {}
        self._warc_digests: dict[str, str] = {}
        self._pending_revisits: list[dict[str, Any]] = []
        self._response_links: list[tuple[dict[str, Any], str, str | None]] = []
        self._request_methods: dict[tuple[str, str], deque[str | None]] = defaultdict(deque)
        self._temp_dir = self.result_root / ".payload-temp"
        self._temp_dir.mkdir(parents=True, exist_ok=True)

    def ingest(self, wacz_path: Path, profile: CaptureProfile, archive_origin: str) -> None:
        self.progress("extract", f"Extracting {profile.name} WACZ responses")
        try:
            with zipfile.ZipFile(wacz_path, "r") as archive:
                members = archive.infolist()
                if len(members) > _MAX_WACZ_MEMBERS:
                    raise ArchiveError("WACZ contains too many members.")
                total_size = 0
                names: set[str] = set()
                for info in members:
                    path = validate_wacz_member(info)
                    total_size += info.file_size
                    if total_size > _MAX_WACZ_UNCOMPRESSED:
                        raise ArchiveError("WACZ uncompressed size exceeds the safety limit.")
                    names.add(path.as_posix())
                if "datapackage.json" not in names:
                    raise ArchiveError("WACZ is missing datapackage.json.")
                warcs = [
                    info
                    for info in members
                    if info.filename.startswith("archive/") and _WARC_SUFFIX.search(info.filename)
                ]
                if not warcs:
                    raise ArchiveError("WACZ contains no archive/*.warc or archive/*.warc.gz members.")
                for info in sorted(warcs, key=lambda item: item.filename):
                    self._ingest_warc(archive, info, profile, archive_origin)
        except zipfile.BadZipFile as exc:
            raise ArchiveError(f"Invalid WACZ ZIP: {wacz_path}") from exc

    def finalize(self) -> ExtractionResult:
        self._resolve_revisits()
        self._link_methods_and_redirects()
        shutil.rmtree(self._temp_dir, ignore_errors=True)
        return self.result

    def _ingest_warc(
        self,
        archive: zipfile.ZipFile,
        info: zipfile.ZipInfo,
        profile: CaptureProfile,
        archive_origin: str,
    ) -> None:
        try:
            with archive.open(info, "r") as stream:
                prefix = stream.read(4)
                stream.seek(0)
                if not (prefix.startswith(b"WARC") or prefix.startswith(b"\x1f\x8b")):
                    self.result.errors.append(
                        {
                            "kind": "archive_parsing_error",
                            "message": "Archive member is not a WARC or gzipped WARC stream.",
                            "captureProfile": profile.name,
                            "archiveOrigin": archive_origin,
                            "warcMember": info.filename,
                        }
                    )
                    return
                record_count = 0
                for index, record in enumerate(ArchiveIterator(stream)):
                    record_count += 1
                    try:
                        if record.rec_type == "response":
                            self._response(record, profile, archive_origin, info.filename, index)
                        elif record.rec_type == "request":
                            self._request(record, profile, archive_origin, info.filename, index)
                        elif record.rec_type == "revisit":
                            self._revisit(record, profile, archive_origin, info.filename, index)
                        elif record.rec_type == "resource":
                            self._resource(record, profile, archive_origin, info.filename, index)
                    except Exception as exc:  # keep a useful capture when one record is bad
                        self.result.errors.append(
                            {
                                "kind": "payload_or_record_error",
                                "message": redact_text(str(exc)),
                                "captureProfile": profile.name,
                                "archiveOrigin": archive_origin,
                                "warcMember": info.filename,
                                "recordIndex": index,
                            }
                        )
                if record_count == 0:
                    self.result.errors.append(
                        {
                            "kind": "archive_parsing_error",
                            "message": "WARC member contained no readable records.",
                            "captureProfile": profile.name,
                            "archiveOrigin": archive_origin,
                            "warcMember": info.filename,
                        }
                    )
        except Exception as exc:
            self.result.errors.append(
                {
                    "kind": "archive_parsing_error",
                    "message": redact_text(str(exc)),
                    "captureProfile": profile.name,
                    "archiveOrigin": archive_origin,
                    "warcMember": info.filename,
                }
            )

    def _request(
        self,
        record: Any,
        profile: CaptureProfile,
        archive_origin: str,
        member: str,
        index: int,
    ) -> None:
        raw_url = record.rec_headers.get_header("WARC-Target-URI") or ""
        url, redacted = redact_url(raw_url)
        method = _request_method(record)
        self._request_methods[(profile.name, raw_url)].append(method)
        self.result.requests.append(
            {
                "url": url,
                "urlRedacted": redacted,
                "method": method,
                "headers": redact_headers(_headers(record)),
                "timestamp": record.rec_headers.get_header("WARC-Date"),
                "captureProfile": profile.name,
                "archiveOrigin": archive_origin,
                "warcMember": member,
                "warcRecordIndex": index,
                "warcRecordId": record.rec_headers.get_header("WARC-Record-ID"),
                "concurrentTo": record.rec_headers.get_header("WARC-Concurrent-To"),
            }
        )

    def _response(
        self,
        record: Any,
        profile: CaptureProfile,
        archive_origin: str,
        member: str,
        index: int,
    ) -> None:
        raw_url = record.rec_headers.get_header("WARC-Target-URI") or ""
        url, redacted = redact_url(raw_url)
        headers = _headers(record)
        header_lookup = {name.lower(): value for name, value in headers}
        status = _status(record)
        content_type = normalize_mime(header_lookup.get("content-type"))
        raw_location = header_lookup.get("location")
        redirect_raw = urljoin(raw_url, raw_location) if raw_location else None
        redirect_to = redact_url(redirect_raw)[0] if redirect_raw else None

        raw_fd, raw_name = tempfile.mkstemp(prefix="raw-", suffix=".tmp", dir=self._temp_dir)
        os.close(raw_fd)
        raw_temp = Path(raw_name)
        try:
            with raw_temp.open("wb") as output:
                shutil.copyfileobj(record.raw_stream, output, length=1024 * 1024)
            selected, decoded = decode_http_entity(raw_temp, header_lookup.get("content-encoding"), self._temp_dir)
            sha256_hex, size = self._hash_file(selected)
            duplicate = sha256_hex in self._payloads
            if duplicate:
                local_path = self._payloads[sha256_hex]
            else:
                local_path = local_payload_path(raw_url, content_type, sha256_hex, force_raw=not decoded)
                destination = safe_output_path(self.result_root, local_path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(selected, destination)
                self._payloads[sha256_hex] = local_path
            if selected.exists():
                selected.unlink()
            if raw_temp.exists():
                raw_temp.unlink()
        except Exception:
            raw_temp.unlink(missing_ok=True)
            raise

        warc_digest = _normalize_warc_digest(record.rec_headers.get_header("WARC-Payload-Digest"))
        if warc_digest:
            self._warc_digests[warc_digest] = sha256_hex
        representation = "decoded-http-entity" if decoded else "raw-content-encoded-http-entity"
        resource = {
            "url": url,
            "urlRedacted": redacted,
            "finalUrl": None,
            "method": None,
            "status": status,
            "responseHeaders": redact_headers(headers),
            "contentType": content_type,
            "contentEncoding": header_lookup.get("content-encoding"),
            "contentRepresentation": representation,
            "timestamp": record.rec_headers.get_header("WARC-Date"),
            "size": size,
            "sha256": sha256_hex,
            "category": "other" if not decoded else classify_content_type(content_type),
            "localPath": local_path,
            "deduplicated": duplicate,
            "captureProfile": profile.name,
            "archiveOrigin": archive_origin,
            "warcMember": member,
            "warcRecordIndex": index,
            "warcRecordId": record.rec_headers.get_header("WARC-Record-ID"),
            "warcPayloadDigest": record.rec_headers.get_header("WARC-Payload-Digest"),
        }
        response = {
            "url": url,
            "urlRedacted": redacted,
            "finalUrl": None,
            "method": None,
            "status": status,
            "headers": redact_headers(headers),
            "redirectTo": redirect_to,
            "timestamp": record.rec_headers.get_header("WARC-Date"),
            "contentType": content_type,
            "payloadSize": size,
            "sha256": sha256_hex,
            "localPath": local_path,
            "captureProfile": profile.name,
            "archiveOrigin": archive_origin,
            "warcMember": member,
            "warcRecordIndex": index,
            "warcRecordId": record.rec_headers.get_header("WARC-Record-ID"),
        }
        response_index = len(self.result.responses)
        resource["networkResponseIndex"] = response_index
        self.result.resources.append(resource)
        self.result.responses.append(response)
        self._response_links.append((response, raw_url, redirect_raw))
        self._response_links.append((resource, raw_url, redirect_raw))
        if not decoded:
            self.result.errors.append(
                {
                    "kind": "payload_decoding_error",
                    "message": f"Could not decode Content-Encoding {header_lookup.get('content-encoding')!r}; raw entity preserved.",
                    "url": url,
                    "captureProfile": profile.name,
                    "archiveOrigin": archive_origin,
                }
            )
        if status is not None and status >= 400:
            self.result.errors.append(
                {
                    "kind": "http_error",
                    "message": f"HTTP {status}",
                    "url": url,
                    "status": status,
                    "captureProfile": profile.name,
                    "archiveOrigin": archive_origin,
                }
            )

    def _revisit(
        self,
        record: Any,
        profile: CaptureProfile,
        archive_origin: str,
        member: str,
        index: int,
    ) -> None:
        raw_url = record.rec_headers.get_header("WARC-Target-URI") or ""
        url, redacted = redact_url(raw_url)
        headers = _headers(record)
        header_lookup = {name.lower(): value for name, value in headers}
        self._pending_revisits.append(
            {
                "url": url,
                "urlRedacted": redacted,
                "_rawUrl": raw_url,
                "finalUrl": url,
                "method": None,
                "status": _status(record),
                "responseHeaders": redact_headers(headers),
                "contentType": normalize_mime(header_lookup.get("content-type")),
                "contentEncoding": header_lookup.get("content-encoding"),
                "contentRepresentation": "revisit-reference",
                "timestamp": record.rec_headers.get_header("WARC-Date"),
                "size": None,
                "sha256": None,
                "category": None,
                "localPath": None,
                "deduplicated": True,
                "captureProfile": profile.name,
                "archiveOrigin": archive_origin,
                "warcMember": member,
                "warcRecordIndex": index,
                "warcRecordId": record.rec_headers.get_header("WARC-Record-ID"),
                "warcPayloadDigest": record.rec_headers.get_header("WARC-Payload-Digest"),
                "warcRefersTo": record.rec_headers.get_header("WARC-Refers-To"),
            }
        )

    def _resource(
        self,
        record: Any,
        profile: CaptureProfile,
        archive_origin: str,
        member: str,
        index: int,
    ) -> None:
        target = record.rec_headers.get_header("WARC-Target-URI") or ""
        match = re.match(r"^urn:(view|fullPage|fullPageFinal|thumbnail):", target)
        if not match:
            return
        kind = match.group(1)
        if kind == "thumbnail":
            return
        suffix = "full" if kind == "fullPageFinal" else "initial"
        destination = self.result_root / "screenshots" / f"{profile.name}-{suffix}.png"
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".png.tmp")
        with temporary.open("wb") as output:
            shutil.copyfileobj(record.raw_stream, output, length=1024 * 1024)
        temporary.replace(destination)
        relative = destination.relative_to(self.result_root).as_posix()
        if relative not in self.result.screenshots:
            self.result.screenshots.append(relative)

    def _resolve_revisits(self) -> None:
        for item in self._pending_revisits:
            raw_url = item.pop("_rawUrl")
            digest = _normalize_warc_digest(item.get("warcPayloadDigest"))
            sha256_hex = self._warc_digests.get(digest or "")
            if sha256_hex and sha256_hex in self._payloads:
                local_path = self._payloads[sha256_hex]
                item["sha256"] = sha256_hex
                item["localPath"] = local_path
                item["size"] = safe_output_path(self.result_root, local_path).stat().st_size
                item["category"] = local_path.split("/", 1)[0]
            else:
                self.result.errors.append(
                    {
                        "kind": "unresolved_revisit",
                        "message": "WARC revisit record could not be linked to an extracted payload.",
                        "url": item["url"],
                        "captureProfile": item["captureProfile"],
                        "archiveOrigin": item["archiveOrigin"],
                    }
                )
            response = {
                "url": item["url"],
                "urlRedacted": item["urlRedacted"],
                "finalUrl": item["finalUrl"],
                "method": None,
                "status": item["status"],
                "headers": item["responseHeaders"],
                "redirectTo": None,
                "timestamp": item["timestamp"],
                "contentType": item["contentType"],
                "payloadSize": item["size"],
                "sha256": item["sha256"],
                "localPath": item["localPath"],
                "captureProfile": item["captureProfile"],
                "archiveOrigin": item["archiveOrigin"],
                "warcMember": item["warcMember"],
                "warcRecordIndex": item["warcRecordIndex"],
                "warcRecordId": item["warcRecordId"],
                "revisit": True,
            }
            item["networkResponseIndex"] = len(self.result.responses)
            self.result.resources.append(item)
            self.result.responses.append(response)
            self._response_links.append((response, raw_url, None))
            self._response_links.append((item, raw_url, None))

    def _link_methods_and_redirects(self) -> None:
        by_url: dict[tuple[str, str], list[tuple[dict[str, Any], str | None]]] = defaultdict(list)
        for item, raw_url, redirect_raw in self._response_links:
            methods = self._request_methods.get((item["captureProfile"], raw_url))
            if item.get("method") is None and methods:
                item["method"] = methods[0]
            by_url[(item["captureProfile"], raw_url)].append((item, redirect_raw))

        def final_url(profile: str, start: str) -> str:
            current = start
            seen: set[str] = set()
            for _ in range(20):
                if current in seen:
                    break
                seen.add(current)
                candidates = by_url.get((profile, current), [])
                redirect = next((target for _item, target in candidates if target), None)
                if not redirect:
                    break
                if (profile, redirect) not in by_url:
                    break
                current = redirect
            return redact_url(current)[0]

        for item, raw_url, _redirect_raw in self._response_links:
            item["finalUrl"] = final_url(item["captureProfile"], raw_url)

    @staticmethod
    def _hash_file(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size
