# Clipline

**Streamer-focused video editor.** Turn stream VOD moments into shortform clips, transcribe captions, build longform derivatives, and export.

![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-Backend-green?logo=flask&logoColor=white)
![FFmpeg](https://img.shields.io/badge/FFmpeg-Powered-orange?logo=ffmpeg&logoColor=white)
![License](https://img.shields.io/badge/License-AGPL_v3-blue)

---

## What It Does

- Create multi-clip VOD projects for stream sessions.
- Pull recent Twitch VODs, Twitch markers, and Twitch clips via shared `auth.deutschmark.online` Twitch login.
- Import manual timestamps from any hotkey or marker workflow.
- Surface all those moments in a `Session Inbox`.
- Prep moments as shorts with streamer-specific presets:
  - `Gameplay Focus`
  - `Facecam Top`
  - `Baked Text Punch`
- Batch-prep a whole inbox and batch-queue prepared shorts for longform.
- Stitch clips into a preview sequence, transcribe captions, and export.
- Build a horizontal longform derivative from queued prepared shorts.
- Use a saved facecam guide instead of auto-detection for recurring stream layouts.

## Captions

- 1-click caption dependency install for `faster-whisper` + `torch`.
- Managed captioning virtualenv owned by the app.
- Optional `pyannote.audio` install for diarization.
- Editable captions with speaker colors, ASS/SRT export, and burn-in control.

---

## Quick Start

### Run from source

```bash
git clone https://github.com/thedeutschmark/clipline.git
cd clipline
pip install -r requirements.txt
python desktop.py
```

Browser-only mode:

```bash
python app.py
```

The local app runs on `http://localhost:3000` by default.

### Build the EXE

```bat
build.bat
```

Outputs `dist\clipline.exe`.

## State location

Clipline stores its settings, runtime tools, and captioning virtualenv in:

- Windows: `%LOCALAPPDATA%\clipline\`

## Environment overrides

- `CLIPLINE_HOST` — bind host (default `localhost`)
- `CLIPLINE_PORT` — bind port (default `3000`)
- `CLIPLINE_SHARED_AUTH_URL` — shared Twitch auth origin (default `https://auth.deutschmark.online`)

---

## License

AGPL-3.0 — see [LICENSE](LICENSE).
