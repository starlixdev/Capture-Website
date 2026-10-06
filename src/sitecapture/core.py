"""Shared end-to-end capture orchestration used by both CLI and GUI."""

from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sitecapture.capture.native import NativeArtifact, NativeBrowserRunner, command_for_manifest
from sitecapture.config import CaptureConfig, redact_url
from sitecapture.errors import CaptureCancelled, PackagingError
from sitecapture.extractor.wacz import WaczExtractor
from sitecapture.packager.zipper import create_deterministic_zip, verify_zip
from sitecapture.processor.manifest import build_manifest, write_json
from sitecapture.security import redact_text
from sitecapture.version import __version__

ProgressCallback = Callable[[str, str], None]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class CaptureResult:
    url: str
    output_directory: Path
    zip_path: Path
    manifest: dict[str, Any]

    @property
    def archive_paths(self) -> list[Path]:
        return sorted((self.output_directory / "archive").glob("*.wacz"))


class CaptureEngine:
    def __init__(
        self,
        config: CaptureConfig,
        *,
        runner: NativeBrowserRunner | None = None,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.config = config
        self.runner = runner or NativeBrowserRunner(config)
        self.progress = progress or (lambda _phase, _message: None)

    def run(self, cancel_event: threading.Event | None = None) -> CaptureResult:
        cancel_event = cancel_event or threading.Event()
        started_at = _utc_now()
        self.runner.preflight(self.progress)
        site_root = self.config.output_dir / self.config.slug
        site_root.mkdir(parents=True, exist_ok=True)
        job_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        job_root = site_root / f".sitecapture-work-{job_id}"
        job_root.mkdir(parents=False, exist_ok=False)
        result_dir = job_root / "result" / f"{self.config.slug}-complete"
        artifacts: list[NativeArtifact] = []
        profile_metadata: list[dict[str, Any]] = []
        try:
            for profile in self.config.profiles:
                if cancel_event.is_set():
                    raise CaptureCancelled("Capture cancelled before the next profile started.")
                profile_started = _utc_now()
                collection = f"{self.config.slug}-{job_id.lower()}-{profile.name}"
                container = f"sitecapture-{self.config.slug[:20]}-{job_id[-8:]}-{profile.name}".lower()
                artifact = self.runner.capture_profile(
                    profile,
                    job_root / "native-browser" / profile.name,
                    collection,
                    container,
                    cancel_event,
                    self.progress,
                )
                profile_ended = _utc_now()
                artifacts.append(artifact)
                profile_metadata.append(
                    {
                        "name": profile.name,
                        "viewport": profile.viewport,
                        "mobileDevice": profile.mobile_device,
                        "userAgentOverride": profile.user_agent,
                        "startedAt": profile_started,
                        "endedAt": profile_ended,
                        "sourceUrl": redact_url(self.config.url)[0],
                        "captureCommand": command_for_manifest(artifact.command),
                    }
                )

            result_dir.mkdir(parents=True, exist_ok=False)
            extractor = WaczExtractor(result_dir, self.progress)
            log_errors: list[dict[str, Any]] = []
            for artifact, metadata in zip(artifacts, profile_metadata, strict=True):
                archive_name = self._archive_name(artifact)
                archive_relative = f"archive/{archive_name}"
                archive_destination = result_dir / archive_relative
                archive_destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(artifact.wacz_path, archive_destination)
                metadata["archivePath"] = archive_relative
                self._copy_logs(artifact, result_dir)
                log_errors.extend(self._read_capture_errors(artifact))
                extractor.ingest(archive_destination, artifact.profile, archive_relative)

            extraction = extractor.finalize()
            extraction.errors.extend(log_errors)
            self.progress("metadata", "Writing manifest, network metadata, logs, and capture README")
            ended_at = _utc_now()
            write_json(result_dir / "network" / "requests.json", extraction.requests)
            write_json(result_dir / "network" / "responses.json", extraction.responses)
            write_json(result_dir / "logs" / "errors.json", extraction.errors)
            manifest = build_manifest(
                self.config,
                started_at=started_at,
                ended_at=ended_at,
                profile_metadata=profile_metadata,
                resources=extraction.resources,
                requests=extraction.requests,
                responses=extraction.responses,
                errors=extraction.errors,
                capture_engine={
                    "name": getattr(self.runner, "engine_name", "capture fixture"),
                    "playwrightVersion": getattr(self.runner, "playwright_version", None),
                    "browser": getattr(self.runner, "browser_name", None),
                    "browserVersion": getattr(self.runner, "browser_version", None),
                    "browserChannel": getattr(self.runner, "channel", None),
                    "isolation": (
                        "fresh non-persistent context; user browser profile is never loaded"
                        if self.config.browser_session == "isolated"
                        else "CaptureWebsite-owned persistent browser profile; personal browser profile is never loaded"
                    ),
                    "dockerUsed": False,
                },
            )
            write_json(result_dir / "manifest.json", manifest)
            self._write_capture_readme(result_dir, manifest, extraction.errors)

            self.progress("package", "Creating and verifying the final ZIP")
            staged_zip = create_deterministic_zip(result_dir, job_root / f"{self.config.slug}-complete.zip")
            final_dir = site_root / f"{self.config.slug}-complete"
            final_zip = site_root / f"{self.config.slug}-complete.zip"
            cleanup_warnings = self._finalize_outputs(result_dir, staged_zip, final_dir, final_zip, job_id)
            for warning in cleanup_warnings:
                self.progress("cleanup", warning)
            shutil.rmtree(job_root, ignore_errors=True)
            self.progress("complete", f"Capture complete: {final_zip}")
            return CaptureResult(
                url=redact_url(self.config.url)[0],
                output_directory=final_dir,
                zip_path=final_zip,
                manifest=manifest,
            )
        except (Exception, KeyboardInterrupt) as exc:
            self._retain_failed_job(job_root, site_root, job_id, exc, started_at)
            raise

    def _archive_name(self, artifact: NativeArtifact) -> str:
        if len(self.config.profiles) == 1:
            return f"{self.config.slug}.wacz"
        return f"{self.config.slug}-{artifact.profile.name}.wacz"

    def _copy_logs(self, artifact: NativeArtifact, result_dir: Path) -> None:
        logs_dir = result_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        self._copy_redacted_text(
            artifact.stdout_log,
            logs_dir / f"native-browser-{artifact.profile.name}-stdout.log",
        )
        source_logs = artifact.collection_dir / "logs"
        if source_logs.is_dir():
            for index, source in enumerate(sorted(source_logs.rglob("*"))):
                if source.is_file() and not source.is_symlink():
                    safe_name = f"native-browser-{artifact.profile.name}-{index:03d}-{source.name}"
                    self._copy_redacted_text(source, logs_dir / safe_name)

    @staticmethod
    def _copy_redacted_text(source: Path, destination: Path) -> None:
        with source.open("r", encoding="utf-8", errors="replace") as input_stream, destination.open(
            "w", encoding="utf-8", newline="\n"
        ) as output_stream:
            for line in input_stream:
                output_stream.write(redact_text(line))

    def _read_capture_errors(self, artifact: NativeArtifact) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        source_logs = artifact.collection_dir / "logs"
        if not source_logs.is_dir():
            return results
        for source in sorted(source_logs.rglob("*")):
            if not source.is_file() or source.is_symlink():
                continue
            with source.open("r", encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    level = str(item.get("logLevel", "")).lower()
                    if level not in {"warn", "error", "interrupt", "fatal"}:
                        continue
                    message = redact_text(str(item.get("message", "Native browser warning")))
                    context = self._redact_log_context(item.get("context"))
                    results.append(
                        {
                            "kind": self._log_error_kind(message),
                            "message": message,
                            "logLevel": level,
                            "context": context,
                            "timestamp": item.get("timestamp"),
                            "captureProfile": artifact.profile.name,
                            "archiveOrigin": f"archive/{self._archive_name(artifact)}",
                        }
                    )
        return results

    @staticmethod
    def _redact_log_context(value: Any) -> Any:
        if value is None:
            return None
        serialized = json.dumps(value, ensure_ascii=False)
        return json.loads(redact_text(serialized))

    @staticmethod
    def _log_error_kind(message: str) -> str:
        lowered = message.lower()
        if "dns" in lowered or "name_not_resolved" in lowered:
            return "dns"
        if "certificate" in lowered or "tls" in lowered or "ssl" in lowered:
            return "tls"
        if "timeout" in lowered or "timed out" in lowered:
            return "timeout"
        if "connection" in lowered or "network" in lowered:
            return "connection_failure"
        if "blocked" in lowered or "private" in lowered or "localhost" in lowered:
            return "blocked_request"
        return "browser_error"

    def _write_capture_readme(
        self,
        result_dir: Path,
        manifest: dict[str, Any],
        errors: list[dict[str, Any]],
    ) -> None:
        stats = manifest["statistics"]
        profiles = ", ".join(item["name"] for item in manifest["capture"]["profiles"])
        archive_names = ", ".join(path.name for path in sorted((result_dir / "archive").glob("*.wacz")))
        error_kinds = Counter(item.get("kind", "unknown") for item in errors)
        limitations = ", ".join(f"{name}: {count}" for name, count in sorted(error_kinds.items())) or "None recorded"
        text = f"""CaptureWebsite capture
======================

Requested URL: {manifest['capture']['inputUrl']}
Capture started: {manifest['capture']['startedAt']}
Capture ended: {manifest['capture']['endedAt']}
CaptureWebsite version: {__version__}
Capture engine: {manifest['captureEngine']['name']}
Browser: {manifest['captureEngine'].get('browser')} {manifest['captureEngine'].get('browserVersion')}
Profiles: {profiles}
Original archive file(s): {archive_names}

What was captured
-----------------
"Complete" means everything the controlled browser successfully observed or downloaded
during the configured visit. It does not mean backend code, databases, secrets,
inaccessible authenticated content, or resources the browser never requested.

Responses recorded: {stats['totalResponses']}
Successful responses: {stats['successfulResponses']}
Failed HTTP responses: {stats['failedResponses']}
Extracted resource records: {stats['extractedResources']}
Unique extracted payloads: {stats['uniquePayloads']}
Deduplicated payload references: {stats['deduplicatedPayloads']}
Known failures/limitations: {limitations}

Archive and extracted folders
-----------------------------
The WACZ file(s) in archive/ are the archival evidence generated from the HTTP responses
observed by the selected controlled browser session. The type-based
folders are an inspection-friendly representation of captured HTTP entities. When a
standard Content-Encoding could be decoded, the extracted payload is decoded; decoding
status is recorded per resource in manifest.json. Only categories actually observed are
created. The extracted files are not guaranteed to form a directly runnable offline clone.

Screenshots are native-browser WARC resources copied without resizing and use stable
desktop/mobile initial/full names.

Security and legal warning
--------------------------
Isolated mode starts with an empty temporary profile. Persistent mode uses only a
CaptureWebsite-owned browser profile and never loads the user's ordinary Chrome/Edge profile.
CaptureWebsite excludes cookies, authorization headers, API-key headers, request bodies, and
secret-looking URL parameters from the WARC and derived metadata. Response content itself may
still be sensitive and the archive must be protected. The operator is responsible for permission
to archive the target. CaptureWebsite does not bypass CAPTCHA, paywalls, DRM, or access controls.
"""
        path = result_dir / "README.txt"
        temporary = path.with_suffix(".txt.tmp")
        temporary.write_text(text, encoding="utf-8", newline="\n")
        temporary.replace(path)

    @staticmethod
    def _finalize_outputs(
        staged_dir: Path,
        staged_zip: Path,
        final_dir: Path,
        final_zip: Path,
        job_id: str,
    ) -> list[str]:
        """Publish the directory and ZIP as one rollback-capable operation."""
        backup_dir = final_dir.parent / f".{final_dir.name}-previous-{job_id}"
        backup_zip = final_zip.parent / f".{final_zip.name}-previous-{job_id}"
        backed_up_dir = False
        backed_up_zip = False
        published_dir = False
        published_zip = False

        try:
            if final_dir.exists():
                os.replace(final_dir, backup_dir)
                backed_up_dir = True
            if final_zip.exists():
                os.replace(final_zip, backup_zip)
                backed_up_zip = True

            os.replace(staged_dir, final_dir)
            published_dir = True
            os.replace(staged_zip, final_zip)
            published_zip = True
            verify_zip(final_zip, expected_root=final_dir.name)
        except Exception as publish_error:
            rollback_errors: list[str] = []

            if published_zip and final_zip.exists():
                try:
                    os.replace(final_zip, staged_zip)
                except OSError as exc:
                    rollback_errors.append(f"new ZIP: {exc}")
            if published_dir and final_dir.exists():
                try:
                    os.replace(final_dir, staged_dir)
                except OSError as exc:
                    rollback_errors.append(f"new result directory: {exc}")
            if backed_up_zip and backup_zip.exists():
                try:
                    os.replace(backup_zip, final_zip)
                except OSError as exc:
                    rollback_errors.append(f"previous ZIP: {exc}")
            if backed_up_dir and backup_dir.exists():
                try:
                    os.replace(backup_dir, final_dir)
                except OSError as exc:
                    rollback_errors.append(f"previous result directory: {exc}")

            if rollback_errors:
                details = "; ".join(rollback_errors)
                raise PackagingError(
                    f"Could not publish the new capture and rollback was incomplete: {details}"
                ) from publish_error
            raise

        cleanup_warnings: list[str] = []
        if backup_dir.exists():
            try:
                shutil.rmtree(backup_dir)
            except OSError as exc:
                cleanup_warnings.append(
                    f"The new capture is complete, but an old result backup could not be removed: {exc}"
                )
        if backup_zip.exists():
            try:
                backup_zip.unlink()
            except OSError as exc:
                cleanup_warnings.append(
                    f"The new capture is complete, but an old ZIP backup could not be removed: {exc}"
                )
        return cleanup_warnings

    @staticmethod
    def _retain_failed_job(
        job_root: Path,
        site_root: Path,
        job_id: str,
        error: BaseException,
        started_at: str,
    ) -> None:
        if not job_root.exists():
            return
        try:
            write_json(
                job_root / "failure.json",
                {
                    "status": "cancelled" if isinstance(error, CaptureCancelled) else "failed",
                    "kind": getattr(error, "kind", "unexpected_error"),
                    "message": redact_text(str(error)),
                    "startedAt": started_at,
                    "failedAt": _utc_now(),
                },
            )
            failed = site_root / f"{site_root.name}-failed-{job_id}"
            if not failed.exists():
                job_root.rename(failed)
        except OSError:
            pass
