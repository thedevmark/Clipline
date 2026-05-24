"""Project stage — the entry point for a fresh session.

Pre-VOD welcome screen. ALERT §7 trap: keep this *ungated* — don't add a
"don't show again" QSettings flag. Users should see it every launch; the
real first-run pinch point is the dependency check, not the welcome.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from native.ui import theme


class ProjectStage(QWidget):
    def __init__(
        self,
        on_open_local: Callable[[], None],
        on_start_session: Callable[[], None],
        icon_path: Path | None = None,
    ) -> None:
        super().__init__()
        self.setObjectName("stage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)  # ALERT §4

        outer = QVBoxLayout(self)
        outer.setContentsMargins(80, 60, 80, 60)
        outer.setSpacing(28)
        outer.addStretch(1)

        # Header row: icon + welcome text
        header = QHBoxLayout()
        header.setSpacing(28)
        if icon_path is not None and Path(icon_path).exists():
            icon_label = QLabel()
            icon_label.setPixmap(QPixmap(str(icon_path)).scaled(
                88, 88, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
            header.addWidget(icon_label, 0, Qt.AlignmentFlag.AlignTop)

        copy = QVBoxLayout()
        copy.setSpacing(6)
        kicker = QLabel("STREAMER WORKFLOW")
        kicker.setProperty("kicker", True)
        copy.addWidget(kicker)
        title = QLabel("Turn a stream session into a tray of shorts")
        title.setStyleSheet(f"color: {theme.INK_BRIGHT}; font-size: 28px; font-weight: 600;")
        copy.addWidget(title)
        sub = QLabel(
            "Load a Twitch VOD or local recording, mark the moments you want, and "
            "ship them as shorts or one longform cut. Captions and exports run natively — "
            "no browser engine, no web shell."
        )
        sub.setWordWrap(True)
        sub.setFixedWidth(720)  # ALERT §4: constrain wrap labels.
        sub.setProperty("hint", True)
        copy.addWidget(sub)
        header.addLayout(copy, 1)
        outer.addLayout(header)

        outer.addSpacing(20)

        # Actions card
        card = QFrame()
        card.setObjectName("card")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(28, 24, 28, 24)
        card_layout.setSpacing(16)

        actions_kicker = QLabel("GET STARTED")
        actions_kicker.setProperty("kicker", True)
        card_layout.addWidget(actions_kicker)

        actions_row = QHBoxLayout()
        actions_row.setSpacing(12)

        open_btn = QPushButton("Open Local Video…")
        open_btn.setProperty("primary", True)
        open_btn.setMinimumHeight(40)
        open_btn.clicked.connect(lambda: (on_open_local(), on_start_session()))
        actions_row.addWidget(open_btn)

        ingest_btn = QPushButton("Go to Ingest")
        ingest_btn.setMinimumHeight(40)
        ingest_btn.clicked.connect(on_start_session)
        actions_row.addWidget(ingest_btn)

        actions_row.addStretch(1)
        card_layout.addLayout(actions_row)

        notes = QLabel(
            "Tip — drag a video file directly onto the Ingest stage to load it. "
            "Use the Ingest controls to mark in/out points and the Output stage to render."
        )
        notes.setWordWrap(True)
        notes.setProperty("hint", True)
        card_layout.addWidget(notes)

        outer.addWidget(card)
        outer.addStretch(2)
