# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

import playwright
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = (
    collect_submodules("warcio")
    + collect_submodules("playwright")
    + ["brotli", "greenlet"]
)
playwright_driver = Path(playwright.__file__).resolve().parent / "driver"

a = Analysis(
    ["src/sitecapture/gui/app.py"],
    pathex=["src"],
    binaries=[],
    datas=[(str(playwright_driver), "playwright/driver")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="CaptureWebsite",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
