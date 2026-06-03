"""Caption editor dialog — review/edit the transcript, then export ASS/SRT.

Replaces the legacy ``caption-editor.js``. Word-level rows keep whisper's exact
timing (so karaoke animation stays accurate); editing is limited to fixing the
text and toggling words on/off. Per-speaker colour assignment and a burn-in flag
complete the MVP; multi-speaker diarization (auto-run) is wired to the
``speaker_count_changed`` signal consumed by a later stage.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
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


# Column indices — named constants so refactors stay readable.
_COL_ON = 0
_COL_START = 1
_COL_END = 2
_COL_TEXT = 3
_COL_SPEAKER = 4


class CaptionEditor(QDialog):
    # Emitted when the user changes the speaker-count selector.
    # Value is int (2/3/4) or None for "Auto".
    speaker_count_changed = Signal(object)

    def __init__(
        self,
        words: list[dict],
        ass_path: Optional[str] = None,
        srt_path: Optional[str] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Caption Editor")
        self.setMinimumSize(720, 580)
        self.setStyleSheet(theme.GLOBAL_QSS)

        self._words = [dict(w) for w in words]
        self._ass_path = ass_path
        self._srt_path = srt_path

        # Task 11 state holders.
        self._speaker_colors: dict = {}            # speaker_id -> "#RRGGBB"
        self._speaker_pos: dict = {}               # speaker_id -> (x,y) normalized (filled by Task 12)
        self._line_overrides: dict = {}            # line_start_seconds -> (x,y) (filled by Task 12)

        # Legacy single-colour kept for _set_color_swatch compat; now mirrors
        # the currently selected speaker's colour.
        self._speaker_color = self._color_for_speaker(self._sorted_speakers()[0]
                                                      if self._sorted_speakers()
                                                      else "SPEAKER_0")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(12)

        head = QLabel(
            f"{len(self._words)} words transcribed — fix text, toggle words, assign speakers, then export."
        )
        head.setProperty("hint", True)
        head.setWordWrap(True)
        lay.addWidget(head)

        # ── Table ──────────────────────────────────────────────────────
        self._table = QTableWidget(len(self._words), 5)
        self._table.setHorizontalHeaderLabels(["On", "Start", "End", "Text", "Speaker"])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(_COL_ON, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COL_START, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COL_END, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COL_TEXT, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(_COL_SPEAKER, QHeaderView.ResizeMode.ResizeToContents)
        for row, w in enumerate(self._words):
            on = QTableWidgetItem()
            on.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            on.setCheckState(Qt.CheckState.Checked if w.get("enabled", True) else Qt.CheckState.Unchecked)
            self._table.setItem(row, _COL_ON, on)
            for col, val in ((_COL_START, _fmt(w["start"])), (_COL_END, _fmt(w["end"]))):
                item = QTableWidgetItem(val)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self._table.setItem(row, col, item)
            self._table.setItem(row, _COL_TEXT, QTableWidgetItem(w["text"]))
            sp_item = QTableWidgetItem(w.get("speaker", "SPEAKER_0"))
            sp_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self._table.setItem(row, _COL_SPEAKER, sp_item)
        lay.addWidget(self._table, 1)

        # ── Speaker controls ───────────────────────────────────────────
        speaker_row = QHBoxLayout()

        # Speaker count selector.
        speaker_row.addWidget(QLabel("Speakers:"))
        self._count_combo = QComboBox()
        self._count_combo.addItems(["Auto", "2", "3", "4"])
        self._count_combo.setFixedWidth(72)
        self._count_combo.currentTextChanged.connect(self._on_count_changed)
        speaker_row.addWidget(self._count_combo)

        speaker_row.addSpacing(16)

        # Speaker assignment.
        speaker_row.addWidget(QLabel("Assign to:"))
        self._speaker_combo = QComboBox()
        self._speaker_combo.setFixedWidth(120)
        self._speaker_combo.currentTextChanged.connect(self._on_speaker_combo_changed)
        speaker_row.addWidget(self._speaker_combo)

        self._assign_btn = QPushButton("Assign selected rows")
        self._assign_btn.clicked.connect(self._assign_speaker)
        speaker_row.addWidget(self._assign_btn)

        speaker_row.addSpacing(16)

        # Colour for current speaker.
        self._color_btn = QPushButton("Speaker colour…")
        self._color_btn.clicked.connect(self._pick_color)
        speaker_row.addWidget(self._color_btn)

        self._burn_in = QCheckBox("Burn captions into video on render")
        self._burn_in.setChecked(True)
        speaker_row.addWidget(self._burn_in)

        speaker_row.addStretch(1)
        lay.addLayout(speaker_row)

        # Populate speaker combo + swatch after widgets exist.
        self._refresh_speaker_combo()

        # ── Actions ───────────────────────────────────────────────────
        actions = QHBoxLayout()
        actions.addStretch(1)
        export_ass = QPushButton("Export ASS…")
        export_ass.setProperty("primary", True)
        export_ass.clicked.connect(self._export_ass)
        actions.addWidget(export_ass)
        export_srt = QPushButton("Export SRT…")
        export_srt.clicked.connect(self._export_srt)
        actions.addWidget(export_srt)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        actions.addWidget(close_btn)
        lay.addLayout(actions)

    # ── Public accessors (required by later tasks) ─────────────────────

    def selected_speaker_count(self) -> int | None:
        """Return selected speaker count, or None for 'Auto'."""
        text = self._count_combo.currentText()
        return None if text == "Auto" else int(text)

    def result_words(self) -> list[dict]:
        return self._collect_words()

    def result_speakers(self) -> dict:
        out = {}
        speakers = sorted({w.get("speaker", "SPEAKER_0") for w in self._collect_words()})
        for i, sp in enumerate(speakers):
            out[sp] = {
                "color": self._speaker_colors.get(sp, captions.DEFAULT_SPEAKER_COLORS[i % 5]),
                "pos": self._speaker_pos.get(sp, (0.5, 0.85)),
            }
        return out

    def result_overrides(self) -> dict:
        return dict(self._line_overrides)

    # ── Properties ────────────────────────────────────────────────────

    @property
    def burn_in(self) -> bool:
        return self._burn_in.isChecked()

    # ── Helpers ───────────────────────────────────────────────────────

    def _sorted_speakers(self) -> list[str]:
        return sorted({w.get("speaker", "SPEAKER_0") for w in self._words})

    def _color_for_speaker(self, speaker: str) -> str:
        """Return the stored colour for *speaker*, seeding lazily from the palette."""
        if speaker not in self._speaker_colors:
            idx = self._sorted_speakers().index(speaker) if speaker in self._sorted_speakers() else 0
            self._speaker_colors[speaker] = captions.DEFAULT_SPEAKER_COLORS[idx % 5]
        return self._speaker_colors[speaker]

    def _refresh_speaker_combo(self) -> None:
        """Rebuild the speaker combo from current word data and refresh swatch."""
        speakers = self._sorted_speakers()
        current = self._speaker_combo.currentText()
        self._speaker_combo.blockSignals(True)
        self._speaker_combo.clear()
        self._speaker_combo.addItems(speakers)
        # Restore selection if still present; otherwise pick first.
        if current in speakers:
            self._speaker_combo.setCurrentText(current)
        self._speaker_combo.blockSignals(False)
        self._update_swatch()

    def _current_speaker(self) -> str:
        return self._speaker_combo.currentText() or "SPEAKER_0"

    def _update_swatch(self) -> None:
        sp = self._current_speaker()
        color = self._color_for_speaker(sp)
        self._speaker_color = color  # keep legacy attr in sync
        self._set_color_swatch()

    def _set_color_swatch(self) -> None:
        self._color_btn.setStyleSheet(
            f"background-color: {self._speaker_color}; color: {theme.ACCENT_INK}; font-weight: 600;"
        )

    # ── Slots ─────────────────────────────────────────────────────────

    def _on_count_changed(self, text: str) -> None:
        self.speaker_count_changed.emit(self.selected_speaker_count())

    def _on_speaker_combo_changed(self, _text: str) -> None:
        self._update_swatch()

    def _pick_color(self) -> None:
        sp = self._current_speaker()
        current_color = self._color_for_speaker(sp)
        chosen = QColorDialog.getColor(QColor(current_color), self, f"Colour for {sp}")
        if chosen.isValid():
            self._speaker_colors[sp] = chosen.name()
            self._speaker_color = chosen.name()
            self._set_color_swatch()

    def _assign_speaker(self) -> None:
        """Assign the selected speaker to all highlighted rows."""
        sp = self._current_speaker()
        selected_rows = {idx.row() for idx in self._table.selectedIndexes()}
        if not selected_rows:
            return
        for row in selected_rows:
            self._words[row]["speaker"] = sp
            item = self._table.item(row, _COL_SPEAKER)
            if item is not None:
                item.setText(sp)
        # Combo list might not change (speaker already existed), but refresh swatch.
        self._refresh_speaker_combo()

    def _collect_words(self) -> list[dict]:
        words = []
        for row, base in enumerate(self._words):
            text_item = self._table.item(row, _COL_TEXT)
            on_item = self._table.item(row, _COL_ON)
            w = dict(base)
            w["text"] = text_item.text() if text_item else base["text"]
            w["enabled"] = on_item.checkState() == Qt.CheckState.Checked if on_item else True
            words.append(w)
        return words

    def _speakers(self) -> dict:
        """Legacy helper used by export methods — delegates to result_speakers()."""
        return self.result_speakers()

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
