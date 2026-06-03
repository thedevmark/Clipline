"""Caption editor dialog — review/edit the transcript, then export ASS/SRT.

Replaces the legacy ``caption-editor.js``. Word-level rows keep whisper's exact
timing (so karaoke animation stays accurate); editing is limited to fixing the
text and toggling words on/off. Per-speaker colour assignment and a burn-in flag
complete the MVP; multi-speaker diarization (auto-run) is wired to the
``speaker_count_changed`` signal consumed by a later stage.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal, QPoint
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
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


# ── Module-level coordinate helpers (Task 12) ────────────────────────────────

def norm_to_px(pos, w, h):
    """Convert normalized (0–1) position to pixel coordinates."""
    return (int(round(pos[0] * w)), int(round(pos[1] * h)))


def px_to_norm(px, w, h):
    """Convert pixel coordinates to normalized (0–1) position, clamped."""
    nx = round(max(0.0, min(1.0, px[0] / w)), 4)
    ny = round(max(0.0, min(1.0, px[1] / h)), 4)
    return (nx, ny)


# Column indices — named constants so refactors stay readable.
_COL_ON = 0
_COL_START = 1
_COL_END = 2
_COL_TEXT = 3
_COL_SPEAKER = 4

# Preview dimensions (9:16 portrait)
_PREVIEW_W = 240
_PREVIEW_H = 427

# Chip size (half-width used to center chip on its logical position)
_CHIP_W = 36
_CHIP_H = 20


class _SpeakerChip(QLabel):
    """A small draggable chip representing a speaker's default caption position.

    Drag mechanics:
    - ``mousePressEvent`` records the click offset within the chip and which
      speaker is being dragged.
    - ``mouseMoveEvent`` moves the chip within the preview bounds.
    - ``mouseReleaseEvent`` calls back the preview to commit the new position.
    """

    def __init__(self, speaker: str, color: str, preview: "PositionPreview") -> None:
        super().__init__(speaker, preview)
        self._speaker = speaker
        self._preview = preview
        self._drag_offset: Optional[QPoint] = None

        self.setFixedSize(_CHIP_W, _CHIP_H)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._apply_style(color)
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def _apply_style(self, color: str) -> None:
        # Determine a contrasting ink colour (simple luminance heuristic).
        try:
            c = QColor(color)
            lum = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
            ink = "#000000" if lum > 128 else "#ffffff"
        except Exception:
            ink = "#ffffff"
        self.setStyleSheet(
            f"background-color:{color}; color:{ink}; border-radius:4px;"
            f" font-size:9px; font-weight:700; padding:1px 2px;"
        )

    def update_color(self, color: str) -> None:
        self._apply_style(color)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None:
            # Map movement to preview coordinates.
            new_pos = self.pos() + event.position().toPoint() - self._drag_offset
            pw, ph = self._preview.preview_size()
            # Clamp so the chip stays fully inside the preview.
            x = max(0, min(pw - _CHIP_W, new_pos.x()))
            y = max(0, min(ph - _CHIP_H, new_pos.y()))
            self.move(x, y)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._drag_offset is not None:
            self._drag_offset = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            # Commit position: center of chip → normalized.
            pw, ph = self._preview.preview_size()
            cx = self.x() + _CHIP_W // 2
            cy = self.y() + _CHIP_H // 2
            self._preview.on_chip_dropped(self._speaker, px_to_norm((cx, cy), pw, ph))
        super().mouseReleaseEvent(event)


class PositionPreview(QFrame):
    """9:16 preview area: drag chips to set per-speaker default positions;
    click empty space (when a table row is selected) to set a per-line override.
    """

    def __init__(self, editor: "CaptionEditor", parent=None) -> None:
        super().__init__(parent)
        self._editor = editor
        self.setFixedSize(_PREVIEW_W, _PREVIEW_H)
        self.setStyleSheet("background-color: #1a1a2e; border: 1px solid #333;")
        self._chips: dict[str, _SpeakerChip] = {}
        self._bg_label: Optional[QLabel] = None

    def preview_size(self):
        return (_PREVIEW_W, _PREVIEW_H)

    def set_background(self, pixmap: QPixmap) -> None:
        if self._bg_label is None:
            self._bg_label = QLabel(self)
            self._bg_label.setGeometry(0, 0, _PREVIEW_W, _PREVIEW_H)
            self._bg_label.lower()
        self._bg_label.setPixmap(pixmap.scaled(
            _PREVIEW_W, _PREVIEW_H,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))
        self._bg_label.show()

    def refresh_chips(self) -> None:
        """Rebuild / reposition chips from editor state."""
        speakers = self._editor._sorted_speakers()
        # Remove chips for speakers that no longer exist.
        gone = [s for s in list(self._chips) if s not in speakers]
        for s in gone:
            self._chips[s].deleteLater()
            del self._chips[s]

        for sp in speakers:
            color = self._editor._color_for_speaker(sp)
            if sp not in self._chips:
                chip = _SpeakerChip(sp, color, self)
                chip.show()
                self._chips[sp] = chip
            else:
                self._chips[sp].update_color(color)
            pos = self._editor._speaker_pos.get(sp, (0.5, 0.85))
            px, py = norm_to_px(pos, _PREVIEW_W, _PREVIEW_H)
            # Position chip so its center is at (px, py), clamped.
            self._chips[sp].move(
                max(0, min(_PREVIEW_W - _CHIP_W, px - _CHIP_W // 2)),
                max(0, min(_PREVIEW_H - _CHIP_H, py - _CHIP_H // 2)),
            )

    def on_chip_dropped(self, speaker: str, norm_pos) -> None:
        self._editor._speaker_pos[speaker] = norm_pos

    def mousePressEvent(self, event) -> None:
        """Click on empty space → per-line override for selected row."""
        if event.button() == Qt.MouseButton.LeftButton:
            click_pt = event.position().toPoint()
            # Only register if we didn't hit a chip (chips handle their own events).
            for chip in self._chips.values():
                if chip.geometry().contains(click_pt):
                    super().mousePressEvent(event)
                    return
            # Get selected row from editor table.
            line_start = self._editor._selected_row_start()
            if line_start is not None:
                norm = px_to_norm((click_pt.x(), click_pt.y()), _PREVIEW_W, _PREVIEW_H)
                self._editor._line_overrides[line_start] = norm
                self._editor._update_override_label()
        super().mousePressEvent(event)


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
        source_path: Optional[str] = None,
        ffmpeg: Optional[str] = None,
        clip_ms: Optional[tuple] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Caption Editor")
        self.setMinimumSize(960, 580)
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

        # ── Main area: table + preview side-by-side ────────────────────
        main_row = QHBoxLayout()
        main_row.setSpacing(16)

        left_col = QVBoxLayout()
        left_col.setSpacing(8)

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
        self._populate_table()
        self._table.itemSelectionChanged.connect(self._on_table_selection_changed)
        left_col.addWidget(self._table, 1)

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
        left_col.addLayout(speaker_row)

        main_row.addLayout(left_col, 1)

        # ── Right column: preview + override controls ──────────────────
        right_col = QVBoxLayout()
        right_col.setSpacing(6)

        preview_label = QLabel("Position preview")
        preview_label.setProperty("hint", True)
        right_col.addWidget(preview_label)

        self._preview = PositionPreview(self)
        right_col.addWidget(self._preview)

        # Override status label + clear button.
        self._override_label = QLabel("No line selected")
        self._override_label.setProperty("hint", True)
        self._override_label.setWordWrap(True)
        self._override_label.setMaximumWidth(_PREVIEW_W)
        right_col.addWidget(self._override_label)

        self._clear_override_btn = QPushButton("Clear line override")
        self._clear_override_btn.setEnabled(False)
        self._clear_override_btn.clicked.connect(self._clear_line_override)
        right_col.addWidget(self._clear_override_btn)

        right_col.addStretch(1)
        main_row.addLayout(right_col)

        lay.addLayout(main_row, 1)

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

        # Populate speaker combo + swatch after widgets exist.
        self._refresh_speaker_combo()

        # Initial chip layout.
        self._preview.refresh_chips()

        # Try to extract a background frame for the preview.
        self._try_load_preview_frame(source_path, ffmpeg, clip_ms)

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

    # ── Public reload (Task 10 re-diarize) ────────────────────────────

    def reload_words(self, words: list[dict]) -> None:
        """Replace the transcript with re-diarized *words*, keeping colours/positions.

        Used after a speaker-count re-run: the word text/timing is unchanged but
        the ``speaker`` labels are refreshed. Per-speaker colours and positions
        are intentionally preserved so the user's manual tweaks survive a recount.
        """
        self._words = [dict(w) for w in words]
        self._table.setRowCount(len(self._words))
        self._populate_table()
        self._refresh_speaker_combo()
        self._preview.refresh_chips()
        self._update_override_label()

    # ── Helpers ───────────────────────────────────────────────────────

    def _populate_table(self) -> None:
        """(Re)fill every table row from ``self._words``."""
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

    def _selected_row_start(self) -> Optional[float]:
        """Return the start-time (seconds) of the first selected row, or None."""
        rows = sorted({idx.row() for idx in self._table.selectedIndexes()})
        if not rows:
            return None
        return self._words[rows[0]]["start"]

    def _update_override_label(self) -> None:
        """Refresh the override status label and Clear button for the selected row."""
        line_start = self._selected_row_start()
        if line_start is None:
            self._override_label.setText("No line selected")
            self._clear_override_btn.setEnabled(False)
            return
        if line_start in self._line_overrides:
            pos = self._line_overrides[line_start]
            self._override_label.setText(
                f"Override at {pos[0]:.2f}, {pos[1]:.2f}"
                f" (t={line_start:.2f}s)"
            )
            self._clear_override_btn.setEnabled(True)
        else:
            self._override_label.setText(
                f"Selected row t={line_start:.2f}s — click preview to set override"
            )
            self._clear_override_btn.setEnabled(False)

    def _try_load_preview_frame(
        self,
        source_path: Optional[str],
        ffmpeg: Optional[str],
        clip_ms: Optional[tuple],
    ) -> None:
        """Extract one frame from *source_path* and show it in the preview.

        Falls back silently if parameters are absent or extraction fails.
        """
        if not source_path or not ffmpeg:
            return
        try:
            from native.services.paths import PROCESSING_DIR
            PROCESSING_DIR.mkdir(parents=True, exist_ok=True)
            # Midpoint in seconds.
            if clip_ms:
                mid = (clip_ms[0] + clip_ms[1]) / 2000.0
            else:
                mid = 1.0
            out_png = PROCESSING_DIR / "_preview_frame.png"
            result = subprocess.run(
                [ffmpeg, "-y", "-ss", str(mid), "-i", source_path,
                 "-frames:v", "1", str(out_png)],
                capture_output=True,
                timeout=10,
            )
            if result.returncode == 0 and out_png.exists():
                px = QPixmap(str(out_png))
                if not px.isNull():
                    self._preview.set_background(px)
        except Exception:
            # Frame extraction is best-effort; never crash the editor.
            pass

    # ── Slots ─────────────────────────────────────────────────────────

    def _on_count_changed(self, text: str) -> None:
        self.speaker_count_changed.emit(self.selected_speaker_count())

    def _on_speaker_combo_changed(self, _text: str) -> None:
        self._update_swatch()

    def _on_table_selection_changed(self) -> None:
        self._update_override_label()

    def _pick_color(self) -> None:
        sp = self._current_speaker()
        current_color = self._color_for_speaker(sp)
        chosen = QColorDialog.getColor(QColor(current_color), self, f"Colour for {sp}")
        if chosen.isValid():
            self._speaker_colors[sp] = chosen.name()
            self._speaker_color = chosen.name()
            self._set_color_swatch()
            # Refresh chip colour in preview.
            self._preview.refresh_chips()

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
        self._preview.refresh_chips()

    def _clear_line_override(self) -> None:
        """Remove the per-line override for the currently selected row."""
        line_start = self._selected_row_start()
        if line_start is not None:
            self._line_overrides.pop(line_start, None)
            self._update_override_label()

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
