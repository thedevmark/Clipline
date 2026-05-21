<p align="center"><img src="static/img/app-icon.svg" alt="Clipline" width="128"></p>

<h1 align="center">Clipline</h1>

<p align="center"><strong>Turn livestream VOD moments into shortform clips — batch crop, faster-whisper auto-captions, ASS/SRT export. A six-hour stream becomes a tray of ready-to-post shorts.</strong></p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white">
  <img src="https://img.shields.io/badge/FFmpeg-Powered-orange?logo=ffmpeg&logoColor=white">
  <img src="https://img.shields.io/badge/faster--whisper-captions-5fb0c8">
  <img src="https://img.shields.io/badge/License-MIT-yellow">
</p>

---

## What it does

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

### Captions

- 1-click caption dependency install for `faster-whisper` + `torch`.
- Managed captioning virtualenv owned by the app.
- Optional `pyannote.audio` install for diarization.
- Editable captions with speaker colors, ASS/SRT export, and burn-in control.

---

## Getting started

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

---

## Configuration

### State location

Clipline stores its settings, runtime tools, and captioning virtualenv in:

- Windows: `%LOCALAPPDATA%\clipline\`

### Environment overrides

| Variable | Default | Description |
| --- | --- | --- |
| `CLIPLINE_HOST` | `localhost` | Bind host |
| `CLIPLINE_PORT` | `3000` | Bind port |
| `CLIPLINE_SHARED_AUTH_URL` | `https://auth.deutschmark.online` | Shared Twitch auth origin |

---

## You might also like

Part of the [deutschmark](https://github.com/thedeutschmark) stream toolset — tools built to work together:

| Tool | What it is |
| --- | --- |
| **[The Stream Toolset](https://toolset.deutschmark.online)** | OBS overlays + companion apps. One login, no subscriptions. |
| **[Alert! Alert!](https://github.com/thedeutschmark/alert-alert)** | Make clean stream-alert clips from any video source. |

<sub>See everything → [github.com/thedeutschmark](https://github.com/thedeutschmark)</sub>

---

## License

MIT — see [LICENSE](LICENSE).
