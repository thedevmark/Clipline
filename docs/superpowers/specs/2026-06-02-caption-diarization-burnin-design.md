# Design: Speaker-separated, positioned, burned-in captions

**Date:** 2026-06-02
**Status:** Approved for spec review
**Target release:** v0.2.3 (single release; built in internal phases)

## Problem

The caption editor exposes a "Burn captions into the video on render" checkbox and a
single speaker colour, but **none of it reaches a render**. When the editor closes its
result is discarded: `Clip` has no caption field, `build_clip_export_args` has no
subtitle support, and the burn-in flag is read by nobody. The checkbox is a lying
control.

Separately, the user's real workflow is multi-person streams where each speaker's
captions should appear in a **different place on screen, in a different colour** (e.g.
above each person). Today captions are single-speaker, bottom-centre, sidecar-only.

## Goals

1. Captions actually burn into renders — **every clip and the longform build**.
2. **Automatic speaker separation** (2–4 speakers) as a draft the user fine-tunes.
3. Per-speaker **colour** and **on-screen position**; any individual caption line can be
   repositioned (override).
4. Stay true to the project's constraints: **no Python/pip for end users**, no EXE
   bloat, fully offline, AGPL-3.0-compatible, **commercial use safe** (monetized clips).

## Non-goals (this release)

- Automatic head/face tracking (needs excluded ML). Positions are user-placed zones.
- Cross-*session* speaker identity (same colour for a person across different VODs) —
  needs the sherpa-onnx embedding-manager C-API; deferred to a later phase. Within one
  source, the single diarization pass already yields consistent labels across its clips.
- Overlapping-speech perfection — diarization emits one speaker per region; crosstalk is
  fixed by the manual editor, not the model.
- Diarization in non-English languages (English embedding model by default).

## Decisions (from brainstorming)

| Question | Decision |
|---|---|
| Sequencing | Build all of it, release once as v0.2.3 |
| Speaker source | Auto-diarization, download-on-demand, manual fine-tune |
| Engine | **sherpa-onnx** CLI (Apache-2.0), download-on-demand like whisper.cpp |
| Models | **pyannote-segmentation-3.0** (MIT) + **3dspeaker_speech_eres2net_sv_en_voxceleb_16k** (Apache-2.0, English); `nemo_en_titanet_small` (CC-BY-4.0) as quality alt |
| Burn-in scope | Every clip **and** longform; per-clip time-shifted `.ass` |
| Positioning | Per-speaker draggable zone + per-line override + per-speaker colour |
| Speaker count | "Auto / 2 / 3 / 4" control (pinning N beats auto-estimate — issue #1466) |
| Integration | CLI subprocess (safer than C-API; native crash reports in in-process path) |

### Why a dependency and not build-our-own
Researched and confirmed: building our own (onnxruntime + custom clustering) costs
+30–60 MB EXE, PyInstaller/onnxruntime DLL pain, L effort, and worse quality; training
an embedding model is XL effort for negative return. sherpa-onnx adds **0 MB** to the EXE
(downloaded on demand), reuses the existing whisper.cpp WAV, and is small–medium effort.

### Licensing (monetization-critical)
- Engine Apache-2.0; segmentation MIT; embedding Apache-2.0 (or CC-BY-4.0 for TitaNet).
  All flow one-way into AGPL-3.0.
- Models are re-hosted **un-gated** on sherpa-onnx GitHub releases → unattended download
  works (the upstream HuggingFace weights are gated and would break it).
- Diarization **output** (labels + timestamps) carries no copyright claim over clips.
- **Landmine:** the higher-accuracy **Reverb/Revai** models are **non-commercial** — must
  not be used. NeMo Sortformer is CC-BY-NC + broken ONNX — also excluded.
- Ship a `THIRD_PARTY_NOTICES` file (Apache NOTICE for sherpa-onnx + embedding; MIT for
  pyannote-seg).

## Architecture

### Components

1. **`native/services/diarize.py`** (new) — mirrors `whisper_cpp.py`:
   - `is_ready()` / `download(...)`: fetch the pinned **v1.13.2** win-x64 binary set
     (`sherpa-onnx-offline-speaker-diarization.exe` + `onnxruntime.dll` +
     `onnxruntime_providers_shared.dll`, ~15 MB) and the two models (~31 MB) into a
     runtime folder. **Versioned URLs** (no floating tags).
   - `diarize(wav_path, ffmpeg, num_speakers=None, on_progress=None, cancel=None) ->
     list[Segment]`: run the CLI on the existing 16 kHz mono WAV; `num_speakers`
     → `--clustering.num-clusters=N`, else `--clustering.cluster-threshold=0.5`. Parse
     stdout lines `^\s*([\d.]+)\s+--\s+([\d.]+)\s+speaker_(\d+)\s*$` → `Segment(start,
     end, speaker)` (seconds). Background-run; progress from stderr `progress %.2f%%`;
     cancel = terminate subprocess.
   - `assign_speakers(words, segments) -> words`: midpoint rule — each word gets the
     speaker of the segment containing `(start+end)/2`; unassigned words inherit the
     previous word's speaker.

2. **Caption state on `ProjectState`** (new, source-level) — today discarded:
   - `caption_words: list[dict]` (source timeline; each word `{text,start,end,speaker,enabled}`).
   - `speakers: dict[str, {color: str, pos: (x,y)}]` — `pos` normalized 0–1.
   - `line_overrides: dict[float, (x,y)]` — per-line position overrides (normalized),
     **keyed by the line's first-word start time** (stable across text edits; if
     re-grouping changes line boundaries the override simply no longer matches and falls
     back to the speaker default — acceptable).
   - `burn_captions: bool`.
   - A `captions_changed` signal; setters that copy-in.

3. **Expanded caption editor** (`caption_editor.py`) — three regions:
   - **Word table** (existing) — edit text, toggle words on/off.
   - **Speaker panel** — "Speakers: auto/2/3/4" (re-runs diarization), assign selected
     lines to Speaker N, set each speaker's colour.
   - **Positioning preview** — one ffmpeg-extracted frame (active clip midpoint if a clip
     is selected, else source midpoint) as a backdrop, with a draggable block per
     speaker; drag sets that speaker's normalized `pos` (source-level, applies to all
     clips); a selected line can be nudged to write a per-line override.
   - On accept: persist words + speakers + overrides + `burn_in` to `ProjectState`.

4. **Render burn-in plumbing**:
   - `captions.build_clip_ass(words, speakers, overrides, clip_start_ms, clip_end_ms,
     out_w, out_h) -> str`: keep words within `[start,end]`, **shift times by −start**,
     emit one Dialogue per line with `\an5\pos(x*out_w, y*out_h)` and the speaker's style
     colour; per-line override replaces `pos`. Reuses existing `generate_ass_subtitles`
     style/karaoke machinery.
   - `build_clip_export_args(..., subtitle_ass: str | None = None)`: when set, append
     `,ass='<escaped>'` to the `-vf` chain **after** crop/scale (so coords are in output
     space). Windows path escaping: `ass='C\:/Users/…/clip.ass'`.
   - `MainWindow._clip_export_args(clip)`: if `state.burn_captions` and words exist,
     write the clip-local `.ass` to a temp path and pass it through. Applies in
     `_export_clip`, `_render_all_clips`, and `_build_longform`.

5. **`THIRD_PARTY_NOTICES`** bundled via `Clipline.spec`.

### Data flow

```
Shorts ▸ Run caption pass
  whisper.cpp transcribe        → words[] (+ 16 kHz mono WAV)
  diarize.py on that WAV         → speaker segments
  assign_speakers(words, segs)   → words[].speaker
  CaptionEditor (edit text, reassign speakers, colours, drag positions)
  on accept → ProjectState (source timeline)

Render clip / longform
  build_clip_ass(words in [start,end], shift −start, pos→pixels, colour per speaker)
  ffmpeg: …crop/scale…, ass='<clip>.ass'   → burned captions
```

Captions are source-level; each clip derives its own shifted `.ass` at render — this is
what makes "every clip + longform" work and keeps speaker labels consistent across clips.

## Error handling

- Diarization engine not downloaded → editor still works single-speaker; a "Separate
  speakers (download ~50 MB)" button gates the feature like the captions setup.
- Diarization subprocess fails/crashes → surface the error, keep the transcript; user can
  retry or assign speakers manually.
- Long-VOD CPU time → background job + progress + cancel.
- Odd-byte WAV (#3052 heap overflow) → validate/normalize WAV before invoking.
- Missing/garbled `.ass` at render → skip the `ass=` filter, render without burn-in, warn
  (never fail the export over captions).

## Testing & verification

**Automated (unit):**
- `assign_speakers` midpoint mapping, including boundary-straddling words.
- Clip-local `.ass`: word filtering to `[start,end]`, time-shift math, normalized→pixel
  coords, per-speaker colour + per-line override emission.
- Windows `ass=` path escaping.
- CLI stdout parsing regex (incl. malformed lines).

**Manual checkpoint (only the user can do this — gates the tag):**
- Real render on a short known clip: captions appear, correctly timed, correctly
  positioned/coloured per speaker, font legible at 1080×1920. Verified before tagging
  v0.2.3.

## Internal build phases (one release)

1. `diarize.py` service + `assign_speakers` (no UI; unit-tested).
2. Caption state on `ProjectState` + persist editor result.
3. Burn-in render plumbing (clip-local `.ass` + filter) → **render-test checkpoint**.
4. Editor: speaker assignment + colours.
5. Editor: drag-positioning preview + per-line override.
6. `THIRD_PARTY_NOTICES`, CHANGELOG, tag v0.2.3.

## Risks

- **Overlapping speech / crosstalk** → one speaker per region; mitigated by manual editor.
- **Auto speaker-count** unreliable → "pin N" control is the primary path.
- **ffmpeg `ass=` correctness** (timing, escaping, font scale) → not headless-verifiable;
  covered by the manual render checkpoint + unit tests on the pure logic.
- **Upstream re-tag / stale binary** → pinned versioned download URLs.
