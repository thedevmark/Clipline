"""Timeline strip with a waveform background and draggable trim handles.

Shows the active clip's [in, out] range against the full source duration. The
two handles drag to trim; dragging the body slides the whole window. Emits
``range_changed`` live during a drag (so the inspector + label track it) and
``range_committed`` on release (so we only re-render/persist once).

The waveform is an optional QPixmap painted behind the track — when absent
(no audio, or render still running) the track is just a flat bar.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QSizePolicy, QWidget

from native.ui import theme

_HANDLE_W = 9
_HIT = 11  # px tolerance for grabbing a handle


class TimelineStrip(QWidget):
    range_changed = Signal(int, int)     # start_ms, end_ms (live, during drag)
    range_committed = Signal(int, int)   # start_ms, end_ms (on release)

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self._duration_ms = 0
        self._start_ms = 0
        self._end_ms = 0
        self._wave: Optional[QPixmap] = None
        self._drag: Optional[str] = None  # 'start' | 'end' | 'move'
        self._drag_anchor_ms = 0          # for 'move': clip pos at grab time
        self._enabled = False

    # ── public API ──────────────────────────────────────────────────

    def set_source(self, duration_ms: int) -> None:
        self._duration_ms = max(0, duration_ms)
        self._enabled = self._duration_ms > 0
        self.update()

    def set_waveform(self, pixmap: Optional[QPixmap]) -> None:
        self._wave = pixmap
        self.update()

    def set_clip(self, start_ms: int, end_ms: int) -> None:
        self._start_ms = max(0, start_ms)
        self._end_ms = max(self._start_ms, end_ms)
        self.update()

    def clear(self) -> None:
        self._duration_ms = 0
        self._start_ms = 0
        self._end_ms = 0
        self._wave = None
        self._enabled = False
        self.update()

    # ── geometry helpers ────────────────────────────────────────────

    def _track_rect(self) -> QRectF:
        m = _HANDLE_W + 2
        return QRectF(m, 10, max(1.0, self.width() - 2 * m), self.height() - 30)

    def _ms_to_x(self, ms: int) -> float:
        r = self._track_rect()
        if self._duration_ms <= 0:
            return r.left()
        return r.left() + (ms / self._duration_ms) * r.width()

    def _x_to_ms(self, x: float) -> int:
        r = self._track_rect()
        if r.width() <= 0:
            return 0
        frac = (x - r.left()) / r.width()
        return int(max(0, min(1.0, frac)) * self._duration_ms)

    # ── painting ─────────────────────────────────────────────────────

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = self._track_rect()

        # Track background — a panel shade so the full strip reads against the
        # stage, with a thin border to bound it.
        p.setPen(QColor(theme.BORDER))
        p.setBrush(QColor(theme.BG_PANEL))
        p.drawRoundedRect(r, 6, 6)

        if self._wave is not None and not self._wave.isNull():
            p.drawPixmap(r.toRect(), self._wave)

        if not self._enabled:
            p.setPen(QColor(theme.INK_DIM))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Load a source to trim")
            p.end()
            return

        x0 = self._ms_to_x(self._start_ms)
        x1 = self._ms_to_x(self._end_ms)

        # Dim the regions outside the selection.
        p.setPen(Qt.PenStyle.NoPen)
        dim = QColor(theme.BG_INK)
        dim.setAlpha(180)
        p.setBrush(dim)
        p.drawRect(QRectF(r.left(), r.top(), x0 - r.left(), r.height()))
        p.drawRect(QRectF(x1, r.top(), r.right() - x1, r.height()))

        # Selection: faint accent fill + outline.
        sel = QColor(theme.ACCENT)
        sel.setAlpha(40)
        p.setBrush(sel)
        p.setPen(QColor(theme.ACCENT))
        p.drawRect(QRectF(x0, r.top(), max(1.0, x1 - x0), r.height()))

        # Handles.
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.ACCENT))
        for hx in (x0, x1):
            p.drawRoundedRect(QRectF(hx - _HANDLE_W / 2, r.top() - 4, _HANDLE_W, r.height() + 8), 3, 3)

        # Time labels under the handles.
        p.setPen(QColor(theme.INK_MUTED))
        p.drawText(QRectF(r.left(), r.bottom() + 2, r.width(), 16),
                   Qt.AlignmentFlag.AlignLeft, _fmt(self._start_ms))
        p.drawText(QRectF(r.left(), r.bottom() + 2, r.width(), 16),
                   Qt.AlignmentFlag.AlignRight, _fmt(self._end_ms))
        p.end()

    # ── mouse ────────────────────────────────────────────────────────

    def mousePressEvent(self, event) -> None:
        if not self._enabled:
            return
        x = event.position().x()
        x0 = self._ms_to_x(self._start_ms)
        x1 = self._ms_to_x(self._end_ms)
        if abs(x - x0) <= _HIT:
            self._drag = "start"
        elif abs(x - x1) <= _HIT:
            self._drag = "end"
        elif x0 < x < x1:
            self._drag = "move"
            self._drag_anchor_ms = self._x_to_ms(x) - self._start_ms
        else:
            self._drag = None

    def mouseMoveEvent(self, event) -> None:
        if self._drag is None or not self._enabled:
            # Hover cursor feedback near handles.
            if self._enabled:
                x = event.position().x()
                near = abs(x - self._ms_to_x(self._start_ms)) <= _HIT or \
                    abs(x - self._ms_to_x(self._end_ms)) <= _HIT
                self.setCursor(Qt.CursorShape.SizeHorCursor if near else Qt.CursorShape.ArrowCursor)
            return
        ms = self._x_to_ms(event.position().x())
        if self._drag == "start":
            self._start_ms = max(0, min(ms, self._end_ms))
        elif self._drag == "end":
            self._end_ms = min(self._duration_ms, max(ms, self._start_ms))
        elif self._drag == "move":
            span = self._end_ms - self._start_ms
            new_start = max(0, min(ms - self._drag_anchor_ms, self._duration_ms - span))
            self._start_ms = new_start
            self._end_ms = new_start + span
        self.range_changed.emit(self._start_ms, self._end_ms)
        self.update()

    def mouseReleaseEvent(self, _event) -> None:
        if self._drag is not None:
            self._drag = None
            self.range_committed.emit(self._start_ms, self._end_ms)


def _fmt(ms: int) -> str:
    s = max(0, ms) // 1000
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
