"""whisper.cpp transcription backend — zero-dependency captions for streamers.

The audience can't ``pip install`` and won't have Python, so faster-whisper (+
multi-GB torch) is a non-starter for distribution. Instead we treat the speech
engine exactly like ffmpeg/yt-dlp: a self-contained native binary + a model
file, **downloaded once with a single click** into the runtime dir. No Python,
no terminal, ~75 MB total.

    binary: whisper.cpp ``whisper-bin-x64.zip`` (CPU build, no CUDA)
    model:  ``ggml-base.en-q5_1.bin`` (~60 MB, English, CPU-fast)

Transcription: ffmpeg extracts 16 kHz mono WAV → whisper-cli writes JSON-full →
we merge its tokens into word-level timing for the existing ASS karaoke path.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

from native.services.paths import RUNTIME_DIR

WHISPER_VERSION = "v1.8.5"
WHISPER_DIR = RUNTIME_DIR / "whisper"
MODEL_NAME = "ggml-base.en-q5_1.bin"
MODEL_PATH = WHISPER_DIR / MODEL_NAME

BINARY_URL = (
    f"https://github.com/ggml-org/whisper.cpp/releases/download/{WHISPER_VERSION}/whisper-bin-x64.zip"
)
MODEL_URL = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{MODEL_NAME}"

DOWNLOAD_SIZE_LABEL = "~75 MB"


def _find_binary() -> Optional[Path]:
    if not WHISPER_DIR.exists():
        return None
    for name in ("whisper-cli.exe", "main.exe", "whisper-cli", "main"):
        hits = list(WHISPER_DIR.rglob(name))
        if hits:
            return hits[0]
    return None


def is_ready() -> bool:
    return _find_binary() is not None and MODEL_PATH.exists()


# ── download / provision ───────────────────────────────────────────

def _download(url: str, dest: Path, on_pct: Optional[Callable[[float], None]]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        read = 0
        tmp = dest.with_suffix(dest.suffix + ".part")
        with open(tmp, "wb") as f:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                read += len(chunk)
                if total and on_pct:
                    on_pct(read / total)
        tmp.replace(dest)


def download(
    on_progress: Optional[Callable[[str], None]] = None,
    on_pct: Optional[Callable[[float], None]] = None,
) -> None:
    """Fetch + unpack the whisper.cpp binary and model into ``WHISPER_DIR``."""
    WHISPER_DIR.mkdir(parents=True, exist_ok=True)

    if _find_binary() is None:
        if on_progress:
            on_progress("Downloading speech engine…")
        with tempfile.TemporaryDirectory() as td:
            zip_path = Path(td) / "whisper.zip"
            _download(BINARY_URL, zip_path, on_pct)
            if on_progress:
                on_progress("Unpacking speech engine…")
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(WHISPER_DIR)
        if _find_binary() is None:
            raise RuntimeError("whisper.cpp binary not found after unpacking.")

    if not MODEL_PATH.exists():
        if on_progress:
            on_progress(f"Downloading speech model ({MODEL_NAME})…")
        if on_pct:
            on_pct(0.0)
        _download(MODEL_URL, MODEL_PATH, on_pct)

    if on_progress:
        on_progress("Speech engine ready.")


# ── transcription ──────────────────────────────────────────────────

_SPECIAL_PREFIXES = ("[", "<")


def transcribe(
    media_path: Path,
    ffmpeg: str,
    on_progress: Optional[Callable[[str], None]] = None,
) -> list[dict]:
    """Transcribe ``media_path`` to word-level dicts via whisper.cpp."""
    binary = _find_binary()
    if binary is None or not MODEL_PATH.exists():
        raise RuntimeError("Captioning engine isn't set up yet.")

    with tempfile.TemporaryDirectory(prefix="clipline-stt-") as td:
        tmp = Path(td)
        wav = tmp / "audio.wav"
        if on_progress:
            on_progress("Extracting audio…")
        extract = subprocess.run(
            [ffmpeg, "-y", "-i", str(media_path), "-ar", "16000", "-ac", "1",
             "-c:a", "pcm_s16le", str(wav)],
            capture_output=True, text=True,
        )
        if extract.returncode != 0 or not wav.exists():
            raise RuntimeError((extract.stderr or "audio extraction failed").strip()[:300])

        if on_progress:
            on_progress("Transcribing…")
        out_prefix = tmp / "out"
        run = subprocess.run(
            [str(binary), "-m", str(MODEL_PATH), "-f", str(wav),
             "-l", "en", "-ojf", "-of", str(out_prefix), "-np"],
            capture_output=True, text=True, cwd=str(binary.parent),
        )
        json_path = out_prefix.with_suffix(".json")
        if run.returncode != 0 or not json_path.exists():
            raise RuntimeError((run.stderr or "transcription failed").strip()[:300])

        data = json.loads(json_path.read_text(encoding="utf-8"))
    return _words_from_json(data)


def _words_from_json(data: dict) -> list[dict]:
    words: list[dict] = []
    for segment in data.get("transcription", []):
        tokens = segment.get("tokens") or []
        if tokens:
            words.extend(_merge_tokens(tokens))
        else:
            # No token detail — fall back to one "word" per segment.
            off = segment.get("offsets", {})
            text = (segment.get("text") or "").strip()
            if text:
                words.append({
                    "text": text,
                    "start": round(off.get("from", 0) / 1000, 3),
                    "end": round(off.get("to", 0) / 1000, 3),
                    "speaker": "SPEAKER_0", "enabled": True,
                })
    return words


def _merge_tokens(tokens: list[dict]) -> list[dict]:
    """Merge whisper.cpp BPE tokens into whole words using leading-space cues."""
    out: list[dict] = []
    current: Optional[dict] = None
    for tok in tokens:
        raw = tok.get("text", "")
        stripped = raw.strip()
        if not stripped or stripped.startswith(_SPECIAL_PREFIXES):
            continue  # skip [_BEG_], timestamp tokens, etc.
        off = tok.get("offsets", {})
        start = round(off.get("from", 0) / 1000, 3)
        end = round(off.get("to", 0) / 1000, 3)
        if raw.startswith(" ") or current is None:
            if current is not None:
                out.append(current)
            current = {"text": stripped, "start": start, "end": end,
                       "speaker": "SPEAKER_0", "enabled": True}
        else:
            current["text"] += stripped
            current["end"] = end
    if current is not None:
        out.append(current)
    return out
