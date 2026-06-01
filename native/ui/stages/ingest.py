"""Ingest stage — load a source video and preview it natively.

This is the architectural payoff of the rewrite. The current Flask + WebEngine
build wraps an HTML5 ``<video>`` element; here we drive ``QMediaPlayer`` +
``QVideoWidget`` directly through PySide6's bundled FFmpeg backend. Same
H.264 / AAC media plays without a Chromium runtime.

Ships in Phase 2: local-file source. URL ingest via yt-dlp comes in a follow-up
once the worker primitive is in place; the UI affordances are stubbed below
so wiring it later is a 30-line patch.

Hotkeys (per the streamer keymap in the legacy reel.js):
- Space      play/pause
- I          mark in at playhead
- O          mark out at playhead
- [ / ]      nudge playhead -1s / +1s
- Shift+[ / Shift+] nudge -0.1s / +0.1s
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QMimeData, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QKeySequence, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from native.ui import theme
from native.ui.project_state import Clip, ProjectState
from native.ui.twitch_panel import TwitchPanel
from native.workers import JobRunner


def _format_ms(ms: int) -> str:
    if ms < 0:
        ms = 0
    total_seconds = ms // 1000
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    sub = (ms % 1000) // 100
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}.{sub}"
    return f"{minutes}:{seconds:02d}.{sub}"


class _DropZone(QFrame):
    """Drag-drop target that emits ``dropped`` with the chosen path."""

    dropped = Signal(Path)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("dropzone")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAcceptDrops(True)
        self.setMinimumHeight(180)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(6)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title = QLabel("Drag a video file here")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(f"color: {theme.INK_BRIGHT}; font-size: 16px; font-weight: 600;")
        hint = QLabel("…or use the Browse button. MP4 / MOV / MKV / WebM.")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setProperty("hint", True)
        layout.addWidget(title)
        layout.addWidget(hint)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setProperty("hover", "true")
            self.style().unpolish(self)
            self.style().polish(self)

    def dragLeaveEvent(self, event) -> None:
        self.setProperty("hover", "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def dropEvent(self, event: QDropEvent) -> None:
        for url in event.mimeData().urls():
            if url.isLocalFile():
                self.dropped.emit(Path(url.toLocalFile()))
                break
        self.setProperty("hover", "false")
        self.style().unpolish(self)
        self.style().polish(self)
        event.acceptProposedAction()


class IngestStage(QWidget):
    """Load a source, preview it, mark in/out, hand off to the worker."""

    request_export = Signal(int, int)   # start_ms, end_ms — wired by the window
    request_download = Signal(str)      # a URL to fetch via yt-dlp — wired by the window

    def __init__(self, state: ProjectState, runner: JobRunner) -> None:
        super().__init__()
        self.setObjectName("stage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._state = state
        self._runner = runner
        self._duration_ms = 0
        self._in_ms = 0
        self._out_ms = 0

        # ---- Player ----------------------------------------------------
        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._audio.setMuted(True)  # ALERT §5: don't auto-blast audio on preview load
        self._player.setAudioOutput(self._audio)
        self._video = QVideoWidget(self)
        self._video.setMinimumSize(QSize(640, 360))
        self._video.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._player.setVideoOutput(self._video)
        self._player.setLoops(QMediaPlayer.Loops.Infinite)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.positionChanged.connect(self._on_position_changed)
        self._player.mediaStatusChanged.connect(self._on_status_changed)

        # ---- Layout ----------------------------------------------------
        outer = QHBoxLayout(self)
        outer.setContentsMargins(32, 24, 32, 24)
        outer.setSpacing(24)

        outer.addLayout(self._build_left_column(), 1)
        outer.addLayout(self._build_right_column(), 0)

        # ---- Hotkeys ---------------------------------------------------
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, activated=self._toggle_play)
        QShortcut(QKeySequence(Qt.Key.Key_I), self, activated=self._mark_in)
        QShortcut(QKeySequence(Qt.Key.Key_O), self, activated=self._mark_out)
        QShortcut(QKeySequence("["), self, activated=lambda: self._nudge(-1000))
        QShortcut(QKeySequence("]"), self, activated=lambda: self._nudge(1000))
        QShortcut(QKeySequence("Shift+["), self, activated=lambda: self._nudge(-100))
        QShortcut(QKeySequence("Shift+]"), self, activated=lambda: self._nudge(100))

        # ---- State wiring ----------------------------------------------
        self._state.source_changed.connect(self._on_source_changed)

    # ────────────────────────────────────────────────────────────────────
    # Public API used by the window
    # ────────────────────────────────────────────────────────────────────

    def open_local_file_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open video source",
            "",
            "Video files (*.mp4 *.mov *.mkv *.webm *.m4v *.avi)",
        )
        if path:
            self._load_path(Path(path))

    def current_range_ms(self) -> tuple[int, int]:
        return self._in_ms, self._out_ms

    # ────────────────────────────────────────────────────────────────────
    # UI builders
    # ────────────────────────────────────────────────────────────────────

    def _build_left_column(self) -> QVBoxLayout:
        column = QVBoxLayout()
        column.setSpacing(16)

        # Source intake card (URL field stub + Browse + drop zone).
        card = QFrame()
        card.setObjectName("card")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 18, 20, 18)
        card_layout.setSpacing(10)

        kicker = QLabel("SOURCE")
        kicker.setProperty("kicker", True)
        card_layout.addWidget(kicker)
        title = QLabel("Pick the video you want to clip")
        title.setProperty("title", True)
        card_layout.addWidget(title)

        url_row = QHBoxLayout()
        url_row.setSpacing(8)
        self._url_input = QLineEdit()
        self._url_input.setPlaceholderText("Paste a Twitch / YouTube / TikTok URL…")
        self._url_input.returnPressed.connect(self._submit_url)
        url_row.addWidget(self._url_input, 1)
        self._url_btn = QPushButton("Load URL")
        self._url_btn.clicked.connect(self._submit_url)
        url_row.addWidget(self._url_btn)
        card_layout.addLayout(url_row)

        local_row = QHBoxLayout()
        local_row.setSpacing(8)
        browse_btn = QPushButton("Browse Local File…")
        browse_btn.setProperty("primary", True)
        browse_btn.clicked.connect(self.open_local_file_dialog)
        local_row.addWidget(browse_btn)
        local_row.addStretch(1)
        card_layout.addLayout(local_row)

        self._drop = _DropZone()
        self._drop.dropped.connect(self._load_path)
        card_layout.addWidget(self._drop)

        column.addWidget(card)

        # Twitch connect + VOD/clip browser — the auth-first ingest base.
        self._twitch = TwitchPanel(self._runner, on_ingest=self.request_download.emit)
        column.addWidget(self._twitch)

        # Preview card.
        preview_card = QFrame()
        preview_card.setObjectName("card")
        preview_card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        preview_layout = QVBoxLayout(preview_card)
        preview_layout.setContentsMargins(20, 18, 20, 18)
        preview_layout.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(8)
        ptitle = QLabel("PREVIEW")
        ptitle.setProperty("kicker", True)
        head.addWidget(ptitle)
        head.addStretch(1)
        self._meta_label = QLabel("Load a source to start.")
        self._meta_label.setProperty("hint", True)
        head.addWidget(self._meta_label)
        preview_layout.addLayout(head)

        preview_layout.addWidget(self._video, 1)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self._play_btn = QPushButton("Play")
        self._play_btn.setEnabled(False)
        self._play_btn.clicked.connect(self._toggle_play)
        controls.addWidget(self._play_btn)
        self._mute_btn = QPushButton("Unmute")
        self._mute_btn.setEnabled(False)
        self._mute_btn.clicked.connect(self._toggle_mute)
        controls.addWidget(self._mute_btn)
        self._time_label = QLabel("0:00.0 / 0:00.0")
        self._time_label.setProperty("mono", True)
        controls.addWidget(self._time_label)
        controls.addStretch(1)
        preview_layout.addLayout(controls)

        self._scrub = QSlider(Qt.Orientation.Horizontal)
        self._scrub.setEnabled(False)
        self._scrub.setRange(0, 0)
        self._scrub.sliderMoved.connect(self._player.setPosition)
        preview_layout.addWidget(self._scrub)

        column.addWidget(preview_card, 1)

        return column

    def _build_right_column(self) -> QVBoxLayout:
        column = QVBoxLayout()
        column.setSpacing(16)
        column.setAlignment(Qt.AlignmentFlag.AlignTop)

        card = QFrame()
        card.setObjectName("card")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        card.setMinimumWidth(340)
        card.setMaximumWidth(380)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 18, 20, 18)
        card_layout.setSpacing(10)

        kicker = QLabel("MARK A CLIP")
        kicker.setProperty("kicker", True)
        card_layout.addWidget(kicker)

        hint = QLabel(
            "Use Space to play/pause, I to mark in, O to mark out. "
            "Then click Add Clip — the Output stage renders the marked range."
        )
        hint.setWordWrap(True)
        hint.setFixedWidth(320)
        hint.setProperty("hint", True)
        card_layout.addWidget(hint)

        row = QHBoxLayout()
        row.setSpacing(6)
        self._in_btn = QPushButton("Mark In")
        self._in_btn.clicked.connect(self._mark_in)
        self._in_btn.setEnabled(False)
        self._out_btn = QPushButton("Mark Out")
        self._out_btn.clicked.connect(self._mark_out)
        self._out_btn.setEnabled(False)
        row.addWidget(self._in_btn)
        row.addWidget(self._out_btn)
        card_layout.addLayout(row)

        self._range_label = QLabel("Range: —")
        self._range_label.setProperty("mono", True)
        card_layout.addWidget(self._range_label)

        self._add_btn = QPushButton("Add Clip")
        self._add_btn.setProperty("primary", True)
        self._add_btn.setEnabled(False)
        self._add_btn.clicked.connect(self._on_add_clip)
        card_layout.addWidget(self._add_btn)

        export_btn = QPushButton("Export Marked Range…")
        export_btn.clicked.connect(self._on_export_marked)
        export_btn.setEnabled(False)
        self._export_btn = export_btn
        card_layout.addWidget(export_btn)

        card_layout.addStretch(1)
        column.addWidget(card)

        return column

    # ────────────────────────────────────────────────────────────────────
    # Player wiring
    # ────────────────────────────────────────────────────────────────────

    def _submit_url(self) -> None:
        url = self._url_input.text().strip()
        if url:
            self.request_download.emit(url)

    def _load_path(self, path: Path) -> None:
        self._state.set_source(path)

    def _on_source_changed(self, path: Optional[Path]) -> None:
        if path is None:
            self._player.stop()
            self._player.setSource(QUrl())
            self._meta_label.setText("Load a source to start.")
            return
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        self._meta_label.setText(path.name)
        self._in_ms = 0
        self._out_ms = 0
        self._update_range_label()
        self._play_btn.setEnabled(True)
        self._mute_btn.setEnabled(True)
        self._in_btn.setEnabled(True)
        self._out_btn.setEnabled(True)
        self._scrub.setEnabled(True)
        self._add_btn.setEnabled(True)
        self._export_btn.setEnabled(True)

    def _on_status_changed(self, status: QMediaPlayer.MediaStatus) -> None:
        if status == QMediaPlayer.MediaStatus.LoadedMedia:
            # ALERT §5: render frame 1 without autoplaying loudly.
            self._player.play()
            self._player.pause()

    def _on_duration_changed(self, ms: int) -> None:
        self._duration_ms = ms
        self._scrub.setRange(0, ms)
        self._out_ms = ms
        self._state.set_metadata(duration_ms=ms)
        self._update_range_label()
        self._update_time_label(self._player.position())

    def _on_position_changed(self, ms: int) -> None:
        if not self._scrub.isSliderDown():
            self._scrub.setValue(ms)
        self._update_time_label(ms)

    def _update_time_label(self, ms: int) -> None:
        self._time_label.setText(f"{_format_ms(ms)} / {_format_ms(self._duration_ms)}")

    def _toggle_play(self) -> None:
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
            self._play_btn.setText("Play")
        else:
            self._player.play()
            self._play_btn.setText("Pause")

    def _toggle_mute(self) -> None:
        muted = not self._audio.isMuted()
        self._audio.setMuted(muted)
        self._mute_btn.setText("Unmute" if muted else "Mute")

    def _nudge(self, delta_ms: int) -> None:
        if self._duration_ms <= 0:
            return
        pos = max(0, min(self._duration_ms, self._player.position() + delta_ms))
        self._player.setPosition(pos)

    # ────────────────────────────────────────────────────────────────────
    # Mark in / out
    # ────────────────────────────────────────────────────────────────────

    def _mark_in(self) -> None:
        if self._duration_ms <= 0:
            return
        self._in_ms = self._player.position()
        if self._out_ms < self._in_ms:
            self._out_ms = self._in_ms
        self._update_range_label()

    def _mark_out(self) -> None:
        if self._duration_ms <= 0:
            return
        self._out_ms = self._player.position()
        if self._in_ms > self._out_ms:
            self._in_ms = self._out_ms
        self._update_range_label()

    def _update_range_label(self) -> None:
        dur = max(0, self._out_ms - self._in_ms)
        self._range_label.setText(
            f"Range: {_format_ms(self._in_ms)} → {_format_ms(self._out_ms)}    ({_format_ms(dur)})"
        )

    def _on_add_clip(self) -> None:
        if self._state.source is None or self._duration_ms <= 0:
            return
        idx = len(self._state.clips) + 1
        title = f"Clip {idx}"
        self._state.add_clip(Clip(title=title, start_ms=self._in_ms, end_ms=self._out_ms))

    def _on_export_marked(self) -> None:
        if self._out_ms <= self._in_ms:
            return
        self.request_export.emit(self._in_ms, self._out_ms)
