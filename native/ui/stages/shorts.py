"""Shorts stage — caption pass (whisper.cpp) + per-short polish.

Captions are a one-click feature: the speech engine (whisper.cpp binary +
model, ~75 MB) downloads on demand into the runtime dir — no Python, no pip, no
terminal, because the audience has none of those. Once set up, "Run caption
pass" transcribes the current source and opens the caption editor.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from native.services import captions, whisper_cpp
from native.services.paths import PROCESSING_DIR
from native.ui import theme
from native.ui.caption_editor import CaptionEditor
from native.ui.project_state import ProjectState
from native.workers import JobRunner, caption_pass, download_captioner


class ShortsStage(QWidget):
    def __init__(self, state: ProjectState, runner: JobRunner, ffmpeg: str) -> None:
        super().__init__()
        self.setObjectName("stage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._state = state
        self._runner = runner
        self._ffmpeg = ffmpeg

        outer = QVBoxLayout(self)
        outer.setContentsMargins(48, 40, 48, 40)
        outer.setSpacing(20)

        kicker = QLabel("SHORTS POLISH")
        kicker.setProperty("kicker", True)
        outer.addWidget(kicker)

        title = QLabel("Caption pass")
        title.setProperty("title", True)
        outer.addWidget(title)

        hint = QLabel(
            "Auto-caption your clips with word-level karaoke timing. The first run "
            "downloads a small speech engine (~75 MB) — one click, no setup, it stays "
            "on your machine. Everything runs locally."
        )
        hint.setWordWrap(True)
        hint.setFixedWidth(720)
        hint.setProperty("hint", True)
        outer.addWidget(hint)

        # Engine card.
        card = QFrame()
        card.setObjectName("card")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 18, 20, 18)
        card_layout.setSpacing(12)

        status_kicker = QLabel("CAPTIONING ENGINE")
        status_kicker.setProperty("kicker", True)
        card_layout.addWidget(status_kicker)

        self._engine_label = QLabel()
        self._engine_label.setWordWrap(True)
        card_layout.addWidget(self._engine_label)

        row = QHBoxLayout()
        row.setSpacing(10)
        self._setup_btn = QPushButton(f"Set up captions ({whisper_cpp.DOWNLOAD_SIZE_LABEL})")
        self._setup_btn.clicked.connect(self._setup_captions)
        row.addWidget(self._setup_btn)
        self._run_btn = QPushButton("Run caption pass")
        self._run_btn.setProperty("primary", True)
        self._run_btn.clicked.connect(self._run_caption_pass)
        row.addWidget(self._run_btn)
        self._status = QLabel("")
        self._status.setProperty("hint", True)
        row.addWidget(self._status, 1)
        card_layout.addLayout(row)

        outer.addWidget(card)
        outer.addStretch(1)

        self._refresh_engine_state()

    # ── engine state ─────────────────────────────────────────────────

    def _refresh_engine_state(self) -> None:
        ready = captions.available()
        if ready:
            self._engine_label.setText("✓ Ready — captions run fully on your machine.")
            self._engine_label.setStyleSheet(f"color: {theme.ACCENT}; font-weight: 600;")
        else:
            self._engine_label.setText(
                "Not installed yet. Click “Set up captions” for the one-time download."
            )
            self._engine_label.setStyleSheet(f"color: {theme.INK_MUTED};")
        self._setup_btn.setVisible(not ready)
        self._run_btn.setEnabled(ready)

    # ── one-click setup ──────────────────────────────────────────────

    def _setup_captions(self) -> None:
        self._setup_btn.setEnabled(False)
        self._status.setText("Downloading…")
        self._runner.run(
            download_captioner,
            on_progress=lambda msg: self._status.setText(msg),
            on_progress_pct=lambda pct: self._status.setText(f"Downloading… {int(pct * 100)}%"),
            on_finished=self._on_setup_done,
            on_error=self._on_setup_error,
        )

    def _on_setup_done(self, _result: object) -> None:
        self._status.setText("Speech engine ready.")
        self._refresh_engine_state()

    def _on_setup_error(self, message: str) -> None:
        self._setup_btn.setEnabled(True)
        self._status.setText("Download failed")
        QMessageBox.warning(self, "Captioning setup failed", message)

    # ── caption pass ─────────────────────────────────────────────────

    def _run_caption_pass(self) -> None:
        if not captions.available():
            self._refresh_engine_state()
            return
        source = self._state.source
        if source is None:
            QMessageBox.information(
                self, "No source", "Load a video in the Ingest stage first."
            )
            return
        self._run_btn.setEnabled(False)
        self._status.setText("Transcribing…")
        self._runner.run(
            caption_pass,
            self._ffmpeg,
            source,
            PROCESSING_DIR,
            on_progress=lambda msg: self._status.setText(msg),
            on_finished=self._on_captions_ready,
            on_error=self._on_captions_error,
        )

    def _on_captions_ready(self, result: object) -> None:
        self._run_btn.setEnabled(True)
        data = result if isinstance(result, dict) else {}
        words = data.get("words", [])
        self._status.setText(f"{len(words)} words — opening editor")
        editor = CaptionEditor(words, data.get("ass"), data.get("srt"), parent=self)
        editor.exec()

    def _on_captions_error(self, message: str) -> None:
        self._run_btn.setEnabled(True)
        self._status.setText("Caption pass failed")
        QMessageBox.warning(self, "Caption pass failed", message)
