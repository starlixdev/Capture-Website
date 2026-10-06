"""Manifest statistics and serialization."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from sitecapture.config import CaptureConfig, redact_url
from sitecapture.version import __version__

SCHEMA_VERSION = "1.1"


def build_manifest(
    config: CaptureConfig,
    *,
    started_at: str,
    ended_at: str,
    profile_metadata: list[dict[str, Any]],
    resources: list[dict[str, Any]],
    requests: list[dict[str, Any]],
    responses: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    capture_engine: dict[str, Any] | None = None,
) -> dict[str, Any]:
    extracted = [item for item in resources if item.get("localPath")]
    unique_paths = {item["localPath"] for item in extracted}
    category_counts = Counter(item.get("category", "other") for item in extracted)
    response_failures = sum(1 for item in responses if (item.get("status") or 0) >= 400)
    redirects = sum(1 for item in responses if 300 <= (item.get("status") or 0) < 400)
    input_url, input_redacted = redact_url(config.url)
    command = {
        "full": config.full,
        "timeoutSeconds": config.timeout,
        "scroll": config.scroll,
        "screenshots": config.screenshots,
        "visibleBrowser": config.visible_browser,
        "browserSession": config.browser_session,
        "profiles": [profile.name for profile in config.profiles],
        "scope": "single-page-comprehensive" if config.full else "single-page",
        "pageLimit": 1,
        "privateNetworkBlocked": not config.allow_private,
        "dnsPinnedProxy": True,
        "downloadsBlocked": True,
        "serviceWorkersBlocked": True,
        "webSocketsBlocked": True,
    }
    return {
        "schemaVersion": SCHEMA_VERSION,
        "siteCaptureVersion": __version__,
        "captureEngine": capture_engine
        or {"name": "CaptureWebsite Native Browser", "isolation": "temporary browser context"},
        "capture": {
            "inputUrl": input_url,
            "inputUrlRedacted": input_redacted,
            "startedAt": started_at,
            "endedAt": ended_at,
            "profiles": profile_metadata,
            "configuration": command,
            "definitionOfComplete": (
                "Everything the controlled browser successfully observed or downloaded during the configured visit."
            ),
        },
        "statistics": {
            "totalRequests": len(requests),
            "totalResponses": len(responses),
            "successfulResponses": sum(1 for item in responses if 200 <= (item.get("status") or 0) < 400),
            "failedResponses": response_failures,
            "recordedErrors": len(errors),
            "redirects": redirects,
            "extractedResources": len(extracted),
            "uniquePayloads": len(unique_paths),
            "deduplicatedPayloads": max(0, len(extracted) - len(unique_paths)),
            "categoryCounts": dict(sorted(category_counts.items())),
        },
        "resources": resources,
    }


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    temporary.replace(path)
