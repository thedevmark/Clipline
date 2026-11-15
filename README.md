# Clipline

**A local desktop video editor built around Twitch moments.** Pick a clip your community already made, edit it once on a real timeline, and export a landscape and a portrait version from the same edit.

<p align="center">
  <img src="https://img.shields.io/badge/editor-FilmCraft_(Rust)-b7410e">
  <img src="https://img.shields.io/badge/shell-PySide6-41cd52?logo=qt&logoColor=white">
  <img src="https://img.shields.io/badge/captions-WhisperX_%2B_faster--whisper-5fb0c8">
  <img src="https://img.shields.io/badge/License-AGPL_v3-blue">
</p>

> **Status: development preview.** The rebuild runs end to end on a development machine (Twitch intake, edit, captions, dual export), but there is no installer or tagged release yet. Screenshots are coming.

---

## What changed

Clipline used to be a one-screen "cut a vertical short" tool. It has been rebuilt into a full editor with a Premiere-style timeline, and the Twitch side now feeds that editor directly.

Instead of writing a video editor from scratch, Clipline is built on top of [FilmCraft](https://github.com/storytold/filmcraft), an open-source, clean-room reimplementation of a Premiere-style editor written in Rust. FilmCraft does the editing, playback, compositing and rendering. Clipline adds everything around it that is specific to streamers: getting clips in, linking two formats to one edit, captions, creator presets and delivery.

## What it does

**Twitch in**
- Connect Twitch, browse your channel's clips, load more, and pull a clip down as a source.
- Clips download in a background worker you can cancel, resume or stop, so a failed download never leaves a half-imported project.
- Local video files work too: add a file, verify it, and turn a range of it into a Moment.

**Creator Library**
- Every clip becomes a *Moment* with its own saved edit, version history and checkpoints.
- Open any Moment in the editor, save a checkpoint, close, and come back later. Interrupted edits can be recovered.

**One edit, two formats**
- Each Moment has a landscape (16:9) and a portrait (9:16) composition that share the same cuts and timing.
- Framing, facecam treatment, captions and titles can differ per format without the two drifting apart.
- "Export both formats" renders landscape and portrait together at full resolution, with NVIDIA hardware encoding when available.

**Captions**
- Generate captions locally. Nothing is uploaded.
- Choose a profile: Quick (GPU), Large model (GPU, Whisper large-v3) or CPU.
- Review a proposal before accepting it. Edit words, fix timing, split, merge and delete words, and replace a repeated misspelling everywhere it occurs.
- Accepted captions land in the editor as normal, editable caption tracks in both formats.

**Creator Kits**
- Save your channel's vocabulary (names, in-jokes, game terms) once. Caption generation uses it as hints so those words come out right.
- Kits keep your setup consistent across Moments.

**Hooks and CTAs**
- Add an opening hook or closing call-to-action title to either composition. Titles are real editor graphics, so you can still edit, undo and move them.

**Still here from the classic app**
- Facecam guide and stacked vertical layout, waveform trimming, karaoke captions with per-speaker colours, offline speaker separation, ASS/SRT export and burn-in. The classic app (`desktop.py`) stays in the repo during the transition.

## How much comes from other open-source projects

A lot, on purpose. The rebuild's rule was: do not write codecs or a timeline engine that already exists.

| Part | Comes from | Size | What Clipline adds |
| --- | --- | --- | --- |
| Editor, timeline, playback, compositing, rendering, codecs | [FilmCraft](https://github.com/storytold/filmcraft) 0.9.0 (MIT or Apache-2.0) | ~407,000 lines of Rust in ~1,000 files | A ~17,700-line patch: linked landscape/portrait edits, caption tracks, hook/CTA titles, Creator Kit helper, export snapshot helper, autosave and managed close |
| App shell, Creator Library, Twitch, workers, captions UI | Clipline (this repo) | ~22,000 lines of Python | All of it, plus ~16,000 lines of tests |
| Speech-to-text | [faster-whisper](https://github.com/SYSTRAN/faster-whisper), [CTranslate2](https://github.com/OpenNMT/CTranslate2), OpenAI Whisper models | | Profiles, vocabulary hints, review and correction |
| Word alignment | [WhisperX](https://github.com/m-bain/whisperX) | | Accepted-word history and timing correction |
| Speaker separation | [pyannote.audio](https://github.com/pyannote/pyannote-audio), [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx), [ONNX Runtime](https://github.com/microsoft/onnxruntime) | | Speaker colours and positions |
| ML runtime | [PyTorch](https://pytorch.org/), [Hugging Face Transformers](https://github.com/huggingface/transformers) | | |
| Desktop UI toolkit | [Qt](https://www.qt.io/) via [PySide6](https://doc.qt.io/qtforpython/) (LGPL-3.0) | | |
| Media tools | [FFmpeg](https://ffmpeg.org/), [yt-dlp](https://github.com/yt-dlp/yt-dlp) | | Probing, source checks, downloads |

By line count, roughly nine of every ten lines that run when you edit a clip are FilmCraft's. Clipline's own code is the streamer workflow around that engine.

The FilmCraft source is not copied into this repo. `vendor/filmcraft/pin.json` pins the exact upstream release archive and its SHA-256, and `vendor/filmcraft/clipline-native-edits.patch` holds every Clipline change to it. `scripts/engine/prepare_filmcraft.py` verifies a downloaded archive and the patch against that pin, extracts the archive, and applies the patch.

GPU captioning uses NVIDIA CUDA libraries (cuBLAS, cuDNN), which are free to redistribute but not open source.

Full attributions and licenses: [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt).

## Running it

There is no installer yet. To run from source you need Python 3.10+, the Rust toolchain FilmCraft requires (1.95+), and a FilmCraft build prepared from the pinned archive and patch.

```bash
git clone https://github.com/thedevmark/Clipline.git
cd Clipline
pip install -r requirements.txt
python scripts/engine/prepare_filmcraft.py --archive filmcraft-0.9.0.zip --patch vendor/filmcraft/clipline-native-edits.patch --pin vendor/filmcraft/pin.json --destination build/filmcraft
python creator_desktop.py --help
```

Download the archive from the `archive_url` in `vendor/filmcraft/pin.json`, then build FilmCraft in the destination folder with Cargo.

`creator_desktop.py` starts the Creator Library. The editor, Twitch intake, caption profiles and export are each switched on by passing a runtime manifest that points at the exact local tools to use. Without them the library still opens, with those features off.

The classic one-screen app still runs with `python desktop.py`.

## Where things are stored

- Windows: `%LOCALAPPDATA%\clipline\` (settings, Creator Library workspace, downloaded engines and models).
- Everything stays on your machine. Connecting Twitch and downloading models are the only network operations.

## Roadmap

- Installer and a tagged release.
- Linked transitions and title styling from Creator Kits.
- Screenshots and a short demo.

---

## deutschmark's other apps

<table>
<tr><td align="center" width="56"><img src=".github/apps/pathos.svg" width="44" alt=""></td><td><a href="https://yourpathos.app"><b>Pathos</b></a><br>Worker-side job search with source-linked roles, evidence-checked resumes, and application tracking.</td></tr>
<tr><td align="center" width="56"><img src=".github/apps/markskill.svg" width="44" alt=""></td><td><a href="https://github.com/thedevmark/markskill"><b>Markskill</b></a><br>A product-engineering skill for AI agents: trace behavior to its owner, fix root causes, shape interfaces around real tasks, and verify claims with evidence.</td></tr>
<tr><td align="center" width="56"><img src=".github/apps/alert-alert.svg" width="39" alt=""></td><td><a href="https://github.com/thedevmark/alert-alert"><b>Alert! Alert!</b></a><br>Turn a video URL or local file into a cropped, trimmed stream alert.</td></tr>
<tr><td align="center" width="56"><img src=".github/apps/auto-iphone-uploader.svg" width="39" alt=""></td><td><a href="https://github.com/thedevmark/auto-iphone-uploader"><b>Auto iPhone Uploader</b></a><br>Write a video's title and captions once on your PC, then post it from the real apps on your iPhone. Early preview.</td></tr>
<tr><td align="center" width="56"><img src=".github/apps/streamer-online.png" width="44" alt=""></td><td><a href="https://streamer.deutschmark.online"><b>Streamer Online</b></a><br>Build OBS scenes and browser-source overlays with connected streamer tools.</td></tr>
<tr><td align="center" width="56"><img src=".github/apps/forgetmenot.png" width="32" alt=""></td><td><a href="https://github.com/thedevmark/forgetmenot"><b>ForgetMeNot</b></a><br>A local-first Twitch bot that remembers regulars, callbacks, and stream lore.</td></tr>
</table>

<sub>All projects → <a href="https://github.com/thedevmark">github.com/thedevmark</a></sub>

## License

AGPL-3.0 — see [LICENSE](LICENSE). FilmCraft is MIT or Apache-2.0; the Clipline patch to it is distributed under AGPL-3.0 as part of this project.
