"""Persistent user settings (output dir, theme, etc.) for Clipline.

Backed by a single JSON file under ``APP_STATE_DIR``. Lifted out of
``app.py`` so the native shell can read/write settings without importing
Flask.
"""
from __future__ import annotations

import json
from pathlib import Path
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
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)


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
