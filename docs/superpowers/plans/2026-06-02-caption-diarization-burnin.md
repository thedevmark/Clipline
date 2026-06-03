# Speaker-Separated, Positioned, Burned-In Captions — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Burn word-timed captions into every rendered clip and the longform build, with offline auto speaker separation (2–4 speakers) and per-speaker colour + draggable on-screen position.

**Architecture:** Diarize the whole source once with the sherpa-onnx CLI (downloaded on demand like whisper.cpp), map its speaker segments onto existing whisper word timestamps, persist the edited result on `ProjectState`, and at render derive a clip-local time-shifted `.ass` that ffmpeg burns via an `ass=` video filter. A three-region caption editor lets the user fix text, reassign speakers + colours, and drag per-speaker positions.

**Tech Stack:** Python 3.11, PySide6, ffmpeg/ffprobe (existing), sherpa-onnx offline diarization CLI (new, downloaded), ASS subtitles. Tests: stdlib `unittest` (no new runtime deps).

**Spec:** `docs/superpowers/specs/2026-06-02-caption-diarization-burnin-design.md`

---

## Conventions

- **Run a test:** `python -m unittest tests.<module> -v` (run from repo root `D:\Documents\GitHub\clipline`).
- **Run all tests:** `python -m unittest discover -s tests -v`
- Tests need PySide6 only for editor tests; set `QT_QPA_PLATFORM=offscreen` for those.
- Times: word dicts use **seconds** (floats) as produced by whisper.cpp; `Clip` uses **milliseconds** (ints). Conversions are explicit in code.
- Commit after every green step. Stage only the files named in the task.

## File Structure

- Create: `tests/__init__.py` — makes `tests` a package for `unittest`.
- Create: `native/services/diarize.py` — download + run sherpa-onnx diarization; `Segment`, `parse_segments`, `assign_speakers`.
- Create: `tests/test_diarize.py`, `tests/test_captions_burnin.py`, `tests/test_export_args.py`, `tests/test_project_state_captions.py`.
- Create: `THIRD_PARTY_NOTICES.txt` — attributions.
- Modify: `native/services/captions.py` — add `build_clip_ass(...)` and `escape_ass_path(...)`.
- Modify: `native/services/export_presets.py` — `build_clip_export_args(..., subtitle_ass=None)`.
- Modify: `native/ui/project_state.py` — caption state + `captions_changed` signal.
- Modify: `native/ui/caption_editor.py` — speaker panel + positioning preview; expose result.
- Modify: `native/ui/stages/shorts.py` — run diarization after transcription; persist editor result.
- Modify: `native/ui/window.py` — pass clip-local `.ass` into the three render paths.
- Modify: `Clipline.spec` — bundle `THIRD_PARTY_NOTICES.txt`.
- Modify: `CHANGELOG.md`, `native/__init__.py` (`__version__ = "0.2.3"`).

---

# Phase 1 — Diarization service + speaker mapping

### Task 1: Test package + Segment parsing

**Files:**
- Create: `tests/__init__.py`
- Create: `native/services/diarize.py`
- Test: `tests/test_diarize.py`

- [ ] **Step 1: Create the test package marker**

```python
# tests/__init__.py
```
(empty file)

- [ ] **Step 2: Write the failing test for stdout parsing**

```python
# tests/test_diarize.py
import unittest

from native.services.diarize import Segment, parse_segments


class TestParseSegments(unittest.TestCase):
    def test_parses_standard_lines(self):
        out = "0.000 -- 1.500 speaker_00\n1.500 -- 4.250 speaker_01\n"
        self.assertEqual(
            parse_segments(out),
            [Segment(0.0, 1.5, 0), Segment(1.5, 4.25, 1)],
        )

    def test_ignores_noise_and_blank_lines(self):
        out = "Duration : 10\n\n2.000 -- 3.000 speaker_02\nElapsed seconds: 1\n"
        self.assertEqual(parse_segments(out), [Segment(2.0, 3.0, 2)])

    def test_empty_output_is_empty_list(self):
        self.assertEqual(parse_segments(""), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m unittest tests.test_diarize -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'native.services.diarize'`

- [ ] **Step 4: Create the module with the parser**

```python
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
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m unittest tests.test_diarize -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add tests/__init__.py tests/test_diarize.py native/services/diarize.py
git commit -m "feat(diarize): sherpa-onnx segment parser + test scaffold"
```

---

### Task 2: Map speaker segments onto whisper words (midpoint rule)

**Files:**
- Modify: `native/services/diarize.py`
- Test: `tests/test_diarize.py`

- [ ] **Step 1: Add the failing test**

Append to `tests/test_diarize.py`:

```python
from native.services.diarize import assign_speakers


class TestAssignSpeakers(unittest.TestCase):
    def _w(self, text, start, end):
        return {"text": text, "start": start, "end": end}

    def test_assigns_by_word_midpoint(self):
        words = [self._w("a", 0.0, 0.4), self._w("b", 2.0, 2.4)]
        segs = [Segment(0.0, 1.0, 0), Segment(1.0, 3.0, 1)]
        out = assign_speakers(words, segs)
        self.assertEqual([w["speaker"] for w in out], ["SPEAKER_0", "SPEAKER_1"])

    def test_word_outside_all_segments_inherits_previous(self):
        words = [self._w("a", 0.0, 0.4), self._w("b", 9.0, 9.4)]
        segs = [Segment(0.0, 1.0, 2)]
        out = assign_speakers(words, segs)
        self.assertEqual([w["speaker"] for w in out], ["SPEAKER_2", "SPEAKER_2"])

    def test_no_segments_defaults_speaker_0(self):
        words = [self._w("a", 0.0, 0.4)]
        out = assign_speakers(words, [])
        self.assertEqual(out[0]["speaker"], "SPEAKER_0")

    def test_does_not_mutate_input(self):
        words = [self._w("a", 0.0, 0.4)]
        assign_speakers(words, [Segment(0.0, 1.0, 1)])
        self.assertNotIn("speaker", words[0])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_diarize.TestAssignSpeakers -v`
Expected: FAIL — `ImportError: cannot import name 'assign_speakers'`

- [ ] **Step 3: Implement `assign_speakers`**

Append to `native/services/diarize.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_diarize -v`
Expected: PASS (7 tests total)

- [ ] **Step 5: Commit**

```bash
git add native/services/diarize.py tests/test_diarize.py
git commit -m "feat(diarize): map speaker segments onto whisper words (midpoint rule)"
```

---

### Task 3: Download + run plumbing (mirrors whisper_cpp)

**Files:**
- Modify: `native/services/diarize.py`
- Reference (read first): `native/services/whisper_cpp.py` (the download/run/`is_ready` pattern to copy), `native/services/paths.py` (`RUNTIME_BIN_DIR` / runtime dirs).

> No unit test for the network/subprocess parts (they hit the real CLI + network);
> they are covered by the manual render checkpoint and by `--selftest-deps`. Keep
> the pure logic (parsing, mapping) in the tested functions above.

- [ ] **Step 1: Read the patterns to copy**

Run: open `native/services/whisper_cpp.py` and note `is_ready()`, the download
helper (URL constants, extract into runtime dir, progress callback), and how
`transcribe()` builds the 16 kHz mono WAV (reuse that WAV path here). Open
`native/services/paths.py` for the runtime directory constant.

- [ ] **Step 2: Add version/URL constants and `is_ready()`**

Append to `native/services/diarize.py` (adjust `RUNTIME_*` import to match `paths.py`):

```python
import os
import platform
import subprocess
import tarfile
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from native.services.paths import RUNTIME_DIR  # same runtime root whisper.cpp uses

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
```

- [ ] **Step 3: Add the downloader (copy whisper_cpp's structure)**

Append a `download(on_progress=None)` that downloads the three URLs into
`DIARIZE_DIR`, extracting the two `.tar.bz2` archives with `tarfile` and saving
the `.onnx` directly. Follow `whisper_cpp.py`'s exact progress/error idiom (same
`on_progress(str)` callback, same partial-file cleanup on failure). After
extraction, flatten so `_EXE` resolves (the binary tarball nests under `bin/` —
move/copy `bin/sherpa-onnx-offline-speaker-diarization.exe`, `onnxruntime.dll`,
`onnxruntime_providers_shared.dll` to `DIARIZE_DIR`).

```python
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
```

- [ ] **Step 4: Add `diarize()` (subprocess) + WAV guard**

```python
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
```

- [ ] **Step 5: Smoke-check import (no network)**

Run: `python -c "import native.services.diarize as d; print(d.is_ready(), d.DOWNLOAD_SIZE_LABEL)"`
Expected: prints `False ~50 MB` (binary not downloaded yet) with no import errors.

- [ ] **Step 6: Commit**

```bash
git add native/services/diarize.py
git commit -m "feat(diarize): download-on-demand sherpa-onnx engine + CLI runner"
```

---

# Phase 2 — Caption state on ProjectState

### Task 4: Source-level caption state + signal

**Files:**
- Modify: `native/ui/project_state.py`
- Test: `tests/test_project_state_captions.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_project_state_captions.py
import unittest

from native.ui.project_state import ProjectState


class TestCaptionState(unittest.TestCase):
    def setUp(self):
        self.state = ProjectState()

    def test_defaults_empty(self):
        self.assertEqual(self.state.caption_words, [])
        self.assertEqual(self.state.speakers, {})
        self.assertEqual(self.state.line_overrides, {})
        self.assertFalse(self.state.burn_captions)

    def test_set_captions_copies_in_and_emits(self):
        seen = {}
        self.state.captions_changed.connect(lambda: seen.setdefault("hit", True))
        words = [{"text": "hi", "start": 0.0, "end": 0.5, "speaker": "SPEAKER_0", "enabled": True}]
        speakers = {"SPEAKER_0": {"color": "#FFD700", "pos": (0.5, 0.85)}}
        self.state.set_captions(words, speakers, {1.0: (0.2, 0.1)}, burn_in=True)
        self.assertEqual(self.state.caption_words[0]["text"], "hi")
        self.assertEqual(self.state.speakers["SPEAKER_0"]["color"], "#FFD700")
        self.assertEqual(self.state.line_overrides[1.0], (0.2, 0.1))
        self.assertTrue(self.state.burn_captions)
        self.assertTrue(seen.get("hit"))

    def test_set_captions_is_defensive_copy(self):
        words = [{"text": "hi", "start": 0.0, "end": 0.5}]
        self.state.set_captions(words, {}, {}, burn_in=False)
        words[0]["text"] = "mutated"
        self.assertEqual(self.state.caption_words[0]["text"], "hi")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `QT_QPA_PLATFORM=offscreen python -m unittest tests.test_project_state_captions -v`
Expected: FAIL — `AttributeError: 'ProjectState' object has no attribute 'caption_words'`

- [ ] **Step 3: Add the state + signal**

In `native/ui/project_state.py`, add to the signals block:
```python
    captions_changed = Signal()            # caption words/speakers/positions changed
```
In `__init__`, add:
```python
        self._caption_words: list[dict] = []
        self._speakers: dict[str, dict] = {}
        self._line_overrides: dict[float, tuple[float, float]] = {}
        self._burn_captions: bool = False
```
Add properties + setter:
```python
    @property
    def caption_words(self) -> list[dict]:
        return [dict(w) for w in self._caption_words]

    @property
    def speakers(self) -> dict:
        return {k: dict(v) for k, v in self._speakers.items()}

    @property
    def line_overrides(self) -> dict:
        return dict(self._line_overrides)

    @property
    def burn_captions(self) -> bool:
        return self._burn_captions

    def set_captions(self, words, speakers, line_overrides, burn_in) -> None:
        self._caption_words = [dict(w) for w in words]
        self._speakers = {k: dict(v) for k, v in speakers.items()}
        self._line_overrides = dict(line_overrides)
        self._burn_captions = bool(burn_in)
        self.captions_changed.emit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `QT_QPA_PLATFORM=offscreen python -m unittest tests.test_project_state_captions -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add native/ui/project_state.py tests/test_project_state_captions.py
git commit -m "feat(state): source-level caption words/speakers/positions on ProjectState"
```

---

# Phase 3 — Render burn-in plumbing (ends at the manual render checkpoint)

### Task 5: ffmpeg `ass=` path escaping

**Files:**
- Modify: `native/services/captions.py`
- Test: `tests/test_captions_burnin.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_captions_burnin.py
import unittest

from native.services.captions import escape_ass_path


class TestEscapeAssPath(unittest.TestCase):
    def test_windows_drive_colon_escaped_and_slashes_forward(self):
        # ffmpeg -vf needs the drive colon escaped and forward slashes.
        self.assertEqual(
            escape_ass_path(r"C:\Users\mark\AppData\clip.ass"),
            r"C\:/Users/mark/AppData/clip.ass",
        )

    def test_plain_relative_path_unchanged_slashes(self):
        self.assertEqual(escape_ass_path("out/clip.ass"), "out/clip.ass")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_captions_burnin -v`
Expected: FAIL — `ImportError: cannot import name 'escape_ass_path'`

- [ ] **Step 3: Implement the escaper**

Add to `native/services/captions.py`:

```python
def escape_ass_path(path: str) -> str:
    """Escape a filesystem path for use inside an ffmpeg ``ass=`` filter value.

    ffmpeg parses the filter graph specially: backslashes and the Windows drive
    colon must be escaped. Convention that works on Windows: forward slashes +
    escape the drive ``:`` as ``\\:`` (so ``C:\\x`` -> ``C\\:/x``).
    """
    p = path.replace("\\", "/")
    return p.replace(":", r"\:", 1) if len(p) > 1 and p[1] == ":" else p
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_captions_burnin -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add native/services/captions.py tests/test_captions_burnin.py
git commit -m "feat(captions): ffmpeg ass= path escaping helper"
```

---

### Task 6: Clip-local `.ass` generation (filter + time-shift + positions)

**Files:**
- Modify: `native/services/captions.py`
- Test: `tests/test_captions_burnin.py`

- [ ] **Step 1: Add the failing test**

Append to `tests/test_captions_burnin.py`:

```python
from native.services.captions import build_clip_ass


class TestBuildClipAss(unittest.TestCase):
    def _words(self):
        # speakers across the source timeline (seconds)
        return [
            {"text": "hello", "start": 1.0, "end": 1.4, "speaker": "SPEAKER_0", "enabled": True},
            {"text": "world", "start": 1.4, "end": 1.9, "speaker": "SPEAKER_0", "enabled": True},
            {"text": "later", "start": 9.0, "end": 9.4, "speaker": "SPEAKER_1", "enabled": True},
        ]

    def test_keeps_only_words_in_clip_and_shifts_to_zero(self):
        speakers = {"SPEAKER_0": {"color": "#FFD700", "pos": (0.5, 0.85)}}
        ass = build_clip_ass(self._words(), speakers, {}, 1000, 3000, 1080, 1920)
        self.assertIn("hello", ass)
        self.assertNotIn("later", ass)            # outside [1s,3s]
        self.assertIn("Dialogue: 0,0:00:00.00", ass)  # 1.0s shifted to 0

    def test_position_scaled_to_output_pixels(self):
        speakers = {"SPEAKER_0": {"color": "#FFD700", "pos": (0.5, 0.85)}}
        ass = build_clip_ass(self._words(), speakers, {}, 1000, 3000, 1080, 1920)
        self.assertIn(r"\pos(540,1632)", ass)     # 0.5*1080, 0.85*1920
        self.assertIn(r"\an5", ass)

    def test_line_override_replaces_speaker_pos(self):
        speakers = {"SPEAKER_0": {"color": "#FFD700", "pos": (0.5, 0.85)}}
        # override keyed by the line's first-word *source* start time (1.0s)
        ass = build_clip_ass(self._words(), speakers, {1.0: (0.1, 0.1)}, 1000, 3000, 1080, 1920)
        self.assertIn(r"\pos(108,192)", ass)       # 0.1*1080, 0.1*1920
        self.assertNotIn(r"\pos(540,1632)", ass)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_captions_burnin.TestBuildClipAss -v`
Expected: FAIL — `ImportError: cannot import name 'build_clip_ass'`

- [ ] **Step 3: Implement `build_clip_ass`**

Add to `native/services/captions.py` (reuses `group_words_into_lines`,
`format_ass_time`, `normalize_caption_style`, and the colour conversion already
in `generate_ass_subtitles`):

```python
def _ass_color(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    if len(h) == 6:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    else:
        r, g, b = 255, 255, 255
    return f"&H00{b:02X}{g:02X}{r:02X}&"


def build_clip_ass(
    words: list[dict],
    speakers: dict,
    line_overrides: dict,
    clip_start_ms: int,
    clip_end_ms: int,
    out_w: int,
    out_h: int,
    style: dict | None = None,
) -> str:
    """Build an ASS subtitle string local to one clip.

    Words are filtered to ``[clip_start_ms, clip_end_ms]`` and shifted so the
    clip starts at 0. Each line is positioned with ``\\an5\\pos(x,y)`` where x/y
    come from the speaker's normalized ``pos`` scaled to ``out_w``/``out_h`` (or
    a per-line override keyed by the line's first-word source start time), and
    coloured per speaker. Designed to sit at the END of the -vf chain so coords
    are in output pixel space.
    """
    cfg = normalize_caption_style(style)
    cs, ce = clip_start_ms / 1000.0, clip_end_ms / 1000.0
    kept = [w for w in words if cs <= (float(w["start"]) + float(w["end"])) / 2.0 <= ce]

    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {out_w}\nPlayResY: {out_h}\nWrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
    )
    font_size = int(72 * cfg["font_scale"] * out_w / 1080)
    bold = -1 if cfg["bold"] else 0
    used = {w.get("speaker", "SPEAKER_0") for w in kept} or {"SPEAKER_0"}
    for sp in sorted(used):
        color = _ass_color(speakers.get(sp, {}).get("color", "#FFFFFF"))
        header += (
            f"Style: {sp},{cfg['font_family']},{font_size},{color},&H000000FF,"
            f"&H00000000&,&H64000000&,{bold},0,0,0,100,100,0,0,1,"
            f"{cfg['outline']},{cfg['shadow']},5,30,30,30,1\n"
        )

    body = "\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    for line in group_words_into_lines(kept, max_words=cfg["max_words"]):
        if not line.get("enabled", True):
            continue
        sp = line["speaker"]
        src_start = line["start"]  # source-timeline seconds = override key
        if src_start in line_overrides:
            nx, ny = line_overrides[src_start]
        else:
            nx, ny = speakers.get(sp, {}).get("pos", (0.5, 0.85))
        px, py = int(round(nx * out_w)), int(round(ny * out_h))
        start = format_ass_time(max(0.0, line["start"] - cs))
        end = format_ass_time(max(0.0, line["end"] - cs))
        if cfg["karaoke"]:
            text = " ".join(
                f"{{\\kf{max(1, int((w['end'] - w['start']) * 100))}}}"
                f"{(w['text'].upper() if cfg['all_caps'] else w['text'])}"
                for w in line["words"]
            )
        else:
            text = " ".join(
                (w["text"].upper() if cfg["all_caps"] else w["text"]) for w in line["words"]
            )
        body += f"Dialogue: 0,{start},{end},{sp},,0,0,0,,{{\\an5\\pos({px},{py})}}{text}\n"
    return header + body
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_captions_burnin -v`
Expected: PASS (5 tests total in file)

- [ ] **Step 5: Commit**

```bash
git add native/services/captions.py tests/test_captions_burnin.py
git commit -m "feat(captions): clip-local ASS with time-shift, per-speaker colour + position"
```

---

### Task 7: Thread the subtitle into export args

**Files:**
- Modify: `native/services/export_presets.py:110-137`
- Test: `tests/test_export_args.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_export_args.py
import unittest

from native.services.export_presets import build_clip_export_args, style_preset_by_key, format_preset_by_key


class TestExportArgsSubtitle(unittest.TestCase):
    def test_no_subtitle_has_no_ass_filter(self):
        args = build_clip_export_args(0, 1000, style_preset_by_key("gameplay_focus"))
        vf = args[args.index("-vf") + 1]
        self.assertNotIn("ass=", vf)

    def test_subtitle_appended_to_vf_chain(self):
        args = build_clip_export_args(
            0, 1000, style_preset_by_key("gameplay_focus"),
            fmt=format_preset_by_key("shorts"), subtitle_ass=r"C\:/tmp/clip.ass",
        )
        vf = args[args.index("-vf") + 1]
        self.assertTrue(vf.rstrip().endswith(r"ass='C\:/tmp/clip.ass'"))
        self.assertIn("crop=1080:1920", vf)  # ass comes AFTER scale/crop


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_export_args -v`
Expected: FAIL — `TypeError: build_clip_export_args() got an unexpected keyword argument 'subtitle_ass'`

- [ ] **Step 3: Add the parameter**

In `native/services/export_presets.py`, change the signature and the `-vf` assembly:

```python
def build_clip_export_args(
    start_ms: int,
    end_ms: int,
    style: StylePreset,
    fmt: FormatPreset | None = None,
    normalize_audio: bool = True,
    subtitle_ass: str | None = None,
) -> list[str]:
    vf = style.video_filter + (
        f",scale={fmt.width}:{fmt.height}:force_original_aspect_ratio=increase,"
        f"crop={fmt.width}:{fmt.height}"
        if fmt is not None
        else ""
    )
    if subtitle_ass:
        vf += f",ass='{subtitle_ass}'"
    args: list[str] = [
        "-ss", f"{start_ms / 1000:.3f}",
        "-to", f"{end_ms / 1000:.3f}",
        "-vf", vf,
        "-c:v", "libx264",
        "-c:a", "aac",
        "-pix_fmt", "yuv420p",
    ]
    if normalize_audio:
        args.extend(["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"])
    return args
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_export_args -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add native/services/export_presets.py tests/test_export_args.py
git commit -m "feat(export): optional ass= subtitle filter on clip export args"
```

---

### Task 8: Wire clip-local `.ass` into the three render paths

**Files:**
- Modify: `native/ui/window.py` (`_clip_export_args`, used by `_export_clip`, `_render_all_clips`, `_build_longform`)
- Reference: `native/services/paths.py` (a temp/processing dir for the `.ass`)

> No unit test (it spawns ffmpeg). Verified by the manual render checkpoint (Task 9).

- [ ] **Step 1: Add a helper to build a clip's `.ass` path + filter string**

In `native/ui/window.py`, import the new functions and add:

```python
from native.services.captions import build_clip_ass, escape_ass_path
from native.services.diarize import assign_speakers  # (only if re-diarizing here; usually not)
# plus: from native.services.paths import PROCESSING_DIR  (or the existing temp dir)

    def _clip_subtitle(self, clip: Clip, out_w: int, out_h: int) -> str | None:
        """Write a clip-local .ass if burn-in is on, return the escaped path."""
        if not self._state.burn_captions or not self._state.caption_words:
            return None
        ass = build_clip_ass(
            self._state.caption_words, self._state.speakers, self._state.line_overrides,
            clip.start_ms, clip.end_ms, out_w, out_h,
        )
        path = PROCESSING_DIR / f"caption_{clip.start_ms}_{clip.end_ms}.ass"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(ass, encoding="utf-8")
        return escape_ass_path(str(path))
```

- [ ] **Step 2: Use it in `_clip_export_args`**

Update `_clip_export_args` (currently builds `style`/`fmt` then calls
`build_clip_export_args`) to compute the format dims, build the subtitle, and
pass it through:

```python
    def _clip_export_args(self, clip: Clip) -> list[str]:
        style = style_preset_by_key(self._state.style_preset_key)
        fmt = format_preset_by_key(self._state.format_preset_key)
        subtitle = self._clip_subtitle(clip, fmt.width, fmt.height)
        return build_clip_export_args(
            clip.start_ms, clip.end_ms, style, fmt, subtitle_ass=subtitle,
        )
```

(Confirmed accessors: `ProjectState.style_preset_key` / `format_preset_key`. The
existing `_clip_export_args` already uses them; this only adds the subtitle.
`_render_all_clips` and `_build_longform` call `_clip_export_args`, so they
inherit burn-in automatically.)

- [ ] **Step 3: Smoke-check import**

Run: `QT_QPA_PLATFORM=offscreen python -c "import native.ui.window"`
Expected: no import error.

- [ ] **Step 4: Commit**

```bash
git add native/ui/window.py
git commit -m "feat(render): burn clip-local captions into clip + longform exports"
```

---

### Task 9: MANUAL RENDER CHECKPOINT (gates the v0.2.3 tag)

**Files:** none (verification only).

> This is the spec's required human checkpoint — burn-in correctness (timing,
> ass= escaping, font scale) is NOT headless-verifiable. Do not tag v0.2.3
> until this passes.

- [ ] **Step 1: Prepare state in a quick REPL or temporary `--selftest` path**

Set `ProjectState` caption_words to a few known words with two speakers + colours
+ positions, `burn_captions=True`, then render one short clip through the normal
Output flow.

- [ ] **Step 2: Open the rendered mp4 and verify by eye**

Confirm: captions appear; text matches; **timing** lines up with speech;
SPEAKER_0 vs SPEAKER_1 show the right **colours** and **positions**; font is
legible at the chosen format (e.g. 1080×1920). Note any offset.

- [ ] **Step 3: If timing/escaping/scale is wrong, STOP and fix the relevant Task (5/6/7/8) before continuing.** Re-render until correct.

- [ ] **Step 4: Record the result**

Confirm in the working notes that the render checkpoint passed (so reviewers know
the visual path was validated, not just the unit tests).

---

# Phase 4 — Editor: speaker assignment + colours

### Task 10: Run diarization after the caption pass; persist editor result

**Files:**
- Modify: `native/ui/stages/shorts.py` (`_run_caption_pass`, `_on_captions_ready`)
- Reference: `native/services/diarize.py`, `native/workers.py` (`JobRunner.run`)

- [ ] **Step 1: After transcription, run diarization on the same WAV**

In `shorts.py`, the caption pass currently returns `{words, ass, srt}` and opens
the editor. Extend the worker chain: after words are ready, if `diarize.is_ready()`,
run `diarize.diarize(wav, num_speakers=None)` on the WAV the caption pass used,
then `words = assign_speakers(words, segments)`. If the engine isn't installed,
skip (single-speaker) and show a "Separate speakers (download ~50 MB)" button
that calls `diarize.download(...)` via `JobRunner` (mirror the existing
`_setup_captions` button that downloads whisper.cpp).

- [ ] **Step 2: Persist the editor result to ProjectState on accept**

Replace the discard-on-close in `_on_captions_ready`:

```python
    def _on_captions_ready(self, result: object) -> None:
        self._run_btn.setEnabled(True)
        data = result if isinstance(result, dict) else {}
        words = data.get("words", [])
        editor = CaptionEditor(words, data.get("ass"), data.get("srt"), parent=self)
        if editor.exec():
            self._state.set_captions(
                editor.result_words(),
                editor.result_speakers(),
                editor.result_overrides(),
                burn_in=editor.burn_in,
            )
```

(`ShortsStage` already holds `self._state`; confirm and pass it if not.)

- [ ] **Step 3: Smoke-check import**

Run: `QT_QPA_PLATFORM=offscreen python -c "import native.ui.stages.shorts"`
Expected: no import error (the new `result_*` methods land in Task 11/12 — if you
run before those, stub them returning `[]`/`{}` to keep imports clean, then fill in).

- [ ] **Step 4: Commit**

```bash
git add native/ui/stages/shorts.py
git commit -m "feat(shorts): diarize after caption pass, persist editor result to state"
```

---

### Task 11: Speaker panel in the editor (assign + colour + count)

**Files:**
- Modify: `native/ui/caption_editor.py`
- Test: `tests/test_captions_burnin.py` (logic-only: speaker collection)

- [ ] **Step 1: Add the speaker controls + result accessors**

In `CaptionEditor.__init__`, after the existing word table + colour button, add:
- A "Speakers" `QComboBox` (Auto / 2 / 3 / 4) — on change, emit a signal the
  ShortsStage connects to re-run `diarize.diarize(wav, num_speakers=N)` and
  reload the table (wire in ShortsStage; the editor just exposes the chosen N via
  `selected_speaker_count() -> int | None`).
- An "Assign selected rows to" `QComboBox` of discovered speakers + an "Apply"
  button that sets `w["speaker"]` on the selected word rows.
- Per-speaker colour: keep the existing colour button but make it set the colour
  for the currently-selected speaker in a `self._speaker_colors: dict[str,str]`
  (seed from `captions.DEFAULT_SPEAKER_COLORS` by speaker index).

Add result accessors used by ShortsStage:
```python
    def result_words(self) -> list[dict]:
        return self._collect_words()

    def result_speakers(self) -> dict:
        out = {}
        for i, sp in enumerate(sorted({w.get("speaker", "SPEAKER_0") for w in self._words})):
            out[sp] = {
                "color": self._speaker_colors.get(sp, captions.DEFAULT_SPEAKER_COLORS[i % 5]),
                "pos": self._speaker_pos.get(sp, (0.5, 0.85)),
            }
        return out

    def result_overrides(self) -> dict:
        return dict(self._line_overrides)
```
Initialize `self._speaker_colors = {}`, `self._speaker_pos = {}`,
`self._line_overrides = {}` in `__init__`.

- [ ] **Step 2: Add a logic test for `result_speakers` defaults**

Append to `tests/test_captions_burnin.py`:
```python
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from native.ui.caption_editor import CaptionEditor


class TestEditorResults(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_result_speakers_seeds_colour_and_default_pos(self):
        words = [{"text": "hi", "start": 0.0, "end": 0.4, "speaker": "SPEAKER_0", "enabled": True}]
        ed = CaptionEditor(words)
        sp = ed.result_speakers()
        self.assertIn("SPEAKER_0", sp)
        self.assertEqual(sp["SPEAKER_0"]["pos"], (0.5, 0.85))
        self.assertTrue(sp["SPEAKER_0"]["color"].startswith("#"))
```

- [ ] **Step 3: Run the test**

Run: `QT_QPA_PLATFORM=offscreen python -m unittest tests.test_captions_burnin.TestEditorResults -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add native/ui/caption_editor.py tests/test_captions_burnin.py
git commit -m "feat(editor): speaker assignment, per-speaker colour, count control"
```

---

# Phase 5 — Editor: drag-positioning preview

### Task 12: Draggable per-speaker position preview

**Files:**
- Modify: `native/ui/caption_editor.py`
- Reference: `native/workers.py` / ffmpeg to extract one frame (`-ss <mid> -frames:v 1`).

> UI drag has no clean unit test; correctness is confirmed at the render
> checkpoint. Keep the coordinate math (pixel↔normalized) in tiny pure helpers
> and test those.

- [ ] **Step 1: Add normalized↔pixel helpers + test**

Append to `tests/test_captions_burnin.py`:
```python
from native.ui.caption_editor import norm_to_px, px_to_norm

class TestPreviewCoords(unittest.TestCase):
    def test_round_trip(self):
        self.assertEqual(norm_to_px((0.5, 0.85), 200, 100), (100, 85))
        self.assertEqual(px_to_norm((100, 85), 200, 100), (0.5, 0.85))
```
Add to `caption_editor.py` (module-level):
```python
def norm_to_px(pos, w, h):
    return (int(round(pos[0] * w)), int(round(pos[1] * h)))

def px_to_norm(px, w, h):
    return (round(px[0] / w, 4), round(px[1] / h, 4))
```

- [ ] **Step 2: Add the preview widget**

Add a `QLabel`-based preview that shows one extracted source/clip frame scaled to
a fixed width (e.g. 240 px, 9:16). Overlay one small draggable chip per speaker
(a `QLabel` child you reposition on `mousePressEvent`/`mouseMoveEvent`). On drop,
`self._speaker_pos[sp] = px_to_norm(chip_center, preview_w, preview_h)`. Add a
"Nudge selected line here" affordance: when a word row is selected and the user
drops on the preview while holding it, set
`self._line_overrides[line_start_time] = px_to_norm(...)`.

(Extract the frame via ffmpeg into the processing dir; if extraction fails, fall
back to a flat dark background so positioning still works.)

- [ ] **Step 3: Run all tests**

Run: `QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v`
Expected: PASS (all)

- [ ] **Step 4: Commit**

```bash
git add native/ui/caption_editor.py tests/test_captions_burnin.py
git commit -m "feat(editor): drag-to-position preview with per-line override"
```

---

# Phase 6 — Attributions, changelog, release

### Task 13: Third-party notices + spec bundling

**Files:**
- Create: `THIRD_PARTY_NOTICES.txt`
- Modify: `Clipline.spec` (add the file to `datas`)

- [ ] **Step 1: Write the notices file**

```text
# THIRD_PARTY_NOTICES

Clipline bundles / downloads the following third-party components:

sherpa-onnx (https://github.com/k2-fsa/sherpa-onnx) — Apache License 2.0.
pyannote/segmentation-3.0 (ONNX, via sherpa-onnx releases) — MIT License.
3D-Speaker eres2net speaker embedding (English, via sherpa-onnx releases) — Apache License 2.0.
whisper.cpp (https://github.com/ggml-org/whisper.cpp) — MIT License.

Full license texts are available at the upstream project pages above.
```

- [ ] **Step 2: Bundle it in the EXE**

In `Clipline.spec`, add `('THIRD_PARTY_NOTICES.txt', '.')` to the `datas` list.

- [ ] **Step 3: Commit**

```bash
git add THIRD_PARTY_NOTICES.txt Clipline.spec
git commit -m "docs: third-party notices for sherpa-onnx + models; bundle in EXE"
```

---

### Task 14: Version bump + changelog + tag

**Files:**
- Modify: `native/__init__.py`, `CHANGELOG.md`

- [ ] **Step 1: Bump version**

In `native/__init__.py`: `__version__ = "0.2.3"`.

- [ ] **Step 2: Add the CHANGELOG entry**

Add a `## [0.2.3]` section above `## [0.2.2]` summarizing: real caption burn-in
into every clip + longform; offline auto speaker separation (sherpa-onnx,
download-on-demand); per-speaker colour + draggable position with per-line
overrides.

- [ ] **Step 3: Run the full suite + import smoke**

Run: `QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v`
Run: `python desktop.py --selftest-deps`
Expected: tests PASS; selftest-deps runs.

- [ ] **Step 4: Confirm the render checkpoint (Task 9) passed, then commit + tag**

```bash
git add native/__init__.py CHANGELOG.md
git commit -m "release: v0.2.3 — speaker-separated, positioned, burned-in captions"
git tag -a v0.2.3 -m "v0.2.3 — burned-in captions with speaker separation + positioning"
git push origin main && git push origin v0.2.3
```

(CI builds the EXE on the tag. After CI, apply real release notes with
`gh release edit v0.2.3 --notes-file ...` per the v0.2.2 process.)

---

## Self-Review (completed by plan author)

**Spec coverage:** diarize service (T1–3), speaker mapping (T2), caption state (T4),
ass escaping (T5), clip-local ass (T6), export-arg threading (T7), three render paths
(T8), manual checkpoint (T9), diarize-after-pass + persist (T10), speaker panel (T11),
drag positioning + per-line override (T12), notices (T13), release (T14). Deferred items
(cross-session identity, head-tracking) are explicitly non-goals in the spec.

**Placeholders:** UI tasks (T10–12) describe Qt wiring with concrete code for the
testable seams (result accessors, coord helpers) and exact method names; the drag
interaction is described rather than fully coded because it has no unit seam and is
validated at the render checkpoint — acceptable per the spec's stated verification
strategy.

**Type consistency:** `set_captions(words, speakers, line_overrides, burn_in)`,
`build_clip_ass(words, speakers, line_overrides, clip_start_ms, clip_end_ms, out_w,
out_h)`, `escape_ass_path(str)->str`, `build_clip_export_args(..., subtitle_ass=None)`,
`assign_speakers(words, segments)`, `parse_segments(str)->list[Segment]`, and editor
`result_words/result_speakers/result_overrides` are used consistently across tasks.
Override key is the line's first-word **source** start time (seconds) everywhere.
