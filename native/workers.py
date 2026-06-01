"""Background job infrastructure for the native shell.

The native app does not have a Flask server to push work into; every long
operation (ffmpeg, yt-dlp, faster-whisper) runs in a QThread via the
``JobRunner`` here. The runner holds strong references to active jobs so
they aren't garbage-collected mid-run — ``ALERT_REBUILD_LESSONS.md`` §6.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable, List, Optional, Sequence

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


def longform_export(
    job: WorkerJob,
    ffmpeg: str,
    ffprobe: str,
    source: Path,
    clip_args_list: Sequence[tuple[str, list[str]]],  # [(label, extra_args), ...]
    output_path: Path,
) -> Path:
    """Render each clip, then concat into one MP4.

    Each entry in ``clip_args_list`` is ``(label, extra_args)`` — the same
    shape ``ffmpeg_export`` accepts for a single clip. We:

    1. Render each clip to a temp directory with consistent encoding.
    2. Write a concat-demuxer file list pointing at the temp clips.
    3. Run ``ffmpeg -f concat -safe 0 -i list.txt -c copy output``.

    Stream-copy concat is fast and avoids generation loss; it works because
    every temp clip was re-encoded with the same codec/pix_fmt/sample rate.
    """
    if not clip_args_list:
        raise RuntimeError("No clips to render.")

    total = len(clip_args_list)
    with tempfile.TemporaryDirectory(prefix="clipline-longform-") as tmpdir:
        tmp = Path(tmpdir)
        rendered: list[Path] = []
        for index, (label, extra_args) in enumerate(clip_args_list, start=1):
            job.progress.emit(f"[{index}/{total}] rendering {label}")
            piece = tmp / f"piece-{index:03d}.mp4"
            ffmpeg_export(job, ffmpeg, ffprobe, source, piece, extra_args)
            rendered.append(piece)
            job.progress_pct.emit(index / (total + 1))

        list_path = tmp / "concat.txt"
        with open(list_path, "w", encoding="utf-8") as f:
            for piece in rendered:
                # ffmpeg's concat demuxer wants forward slashes and single-quoted paths.
                escaped = str(piece).replace("\\", "/").replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")

        job.progress.emit(f"stitching {total} clip{'s' if total != 1 else ''} -> {output_path.name}")
        args = [
            ffmpeg, "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_path),
            "-c", "copy",
            str(output_path),
        ]
        proc = subprocess.run(args, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg concat exit {proc.returncode}: {(proc.stderr or '').strip()[:400]}"
            )
    job.progress_pct.emit(1.0)
    job.progress.emit("Done")
    return output_path


def generate_waveform(
    job: WorkerJob,
    ffmpeg: str,
    source: Path,
    out_png: Path,
    width: int = 1600,
    height: int = 120,
) -> Path:
    """Render a single waveform image for the whole source via showwavespic.

    Drawn once per source and used as the timeline background (ALERT §5).
    Best-effort: a source with no audio track will fail here, and the caller
    treats a missing waveform as "draw a flat track".
    """
    out_png.parent.mkdir(parents=True, exist_ok=True)
    args = [
        ffmpeg, "-y",
        "-i", str(source),
        "-filter_complex",
        f"showwavespic=s={width}x{height}:colors=#3FA9BD|#7BD5E5",
        "-frames:v", "1",
        str(out_png),
    ]
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0 or not out_png.exists():
        raise RuntimeError((proc.stderr or "waveform render failed").strip()[:300])
    return out_png


_YTDLP_PCT = re.compile(r"\[download\]\s+([\d.]+)%")


def ytdlp_download(
    job: WorkerJob,
    ytdlp: str,
    ffmpeg_dir: Optional[str],
    url: str,
    dest_dir: Path,
) -> Path:
    """Download a single URL (Twitch VOD/clip, etc.) to ``dest_dir``.

    Downloads into a fresh unique subdir so the resulting media file is
    unambiguous to locate (no --print parsing to disentangle from progress).
    Streams ``--newline`` so the ``[download] NN%`` lines drive the progress
    bar. Returns the path to the downloaded media file.
    """
    from ytdlp import summarize_ytdlp_error  # local import: keep import graph flat

    dest_dir.mkdir(parents=True, exist_ok=True)
    args = [
        ytdlp,
        "--no-playlist",
        "--newline",
        "--no-part",
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "-o", str(dest_dir / "%(title).80s-%(id)s.%(ext)s"),
    ]
    if ffmpeg_dir:
        args += ["--ffmpeg-location", ffmpeg_dir]
    args.append(url)

    job.progress.emit("Starting download…")
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert proc.stdout is not None
    stderr_tail: list[str] = []
    try:
        for line in proc.stdout:
            line = line.rstrip()
            match = _YTDLP_PCT.search(line)
            if match:
                job.progress_pct.emit(min(max(float(match.group(1)) / 100.0, 0.0), 1.0))
            if line.startswith("[download]") or line.startswith("[Merger]"):
                job.progress.emit(line[:120])
    finally:
        ret = proc.wait()
        if proc.stderr is not None:
            stderr_tail = proc.stderr.read().splitlines()[-20:]

    if ret != 0:
        raise RuntimeError(summarize_ytdlp_error("\n".join(stderr_tail)))

    media = [
        p for p in sorted(dest_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
    ]
    if not media:
        raise RuntimeError("Download finished but no media file was produced.")
    job.progress_pct.emit(1.0)
    job.progress.emit(f"Downloaded {media[0].name}")
    return media[0]


def download_captioner(job: WorkerJob) -> bool:
    """Provision the whisper.cpp engine (binary + model). One-click, no pip."""
    from native.services import whisper_cpp

    whisper_cpp.download(on_progress=job.progress.emit, on_pct=job.progress_pct.emit)
    return True


def caption_pass(
    job: WorkerJob,
    ffmpeg: str,
    media_path: Path,
    out_dir: Path,
) -> dict:
    """Transcribe ``media_path`` (whisper.cpp) and write .ass + .srt next to it.

    Returns ``{"words", "ass", "srt"}``.
    """
    from native.services import captions

    words = captions.transcribe_words(
        Path(media_path), ffmpeg, on_progress=job.progress.emit,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(media_path).stem
    ass_path = out_dir / f"{stem}.ass"
    srt_path = out_dir / f"{stem}.srt"
    speakers = {"SPEAKER_0": {"color": captions.DEFAULT_SPEAKER_COLORS[0]}}
    ass_path.write_text(captions.generate_ass_subtitles(words, speakers), encoding="utf-8")
    srt_path.write_text(captions.generate_srt(words), encoding="utf-8")
    job.progress.emit(f"{len(words)} words transcribed")
    return {"words": words, "ass": str(ass_path), "srt": str(srt_path)}


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
