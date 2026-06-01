"""Caption editor dialog — review/edit the transcript, then export ASS/SRT.

Replaces the legacy ``caption-editor.js``. Word-level rows keep whisper's exact
timing (so karaoke animation stays accurate); editing is limited to fixing the
text and toggling words on/off. A single speaker color and a burn-in flag round
out the MVP — multi-speaker diarization is a later add.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from native.services import captions
from native.ui import theme


def _fmt(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}.{int((seconds % 1) * 100):02d}"


class CaptionEditor(QDialog):
    def __init__(
        self,
        words: list[dict],
        ass_path: Optional[str] = None,
        srt_path: Optional[str] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Caption Editor")
        self.setMinimumSize(640, 560)
        self.setStyleSheet(theme.GLOBAL_QSS)
        self._words = [dict(w) for w in words]
        self._ass_path = ass_path
        self._srt_path = srt_path
        self._speaker_color = captions.DEFAULT_SPEAKER_COLORS[0]

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(12)

        head = QLabel(f"{len(self._words)} words transcribed — fix text, toggle words, then export.")
        head.setProperty("hint", True)
        head.setWordWrap(True)
        lay.addWidget(head)

        self._table = QTableWidget(len(self._words), 4)
        self._table.setHorizontalHeaderLabels(["On", "Start", "End", "Text"])
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        for row, w in enumerate(self._words):
            on = QTableWidgetItem()
            on.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            on.setCheckState(Qt.CheckState.Checked if w.get("enabled", True) else Qt.CheckState.Unchecked)
            self._table.setItem(row, 0, on)
            for col, val in ((1, _fmt(w["start"])), (2, _fmt(w["end"]))):
                item = QTableWidgetItem(val)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self._table.setItem(row, col, item)
            self._table.setItem(row, 3, QTableWidgetItem(w["text"]))
        lay.addWidget(self._table, 1)

        # Style controls.
        controls = QHBoxLayout()
        self._color_btn = QPushButton("Speaker colour…")
        self._color_btn.clicked.connect(self._pick_color)
        self._set_color_swatch()
        controls.addWidget(self._color_btn)
        self._burn_in = QCheckBox("Burn captions into the video on render")
        self._burn_in.setChecked(True)
        controls.addWidget(self._burn_in)
        controls.addStretch(1)
        lay.addLayout(controls)

        # Actions.
        actions = QHBoxLayout()
        actions.addStretch(1)
        export_ass = QPushButton("Export ASS…")
        export_ass.setProperty("primary", True)
        export_ass.clicked.connect(self._export_ass)
        actions.addWidget(export_ass)
        export_srt = QPushButton("Export SRT…")
        export_srt.clicked.connect(self._export_srt)
        actions.addWidget(export_srt)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        lay.addLayout(actions)

    @property
    def burn_in(self) -> bool:
        return self._burn_in.isChecked()

    # ── helpers ──────────────────────────────────────────────────────

    def _set_color_swatch(self) -> None:
        self._color_btn.setStyleSheet(
            f"background-color: {self._speaker_color}; color: {theme.ACCENT_INK}; font-weight: 600;"
        )

    def _pick_color(self) -> None:
        chosen = QColorDialog.getColor(QColor(self._speaker_color), self, "Speaker colour")
        if chosen.isValid():
            self._speaker_color = chosen.name()
            self._set_color_swatch()

    def _collect_words(self) -> list[dict]:
        words = []
        for row, base in enumerate(self._words):
            text_item = self._table.item(row, 3)
            on_item = self._table.item(row, 0)
            w = dict(base)
            w["text"] = text_item.text() if text_item else base["text"]
            w["enabled"] = on_item.checkState() == Qt.CheckState.Checked if on_item else True
            words.append(w)
        return words

    def _speakers(self) -> dict:
        return {"SPEAKER_0": {"color": self._speaker_color}}

    def _export_ass(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export ASS", self._ass_path or "captions.ass", "ASS subtitles (*.ass)"
        )
        if path:
            content = captions.generate_ass_subtitles(self._collect_words(), self._speakers())
            Path(path).write_text(content, encoding="utf-8")

    def _export_srt(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export SRT", self._srt_path or "captions.srt", "SRT subtitles (*.srt)"
        )
        if path:
            Path(path).write_text(captions.generate_srt(self._collect_words()), encoding="utf-8")
