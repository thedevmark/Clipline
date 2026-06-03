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
from dataclasses import dataclass

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
