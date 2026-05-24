"""Shorts stage — caption pass + per-short polish.

Phase 4 scope: surface caption-runtime status (faster-whisper, torch,
pyannote.audio) and provide install pointers. We intentionally do *not*
bundle these into the native EXE — they add ~300 MB and most users do
not need diarization on every clip. The original Flask build installed
them into a separately-managed virtualenv under APP_STATE_DIR; native
will follow the same shape in a later phase.

The full caption-editor dialog from the legacy ``static/js/caption-editor.js``
is deferred until ``captions.py`` is extracted into ``native/services/``.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from native.ui import theme


_CAPTION_RUNTIME_PACKAGES = (
    ("faster-whisper", "faster_whisper", "Required for caption pass."),
    ("torch", "torch", "Required by faster-whisper for inference."),
    ("pyannote.audio", "pyannote.audio", "Optional — enables speaker diarization."),
)


def _check_package(import_name: str) -> bool:
    try:
        spec = importlib.util.find_spec(import_name)
    except (ImportError, ValueError):
        return False
    return spec is not None


class ShortsStage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("stage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

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
            "Run faster-whisper over the inbox to produce ASS/SRT captions for each "
            "prepared short. The native shell does not bundle the captioning runtime "
            "(it's ~300 MB of ML dependencies); install it once with pip in the same "
            "Python environment Clipline runs under."
        )
        hint.setWordWrap(True)
        hint.setFixedWidth(720)
        hint.setProperty("hint", True)
        outer.addWidget(hint)

        # Runtime status card.
        card = QFrame()
        card.setObjectName("card")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 18, 20, 18)
        card_layout.setSpacing(10)

        status_kicker = QLabel("RUNTIME STATUS")
        status_kicker.setProperty("kicker", True)
        card_layout.addWidget(status_kicker)

        for label, import_name, note in _CAPTION_RUNTIME_PACKAGES:
            present = _check_package(import_name)
            row = QHBoxLayout()
            marker = QLabel("✓" if present else "•")
            marker.setStyleSheet(
                f"color: {theme.ACCENT if present else theme.INK_DIM};"
                "font-size: 18px; font-weight: 700; min-width: 24px;"
            )
            row.addWidget(marker)
            name = QLabel(label)
            name.setStyleSheet(f"color: {theme.INK_BRIGHT}; font-weight: 600; min-width: 160px;")
            row.addWidget(name)
            n = QLabel(note + ("" if present else "  — not installed"))
            n.setProperty("hint", True)
            row.addWidget(n, 1)
            card_layout.addLayout(row)

        install_row = QHBoxLayout()
        install_btn = QPushButton("Show pip command")
        install_btn.setProperty("primary", True)
        install_btn.clicked.connect(self._show_pip_command)
        install_row.addWidget(install_btn)
        install_row.addStretch(1)
        card_layout.addLayout(install_row)

        self._pip_label = QLabel("")
        self._pip_label.setProperty("mono", True)
        self._pip_label.setWordWrap(True)
        self._pip_label.setStyleSheet(
            f"background-color: {theme.BG_INK}; border: 1px solid {theme.BORDER};"
            f"padding: 10px; border-radius: 6px; color: {theme.ACCENT};"
        )
        self._pip_label.setVisible(False)
        card_layout.addWidget(self._pip_label)

        outer.addWidget(card)

        # Caption editor placeholder.
        editor_card = QFrame()
        editor_card.setObjectName("card")
        editor_card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        editor_layout = QVBoxLayout(editor_card)
        editor_layout.setContentsMargins(20, 18, 20, 18)
        editor_layout.setSpacing(6)
        editor_kicker = QLabel("CAPTION EDITOR")
        editor_kicker.setProperty("kicker", True)
        editor_layout.addWidget(editor_kicker)
        editor_title = QLabel("Coming once captions.py is extracted")
        editor_title.setProperty("title", True)
        editor_layout.addWidget(editor_title)
        editor_body = QLabel(
            "The full editable-caption dialog from the legacy build (speaker colors, "
            "burn-in toggle, ASS/SRT export) lives behind a Flask blueprint today. "
            "Pulling it out into native/services/ is the next chunk of work; until "
            "then, run faster-whisper directly via the install above."
        )
        editor_body.setWordWrap(True)
        editor_body.setFixedWidth(720)
        editor_body.setProperty("hint", True)
        editor_layout.addWidget(editor_body)
        outer.addWidget(editor_card)

        outer.addStretch(1)

    def _show_pip_command(self) -> None:
        self._pip_label.setText(
            "pip install faster-whisper torch pyannote.audio"
        )
        self._pip_label.setVisible(True)
