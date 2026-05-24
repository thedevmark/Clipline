"""Discovery for the CLI tools the app shells out to (ffmpeg, ffprobe, yt-dlp).

Lifted out of ``app.py`` so the native shell can resolve tool paths without
importing Flask. Search order: bundled runtime dir, ``PATH``, then common
Windows install locations (winget / chocolatey / scoop / manual).
"""
from __future__ import annotations

import functools
import glob
import os
import platform
import shutil
from pathlib import Path
from typing import Optional

from native.services.paths import RUNTIME_BIN_DIR


def _is_explicit_tool_path(path: str, tool_name: str) -> bool:
    """True if ``path`` looks like a real filesystem location, not a bare name."""
    p = Path(str(path))
    if p.exists():
        return True
    normalized = p.name.lower()
    bare = {tool_name.lower()}
    if platform.system() == "Windows":
        bare.add(f"{tool_name.lower()}.exe")
    return normalized not in bare


def find_tool(name: str) -> str:
    """Return a path (or bare name fallback) for the given CLI tool."""
    runtime_candidate = RUNTIME_BIN_DIR / (
        f"{name}.exe" if platform.system() == "Windows" else name
    )
    if runtime_candidate.exists():
        return str(runtime_candidate)

    path = shutil.which(name)
    if path:
        return path

    if platform.system() != "Windows":
        return name

    home = Path.home()
    search_patterns = [
        str(home / "AppData/Local/Microsoft/WinGet/Packages" / "**" / f"{name}.exe"),
        str(home / "AppData/Roaming/Python" / "**" / f"{name}.exe"),
        str(home / "AppData/Local/Programs/Python" / "**" / f"{name}.exe"),
        f"C:/ProgramData/chocolatey/bin/{name}.exe",
        str(home / f"scoop/shims/{name}.exe"),
        f"C:/{name}/bin/{name}.exe",
        f"C:/Program Files/{name}/bin/{name}.exe",
    ]
    for pattern in search_patterns:
        matches = glob.glob(pattern, recursive=True)
        if matches:
            return matches[0]
    return name


class _ToolPaths:
    """Lazily resolved tool paths. Recompute via ``refresh()`` after install."""

    def __init__(self) -> None:
        self._cache: dict[str, str] = {}

    def get(self, name: str) -> str:
        if name not in self._cache:
            self._cache[name] = find_tool(name)
        return self._cache[name]

    def refresh(self) -> None:
        self._cache.clear()
        get_env.cache_clear()

    @property
    def ffmpeg(self) -> str:
        return self.get("ffmpeg")

    @property
    def ffprobe(self) -> str:
        return self.get("ffprobe")

    @property
    def ytdlp(self) -> str:
        return self.get("yt-dlp")

    @property
    def deno(self) -> str:
        return self.get("deno")

    @property
    def ffmpeg_dir(self) -> Optional[str]:
        path = self.ffmpeg
        return str(Path(path).parent) if _is_explicit_tool_path(path, "ffmpeg") else None


TOOLS = _ToolPaths()


@functools.lru_cache(maxsize=1)
def get_env() -> dict[str, str]:
    """Return an env dict with ffmpeg and deno directories prepended to PATH."""
    env = os.environ.copy()
    extra_dirs: list[str] = []
    if RUNTIME_BIN_DIR.exists():
        extra_dirs.append(str(RUNTIME_BIN_DIR))
    if TOOLS.ffmpeg_dir:
        extra_dirs.append(TOOLS.ffmpeg_dir)
    deno_path = TOOLS.deno
    if _is_explicit_tool_path(deno_path, "deno"):
        extra_dirs.append(str(Path(deno_path).parent))
    if extra_dirs:
        env["PATH"] = os.pathsep.join(extra_dirs) + os.pathsep + env.get("PATH", "")
    return env
