# -*- mode: python ; coding: utf-8 -*-
"""Native PySide6 PyInstaller spec.

Replaces the Flask + QtWebEngine build of v0.1.x with the native shell. See
``native/MIGRATION_PLAN.md`` for the broader migration. Web stack still
present in the tree during Phases 0-6; Phase 7 deletes ``static/`` and the
Flask routes, after which ``datas`` here can drop ``static`` entirely.

ALERT §3 — ffmpeg DLLs (av*, sw*) must be in ``binaries`` explicitly. The
PySide6 PyInstaller hook usually picks them up, but the lessons from the
sibling repo are clear: list them yourself so a QtMultimedia regression
fails loudly at build time instead of silently in the frozen exe.
"""
from pathlib import Path

import PySide6  # noqa: E402

PYSIDE6_DIR = Path(PySide6.__file__).resolve().parent

# Multimedia / FFmpeg backend DLLs shipped inside the PySide6 wheel. We pin the
# *prefixes* so a version bump (avcodec-61 -> avcodec-62, etc.) doesn't silently
# drop a binary from the frozen build.
_FFMPEG_PREFIXES = ("avcodec-", "avformat-", "avutil-", "swresample-", "swscale-")

binaries = [
    (str(dll), ".")
    for dll in sorted(PYSIDE6_DIR.iterdir())
    if dll.suffix.lower() == ".dll" and dll.name.lower().startswith(_FFMPEG_PREFIXES)
]

# ``static/`` is kept in datas during the migration window so any Flask routes
# left on this branch keep working from a dev checkout. Phase 7 drops it.
# (``native/`` is a Python package — PyInstaller's Analysis picks it up via the
# entry script's imports, so we do not list it in datas.)
datas = [
    ("static", "static"),
]

hiddenimports = [
    "plistlib",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtSvg",
]


a = Analysis(
    ["clipline_native.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Hard-exclude the Chromium runtime so the build fails loudly if anyone
        # reintroduces a webview import. ALERT §2: shipping the wrong spec is
        # the most expensive mistake.
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebChannel",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="clipline",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["static\\favicon.ico"],
)
