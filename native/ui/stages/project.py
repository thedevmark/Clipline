"""Project stage — the entry point for a fresh session.

Pre-VOD welcome screen. ALERT §7 trap: keep this *ungated* — don't add a
"don't show again" QSettings flag. Users should see it every launch; the
real first-run pinch point is the dependency check, not the welcome.
"""
from __future__ import annotations

import platform
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from native.services import diarize, whisper_cpp
from native.services.tools import TOOLS, _is_explicit_tool_path
from native.ui import theme

# winget package IDs for the required CLI tools. ffmpeg + ffprobe ship from a
# single package, so installing either covers both. yt-dlp is its own package.
WINGET_IDS = {
    "ffmpeg": "Gyan.FFmpeg",
    "ffprobe": "Gyan.FFmpeg",
    "yt-dlp": "yt-dlp.yt-dlp",
}
CREATE_NEW_CONSOLE = 0x00000010  # so the user sees winget's progress + prompts


def _render_app_icon(icon_path: Path, size: int) -> Optional[QPixmap]:
    """Crisp app icon at ``size`` logical px, HiDPI-aware.

    Prefers the vector ``app-icon.svg`` (rendered at device-pixel resolution so
    it stays sharp on any display); falls back to the .ico. The old code scaled
    a small .ico frame up to 88px, which is what made it blurry.
    """
    icon_path = Path(icon_path)
    screen = QApplication.primaryScreen()
    ratio = screen.devicePixelRatio() if screen is not None else 1.0
    px = max(1, round(size * ratio))

    svg = icon_path.parent / "img" / "app-icon.svg"
    if svg.exists():
        try:
            from PySide6.QtSvg import QSvgRenderer

            renderer = QSvgRenderer(str(svg))
            if renderer.isValid():
                img = QImage(px, px, QImage.Format.Format_ARGB32_Premultiplied)
                img.fill(Qt.GlobalColor.transparent)
                painter = QPainter(img)
                renderer.render(painter)
                painter.end()
                pm = QPixmap.fromImage(img)
                pm.setDevicePixelRatio(ratio)
                return pm
        except Exception:
            pass  # fall through to the .ico

    pm = QPixmap(str(icon_path))
    if pm.isNull():
        return None
    pm = pm.scaled(
        px, px, Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    pm.setDevicePixelRatio(ratio)
    return pm


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
        self._icon_path = icon_path

        # The content can be taller than the 800px-min window (header + two
        # cards + dep list). Host it in a scroll area so it never clips top &
        # bottom — the old layout centered everything with stretches, so tall
        # content got cut off at both ends.
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root.addWidget(scroll)

        content = QWidget()
        content.setObjectName("stage")
        content.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        scroll.setWidget(content)

        outer = QVBoxLayout(content)
        outer.setContentsMargins(80, 48, 80, 48)
        outer.setSpacing(24)

        # Header row: icon + welcome text
        header = QHBoxLayout()
        header.setSpacing(24)
        if icon_path is not None and Path(icon_path).exists():
            pm = _render_app_icon(icon_path, 72)
            if pm is not None:
                icon_label = QLabel()
                icon_label.setPixmap(pm)
                icon_label.setFixedSize(72, 72)
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

        outer.addSpacing(8)

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
        # on_open_local pops the file picker and advances to Ingest only when a
        # file is actually chosen — cancelling leaves you on the welcome screen.
        # "Go to Ingest" (below) is the distinct path to the empty Ingest stage.
        open_btn.clicked.connect(on_open_local)
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

        dheader = QHBoxLayout()
        dkicker = QLabel("RUNTIME STATUS")
        dkicker.setProperty("kicker", True)
        dheader.addWidget(dkicker)
        dheader.addStretch(1)
        recheck = QPushButton("Re-check")
        recheck.setMinimumHeight(28)
        recheck.clicked.connect(self._on_recheck)
        self._recheck_btn = recheck
        dheader.addWidget(recheck)
        deps_layout.addLayout(dheader)

        # Rebuildable rows live in their own container so "Re-check" can
        # repopulate them after the user installs a missing tool.
        self._deps_body = QVBoxLayout()
        self._deps_body.setSpacing(8)
        deps_layout.addLayout(self._deps_body)

        # Re-check feedback: without this, clicking "Re-check" silently rebuilds
        # identical rows and reads as a dead button when nothing's missing.
        self._deps_status = QLabel("")
        self._deps_status.setWordWrap(True)
        deps_layout.addSpacing(4)
        deps_layout.addWidget(self._deps_status)

        deps_hint = QLabel(
            "Required tools must be on PATH or in the Clipline runtime folder. "
            "Click Install to fetch a missing one via winget, then Re-check — "
            "no relaunch needed."
        )
        deps_hint.setWordWrap(True)
        deps_hint.setFixedWidth(720)
        deps_hint.setProperty("hint", True)
        deps_layout.addSpacing(6)
        deps_layout.addWidget(deps_hint)

        outer.addWidget(deps_card)
        outer.addStretch(1)

        self._refresh_deps()

    # ────────────────────────────────────────────────────────────────────
    # Dependency rows
    # ────────────────────────────────────────────────────────────────────

    def _on_recheck(self) -> None:
        """Show a visible 'Re-checking…' state, then run the scan one tick later.

        The scan itself is fast and synchronous, so without deferring it the
        in-progress frame never paints — the button would look like it did
        nothing. Disabling + relabelling, then scanning via a single-shot timer,
        guarantees the user sees the re-check happen.
        """
        self._recheck_btn.setEnabled(False)
        self._recheck_btn.setText("Re-checking…")
        self._deps_status.setText("Re-checking…")
        self._deps_status.setStyleSheet(f"color: {theme.INK_DIM};")
        QTimer.singleShot(250, self._do_recheck)

    def _do_recheck(self) -> None:
        self._refresh_deps()
        self._recheck_btn.setText("Re-check")
        self._recheck_btn.setEnabled(True)

    def _refresh_deps(self) -> None:
        """Re-scan tool discovery and rebuild the status rows in place."""
        TOOLS.refresh()
        while self._deps_body.count():
            item = self._deps_body.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        missing: list[str] = []
        for name, path in (
            ("ffmpeg", TOOLS.ffmpeg),
            ("ffprobe", TOOLS.ffprobe),
            ("yt-dlp", TOOLS.ytdlp),
        ):
            present = _is_explicit_tool_path(path, name)
            if not present:
                missing.append(name)
            detail = path if present else "not found — install below"
            self._deps_body.addWidget(
                self._dep_row(name, present, detail, install_id=WINGET_IDS.get(name))
            )

        # Optional local ML engines — NOT pip/Python. Each is a 1-click,
        # download-on-demand native engine set up in the Shorts stage (whisper.cpp
        # for captions, sherpa-onnx for speaker separation). Show real readiness.
        for name, ready in (
            ("captions (whisper.cpp)", whisper_cpp.is_ready()),
            ("speaker separation (sherpa-onnx)", diarize.is_ready()),
        ):
            detail = (
                "ready — runs locally"
                if ready
                else "optional — 1-click setup in the Shorts stage (no pip, no terminal)"
            )
            self._deps_body.addWidget(self._dep_row(name, ready, detail, optional=True))

        # Confirm the re-check actually ran. The timestamp changes every click,
        # so the button never reads as inert even when nothing's missing.
        stamp = time.strftime("%H:%M:%S")
        if missing:
            self._deps_status.setText(
                f"Checked {stamp} — missing: {', '.join(missing)}. "
                "Use Install above, then Re-check."
            )
            self._deps_status.setStyleSheet(f"color: {theme.ERROR};")
        else:
            self._deps_status.setText(
                f"Checked {stamp} — all required tools found. Nothing to install."
            )
            self._deps_status.setStyleSheet(f"color: {theme.ACCENT};")

    def _dep_row(
        self,
        name: str,
        present: bool,
        detail: str,
        install_id: str | None = None,
        optional: bool = False,
    ) -> QWidget:
        row_w = QWidget()
        row = QHBoxLayout(row_w)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)

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
        detail_label.setToolTip(detail)
        # Long resolved paths must not force the row wider than the viewport
        # (horizontal scroll is off) — let the label clip and lean on the tooltip.
        detail_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        row.addWidget(detail_label, 1)

        if not present and not optional and install_id:
            install_btn = QPushButton("Install")
            install_btn.setMinimumHeight(28)
            install_btn.clicked.connect(
                lambda _checked=False, wid=install_id, nm=name: self._install_tool(wid, nm)
            )
            row.addWidget(install_btn)

        return row_w

    def _install_tool(self, winget_id: str, name: str) -> None:
        """Launch winget in a visible console to fetch a missing tool."""
        if platform.system() != "Windows":
            QMessageBox.information(
                self, "Install",
                f"Install {name} manually, then click Re-check. (winget id: {winget_id})",
            )
            return
        try:
            subprocess.Popen(
                ["winget", "install", "--id", winget_id, "-e", "--source", "winget"],
                creationflags=CREATE_NEW_CONSOLE,
            )
        except FileNotFoundError:
            QMessageBox.warning(
                self, "winget not found",
                "winget isn't available on this system. Install "
                f"{name} manually ({winget_id}) and click Re-check.",
            )
            return
        except Exception as exc:
            QMessageBox.warning(self, "Install failed", str(exc))
            return
        QMessageBox.information(
            self, "Installing",
            f"A console window is installing {name} via winget.\n\n"
            "When it finishes, click “Re-check” to pick it up — no relaunch needed.",
        )
