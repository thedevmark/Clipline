"""Inbox stage — cut list + inspector + waveform timeline.

The cut list (left) holds every marked clip. Selecting one loads it into the
inspector (right: title / range / duration / notes) and the waveform timeline
(bottom), where the in/out handles drag to retrim. Edits write straight back
to ``ProjectState`` and reflect live in the list. Double-click still renders a
single clip; the Output stage stitches everything.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from native.services.export_presets import STYLE_PRESETS, style_preset_by_key
from native.services.paths import PROCESSING_DIR
from native.ui import theme
from native.ui.project_state import Clip, ProjectState
from native.ui.timeline import TimelineStrip
from native.workers import JobRunner, generate_waveform


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
    """Clip list + inspector + drag-trim timeline."""

    request_export_clip = Signal(object)  # Clip
    request_remove_clip = Signal(int)

    def __init__(self, state: ProjectState, runner: JobRunner, ffmpeg: str) -> None:
        super().__init__()
        self.setObjectName("stage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._state = state
        self._runner = runner
        self._ffmpeg = ffmpeg
        self._populating = False
        self._wave_source: Optional[Path] = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 24, 32, 24)
        outer.setSpacing(14)

        kicker_row = QHBoxLayout()
        kicker = QLabel("CUT LIST")
        kicker.setProperty("kicker", True)
        kicker_row.addWidget(kicker)
        kicker_row.addStretch(1)
        self._count_label = QLabel("0 clips")
        self._count_label.setProperty("hint", True)
        kicker_row.addWidget(self._count_label)
        outer.addLayout(kicker_row)

        # Style preset picker — applies to every clip render dispatched here
        # and to the bulk render on the Output stage.
        preset_row = QHBoxLayout()
        preset_row.setSpacing(10)
        preset_label = QLabel("Style preset:")
        preset_label.setProperty("hint", True)
        preset_row.addWidget(preset_label)
        self._preset_picker = QComboBox()
        for preset in STYLE_PRESETS:
            self._preset_picker.addItem(preset.label, preset.key)
        self._preset_picker.currentIndexChanged.connect(self._on_preset_picked)
        preset_row.addWidget(self._preset_picker)
        self._preset_desc = QLabel(STYLE_PRESETS[0].description)
        self._preset_desc.setProperty("hint", True)
        self._preset_desc.setWordWrap(True)
        preset_row.addWidget(self._preset_desc, 1)
        outer.addLayout(preset_row)

        # Main split: list (left) + inspector (right).
        split = QHBoxLayout()
        split.setSpacing(20)
        split.addLayout(self._build_list_column(), 1)
        split.addWidget(self._build_inspector(), 0)
        outer.addLayout(split, 1)

        # Waveform + drag-trim timeline for the active clip.
        self._timeline = TimelineStrip()
        self._timeline.range_changed.connect(self._on_timeline_live)
        self._timeline.range_committed.connect(self._on_timeline_commit)
        outer.addWidget(self._timeline)

        state.clips_changed.connect(self._render_clips)
        state.active_clip_changed.connect(self._on_active_changed)
        state.source_changed.connect(self._on_source_changed)
        state.source_metadata_changed.connect(self._on_metadata_changed)
        self._list.currentRowChanged.connect(self._on_row_changed)

        self._set_inspector_enabled(False)

    # ── builders ─────────────────────────────────────────────────────

    def _build_list_column(self) -> QVBoxLayout:
        column = QVBoxLayout()
        column.setSpacing(8)
        self._list = QListWidget()
        self._list.itemDoubleClicked.connect(self._on_item_activated)
        column.addWidget(self._list, 1)

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
        column.addLayout(actions)
        return column

    def _build_inspector(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        card.setMinimumWidth(320)
        card.setMaximumWidth(360)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(10)

        k = QLabel("INSPECTOR")
        k.setProperty("kicker", True)
        lay.addWidget(k)

        lay.addWidget(self._field_label("Title"))
        self._title_edit = QLineEdit()
        self._title_edit.editingFinished.connect(self._on_title_edited)
        lay.addWidget(self._title_edit)

        self._range_label = QLabel("Range: —")
        self._range_label.setProperty("mono", True)
        lay.addWidget(self._range_label)
        self._dur_label = QLabel("Duration: —")
        self._dur_label.setProperty("hint", True)
        lay.addWidget(self._dur_label)

        lay.addWidget(self._field_label("Notes"))
        self._notes_edit = QPlainTextEdit()
        self._notes_edit.setFixedHeight(90)
        self._notes_edit.textChanged.connect(self._on_notes_edited)
        lay.addWidget(self._notes_edit)

        self._trim_hint = QLabel("Drag the handles on the timeline below to retrim.")
        self._trim_hint.setProperty("hint", True)
        self._trim_hint.setWordWrap(True)
        lay.addWidget(self._trim_hint)
        lay.addStretch(1)
        return card

    @staticmethod
    def _field_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color: {theme.INK_MUTED}; font-size: 11px; font-weight: 600;")
        return lbl

    # ── presets ──────────────────────────────────────────────────────

    def _on_preset_picked(self, index: int) -> None:
        key = self._preset_picker.itemData(index)
        preset = style_preset_by_key(key)
        self._state.set_style_preset(preset.key)
        self._preset_desc.setText(preset.description)

    # ── list rendering ───────────────────────────────────────────────

    def _render_clips(self, clips: list[Clip]) -> None:
        # Update item text in place when the count matches so editing a clip
        # doesn't churn the selection (which would re-enter the inspector).
        if self._list.count() == len(clips):
            for i, clip in enumerate(clips):
                self._list.item(i).setText(self._clip_text(clip))
        else:
            current = self._list.currentRow()
            self._list.clear()
            for clip in clips:
                self._list.addItem(QListWidgetItem(self._clip_text(clip)))
            if 0 <= current < len(clips):
                self._list.setCurrentRow(current)
        self._count_label.setText(f"{len(clips)} clip{'s' if len(clips) != 1 else ''}")
        has = bool(clips)
        self._render_btn.setEnabled(has)
        self._remove_btn.setEnabled(has)
        # Keep the inspector/timeline labels in sync with the live active clip.
        active = self._state.active_clip
        if active is not None and not self._title_edit.hasFocus():
            self._refresh_range_labels(active)
            self._timeline.set_clip(active.start_ms, active.end_ms)

    @staticmethod
    def _clip_text(clip: Clip) -> str:
        return (f"{clip.title}    {_format_ms(clip.start_ms)} → {_format_ms(clip.end_ms)}"
                f"    ({_format_ms(clip.duration_ms)})")

    # ── selection / inspector population ─────────────────────────────

    def _on_row_changed(self, row: int) -> None:
        self._state.set_active_clip(row if row >= 0 else None)

    def _on_active_changed(self, _index: object) -> None:
        clip = self._state.active_clip
        if clip is None:
            self._set_inspector_enabled(False)
            self._timeline.set_clip(0, 0)
            return
        self._set_inspector_enabled(True)
        self._populating = True
        self._title_edit.setText(clip.title)
        self._notes_edit.setPlainText(clip.notes)
        self._populating = False
        self._refresh_range_labels(clip)
        self._timeline.set_clip(clip.start_ms, clip.end_ms)

    def _set_inspector_enabled(self, on: bool) -> None:
        self._title_edit.setEnabled(on)
        self._notes_edit.setEnabled(on)
        if not on:
            self._populating = True
            self._title_edit.clear()
            self._notes_edit.clear()
            self._populating = False
            self._range_label.setText("Range: —")
            self._dur_label.setText("Duration: —")

    def _refresh_range_labels(self, clip: Clip) -> None:
        self._range_label.setText(
            f"Range: {_format_ms(clip.start_ms)} → {_format_ms(clip.end_ms)}"
        )
        self._dur_label.setText(f"Duration: {_format_ms(clip.duration_ms)}")

    def _on_title_edited(self) -> None:
        if not self._populating and self._state.active_clip is not None:
            self._state.update_active_clip(title=self._title_edit.text())

    def _on_notes_edited(self) -> None:
        if not self._populating and self._state.active_clip is not None:
            self._state.update_active_clip(notes=self._notes_edit.toPlainText())

    # ── timeline (drag-trim) ─────────────────────────────────────────

    def _on_timeline_live(self, start_ms: int, end_ms: int) -> None:
        # Live label feedback while dragging; commit writes to the model.
        self._range_label.setText(f"Range: {_format_ms(start_ms)} → {_format_ms(end_ms)}")
        self._dur_label.setText(f"Duration: {_format_ms(max(0, end_ms - start_ms))}")

    def _on_timeline_commit(self, start_ms: int, end_ms: int) -> None:
        if self._state.active_clip is not None:
            self._state.update_active_clip(start_ms=start_ms, end_ms=end_ms)

    # ── source / waveform ────────────────────────────────────────────

    def _on_source_changed(self, path: Optional[Path]) -> None:
        self._timeline.set_waveform(None)
        if path is None:
            self._timeline.clear()
            return
        duration = int(self._state.metadata.get("duration_ms", 0))
        self._timeline.set_source(duration)
        self._generate_waveform(path)

    def _on_metadata_changed(self, meta: dict) -> None:
        if "duration_ms" in meta:
            self._timeline.set_source(int(meta["duration_ms"]))
            active = self._state.active_clip
            if active is not None:
                self._timeline.set_clip(active.start_ms, active.end_ms)

    def _generate_waveform(self, source: Path) -> None:
        if self._wave_source == source:
            return
        self._wave_source = source
        out_png = PROCESSING_DIR / "waveform.png"
        self._runner.run(
            generate_waveform,
            self._ffmpeg,
            source,
            out_png,
            on_finished=self._on_waveform_ready,
            on_error=lambda _msg: None,  # no audio / failure → flat track, fine
        )

    def _on_waveform_ready(self, result: object) -> None:
        pixmap = QPixmap(str(result))
        if not pixmap.isNull():
            self._timeline.set_waveform(pixmap)

    # ── actions ──────────────────────────────────────────────────────

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
