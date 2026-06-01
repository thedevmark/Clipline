# Clipline → native PySide6: phased migration plan

Companion to `CLIPLINE_NATIVE_ASSESSMENT.md` (verdict + measurements) and
`ALERT_REBUILD_LESSONS.md` (catch-up notes from the sibling repo's rebuild).
Read those first.

Goal: replace `Flask + static/* + QtWebEngine` with a native PySide6 app that
reuses the Python backend (`reel.py`, `captions.py`, `ytdlp.py`, business logic
inside `app.py`). Target: drop ~415 MB → ~70 MB EXE; remove the auth-deflect
webview plumbing; ship the funnel UI as widgets.

The web app keeps running on `main` until the native branch lands. Don't gate
or feature-flag for a half-migrated app — finish the migration on a branch and
swap.

---

## Status — shipped in v0.2.0 (on `main`)

The native rewrite landed: Flask + `static/*` + QtWebEngine are gone, the EXE
dropped from ~415 MB to ~65 MB, and the webview auth-deflect plumbing is
removed. Every phase below shipped at least its minimum viable slice; the
deeper UI (drag-trim, inspector, captions, Twitch live auth) was deliberately
deferred to the v0.2.x line so v0.2.0 could ship a working funnel.

| Phase | What it covers | Status |
|---|---|---|
| 0 | Skeleton + backend decoupling (`native/services/`, `workers.py`, self-tests) | ✅ Done |
| 1 | Window chrome + nav spine (menubar, `QStackedWidget`, status bar, SVG icon) | ✅ Done |
| 2 | Ingest (local file + preview + mark in/out + export) | 🟡 Partial — URL ingest stubbed, Twitch auth not started |
| 3 | Inbox (cut list, double-click render) | 🟡 Partial — inspector, drag-trim, waveform, marker import deferred |
| 4 | Shorts (caption-runtime status + pip pointer, preset picker) | 🟡 Partial — caption *pass* + editor deferred (`captions.py` not extracted) |
| 5 | Output (longform build, format presets) | ✅ Done |
| 6 | Onboarding + dependency setup (ungated welcome + deps check + Install/Re-check) | ✅ Done — guided tour (6.4) deferred |
| 7 | Kill the web stack | ✅ Done — `static/` trimmed to icon assets only (see note) |
| 8 | Release verification (`v0.2.0` tag, downloaded-EXE self-tests) | ✅ Done |

**Deferred to v0.2.x** (tracked here, not lost):

- **URL ingest** — the Ingest URL field + "Load URL" button ship disabled
  (`native/ui/stages/ingest.py`). Wire `ytdlp.py` behind them.
- **Twitch live auth** — system-browser + loopback `QTcpServer` callback
  (Phase 2 plan). Still the open bug from the web build; no webview now, so
  the deflect is gone but the flow itself is unbuilt.
- **Inbox depth** — inspector dock, draggable trim handles, `showwavespic`
  waveform track, thumbnails, Twitch marker/clip import (Phase 3 full scope).
- **Captions** — extract `captions.py` into `native/services/`, run the pass
  on a `QThread`, and port the caption-editor dialog (Phase 4 full scope).
  Today the Shorts stage only reports runtime status and shows the pip command.
- **Guided tour** — first-clip spotlight overlay (Phase 6.4). QSettings-gating
  is allowed here because it's mid-session, not a launch screen.

**Deviation from the written plan:** Phase 7 said "delete `static/` entirely,
`datas = []`." We kept `static/favicon.ico` + `static/img/app-icon.svg` — the
native shell renders the window icon and the welcome-screen icon from them
(`Clipline.spec` datas). Everything else under `static/` (JS/HTML/CSS) is gone.

---

## Pre-flight (do once, before any phase)

1. **Sync the spike branch with main.** `spike/native-eval` predates `main`'s
   de-alert + relicense + cache fix work. Rebase or merge `main` in before
   touching code — otherwise the rewrite will re-introduce the picker, the MIT
   badge, and the cache trap.
2. **Run the PoC on a clean machine.** Regenerate `poc_sample.mp4`
   (`ffmpeg -f lavfi -i testsrc=duration=3:size=1280x720:rate=30 -f lavfi -i sine=frequency=440:duration=3 -c:v libx264 -c:a aac -pix_fmt yuv420p native/poc_sample.mp4`),
   then `python native/native_poc.py`. Expected: `RESULT: PASS`, 30+ decoded
   frames, seek lands at 1500 ms. If it fails on this machine, stop — fix that
   before designing UI. Likely cause: missing `av*.dll`/`sw*.dll` from PySide6.
3. **Decide branch strategy.** Recommend: branch `refactor/native` *from*
   `spike/native-eval` (already has the docs + CI hardening); rebase main into
   it weekly so the funnel logic on main never drifts unmerged.
4. **Wire headless self-test hooks into the app skeleton from day one.** Per
   ALERT §2 + §8, the *only* trustworthy verification is a self-test CLI on
   the downloaded released EXE. Add `--selftest`, `--selftest-deps`,
   `--selftest-batch` to the entrypoint before there's anything to test, so
   they grow with the app.

---

## Phase 0 — Project skeleton + backend decoupling

> **Status: ✅ Done (v0.2.0).** `native/services/` + `native/workers.py`
> exist; `desktop.py` carries `--selftest` / `--selftest-deps`; no Flask import.

Goal: prove the Python backend works without Flask, on a QThread, behind a
trivial Qt window. Two days, mostly subtractive.

**Scope**
- New entrypoint `clipline_native.py` (rename to `desktop.py` at the very end
  to avoid CI confusion mid-migration).
- New package `native/ui/` with `app.py` (`QApplication` + main window) and
  `workers.py` (`QObject`s with signals; moved off the GUI thread via
  `QThread`).
- Audit `app.py` for non-Flask functions: dependency check, output dir
  resolution, ffmpeg/yt-dlp discovery, port-probe (delete — no server),
  storage config. Move these into `native/services/` modules. Leave the
  Flask routes in place on main for now; we'll delete them in Phase 8.
- Import `reel.py`, `captions.py`, `ytdlp.py` as-is. If anything in them
  reaches back into Flask state, lift it out into a plain function.

**Gate (how we know it worked)**
- `QMainWindow` opens, runs one export of a 5-second sample clip end-to-end
  through `reel.py` on a `QThread`, shows progress, writes the file.
- Headless self-test: `clipline_native.py --selftest sample.mp4 out.mp4`
  exits 0.
- No `import flask`, no `requests` for self-calls, no `import` from
  `static/`.

**ALERT traps in this phase**
- §6 threading: hold a list reference to each `QThread` until `finished`
  fires; reassigning to `self.worker` GCs the running thread.
- §3 PyInstaller: edit `Clipline.spec` *now* — add ffmpeg binaries to
  `binaries=`, drop QtWebEngine hidden imports. Build a one-file EXE in
  CI from this skeleton and confirm size dropped before any UI work.

---

## Phase 1 — Window chrome + nav spine

> **Status: ✅ Done (v0.2.0).** `MainWindow` has the menubar, a 5-stage
> `QStackedWidget`, the status bar, and the SVG-rendered app icon.

Goal: the one place the nav lives. Currently the Project/Ingest/Inbox/
Shorts/Output flow is duplicated three times (menubar `desktop.py:159-555`,
workspace tabs `index.html:332-362`, step sections). Collapse to one.

**Scope**
- Single `QMenuBar` with the 5 stages (Project, Ingest, Inbox, Shorts,
  Output). Stage selection drives a `QStackedWidget` in the center.
- Status bar at the bottom with the operation rail (currently `#reel-operation-rail`).
- No panels yet — each stage is a placeholder `QLabel`.
- App icon: load `static/img/app-icon.svg` → `QIcon` via `QSvgRenderer`
  (matches what `scripts/generate_app_icon.py` already does for rasters).
  Bundle the SVG in `datas` per ALERT §3.

**Gate**
- Cmd/Ctrl+1..5 jumps between stages. Window remembers size only (not
  state — see §7 trap below).
- Grab the static UI: `mw.grab().save("phase1.png")`, eyeball it.

**ALERT traps**
- §7 trap: if you add a "skip welcome" QSettings flag, you will set it during
  dev and never see the welcome again. **Don't add launch-landing flags.**
  The welcome is shown every launch, ungated; the dependency check is shown
  every launch with ✓ statuses.
- §4 QSS: any styled `QWidget` background needs `WA_StyledBackground=True`.
  `QLabel` gets a strip behind it unless you add `QLabel { background:
  transparent; }`.

---

## Phase 2 — Ingest stage (source intake + Twitch session)

> **Status: 🟡 Partial (v0.2.0).** Local-file intake, drag-drop, preview, and
> mark in/out → export all work. The URL field + "Load URL" button ship
> **disabled**; `ytdlp.py` is not wired yet. Twitch live auth is **not built**
> — the webview deflect is gone with the web stack, but the system-browser +
> loopback callback flow below is still TODO. → v0.2.x.

Goal: load a VOD by URL or local file; the preview plays.

**Scope**
- `IngestStage` widget. URL field, "Local File" button, dropzone.
- `QMediaPlayer` + `QVideoWidget` for the preview (PoC pattern).
- Twitch session block: channel/game/session metadata, marker textbox,
  pre-roll/post-roll spinners. All keyed to a session model object the
  backend (`reel.py`) consumes — same shape as today's project JSON.
- **Twitch auth**: this is where the webview auth-deflect bug
  (`project_twitch-auth-desktop-bug`) goes away. With no webview, open the
  shared-auth URL in the system browser and receive the callback via a
  loopback `QTcpServer` listener — same pattern alert-alert ended up with.

**Gate**
- Paste a Twitch VOD URL → yt-dlp resolves it → metadata shows → preview
  plays → seek works.
- Headless: `--selftest-ingest <url>` resolves metadata and downloads N
  seconds without launching the GUI.

**ALERT traps**
- §5 preview: `setLoops(Infinite)` + `play(); pause()` to render frame 1
  without autoplaying loudly.
- §1 codec: PoC already proved H.264/AAC; if a different VOD codec fails,
  ffmpeg backend is the right place to look, not Qt.

---

## Phase 3 — Inbox stage (clip cut list + inspector)

> **Status: 🟡 Partial (v0.2.0).** Minimum viable cut list: a `QListWidget` of
> marked clips (title + range), double-click to render one via the worker.
> The inspector dock, custom delegate w/ thumbnails, `QGraphicsScene` timeline
> with draggable trim handles, `showwavespic` waveform, and Twitch
> marker/clip import are all **deferred**. → v0.2.x.

Goal: the cut list. This is the largest single piece — `reel.js` has the
inbox + inspector + drag-trim + bulk ops + import-from-Twitch glue.

**Scope**
- Left dock: project browser + project files (lists, not custom-painted).
- Center: clip list as `QListView` with a custom delegate. Each row paints
  thumbnail + title + range + state pill.
- Right dock: inspector — title, range, duration, short preset, notes,
  fades. Editing inputs writes back to the model immediately.
- Timeline strip below the preview: `QGraphicsScene` with clip windows,
  playhead, draggable trim handles (the current trim is *typed
  timestamps* — drag handles are an upgrade, not a port). Audio waveform
  generated server-side once via `ffmpeg showwavespic` (ALERT §5) and
  drawn as a pixmap track.
- "Add clip", "Import moments", "Detect moments", "Import Twitch markers",
  "Import Twitch clips" → all already exist as Python; wire to menu actions.

**Gate**
- Load a VOD, add 3 clips, drag trim handles, edit inspector fields, see
  changes reflected in preview.
- Bulk: import a Twitch VOD's markers → N clips appear in the inbox.
- Project autosave to disk works (same JSON format as today, so a project
  started in v0.1.3 can be opened by the native build during migration).

**ALERT traps**
- §4 dropshadow: don't use `QGraphicsDropShadowEffect` for clip rows; it
  looks blocky in static grabs and on highlight states. Border-radius +
  flat colors read better.
- §6 batch threading: bulk import = list of `QRunnable`s, NOT N `QThread`s.

---

## Phase 4 — Shorts polish stage (presets + captions)

> **Status: 🟡 Partial (v0.2.0).** Style/format preset picker landed (driving
> `export_presets.py`). The Shorts stage reports caption-runtime status
> (faster-whisper / torch / pyannote) and shows the pip command, but does
> **not** run the pass — `captions.py` is not yet extracted into
> `native/services/`. Caption editor dialog and facecam guide overlay
> **deferred**. → v0.2.x.

Goal: prep one clip or the whole inbox into shorts; run captions.

**Scope**
- Preset picker (Gameplay Focus / Facecam Top / Baked Text Punch). Each
  preset is a `reel.py` recipe — UI just chooses; backend does the work.
- Facecam guide: draggable rect overlay on the preview (`QGraphicsRectItem`
  on top of the video item). Save preset positions per channel as today.
- Caption pass: button kicks off `captions.py` on a `QThread`; live log to
  status bar; ASS/SRT result openable by the caption editor.
- Caption editor: separate `QDialog`, table of segments, speaker color
  picker, burn-in toggle. This replaces `caption-editor.js` (28 KB).
- Runtime status card: shows `faster-whisper` / `torch` / `pyannote`
  installed state with ✓ / Install button. Same shape as today's
  dependency panel, just widgets.

**Gate**
- Prep one clip → short renders → caption pass runs → caption editor
  opens the result.
- Bulk prep all → N renders → progress is per-clip, not per-batch.

**ALERT traps**
- §5 `tpad`+`apad` deadlock for freeze-frame end-buffer — pad video only.
- §9 keyboard shortcuts: `Space` play/pause, `I`/`O` set trim, `[`/`]`
  nudge — already documented in `index.html`. Implement as `QShortcut`
  on the preview widget.

---

## Phase 5 — Output stage (the funnel hero)

> **Status: ✅ Done (v0.2.0).** `OutputStage` surfaces "Build Longform" and
> "Render all" as primary actions and drives `reel.py`'s longform pipeline +
> format presets. Per-stage render progress is coarse (single bar) — fine.

Goal: the long-form-from-shorts pivot. This is the funnel goal — `reel.py`
already has the longform clone pipeline (`reel.py:1653-1705`).

**Scope**
- Queue list of prepared shorts with checkboxes (queue / skip). Bulk
  "Queue all prepared" / "Skip all prepared".
- "Build Longform Project" button — **hero action, primary CTA**. This is
  what `createLongformVersion()` does in `reel.js:4704` today, buried at
  `desktop.py:501`. In the native UI, surface it as the main affordance,
  not a menu item three levels down.
- Format presets: Shorts/Reels (9:16), 4:5 Feed, Square, 16:9 Landscape.
  All drive `reel.py`'s export preset name — no new logic.
- Render queue: progress per stage (download / stitch / caption / encode).

**Gate**
- Build a project with 3 prepared shorts → queue all → "Build Longform
  Project" → second project appears in browser → its render produces a
  16:9 longform MP4 stitched from the shorts.

---

## Phase 6 — Onboarding + dependency setup

> **Status: ✅ Done (v0.2.0).** Welcome + dependency check are shown every
> launch, ungated (no QSettings flag). Missing required tools now show an
> **Install** button (winget) + a **Re-check** that re-scans without relaunch.
> The first-clip guided tour (6.4) is **deferred** — QSettings-gating is fine
> there since it's mid-session, not a launch screen. → v0.2.x.

**Read ALERT §7 carefully before starting this phase.** This is where the
sibling repo lost the most time.

**Scope (the flow that worked for alert-alert)**
1. **Welcome screen, every launch, ungated.** No "don't show again" flag.
   Two paragraphs of pitch + "Get started".
2. **Dependency check step, every launch.** Show ffmpeg / ffprobe / yt-dlp
   as rows with ✓ statuses. If anything is missing, show consent-gated
   download (name the official sources, explicit click — ALERT §7
   non-negotiable). If everything is ✓, the screen is still shown with
   "Continue" and "Re-download" — not skipped.
3. **Paste-a-URL screen** (= the Ingest stage; first-launch routing).
4. **Guided tour fires on the first clip added.** Spotlight overlay over
   the timeline + inspector + render button. First run only — *this* can
   be QSettings-gated because it's mid-session, not launch.

**Gate**
- Fresh Windows user profile (not your dev account) → install the EXE →
  every screen appears in order. Verifies you didn't accidentally gate a
  launch screen behind a flag you set in dev (§7 trap).

**ALERT traps**
- §7 *the* trap — flag-hiding. Don't do it.
- §4 spotlight mask: if the tour's step card overlaps the spotlight hole,
  union the card's geometry back into the mask, or the card clips itself.

---

## Phase 7 — Kill the web stack

> **Status: ✅ Done (v0.2.0).** No `import flask` / `QtWebEngine` in shipped
> code (remaining hits are comments). `static/` is trimmed to the two icon
> assets the native shell renders; all legacy JS/HTML/CSS deleted.
> `clipline_native.py` was renamed to `desktop.py`. **Deviation:** `datas`
> keeps the icons rather than being empty (see Status table note).

Goal: delete the old shell. No "Flask as a fallback".

**Scope**
- Delete `app.py`'s Flask routes (keep the helper functions Phase 0
  extracted into `native/services/`).
- Delete `static/` entirely.
- Delete `desktop.py` (the webview shell). Rename `clipline_native.py` →
  `desktop.py` so the entrypoint name stays stable for users / docs.
- Update `Clipline.spec`:
  - `datas = []` (no more `static/`)
  - `binaries` already has ffmpeg from Phase 0
  - Drop QtWebEngine + QtWebEngineWidgets from `hiddenimports`
  - Drop `PySide6.QtWebEngineCore` etc.
- `requirements.txt`: drop `flask`. Keep `PySide6` (already pulled in via
  QtMultimedia anyway).
- README: replace "Browser-only mode" section with "Run from source"
  pointing to the new entrypoint.

**Gate**
- `git grep -i "flask\|webengine\|static/"` finds zero hits in shipped
  code (CHANGELOG history is fine).
- One-file EXE size: target < 100 MB, ideally ~70 MB per assessment.

---

## Phase 8 — Release verification

> **Status: ✅ Done.** `v0.2.0` is tagged on `main`; the downloaded EXE came in
> at ~65 MB with no `Qt6WebEngine*.dll` and passed the self-tests.

The expensive lesson from ALERT §0 + §2: **verify the downloaded
artifact, not your local `dist/`.**

**Scope**
- Cut a `v0.2.0-rc1` tag. CI builds and uploads `clipline.exe`.
- Download the uploaded EXE (not your local build). On a clean Windows
  profile:
  - File size in 60–100 MB band, no `Qt6WebEngine*.dll` in `_MEI*` temp.
  - Launch: welcome → deps → ingest sequence appears.
  - Run `clipline.exe --selftest sample.mp4 out.mp4` and confirm exit 0.
  - Run `clipline.exe --selftest-batch projects/sample/` and confirm.
- Apply real release notes (`gh release edit v0.2.0-rc1 --notes ...`) —
  CI's `--generate-notes` produces a bare changelog link only (ALERT §2).
- If verification passes, retag as `v0.2.0`.

**Gate**
- The downloaded EXE on a clean profile passes all three self-tests.

---

## Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| `reel.js` behavior gaps not caught until late | High | Treat `reel.js` as the spec. Grep it per panel during port; check feature parity at each phase gate. |
| QMediaPlayer fails on some Twitch VOD codec | Medium | PoC already passes for H.264/AAC. If a real VOD fails, transcode via ffmpeg on download, not in the player. |
| CI clobbers the native EXE with a stray web build | Medium | ALERT §2. The release.yml on this branch already builds whatever spec is at the TAGGED commit. Verify CI is pointing at the right spec *before* tagging. |
| Sibling alert-alert's lessons diverge from Clipline's needs | Low | §3, §5, §6, §7 are framework-level; §9 feature checklist is alert-specific (single-source utility). Port lessons, not features. |
| Twitch auth flow regresses without webview | Medium | Phase 2 — system-browser + loopback callback is the alert-alert pattern, already proven. |

---

## What's NOT in scope

- **Premiere-style multi-panel docking layout.** Red-teamed and rejected
  (`project_premiere-look-direction`) — it fights the funnel. Keep the
  5-stage spine.
- **Theming as a v1 feature.** Today's theme picker is dropped; pick one
  good theme and ship. Themes can come back after parity.
- **In-app updates.** Out of scope for v0.2.0. Users upgrade by
  downloading a new release.
- **The captioning virtualenv UX rework.** The 1-click installer flow is
  already solid in `app.py`; port the wiring, don't redesign it.

---

## Recommended cadence

Phase 0–1: 1 week (skeleton + nav). Verifiable EXE size drop by end of
this week — the strongest early signal that the leverage is real.

Phase 2–5: 3–5 weeks (the four real stages). Each phase ends in a tagged
RC the user can install on a real session and use end-to-end with the
prior phase still web. Stop after each phase, ship an RC, iterate before
moving on.

Phase 6: 1 week.

Phase 7–8: 2–3 days.

Total realistic: ~6–8 weeks of focused work. Sibling repo's rebuild took
similar; the lessons file is what compresses it.
