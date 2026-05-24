"""Shared in-memory model passed between stages.

Phase 0 had no shared state — each stage was independent. Phase 2+ needs the
Ingest stage to tell the Inbox stage "this is the source", and the Inbox to
record marked clips that the Output stage will render. Signals notify
subscribers when the model changes; widgets re-render off those.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Signal


@dataclass
class Clip:
    """A marked range on the current source.

    Times are in milliseconds to match QMediaPlayer's native unit and avoid
    float drift accumulating across set-start / set-end edits.
    """

    title: str
    start_ms: int
    end_ms: int

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)


class ProjectState(QObject):
    """Single source of truth for the current project's live state.

    The Phase 0-3 demo only needs a source path + duration + clip list; the
    full project autosave (matching today's reel.py JSON shape) lands when
    the Output / longform pipeline gets wired in.
    """

    source_changed = Signal(object)        # Path | None
    source_metadata_changed = Signal(dict) # {"title": ..., "duration_ms": ...}
    clips_changed = Signal(list)           # list[Clip]
    active_clip_changed = Signal(object)   # int | None (index)
    style_preset_changed = Signal(str)     # StylePreset.key
    format_preset_changed = Signal(str)    # FormatPreset.key

    def __init__(self) -> None:
        super().__init__()
        self._source: Optional[Path] = None
        self._metadata: dict = {}
        self._clips: list[Clip] = []
        self._active: Optional[int] = None
        self._style_key: str = "gameplay_focus"
        self._format_key: str = "shorts"

    @property
    def source(self) -> Optional[Path]:
        return self._source

    @property
    def metadata(self) -> dict:
        return dict(self._metadata)

    @property
    def clips(self) -> list[Clip]:
        return list(self._clips)

    @property
    def active_clip_index(self) -> Optional[int]:
        return self._active

    @property
    def active_clip(self) -> Optional[Clip]:
        if self._active is None or self._active >= len(self._clips):
            return None
        return self._clips[self._active]

    def set_source(self, path: Optional[Path]) -> None:
        self._source = path
        self._metadata = {}
        self.source_changed.emit(path)
        self.source_metadata_changed.emit({})

    def set_metadata(self, **fields) -> None:
        self._metadata.update(fields)
        self.source_metadata_changed.emit(self.metadata)

    def add_clip(self, clip: Clip) -> int:
        self._clips.append(clip)
        idx = len(self._clips) - 1
        self.clips_changed.emit(self.clips)
        self.set_active_clip(idx)
        return idx

    def remove_clip(self, index: int) -> None:
        if 0 <= index < len(self._clips):
            del self._clips[index]
            if self._active is not None and self._active >= len(self._clips):
                self._active = len(self._clips) - 1 if self._clips else None
            self.clips_changed.emit(self.clips)
            self.active_clip_changed.emit(self._active)

    def update_active_clip(self, *, start_ms: Optional[int] = None, end_ms: Optional[int] = None, title: Optional[str] = None) -> None:
        if self._active is None or self._active >= len(self._clips):
            return
        clip = self._clips[self._active]
        if start_ms is not None:
            clip.start_ms = max(0, min(start_ms, clip.end_ms))
        if end_ms is not None:
            clip.end_ms = max(clip.start_ms, end_ms)
        if title is not None:
            clip.title = title
        self.clips_changed.emit(self.clips)

    def set_active_clip(self, index: Optional[int]) -> None:
        if index is None or 0 <= index < len(self._clips):
            self._active = index
            self.active_clip_changed.emit(index)

    @property
    def style_preset_key(self) -> str:
        return self._style_key

    def set_style_preset(self, key: str) -> None:
        self._style_key = key
        self.style_preset_changed.emit(key)

    @property
    def format_preset_key(self) -> str:
        return self._format_key

    def set_format_preset(self, key: str) -> None:
        self._format_key = key
        self.format_preset_changed.emit(key)
