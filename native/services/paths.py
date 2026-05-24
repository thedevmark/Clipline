"""Filesystem layout for Clipline.

Single source of truth for the directories the app reads and writes. Used by
both the native shell and (during migration) the legacy Flask app — neither
should compute these locally.
"""
from __future__ import annotations

import os
import platform
import sys
from pathlib import Path


if getattr(sys, "frozen", False):
    # PyInstaller one-file: ``sys._MEIPASS`` is the temp extraction dir for
    # bundled assets; ``sys.executable``'s parent is where user files (output,
    # logs, runtime tools) should live so they survive across launches.
    INTERNAL_DIR = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    BASE_DIR = Path(sys.executable).parent.resolve()
else:
    INTERNAL_DIR = Path(__file__).resolve().parents[2]
    BASE_DIR = INTERNAL_DIR

TEMP_DIR = BASE_DIR / "temp"
DOWNLOADS_DIR = TEMP_DIR / "downloads"
PROCESSING_DIR = TEMP_DIR / "processing"

if platform.system() == "Windows":
    _local_app_data = os.environ.get("LOCALAPPDATA")
    _runtime_root_base = Path(_local_app_data) if _local_app_data else BASE_DIR
    RUNTIME_DIR = _runtime_root_base / "clipline" / "runtime"
    APP_STATE_DIR = _runtime_root_base / "clipline"
else:
    RUNTIME_DIR = BASE_DIR / ".runtime"
    APP_STATE_DIR = BASE_DIR / ".appstate"

RUNTIME_BIN_DIR = RUNTIME_DIR / "bin"
SETTINGS_FILE = APP_STATE_DIR / "settings.json"
DEFAULT_OUTPUT_DIR = BASE_DIR / "output"
CAPTION_ENV_DIR = APP_STATE_DIR / "captioning-env"


def ensure_dirs() -> None:
    """Create the directories the app expects. Called from the entrypoint."""
    for directory in (APP_STATE_DIR, DOWNLOADS_DIR, PROCESSING_DIR, RUNTIME_BIN_DIR):
        directory.mkdir(parents=True, exist_ok=True)
