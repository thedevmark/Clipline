# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-05-24

Native shell rewrite. The Flask + QtWebEngine architecture is gone; Clipline is now a pure PySide6 desktop app. EXE drops from ~415 MB to ~65 MB.

### Added
- **Native PySide6 shell** (`desktop.py`) — `QMainWindow` with a single five-stage spine (Project / Ingest / Inbox / Shorts / Output) and a real menubar (File / Stage / Help) with `Ctrl+N`, `Ctrl+O`, `Ctrl+Q`, `Ctrl+1..5`.
- **`QMediaPlayer` + `QVideoWidget` preview** on Ingest. No Chromium runtime — the same H.264/AAC media that played in the WebEngine `<video>` now decodes through PySide6's bundled FFmpeg.
- **Hotkeys**: `Space` play/pause, `I` mark in, `O` mark out, `[ / ]` nudge ±1 s, `Shift+[ / ]` nudge ±100 ms.
- **Drag-and-drop** video files onto the Ingest stage.
- **Clip inbox** (Inbox stage): list of marked ranges, double-click renders a single clip.
- **Style preset picker** — Gameplay Focus / Facecam Top / Baked Text Punch — drives ffmpeg `-vf` filter per render.
- **Output stage with funnel hero**: format preset picker (Shorts/Reels, 4:5 Feed, Square, 16:9 Landscape) and a **"Build Longform Project"** CTA that renders every clip and stitches via ffmpeg concat-demuxer into one longform deliverable.
- **`--selftest <in> <out>` and `--selftest-deps`** CLI hooks for verifying the *downloaded* release artifact (ALERT §0/§2 — never trust local `dist/`).
- **`native/workers.py`**: QThread/QObject JobRunner with continuous progress via `ffmpeg -progress pipe:1`; holds thread references for the lifetime of the job (ALERT §6 GC trap).
- **`native/services/`**: pure-Python paths, settings, tool discovery, and export presets — Flask-free; this is what keeps the frozen EXE small.
- **Brand QSS** pulled from the app icon palette (navy + teal); WA_StyledBackground=True everywhere it matters (ALERT §4 banding fix).
- **Dependency checklist on the Project stage**, shown every launch (no QSettings gate — ALERT §7 trap).
- **README hero banner** at `docs/hero.png` (2560×1200, regen via `scripts/generate_hero.py`).

### Removed
- **`app.py` (Flask routes + helpers)** — replaced by `native/services/` for what was reusable, deleted otherwise.
- **`static/`** — no more `index.html`, `app.js` (47 KB), `reel.js` (242 KB), `auth.js` (19 KB), `caption-editor.js` (28 KB), `style.css` (84 KB). Just `favicon.ico` + `img/app-icon.svg` remain for the EXE icon and window icon.
- **Old `desktop.py`** — the QtWebEngine shell. `clipline_native.py` was renamed to `desktop.py` so the entrypoint name stays stable.
- **`reel.py` and `captions.py`** — both were Flask-coupled (`register_reel_routes(app)`). Native-side reimplementation lands in v0.2.x.
- **`flask` and `waitress`** dropped from `requirements.txt`.
- **QtWebEngine + Chromium runtime** (~210 MB shipped) — gone.

### Deferred to v0.2.x
- **URL ingest via yt-dlp** — the field is visible-but-disabled on Ingest. `ytdlp.py` is still vendored; just needs wiring.
- **Caption pass UI + editable caption dialog** — Shorts stage shows the runtime status and the pip install pointer. The full editor returns once `captions.py` is reimplemented natively.
- **Twitch shared-auth integration** — the `auth.deutschmark.online` callback needs the loopback-server pattern alert-alert uses.
- **Saved facecam guide / per-channel framing presets** — UI affordances on the Ingest stage; data model lands next.
- **Project autosave to disk** — `ProjectState` is in-memory only this release.

## [0.1.3] - 2026-05-24

### Fixed
- **Stale webview cache after upgrade**: v0.1.2 upgraders still saw the old "Start With The Right Tool" picker because QtWebEngine's disk cache served the previous install's `app.js`. The desktop shell now calls `clearHttpCache()` on the default profile at startup, so a new EXE never inherits cached HTML/JS/CSS.
- **Blurry taskbar icon**: `static/favicon.ico` was a single 16x16 frame (605 bytes) because Pillow's ICO encoder ignored `append_images` when the `sizes=` kwarg was also passed. `scripts/generate_app_icon.py` now renders each frame from the SVG at native size and writes a true multi-resolution `.ico` (16 / 24 / 32 / 48 / 64 / 128 / 256, ~22 KB).

## [0.1.2] - 2026-05-24

### Removed
- **Alert Creator chooser and parallel flow**: the legacy "Start With The Right Tool" picker, the `alert` onboarding track, `chooseOnboardingMode`/`buildOnboardingChooserHtml`, the `switchMode` stub, and the stranded Alert Creator shortcuts panel section. Onboarding now goes straight to the single video editor flow.
- **Alert-alert framing**: dropped sibling-repo references from `README.md`, `ytdlp.py`, and dead tests.
- **Dead alert-creator handlers** in `static/js/app.js`.

### Added
- **Vendored `ytdlp.py`** from alert-alert as a first-class module; `app.py` audio download refactored to use it.
- **Desktop logging and crash surfacing**: persistent log files, navigation guard, server crash surfacing, end-to-end URL transparency on `loadFinished`.
- **Open Log Folder** action in the desktop menu; **Reload App** now always returns to Clipline.
- **New app icon**: filmstrip + download-arrow design.

### Changed
- **Relicensed** from MIT to AGPL-3.0.
- Race fix in `/api/status` check-then-read; added `_jobs_lock` for future use.

### Internal
- `.superpowers/` scratch dir gitignored.

## [0.1.1] - 2026-04-27

### Added
- **Tag-driven Build & Release workflow** (`.github/workflows/release.yml`). Pushing a `v*` tag builds `dist/clipline.exe` on Windows runners and uploads it to the GitHub release for that tag.
- First release with a built `dist/clipline.exe` attached.

### Changed
- Track `Clipline.spec` (was being swallowed by `*.spec` gitignore).
- Drop `rawpy`/`numpy` from `requirements.txt` (Film Lab is gone, those were stale).
- Slim `static/css/style.css`: drop alert-only and Film Lab sections (−779 lines).
- Drop Film Lab references from `static/js/app.js`.

No functional code changes vs v0.1.0.

## [0.1.0] - 2026-04-26

### Added
- Initial Clipline release. Extracted from the deutschmark Alert! Alert! repo so the streamer video editor can ship and version on its own.
- **Multi-clip VOD project workflow**: load a Twitch VOD or local file, surface markers / clips / manual moments in a session inbox, prep them as shorts with streamer presets (Gameplay Focus, Facecam Top, Baked Text Punch).
- **Shared Twitch auth** via `auth.deutschmark.online` so the toolkit and Clipline see the same connected session.
- **Caption pass** on top of `faster-whisper`, with optional `pyannote.audio` speaker diarization. ASS/SRT export and burned captions.
- **Longform derivative builder** from queued prepared shorts.
- **Saved facecam guide** per channel for stable Facecam Top framing.
- **One-click runtime install** for `ffmpeg` / `ffprobe` / `yt-dlp`.
- **One-click captioning install** into a managed virtualenv.
- **PySide6 desktop shell** with full Project / Ingest / Inbox / Shorts / Output menu bar.

State location: `%LOCALAPPDATA%\clipline\` on Windows.
