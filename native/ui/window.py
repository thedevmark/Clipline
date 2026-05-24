"""Native Clipline main window — Phase 0 skeleton.

Five-stage nav spine (Project / Ingest / Inbox / Shorts / Output) backed by a
``QStackedWidget`` of placeholder widgets. Each stage gets fleshed out in a
subsequent phase per ``native/MIGRATION_PLAN.md``. This file intentionally
holds no styling — Phase 1 adds QSS and the polished menubar layout.

The Phase 0 demo button on the Project stage runs ``ffmpeg_export`` on a
QThread so we can confirm the full pipeline (UI → JobRunner → ffmpeg →
output file) works end-to-end without the web stack.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from native.workers import JobRunner, ffmpeg_export


STAGES = ("Project", "Ingest", "Inbox", "Shorts", "Output")


class _ProjectStage(QWidget):
    """Phase 0 stage: bare smoke test for the worker pipeline.

    The other four stages get their real widgets in later phases. Project is
    first because the funnel naturally starts here (open / new project) and
    it's the right home for the size/integrity self-test button.
    """

    def __init__(
        self,
        runner: JobRunner,
        ffmpeg: str,
        ffprobe: str,
        output_dir: Path,
    ) -> None:
        super().__init__()
        self._runner = runner
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe
        self._output_dir = output_dir

        layout = QVBoxLayout(self)
        layout.setContentsMargins(48, 48, 48, 48)
        layout.setSpacing(16)

        title = QLabel("Clipline — Phase 0 skeleton")
        title.setStyleSheet("font-size: 22px; font-weight: 600;")
        layout.addWidget(title)

        body = QLabel(
            "Native shell is live. No Flask, no QtWebEngine. Use the button below "
            "to push a sample clip through the worker pipeline end-to-end."
        )
        body.setWordWrap(True)
        body.setFixedWidth(640)  # ALERT §4: constrain wrapping labels or text clips.
        layout.addWidget(body)

        actions = QHBoxLayout()
        self._pick_btn = QPushButton("Run sample export…")
        self._pick_btn.clicked.connect(self._on_run_export_clicked)
        actions.addWidget(self._pick_btn)
        actions.addStretch(1)
        layout.addLayout(actions)

        self._status = QLabel("Idle.")
        layout.addWidget(self._status)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        layout.addWidget(self._progress)

        layout.addStretch(1)

    def _on_run_export_clicked(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Pick a clip to re-encode", "", "Video files (*.mp4 *.mov *.mkv *.webm)"
        )
        if not path_str:
            return
        source = Path(path_str)
        output = self._output_dir / f"phase0-{source.stem}.mp4"
        self._pick_btn.setEnabled(False)
        self._status.setText(f"Exporting → {output.name}")
        self._progress.setValue(0)
        self._runner.run(
            ffmpeg_export,
            self._ffmpeg,
            self._ffprobe,
            source,
            output,
            on_progress=self._status.setText,
            on_progress_pct=lambda pct: self._progress.setValue(int(pct * 100)),
            on_finished=self._on_finished,
            on_error=self._on_error,
        )

    def _on_finished(self, result: object) -> None:
        self._pick_btn.setEnabled(True)
        self._status.setText(f"Wrote {result}")

    def _on_error(self, message: str) -> None:
        self._pick_btn.setEnabled(True)
        self._status.setText(f"Failed: {message}")


class _PlaceholderStage(QWidget):
    def __init__(self, name: str) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        label = QLabel(f"{name} — fills in during Phase {STAGES.index(name) + 1}.")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)


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
        if icon_path is not None and icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self._stack = QStackedWidget(self)
        self._stack.addWidget(_ProjectStage(runner, ffmpeg, ffprobe, output_dir))
        for name in STAGES[1:]:
            self._stack.addWidget(_PlaceholderStage(name))
        self.setCentralWidget(self._stack)

        self.setStatusBar(QStatusBar(self))
        self.statusBar().showMessage("Ready")

        self._build_menubar()
        self._set_stage(0)

    def _build_menubar(self) -> None:
        menubar = self.menuBar()
        for index, name in enumerate(STAGES):
            action = QAction(name, self)
            action.setShortcut(QKeySequence(f"Ctrl+{index + 1}"))
            action.triggered.connect(lambda _checked=False, idx=index: self._set_stage(idx))
            menubar.addAction(action)

    def _set_stage(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        self.statusBar().showMessage(f"Stage: {STAGES[index]}")
