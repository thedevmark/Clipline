"""Main window for the native Clipline shell.

Holds the menubar, the stage stack, the project state, the JobRunner that
drives export work, and the status bar. Stage widgets are dumb — they emit
``request_*`` signals; the window routes those to the worker and the model.

ALERT §7 reminder: do not gate any of the launch-time screens behind a
QSettings flag. The Project stage greets every launch.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QStackedWidget,
    QStatusBar,
)

from native.services.export_presets import (
    build_clip_export_args,
    format_preset_by_key,
    style_preset_by_key,
)
from native.services.paths import DOWNLOADS_DIR
from native.services.settings import get_output_dir
from native.services.tools import TOOLS
from native.ui import theme
from native.ui.project_state import Clip, ProjectState
from native.ui.stages.inbox import InboxStage
from native.ui.stages.ingest import IngestStage
from native.ui.stages.output import OutputStage
from native.ui.stages.project import ProjectStage
from native.ui.stages.shorts import ShortsStage
from native.workers import JobRunner, ffmpeg_export, longform_export, ytdlp_download


STAGE_PROJECT, STAGE_INGEST, STAGE_INBOX, STAGE_SHORTS, STAGE_OUTPUT = range(5)
STAGE_NAMES = ("Project", "Ingest", "Inbox", "Shorts", "Output")


class MainWindow(QMainWindow):
    def __init__(
        self,
        runner: JobRunner,
        ffmpeg: str,
        ffprobe: str,
        output_dir: Path,
        icon_path: Optional[Path] = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("Clipline")
        self.setMinimumSize(1280, 800)
        self.resize(1440, 900)
        if icon_path is not None and Path(icon_path).exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self._runner = runner
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe
        self._output_dir = output_dir
        self._icon_path = icon_path
        self._state = ProjectState()

        self.setStyleSheet(theme.GLOBAL_QSS)

        # ---- Stages -----------------------------------------------------
        self._stack = QStackedWidget(self)
        self._project_stage = ProjectStage(
            on_open_local=self._open_local_via_dialog,
            on_start_session=lambda: self._set_stage(STAGE_INGEST),
            icon_path=icon_path,
        )
        self._ingest_stage = IngestStage(self._state, runner)
        self._ingest_stage.request_export.connect(self._export_marked_range)
        self._ingest_stage.request_download.connect(self._download_url)
        self._inbox_stage = InboxStage(self._state, runner, ffmpeg)
        self._inbox_stage.request_export_clip.connect(self._export_clip)
        self._inbox_stage.request_remove_clip.connect(self._state.remove_clip)

        self._shorts_stage = ShortsStage()
        self._output_stage = OutputStage(self._state)
        self._output_stage.request_render_all.connect(self._render_all_clips)
        self._output_stage.request_build_longform.connect(self._build_longform)

        self._stack.addWidget(self._project_stage)
        self._stack.addWidget(self._ingest_stage)
        self._stack.addWidget(self._inbox_stage)
        self._stack.addWidget(self._shorts_stage)
        self._stack.addWidget(self._output_stage)

        self.setCentralWidget(self._stack)

        # ---- Status bar ------------------------------------------------
        self.setStatusBar(QStatusBar(self))
        self._stage_label = QLabel("Project")
        self._source_label = QLabel("No source loaded")
        self._source_label.setProperty("hint", True)
        self._jobs_label = QLabel("")
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setVisible(False)
        self._progress.setMaximumWidth(200)
        self.statusBar().addWidget(self._stage_label)
        self.statusBar().addWidget(self._source_label, 1)
        self.statusBar().addPermanentWidget(self._jobs_label)
        self.statusBar().addPermanentWidget(self._progress)

        self._state.source_changed.connect(self._on_source_changed)

        self._build_menubar()
        self._set_stage(STAGE_PROJECT)

    # ────────────────────────────────────────────────────────────────────
    # Menubar
    # ────────────────────────────────────────────────────────────────────

    def _build_menubar(self) -> None:
        bar = self.menuBar()

        file_menu = bar.addMenu("File")
        new_action = QAction("New Project", self)
        new_action.setShortcut(QKeySequence.StandardKey.New)
        new_action.triggered.connect(self._new_project)
        file_menu.addAction(new_action)

        open_action = QAction("Open Local Video…", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self._open_local_via_dialog)
        file_menu.addAction(open_action)

        file_menu.addSeparator()
        reveal = QAction("Reveal Output Folder", self)
        reveal.triggered.connect(self._reveal_output)
        file_menu.addAction(reveal)

        file_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(QApplication.quit)
        file_menu.addAction(quit_action)

        stage_menu = bar.addMenu("Stage")
        for index, name in enumerate(STAGE_NAMES):
            action = QAction(name, self)
            action.setShortcut(QKeySequence(f"Ctrl+{index + 1}"))
            action.triggered.connect(lambda _checked=False, idx=index: self._set_stage(idx))
            stage_menu.addAction(action)

        help_menu = bar.addMenu("Help")
        about = QAction("About Clipline", self)
        about.triggered.connect(self._show_about)
        help_menu.addAction(about)

    # ────────────────────────────────────────────────────────────────────
    # Stage routing
    # ────────────────────────────────────────────────────────────────────

    def _set_stage(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        self._stage_label.setText(STAGE_NAMES[index])

    # ────────────────────────────────────────────────────────────────────
    # File / source actions
    # ────────────────────────────────────────────────────────────────────

    def _new_project(self) -> None:
        self._state.set_source(None)
        for i in range(len(self._state.clips), 0, -1):
            self._state.remove_clip(i - 1)
        self._set_stage(STAGE_PROJECT)

    def _open_local_via_dialog(self) -> None:
        self._ingest_stage.open_local_file_dialog()
        if self._state.source is not None:
            self._set_stage(STAGE_INGEST)

    def _reveal_output(self) -> None:
        path = self._output_dir
        path.mkdir(parents=True, exist_ok=True)
        try:
            from PySide6.QtGui import QDesktopServices
            from PySide6.QtCore import QUrl

            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        except Exception as exc:
            QMessageBox.warning(self, "Reveal failed", f"Could not open: {exc}")

    def _on_source_changed(self, path) -> None:
        if path is None:
            self._source_label.setText("No source loaded")
        else:
            self._source_label.setText(f"Source: {path}")

    # ────────────────────────────────────────────────────────────────────
    # Worker dispatch
    # ────────────────────────────────────────────────────────────────────

    def _download_url(self, url: str) -> None:
        """Fetch a URL (Twitch VOD/clip, etc.) via yt-dlp, then load it."""
        if not url:
            return
        self._set_stage(STAGE_INGEST)
        self._progress.setVisible(True)
        self._progress.setValue(0)
        self._jobs_label.setText("Downloading…")
        self._runner.run(
            ytdlp_download,
            TOOLS.ytdlp,
            TOOLS.ffmpeg_dir,
            url,
            DOWNLOADS_DIR,
            on_progress=lambda msg: self._jobs_label.setText(msg),
            on_progress_pct=lambda pct: self._progress.setValue(int(pct * 100)),
            on_finished=self._on_download_finished,
            on_error=self._on_render_error,
        )

    def _on_download_finished(self, result: object) -> None:
        self._progress.setValue(100)
        self._progress.setVisible(False)
        path = Path(str(result))
        self._jobs_label.setText(f"Loaded {path.name}")
        self._state.set_source(path)
        self._set_stage(STAGE_INGEST)

    def _export_marked_range(self, start_ms: int, end_ms: int) -> None:
        if self._state.source is None:
            return
        clip = Clip(title="Marked range", start_ms=start_ms, end_ms=end_ms)
        self._export_clip(clip)

    def _clip_export_args(self, clip: Clip) -> list[str]:
        style = style_preset_by_key(self._state.style_preset_key)
        fmt = format_preset_by_key(self._state.format_preset_key)
        return build_clip_export_args(clip.start_ms, clip.end_ms, style, fmt)

    def _export_clip(self, clip: Clip) -> None:
        source = self._state.source
        if source is None:
            return
        out_name = f"{source.stem}-{clip.title.replace(' ', '_')}-{clip.start_ms}.mp4"
        output = self._output_dir / out_name
        self._progress.setVisible(True)
        self._progress.setValue(0)
        self._jobs_label.setText(f"Rendering {clip.title}…")
        self._runner.run(
            ffmpeg_export,
            self._ffmpeg,
            self._ffprobe,
            source,
            output,
            self._clip_export_args(clip),
            on_progress=lambda msg: self._jobs_label.setText(msg),
            on_progress_pct=lambda pct: self._progress.setValue(int(pct * 100)),
            on_finished=self._on_render_finished,
            on_error=self._on_render_error,
        )

    def _render_all_clips(self) -> None:
        source = self._state.source
        clips = self._state.clips
        if source is None or not clips:
            return
        for clip in clips:
            self._export_clip(clip)

    def _build_longform(self) -> None:
        source = self._state.source
        clips = self._state.clips
        if source is None or not clips:
            return
        fmt = format_preset_by_key(self._state.format_preset_key)
        out_name = f"{source.stem}-longform-{fmt.key}.mp4"
        output = self._output_dir / out_name
        clip_args_list = [
            (clip.title, self._clip_export_args(clip)) for clip in clips
        ]
        self._progress.setVisible(True)
        self._progress.setValue(0)
        self._jobs_label.setText(f"Building longform from {len(clips)} clips…")
        self._runner.run(
            longform_export,
            self._ffmpeg,
            self._ffprobe,
            source,
            clip_args_list,
            output,
            on_progress=lambda msg: self._jobs_label.setText(msg),
            on_progress_pct=lambda pct: self._progress.setValue(int(pct * 100)),
            on_finished=self._on_render_finished,
            on_error=self._on_render_error,
        )

    def _on_render_finished(self, result: object) -> None:
        self._progress.setValue(100)
        self._jobs_label.setText(f"Wrote {Path(str(result)).name}")
        # Hide the progress bar after a brief delay so the 100% reads.
        self._progress.setVisible(False)

    def _on_render_error(self, message: str) -> None:
        self._progress.setVisible(False)
        self._jobs_label.setText("Render failed")
        QMessageBox.warning(self, "Render failed", message)

    # ────────────────────────────────────────────────────────────────────
    # Help
    # ────────────────────────────────────────────────────────────────────

    def _show_about(self) -> None:
        QMessageBox.information(
            self,
            "About Clipline",
            "Clipline — native PySide6 video editor for streamers.\n\n"
            "Phase 2 native build. See native/MIGRATION_PLAN.md for the phased plan.",
        )
