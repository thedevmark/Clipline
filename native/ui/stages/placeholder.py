"""Shared placeholder stage for the workspaces not yet rebuilt.

Used by Shorts / Output / etc. until those phases land. Keeps the look
consistent (kicker + title + body, ALERT §4 attributes set correctly)
instead of raw default-Qt panels.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class PlaceholderStage(QWidget):
    def __init__(self, name: str, body: str) -> None:
        super().__init__()
        self.setObjectName("stage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(64, 64, 64, 64)
        layout.setSpacing(14)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        kicker = QLabel(name.upper())
        kicker.setProperty("kicker", True)
        layout.addWidget(kicker)

        title = QLabel("Coming next phase")
        title.setProperty("title", True)
        layout.addWidget(title)

        b = QLabel(body)
        b.setWordWrap(True)
        b.setFixedWidth(720)
        b.setProperty("hint", True)
        layout.addWidget(b)
        layout.addStretch(1)
