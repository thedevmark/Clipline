# Clipline — catch-up notes from Alert! Alert!'s native rebuild

Clipline and Alert! Alert! shared a starting point, then split. This is everything
Alert! Alert! learned the hard way going from a Flask+QtWebEngine app to a native
PySide6 app. Apply the relevant parts; skip what doesn't fit.

## 0. The single biggest meta-lesson
**Verify the artifact the USER actually gets, in the STATE they're actually in.**
Every painful bug here came from verifying a proxy instead of the real thing:
- Verified the local build, not the published release exe (CI had replaced it).
- Verified a screen on a fresh profile, not with the saved QSettings the user had.

Before claiming something works: reproduce the user's exact condition (download the
released binary; set the QSettings flags they'd have) and confirm.

## 1. Architecture: go native, drop the web stack
- Open-source **QtWebEngine ships no proprietary codecs** → `<video>` can't decode
  H.264 (DEMUXER_ERROR_NO_SUPPORTED_STREAMS). Native **QMediaPlayer +
  QGraphicsVideoItem** plays H.264 fine (Qt 6 ffmpeg backend / Media Foundation).
- Dropping WebEngine took the exe from ~262 MB → ~70 MB.
- Windows port-probe bug: do **not** set `SO_REUSEADDR` on an availability check —
  on Windows bind() then succeeds over a port another app holds (we loaded a
  stranger's localhost app). Irrelevant once you're not running a local server.

## 2. Build & release pipeline (the most expensive mistakes)
- **CI clobbers your asset.** A tag-triggered workflow that does
  `gh release upload --clobber` overwrites any manually-uploaded exe. It also builds
  whatever spec is at the TAGGED commit. We shipped the wrong (web) exe 4× because
  CI rebuilt the wrong spec and clobbered the native one. Fix: point CI at the right
  spec, and the fix must be committed BEFORE the tag is created.
- **After CI runs, download the published asset and verify it** — size, no
  `Qt6WebEngine*.dll`, and run headless self-tests on it. Never trust local `dist/`
  or `gh release view`.
- **Release notes:** CI `--generate-notes` produces only a bare "Full Changelog"
  line. Apply real notes every release: `gh release edit <tag> --notes "..."`.
- Add headless **self-test CLI hooks** and run them on the downloaded exe:
  `--selftest <vid> <out>` (playback), `--selftest-deps`, `--selftest-batch`.

## 3. PyInstaller packaging gotchas
- **Anything you load from disk at runtime MUST be in the spec `datas`** or it won't
  exist in the frozen exe. We shipped a blank app-icon because only `favicon.ico` was
  bundled, not `logo.png`. Same for any sample media. Verify by launching the exe and
  checking the extracted `_MEI*` temp dir for the file.
- **Bundle the ffmpeg media-backend DLLs** (`av*.dll`, `sw*.dll` from the PySide6
  dir) in `binaries` or QMediaPlayer won't play video in the frozen build.
- Resolve runtime paths via `sys._MEIPASS` when frozen, `__file__` otherwise.
- `*.mp4` is usually gitignored — `git add -f` bundled sample clips.

## 4. Qt Widgets / QSS gotchas (each cost a render cycle)
- **QSS `background` does NOT paint on a bare `QWidget`** unless you set
  `setAttribute(Qt.WA_StyledBackground, True)` (or give it a `paintEvent`). A full-
  screen overlay looked transparent (panels showing through) until we set this. For
  rich backdrops, just override `paintEvent` and `fillRect` with a `QRadialGradient`.
- **Label "banding":** `QMainWindow, QWidget { background: ... }` paints a strip
  behind every QLabel. Add `QLabel { background: transparent; }`.
- **Word-wrapped QLabels clip their text** if you don't constrain width — they get a
  one-line sizeHint and cut the rest. Always `setFixedWidth(...)` on wrapping labels
  in a centered column (bit us 3×: welcome sub, deps body, sources).
- **`QGraphicsDropShadowEffect` renders as a boxy halo** in static grabs and looks
  "blocky" on amber CTAs. A clean pill (border-radius ≥ half the height) with no
  shadow reads better.
- Overlays parented to the central widget need `raise_()` AND geometry set to the
  parent rect on show/resize (use a `showEvent` that calls
  `setGeometry(self.parent().rect())`).
- A spotlight/tour overlay using `setMask(full - hole)` will **clip its own step
  card** if the card overlaps the hole — union the card's geometry back into the mask
  (`region.united(QRegion(card.geometry()))`), and place the card beside tall targets.
- Parse tool version strings — don't dump them. ffmpeg `--version` returns a full
  banner; extract `\d+(?:\.\d+)+` for a clean "8.0.1".

## 5. Media / ffmpeg specifics
- **`tpad` (clone last video frame) + `apad` (pad audio) deadlocks ffmpeg.** For a
  freeze-frame end-buffer, pad video only and let audio end naturally.
- Loop the preview (`player.setLoops(Infinite)`) so it doesn't freeze on a black
  frame — but **start paused** (`play(); pause()` to render the first frame) so it
  doesn't autoplay loudly.
- Keep preview playback inside the user's trim bounds: in the position handler, if
  playing and pos ≥ trim_out, seek back to trim_in.
- Audio normalize = `loudnorm=I=-16:TP=-1.5:LRA=11`; fades = `afade` with a
  configurable duration; waveform = `showwavespic` (mono, `scale=sqrt`, bright color)
  rendered to a PNG used as a scrubber track.
- Capture the real clip title cheaply during download with yt-dlp
  `--print-to-file "%(title)s" <file>` (no extra network call) — don't show
  "clip.mp4".

## 6. Threading
- **QThread GC footgun:** reassigning `self.worker = QThread()` while the previous
  one is still finishing garbage-collects a running thread and stalls the chain.
  Retain references in a list for the duration (e.g. batch export).

## 7. Onboarding / UX architecture (and the trap we hit twice)
- **THE TRAP:** don't gate first-run screens behind a QSettings flag you set during
  development — your own testing sets the flag, then you (and the user) never see the
  screen again and report it "broken." Either don't gate launch-landing screens at
  all, or always verify with the flag set.
- Flow that finally worked: **Welcome/intro (every launch, ungated) → Get started →
  Dependency-check step (always shown, with ✓ statuses + Continue + Re-download) →
  paste-a-URL screen → guided tour fires on the first clip (first run only).**
- The dependency/setup screen should be **full-screen and branded**, shown even when
  tools are present (showing ✓), as a real step — not a dim card, not excluded.
- Mid-session (queue emptied) → return to the clean add screen, not the welcome.
- **Consent-gated third-party downloads (legal):** never silently download FFmpeg/
  yt-dlp. Show a screen, name the official sources, require an explicit click, show
  progress. This is non-negotiable.

## 8. Verification discipline that actually catches things
- **Render UI headlessly with `widget.grab().save(png)`** and look at it — you cannot
  `show()` + run a live player in automation (it blocks the event loop). Grab renders
  the static tree. For overlays, `grab()` once to force layout, then show/relayout,
  then grab again.
- **Smoke-test the real handlers, not just internals.** A refactor renamed a method
  but left a stale `signal.connect(self._old)` → the Add button crashed for everyone;
  the self-tests called the internal method directly and missed it. After any
  refactor, grep for stale `self._method` connect targets.
- Commit EVERYTHING the shipped code imports — an uncommitted helper param crashed
  the packaged installer while the local build worked.

## 9. Feature parity checklist (what Alert! Alert! ended up with)
Batch queue (per-clip crop/trim/overrides + Export All), aspect-ratio crop with
draggable box, combined scrubber (waveform + playhead + draggable trim handles),
audio/visual overrides reflected in the preview, segmented export controls
(resolution / quality / fade / fade-length / end-buffer / normalize), volume slider,
drag-and-drop, instant bundled sample clip, keyboard shortcuts, guided tour.

---

The two to emphasize most for Clipline: (1) the CI-clobber + verify-the-downloaded-asset
discipline (§2), and (2) the flag-hiding trap (§7) — both cost the most time on Alert! Alert!.
