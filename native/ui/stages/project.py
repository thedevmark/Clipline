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

from native.services.tools import TOOLS, _is_explicit_tool_path
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

        # Dependency checklist — shown every launch, no QSettings flag (ALERT §7).
        deps_card = QFrame()
        deps_card.setObjectName("card")
        deps_card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        deps_layout = QVBoxLayout(deps_card)
        deps_layout.setContentsMargins(28, 22, 28, 22)
        deps_layout.setSpacing(8)

        dkicker = QLabel("RUNTIME STATUS")
        dkicker.setProperty("kicker", True)
        deps_layout.addWidget(dkicker)

        # Required tools (block stuff from running if missing).
        for name, path in (
            ("ffmpeg", TOOLS.ffmpeg),
            ("ffprobe", TOOLS.ffprobe),
            ("yt-dlp", TOOLS.ytdlp),
        ):
            present = _is_explicit_tool_path(path, name)
            deps_layout.addLayout(self._dep_row(name, present, path if present else "not found — install via choco / winget / scoop"))

        # Optional: captioning ML deps.
        try:
            import importlib.util
            has_faster_whisper = importlib.util.find_spec("faster_whisper") is not None
        except Exception:
            has_faster_whisper = False
        deps_layout.addLayout(self._dep_row(
            "faster-whisper",
            has_faster_whisper,
            "optional — install with pip for the caption pass",
            optional=True,
        ))

        deps_hint = QLabel(
            "Required tools must be on PATH or in the Clipline runtime folder. "
            "If anything is red, install it and relaunch — Clipline picks it up automatically."
        )
        deps_hint.setWordWrap(True)
        deps_hint.setFixedWidth(720)
        deps_hint.setProperty("hint", True)
        deps_layout.addSpacing(6)
        deps_layout.addWidget(deps_hint)

        outer.addWidget(deps_card)
        outer.addStretch(2)

    def _dep_row(self, name: str, present: bool, detail: str, optional: bool = False) -> QHBoxLayout:
        row = QHBoxLayout()
        marker = QLabel("✓" if present else ("○" if optional else "✗"))
        if present:
            color = theme.ACCENT
        elif optional:
            color = theme.INK_DIM
        else:
            color = theme.ERROR
        marker.setStyleSheet(f"color: {color}; font-size: 18px; font-weight: 700; min-width: 22px;")
        row.addWidget(marker)
        label = QLabel(name)
        label.setStyleSheet(f"color: {theme.INK_BRIGHT}; font-weight: 600; min-width: 140px;")
        row.addWidget(label)
        detail_label = QLabel(detail)
        detail_label.setProperty("hint", True)
        row.addWidget(detail_label, 1)
        return row
