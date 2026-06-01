"""First-run guided tour — a spotlight overlay over key controls.

Fires once, when the first clip lands in the inbox (mid-session, so QSettings
gating is allowed here — unlike the launch screens, ALERT §7). Each step dims
the whole stage except a rounded "hole" over a target widget and floats a card
explaining it. The card is drawn on top of the dim *after* the hole is cut, so
it never gets clipped by the spotlight.

Overlay technique: a child widget with ``WA_NoSystemBackground`` so the backing
store under it keeps the real UI; we darken everything except the hole with an
even-odd ``QPainterPath``, leaving the spotlighted widget showing through.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional

from PySide6.QtCore import QPoint, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import QPushButton, QWidget

from native.ui import theme


@dataclass
class TourStep:
    target: QWidget
    title: str
    body: str


_PAD = 8           # spotlight padding around the target
_CARD_W = 320


class GuidedTour(QWidget):
    def __init__(self, parent: QWidget, on_done: Optional[Callable[[], None]] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self._on_done = on_done
        self._steps: List[TourStep] = []
        self._index = 0

        self._card = QWidget(self)
        self._card.setObjectName("card")
        self._card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._card.setFixedWidth(_CARD_W)

        from PySide6.QtWidgets import QLabel, QVBoxLayout, QHBoxLayout

        lay = QVBoxLayout(self._card)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(8)
        self._title = QLabel()
        self._title.setStyleSheet(f"color: {theme.INK_BRIGHT}; font-size: 16px; font-weight: 600;")
        self._title.setWordWrap(True)
        lay.addWidget(self._title)
        self._body = QLabel()
        self._body.setProperty("hint", True)
        self._body.setWordWrap(True)
        lay.addWidget(self._body)
        btn_row = QHBoxLayout()
        self._step_label = QLabel()
        self._step_label.setProperty("hint", True)
        btn_row.addWidget(self._step_label)
        btn_row.addStretch(1)
        skip = QPushButton("Skip")
        skip.clicked.connect(self.finish)
        btn_row.addWidget(skip)
        self._next_btn = QPushButton("Next")
        self._next_btn.setProperty("primary", True)
        self._next_btn.clicked.connect(self._advance)
        btn_row.addWidget(self._next_btn)
        lay.addLayout(btn_row)

        self.hide()

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self, steps: List[TourStep]) -> None:
        steps = [s for s in steps if s.target is not None]
        if not steps:
            self.finish()
            return
        self._steps = steps
        self._index = 0
        self.setGeometry(self.parent().rect())
        self.show()
        self.raise_()
        self._layout_step()

    def _advance(self) -> None:
        self._index += 1
        if self._index >= len(self._steps):
            self.finish()
        else:
            self._layout_step()

    def finish(self) -> None:
        self.hide()
        if self._on_done is not None:
            self._on_done()
        self.deleteLater()

    # ── geometry ─────────────────────────────────────────────────────

    def _spotlight_rect(self) -> QRect:
        step = self._steps[self._index]
        target = step.target
        top_left = target.mapTo(self.parentWidget(), QPoint(0, 0))
        rect = QRect(top_left, target.size())
        return rect.adjusted(-_PAD, -_PAD, _PAD, _PAD).intersected(self.rect())

    def _layout_step(self) -> None:
        step = self._steps[self._index]
        self._title.setText(step.title)
        self._body.setText(step.body)
        self._step_label.setText(f"{self._index + 1} / {len(self._steps)}")
        self._next_btn.setText("Done" if self._index == len(self._steps) - 1 else "Next")

        spot = self._spotlight_rect()
        self._card.adjustSize()
        ch = self._card.height()
        # Prefer placing the card below the spotlight; flip above if no room.
        x = max(12, min(spot.left(), self.width() - _CARD_W - 12))
        if spot.bottom() + 12 + ch <= self.height():
            y = spot.bottom() + 12
        else:
            y = max(12, spot.top() - 12 - ch)
        self._card.move(x, y)
        self._card.raise_()
        self.update()

    # ── painting ─────────────────────────────────────────────────────

    def paintEvent(self, _event) -> None:
        if not self._steps:
            return
        spot = QRectF(self._spotlight_rect())
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Dim everything except the spotlight (even-odd path = full rect minus hole).
        path = QPainterPath()
        path.addRect(QRectF(self.rect()))
        hole = QPainterPath()
        hole.addRoundedRect(spot, 10, 10)
        path = path.subtracted(hole)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(5, 9, 15, 200))
        p.drawPath(path)

        # Accent ring around the spotlight.
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QColor(theme.ACCENT))
        p.drawRoundedRect(spot, 10, 10)
        p.end()

    # Click anywhere on the dim (not the card) advances.
    def mousePressEvent(self, event) -> None:
        if not self._card.geometry().contains(event.position().toPoint()):
            self._advance()
