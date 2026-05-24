# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
