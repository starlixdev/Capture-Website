from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path


def main() -> int:
    output = Path(sys.argv[1]).resolve()
    zips = sorted(output.glob("*/*-complete.zip"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not zips:
        raise SystemExit("No capture ZIP found.")
    latest = zips[0]
    with zipfile.ZipFile(latest) as archive:
        bad = archive.testzip()
        if bad:
            raise SystemExit(f"CRC failure: {bad}")
        names = archive.namelist()
        root = names[0].split("/", 1)[0]
        required = {
            f"{root}/manifest.json",
            f"{root}/network/requests.json",
            f"{root}/network/responses.json",
        }
        missing = required.difference(names)
        if missing:
            raise SystemExit(f"Missing entries: {sorted(missing)}")
        if not any(name.startswith(f"{root}/archive/") and name.endswith(".wacz") for name in names):
            raise SystemExit("No WACZ found in ZIP.")
        manifest = json.loads(archive.read(f"{root}/manifest.json"))
        if manifest["statistics"]["totalResponses"] < 1:
            raise SystemExit("No WARC responses were parsed.")
        if not any(item.get("category") == "html" for item in manifest["resources"]):
            raise SystemExit("No captured HTML response was extracted.")
    print(f"Verified real CaptureWebsite ZIP: {latest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

