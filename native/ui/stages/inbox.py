"""Inbox stage — list of marked clips + bulk render.

Phase 3 slice: the cut list is the minimum viable inbox. Each entry shows
title + range; double-click renders that one clip via the worker pipeline.
The full Inspector / drag-trim / preset picker live in later phases.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from native.ui import theme
from native.ui.project_state import Clip, ProjectState


def _format_ms(ms: int) -> str:
    if ms < 0:
        ms = 0
    total_seconds = ms // 1000
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    sub = (ms % 1000) // 100
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}.{sub}"
    return f"{minutes}:{seconds:02d}.{sub}"


class InboxStage(QWidget):
    """Clip list. Render a clip; remove a clip; nothing fancier yet."""

    request_export_clip = Signal(object)  # Clip
    request_remove_clip = Signal(int)

    def __init__(self, state: ProjectState) -> None:
        super().__init__()
        self.setObjectName("stage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._state = state

        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 24, 32, 24)
        outer.setSpacing(16)

        kicker_row = QHBoxLayout()
        kicker = QLabel("CUT LIST")
        kicker.setProperty("kicker", True)
        kicker_row.addWidget(kicker)
        kicker_row.addStretch(1)
        self._count_label = QLabel("0 clips")
        self._count_label.setProperty("hint", True)
        kicker_row.addWidget(self._count_label)
        outer.addLayout(kicker_row)

        title = QLabel("Marked clips")
        title.setProperty("title", True)
        outer.addWidget(title)

        hint = QLabel(
            "Add clips from the Ingest stage (I / O to mark, then Add Clip). "
            "Double-click a clip to render it. The Output stage stitches everything."
        )
        hint.setWordWrap(True)
        hint.setFixedWidth(720)
        hint.setProperty("hint", True)
        outer.addWidget(hint)

        self._list = QListWidget()
        self._list.itemDoubleClicked.connect(self._on_item_activated)
        outer.addWidget(self._list, 1)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self._render_btn = QPushButton("Render Selected")
        self._render_btn.setProperty("primary", True)
        self._render_btn.clicked.connect(self._on_render_selected)
        self._render_btn.setEnabled(False)
        actions.addWidget(self._render_btn)
        self._remove_btn = QPushButton("Remove Selected")
        self._remove_btn.clicked.connect(self._on_remove_selected)
        self._remove_btn.setEnabled(False)
        actions.addWidget(self._remove_btn)
        actions.addStretch(1)
        outer.addLayout(actions)

        state.clips_changed.connect(self._render_clips)
        self._list.currentRowChanged.connect(self._on_row_changed)

    def _render_clips(self, clips: list[Clip]) -> None:
        self._list.clear()
        for clip in clips:
            text = f"{clip.title}    {_format_ms(clip.start_ms)} → {_format_ms(clip.end_ms)}    ({_format_ms(clip.duration_ms)})"
            self._list.addItem(QListWidgetItem(text))
        self._count_label.setText(f"{len(clips)} clip{'s' if len(clips) != 1 else ''}")
        has = bool(clips)
        self._render_btn.setEnabled(has)
        self._remove_btn.setEnabled(has)

    def _on_row_changed(self, row: int) -> None:
        self._state.set_active_clip(row if row >= 0 else None)

    def _on_item_activated(self, _item: QListWidgetItem) -> None:
        self._on_render_selected()

    def _on_render_selected(self) -> None:
        clip = self._state.active_clip
        if clip is not None:
            self.request_export_clip.emit(clip)

    def _on_remove_selected(self) -> None:
        idx = self._state.active_clip_index
        if idx is not None:
            self.request_remove_clip.emit(idx)
