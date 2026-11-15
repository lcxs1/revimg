# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for revimg.

One self-contained ``revimg.exe`` by default.  Set ``REVIMG_ONEDIR=1`` (or pass
``-OneDir`` to packaging/build_exe.ps1) for a folder build with a faster start.
"""

import os
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
ONEDIR = os.environ.get("REVIMG_ONEDIR") == "1"
NAME = "revimg"

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "scipy",
        "pandas",
        "pytest",
        "IPython",
        "notebook",
        "sqlite3",
        "xmlrpc",
        "pydoc_data",
        "http.server",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

_common = dict(
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "assets" / "revimg.ico"),
    version=str(ROOT / "packaging" / "version_info.txt"),
)

if ONEDIR:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **_common)
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=NAME)
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], **_common)
