"""Persistent user settings (output dir, theme, etc.) for Clipline.

Backed by a single JSON file under ``APP_STATE_DIR``. Lifted out of
``app.py`` so the native shell can read/write settings without importing
Flask.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any

from native.services.paths import APP_STATE_DIR, DEFAULT_OUTPUT_DIR, SETTINGS_FILE


def load_settings() -> dict[str, Any]:
    if not SETTINGS_FILE.exists():
        return {}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(settings: dict[str, Any]) -> None:
    APP_STATE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=SETTINGS_FILE.parent,
                                         prefix=SETTINGS_FILE.name + ".", suffix=".tmp", delete=False) as f:
            temporary = Path(f.name)
            json.dump(settings, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, SETTINGS_FILE)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def get_output_dir() -> Path:
    settings = load_settings()
    configured = str(settings.get("output_dir", "")).strip()
    path = Path(configured).expanduser() if configured else DEFAULT_OUTPUT_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def set_output_dir(path_value: str | Path | None) -> Path:
    raw = str(path_value or "").strip()
    path = DEFAULT_OUTPUT_DIR if not raw else Path(raw).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    settings = load_settings()
    settings["output_dir"] = str(path.resolve())
    save_settings(settings)
    return path


def reset_output_dir() -> Path:
    settings = load_settings()
    settings.pop("output_dir", None)
    save_settings(settings)
    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return DEFAULT_OUTPUT_DIR


def save_facecam_layout(name: str, layout: dict) -> None:
    """Store a named facecam guide and mark it as the last-used one.

    The last-used marker is auto-applied when a new source loads — one
    OBS layout per channel covers the common case.
    """
    key = str(name or "").strip() or "default"
    settings = load_settings()
    layouts = settings.get("facecam_layouts", {})
    layouts[key] = dict(layout)
    settings["facecam_layouts"] = layouts
    settings["last_facecam_layout_name"] = key
    save_settings(settings)


def get_saved_facecam_layouts() -> dict[str, dict]:
    return {k: dict(v) for k, v in load_settings().get("facecam_layouts", {}).items()}


def get_last_facecam_layout() -> dict | None:
    """The most recently saved guide, or None if nothing was ever saved."""
    settings = load_settings()
    name = settings.get("last_facecam_layout_name")
    layouts = settings.get("facecam_layouts", {})
    layout = layouts.get(name) if name else None
    return dict(layout) if layout else None
