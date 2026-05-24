# Clipline native-rewrite assessment

Verdict-first answer to "should Clipline be rewritten as a native Qt app instead
of the WebEngine shell?" — measured against this repo, not vibes. Proof artifacts:
`native/native_poc.py` (run it) and the measurements below.

## Position

**Do not do a full native Qt rewrite to shrink the binary.** The size win is real
and large, but a native rewrite buys it at the worst possible price: a full rewrite
of ~600 KB of mature, working editor frontend. The leverage is poor.

**If binary size is the goal, the high-ROI move is to swap the desktop shell from
QtWebEngine to an OS-provided webview (pywebview/WebView2) and keep the entire
Flask + HTML/JS app unchanged.** That captures essentially all the size win for
~1–2 days of work instead of weeks. (Risk to verify, not assumed: download hooks
and the Twitch auth-deflection logic in `desktop.py` must port to the new shell,
and WebView2's H.264 support must cover the preview — it should, Edge ships H.264.)

A native rewrite is only justified if the goal is *also* killing the Chromium
dependency entirely or deeper OS integration — and even then the work is dominated
by porting `reel.js`, not by the video preview.

## Proof: the size problem is WebEngine, and native playback replaces it

Measured in this environment (`PySide6` on `Python314`):

| Component | Size |
|---|---|
| `Qt6WebEngineCore.dll` (Chromium runtime) | **195.3 MB** |
| `icudtl.dat` | 10.0 MB |
| `qtwebengine_resources.pak` | 2.2 MB |
| `QtWebEngineProcess.exe` + locales + widgets | ~5–10 MB |
| **WebEngine total shipped** | **~210 MB** |

vs. the native multimedia stack that replaces it:

| Component | Size |
|---|---|
| `avcodec-61.dll` | 13.3 MB |
| `avformat-61.dll` | 2.5 MB |
| `Qt6Multimedia.dll` + `avutil` + `ffmpegmediaplugin` | ~3 MB |
| **QtMultimedia total** | **~19 MB** |

The current one-file build is **415 MB** (`dist/clipline.exe`); WebEngine is roughly
half of it. Dropping it for QtMultimedia removes ~190 MB.

**Native playback PoC — PASS** (`python native/native_poc.py`):
- backend: QtMultimedia / bundled FFmpeg
- LoadedMedia: True; duration reported: 3000 ms
- decoded frames delivered to `QVideoSink`: 46
- seek to 1500 ms: landed at 1500 ms
- errors: none

`QMediaPlayer` decodes the same H.264/AAC MP4 the WebEngine `<video>` plays today,
and supports the four operations the preview relies on: load, duration, frame
delivery, seek.

## Reusable vs. rewrite inventory

**Reusable as-is (pure Python backend, no UI):**
- `reel.py` (106 KB) — render/stitch/export ffmpeg orchestration
- `captions.py` (28 KB) — whisper/pyannote caption pipeline
- `ytdlp.py` (10 KB) — VOD/clip download
- Business logic inside `app.py` — tool discovery, ffmpeg env, dependency install, auth proxy

**Full rewrite (JS/HTML/CSS → Qt widgets), ~600 KB:**
- `static/js/reel.js` (**242 KB**) — `ReelMaker`: the entire editor state machine & UI
- `static/index.html` (88 KB) — all workspace panels
- `static/css/style.css` (84 KB) — → Qt stylesheets/layouts
- `static/js/app.js` (47 KB) — app shell, settings, dependency setup
- `static/js/caption-editor.js` (28 KB) — caption editing UI
- `static/js/auth.js` (19 KB) — DmAuth/Twitch flow
- `app.py` (56 KB) Flask HTTP layer — collapses to direct calls or a thin local API

## The editor-specific hard part — and why it isn't the blocker

The preview is **not** a multi-track compositor. It is a single HTML5 `<video>`
(`reel-preview-video`) driven by `currentTime` seeks, with:
- captions via a native `<track>` + `/api/reel/captions/.../vtt`
- canvas `drawImage` only for filmstrip/thumbnail grabs
- DOM/CSS overlays for facecam guide and crop box
- "sequence preview" = seeking / swapping `src` within the one element

Native equivalents are all standard Qt and the risky one (playback/seek/frames) is
proven above:
- scrubbing → `QMediaPlayer.setPosition/position` (proven)
- filmstrip → `QVideoSink` frame grabs (proven frames arrive)
- timeline lanes / waveform / markers → `QGraphicsView` or custom-painted widget
- caption overlay → custom subtitle widget synced to position (moderate; Qt's
  native subtitle support is backend-dependent — don't rely on it)

The real cost is **breadth**: dozens of workspaces (session/inbox/inspector/
captions/output), drag-drop, bulk ops, and settings panels, all currently
HTML/JS, must be rebuilt as widgets. `reel.js` alone is the bulk of that.

## Bottom line

- Size win: ~190 MB, real.
- Cheapest way to get it: OS-webview shell swap, keep the web app. Verify auth +
  download hooks port.
- Native rewrite: weeks of work dominated by porting `reel.js`; justified only if
  removing Chromium entirely / OS integration is itself the goal, not just size.
