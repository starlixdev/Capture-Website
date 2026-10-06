"""CaptureWebsite command-line entry point."""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

from sitecapture.config import CaptureConfig, default_output_dir
from sitecapture.core import CaptureEngine, CaptureResult
from sitecapture.errors import SiteCaptureError
from sitecapture.processor.classifier import CATEGORY_LABELS
from sitecapture.version import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="capturewebsite",
        description=(
            "Capture HTTP resources observed by an isolated or CaptureWebsite-owned persistent Edge/Chrome session, "
            "preserve the WACZ, extract response payloads, and produce a verified ZIP."
        ),
        epilog=(
            "'Complete' means what the configured browser visit successfully observed; it does not "
            "include backend source, inaccessible content, or resources that were never delivered."
        ),
    )
    parser.add_argument("url", help="HTTP or HTTPS page to visit (one page; not an unrestricted domain crawl).")
    parser.add_argument(
        "--full",
        action="store_true",
        help=(
            "Use longer settling and scrolling limits for a comprehensive single-page capture. "
            "Downloads, service workers, popups, and private-network requests remain blocked."
        ),
    )
    parser.add_argument(
        "--desktop",
        action="store_true",
        help="Capture a desktop browser profile. This is the default when --mobile is not supplied.",
    )
    parser.add_argument(
        "--mobile",
        action="store_true",
        help="Capture a separate Pixel 5-style mobile profile. Combine with --desktop for both.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        metavar="SECONDS",
        help="Navigation and capture timeout per profile (10-3600; default: 120).",
    )
    scroll = parser.add_mutually_exclusive_group()
    scroll.add_argument(
        "--scroll",
        dest="scroll",
        action="store_true",
        default=True,
        help="Enable controlled autoscroll for lazy resources (default).",
    )
    scroll.add_argument(
        "--no-scroll",
        dest="scroll",
        action="store_false",
        help="Disable controlled autoscroll; normal page JavaScript and resource loading remain enabled.",
    )
    screenshots = parser.add_mutually_exclusive_group()
    screenshots.add_argument(
        "--screenshots",
        dest="screenshots",
        action="store_true",
        default=True,
        help="Capture initial and final/full screenshots (default).",
    )
    screenshots.add_argument(
        "--no-screenshots",
        dest="screenshots",
        action="store_false",
        help="Do not create screenshots.",
    )
    parser.add_argument(
        "--visible-browser",
        action="store_true",
        help=(
            "Open the selected Edge/Chrome session visibly for compatibility or manual site verification. "
            "The personal browser profile is never loaded."
        ),
    )
    parser.add_argument(
        "--session",
        choices=("isolated", "persistent-chrome", "persistent-edge"),
        default="isolated",
        help=(
            "Browser state mode (default: isolated). Persistent modes reuse only CaptureWebsite-owned profiles "
            "under the application data directory."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=default_output_dir(),
        metavar="DIRECTORY",
        help="Output root (default: CaptureWebsite application data/output). Temporary browser data never uses this as a profile.",
    )
    parser.add_argument(
        "--allow-private",
        action="store_true",
        help=(
            "EXPERT/RISKY: permit local/private targets and non-standard ports. "
            "Use only for infrastructure you own; unavailable in the GUI."
        ),
    )
    parser.add_argument(
        "--browser",
        choices=("auto", "edge", "chrome"),
        default="auto",
        help="Native browser to use (default: auto, preferring Microsoft Edge).",
    )
    parser.add_argument("--version", action="version", version=f"CaptureWebsite {__version__}")
    return parser


def _progress(phase: str, message: str) -> None:
    print(f"[{phase.upper()}] {message}", flush=True)


def print_summary(result: CaptureResult) -> None:
    stats = result.manifest["statistics"]
    counts = stats["categoryCounts"]
    print("=" * 56)
    print("CAPTUREWEBSITE COMPLETE")
    print("=" * 56)
    print(f"\nURL:\n{result.url}\n")
    for category, label in CATEGORY_LABELS.items():
        print(f"{label + '.':.<20} {counts.get(category, 0)}")
    print()
    print(f"{'Responses.':.<20} {stats['totalResponses']}")
    print(f"{'Extracted.':.<20} {stats['extractedResources']}")
    print(f"{'Failed responses.':.<20} {stats['failedResponses']}")
    print(f"{'Recorded errors.':.<20} {stats['recordedErrors']}")
    print(f"{'Unique payloads.':.<20} {stats['uniquePayloads']}")
    print(f"{'Duplicates.':.<20} {stats['deduplicatedPayloads']}")
    print("\nWACZ:")
    for path in result.archive_paths:
        print(path)
    print(f"\nZIP:\n{result.zip_path}")
    print("=" * 56)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    desktop = args.desktop or not args.mobile
    try:
        config = CaptureConfig(
            url=args.url,
            output_dir=args.output,
            full=args.full,
            desktop=desktop,
            mobile=args.mobile,
            timeout=args.timeout,
            scroll=args.scroll,
            screenshots=args.screenshots,
            visible_browser=args.visible_browser,
            allow_private=args.allow_private,
            browser_channel={"auto": "auto", "edge": "msedge", "chrome": "chrome"}[args.browser],
            browser_session={
                "isolated": "isolated",
                "persistent-chrome": "persistent_chrome",
                "persistent-edge": "persistent_edge",
            }[args.session],
        )
        result = CaptureEngine(config, progress=_progress).run(threading.Event())
        print_summary(result)
        return 0
    except SiteCaptureError as exc:
        print(f"CaptureWebsite error [{exc.kind}]: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("CaptureWebsite interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Unexpected CaptureWebsite failure: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
