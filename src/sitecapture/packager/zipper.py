"""Deterministic ZIP generation and post-write verification."""

from __future__ import annotations

import os
import stat
import zipfile
from pathlib import Path, PurePosixPath

from sitecapture.errors import PackagingError

_FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def create_deterministic_zip(source_dir: Path, destination: Path) -> Path:
    source_dir = source_dir.resolve()
    destination = destination.resolve()
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.unlink(missing_ok=True)
    files = sorted(
        (path for path in source_dir.rglob("*") if path.is_file() and not path.is_symlink()),
        key=lambda path: path.relative_to(source_dir).as_posix(),
    )
    if not files:
        raise PackagingError("Refusing to create an empty capture ZIP.")
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            allowZip64=True,
        ) as archive:
            for path in files:
                relative = path.relative_to(source_dir).as_posix()
                arcname = f"{source_dir.name}/{relative}"
                info = zipfile.ZipInfo(arcname, date_time=_FIXED_TIMESTAMP)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                with path.open("rb") as source, archive.open(info, "w", force_zip64=True) as output:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
        verify_zip(temporary, expected_root=source_dir.name)
        os.replace(temporary, destination)
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        if isinstance(exc, PackagingError):
            raise
        raise PackagingError(f"Could not create capture ZIP: {exc}") from exc
    return destination


def verify_zip(path: Path, *, expected_root: str | None = None) -> list[str]:
    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = archive.namelist()
            if not names:
                raise PackagingError("Capture ZIP contains no entries.")
            for name in names:
                member = PurePosixPath(name)
                if member.is_absolute() or ".." in member.parts or "\\" in name:
                    raise PackagingError(f"Unsafe entry in capture ZIP: {name!r}")
            root = expected_root or PurePosixPath(names[0]).parts[0]
            required = {
                f"{root}/manifest.json",
                f"{root}/README.txt",
                f"{root}/network/requests.json",
                f"{root}/network/responses.json",
                f"{root}/logs/errors.json",
            }
            missing = sorted(required.difference(names))
            if missing:
                raise PackagingError(f"Capture ZIP is missing required entries: {', '.join(missing)}")
            if not any(name.startswith(f"{root}/archive/") and name.endswith(".wacz") for name in names):
                raise PackagingError("Capture ZIP does not include its WACZ archive.")
            bad = archive.testzip()
            if bad:
                raise PackagingError(f"Capture ZIP CRC verification failed for {bad}.")
            return names
    except zipfile.BadZipFile as exc:
        raise PackagingError(f"Invalid generated ZIP: {path}") from exc
