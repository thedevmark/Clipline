"""Background job infrastructure for the native shell.

The native app does not have a Flask server to push work into; every long
operation (ffmpeg, yt-dlp, faster-whisper) runs in a QThread via the
``JobRunner`` here. The runner holds strong references to active jobs so
they aren't garbage-collected mid-run — ``ALERT_REBUILD_LESSONS.md`` §6.
"""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Callable, List, Optional

from PySide6.QtCore import QObject, QThread, Signal


class WorkerJob(QObject):
    """A unit of work runnable on a QThread.

    The callable receives the job instance as its first argument so it can
    emit ``progress`` / ``progress_pct`` along the way and return a result
    payload at the end.
    """

    progress = Signal(str)
    progress_pct = Signal(float)
    finished = Signal(object)
    errored = Signal(str)

    def __init__(self, fn: Callable, *args, **kwargs) -> None:
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def run(self) -> None:
        try:
            result = self._fn(self, *self._args, **self._kwargs)
        except Exception as exc:  # noqa: BLE001 — every failure must reach the UI
            self.errored.emit(f"{type(exc).__name__}: {exc}")
            return
        self.finished.emit(result)


class JobRunner:
    """Owns running QThread/WorkerJob pairs for the life of the app."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: List[tuple[QThread, WorkerJob]] = []

    def run(
        self,
        fn: Callable,
        *args,
        on_progress: Optional[Callable[[str], None]] = None,
        on_progress_pct: Optional[Callable[[float], None]] = None,
        on_finished: Optional[Callable[[object], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        **kwargs,
    ) -> WorkerJob:
        job = WorkerJob(fn, *args, **kwargs)
        thread = QThread()
        job.moveToThread(thread)
        thread.started.connect(job.run)

        if on_progress is not None:
            job.progress.connect(on_progress)
        if on_progress_pct is not None:
            job.progress_pct.connect(on_progress_pct)
        if on_finished is not None:
            job.finished.connect(on_finished)
        if on_error is not None:
            job.errored.connect(on_error)

        job.finished.connect(thread.quit)
        job.errored.connect(thread.quit)
        thread.finished.connect(lambda: self._drop(thread, job))

        with self._lock:
            self._jobs.append((thread, job))
        thread.start()
        return job

    def _drop(self, thread: QThread, job: WorkerJob) -> None:
        with self._lock:
            self._jobs = [pair for pair in self._jobs if pair[0] is not thread]
        thread.deleteLater()
        job.deleteLater()

    def wait_all(self, timeout_ms: int = 60_000) -> None:
        """Block until every running job finishes. Used by ``--selftest``."""
        with self._lock:
            running = list(self._jobs)
        for thread, _ in running:
            thread.wait(timeout_ms)


def ffmpeg_export(
    job: WorkerJob,
    ffmpeg: str,
    ffprobe: str,
    input_path: Path,
    output_path: Path,
    extra_args: Optional[List[str]] = None,
) -> Path:
    """Phase 0 export primitive: re-encode an input clip via ffmpeg.

    Streams ``-progress pipe:1`` so progress updates flow continuously on the
    worker thread rather than waiting for completion. Default extra args run
    a loudness-normalized H.264/AAC re-encode — the same primitive operation
    ``reel.py`` orchestrates today, just driven directly from the worker
    instead of through Flask.
    """
    args = [
        ffmpeg,
        "-y",
        "-i", str(input_path),
        "-progress", "pipe:1",
        "-nostats",
        "-loglevel", "error",
    ]
    if extra_args:
        args.extend(extra_args)
    else:
        args.extend([
            "-c:v", "libx264",
            "-c:a", "aac",
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-pix_fmt", "yuv420p",
        ])
    args.append(str(output_path))

    job.progress.emit(f"ffmpeg -> {output_path.name}")
    duration_us = _probe_duration_us(ffprobe, input_path)

    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            key, _, value = line.strip().partition("=")
            if not key:
                continue
            if key == "out_time_us" and value.isdigit() and duration_us:
                pct = min(max(int(value) / duration_us, 0.0), 1.0)
                job.progress_pct.emit(pct)
            elif key == "progress" and value == "end":
                break
    finally:
        ret = proc.wait()
    if ret != 0:
        err = proc.stderr.read() if proc.stderr else ""
        raise RuntimeError(f"ffmpeg exit {ret}: {err.strip()[:400]}")
    job.progress_pct.emit(1.0)
    job.progress.emit("Done")
    return output_path


def _probe_duration_us(ffprobe: str, input_path: Path) -> Optional[int]:
    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(input_path),
            ],
            capture_output=True, text=True, timeout=10,
        )
        return int(float(result.stdout.strip()) * 1_000_000)
    except Exception:
        return None
