# native/services/diarize.py
"""Offline speaker diarization via the sherpa-onnx CLI.

Mirrors ``whisper_cpp.py``: a native binary + ONNX models are downloaded on
demand into the runtime dir and run fully offline (no Python for end users).
We feed it the same 16 kHz mono WAV whisper.cpp already extracts, parse its
``start -- end speaker_NN`` segments, and map them onto whisper word timings.

Engine: sherpa-onnx (Apache-2.0). Models (re-hosted un-gated on sherpa-onnx
GitHub releases, commercial-safe): pyannote-segmentation-3.0 (MIT) +
3dspeaker_speech_eres2net_sv_en_voxceleb_16k (Apache-2.0, English). The
non-commercial Reverb/Revai and CC-BY-NC NeMo Sortformer models are NOT used.
"""
from __future__ import annotations

import re
import subprocess
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from native.services.paths import RUNTIME_DIR

_SEG_RE = re.compile(r"^\s*([\d.]+)\s+--\s+([\d.]+)\s+speaker_(\d+)\s*$")


@dataclass(frozen=True)
class Segment:
    start: float  # seconds
    end: float    # seconds
    speaker: int


def parse_segments(stdout: str) -> list[Segment]:
    """Parse the diarization CLI stdout into ordered Segments."""
    segments: list[Segment] = []
    for line in stdout.splitlines():
        m = _SEG_RE.match(line)
        if m:
            segments.append(Segment(float(m.group(1)), float(m.group(2)), int(m.group(3))))
    return segments


def assign_speakers(words: list[dict], segments: list[Segment]) -> list[dict]:
    """Return copies of ``words`` with a ``speaker`` label per word.

    Each word takes the speaker of the segment containing its midpoint. Words
    in no segment inherit the previous word's speaker; the first such word (or
    any word when there are no segments) defaults to SPEAKER_0. Labels are
    ``"SPEAKER_<n>"`` to match the existing whisper_cpp word schema.
    """
    out: list[dict] = []
    last = "SPEAKER_0"
    for w in words:
        mid = (float(w["start"]) + float(w["end"])) / 2.0
        label = None
        for seg in segments:
            if seg.start <= mid < seg.end:
                label = f"SPEAKER_{seg.speaker}"
                break
        if label is None:
            label = last
        last = label
        nw = dict(w)
        nw["speaker"] = label
        out.append(nw)
    return out


# ── download / provision ───────────────────────────────────────────

# Pinned so an upstream re-tag can never feed a stale/incompatible binary.
SHERPA_VERSION = "v1.13.2"
_BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
BIN_URL = f"{_BASE}/{SHERPA_VERSION}/sherpa-onnx-{SHERPA_VERSION}-win-x64-shared-MD-Release-no-tts.tar.bz2"
SEG_URL = f"{_BASE}/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
EMB_URL = f"{_BASE}/speaker-recongition-models/3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx"

DIARIZE_DIR = RUNTIME_DIR / "diarize"
_EXE = DIARIZE_DIR / "sherpa-onnx-offline-speaker-diarization.exe"
_SEG_MODEL = DIARIZE_DIR / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
_EMB_MODEL = DIARIZE_DIR / "3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx"

DOWNLOAD_SIZE_LABEL = "~50 MB"


def is_ready() -> bool:
    """True if the diarization binary + both models are present."""
    return _EXE.exists() and _SEG_MODEL.exists() and _EMB_MODEL.exists()


def _download_file(url: str, dest: Path, on_progress=None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if on_progress:
        on_progress(f"Downloading {dest.name}…")
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def download(on_progress: Optional[Callable[[str], None]] = None) -> None:
    DIARIZE_DIR.mkdir(parents=True, exist_ok=True)
    bin_tar = DIARIZE_DIR / "bin.tar.bz2"
    seg_tar = DIARIZE_DIR / "seg.tar.bz2"
    _download_file(BIN_URL, bin_tar, on_progress)
    _download_file(SEG_URL, seg_tar, on_progress)
    _download_file(EMB_URL, _EMB_MODEL, on_progress)
    if on_progress:
        on_progress("Extracting…")
    for tar_path in (bin_tar, seg_tar):
        with tarfile.open(tar_path, "r:bz2") as tf:
            tf.extractall(DIARIZE_DIR)
        tar_path.unlink(missing_ok=True)
    # Flatten the binary set out of the nested release folder to DIARIZE_DIR.
    for name in (
        "sherpa-onnx-offline-speaker-diarization.exe",
        "onnxruntime.dll",
        "onnxruntime_providers_shared.dll",
    ):
        for found in DIARIZE_DIR.rglob(name):
            target = DIARIZE_DIR / name
            if found != target:
                found.replace(target)
            break


# ── diarization runner ─────────────────────────────────────────────

def diarize(
    wav_path: Path,
    num_speakers: Optional[int] = None,
    on_progress: Optional[Callable[[str], None]] = None,
    cancel: Optional[object] = None,
) -> list[Segment]:
    """Run the sherpa-onnx diarization CLI on a 16 kHz mono WAV.

    ``num_speakers`` pins the cluster count (more accurate when known); when
    None we fall back to threshold auto-estimation. ``cancel`` is anything with
    ``.is_set()`` — checked to terminate the subprocess early.
    """
    if not is_ready():
        raise RuntimeError("Diarization engine not installed.")
    clustering = (
        [f"--clustering.num-clusters={int(num_speakers)}"]
        if num_speakers and num_speakers > 0
        else ["--clustering.cluster-threshold=0.5"]
    )
    args = [
        str(_EXE),
        f"--segmentation.pyannote-model={_SEG_MODEL}",
        f"--embedding.model={_EMB_MODEL}",
        *clustering,
        str(wav_path),
    ]
    if on_progress:
        on_progress("Separating speakers…")
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    while proc.poll() is None:
        if cancel is not None and getattr(cancel, "is_set", lambda: False)():
            proc.terminate()
            raise RuntimeError("Speaker separation cancelled.")
        try:
            proc.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            continue
    if proc.returncode != 0:
        err = (proc.stderr.read() if proc.stderr else "").strip()[:400]
        raise RuntimeError(f"Diarization failed ({proc.returncode}): {err}")
    return parse_segments(proc.stdout.read() if proc.stdout else "")
