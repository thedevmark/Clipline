# Clipline rebuild: Twitch clips to editable landscape and portrait videos

Date: 2026-10-14
Status: proposed specification; implementation and engine selection have not started.
Planning update, 2026-10-18: the [consolidated master plan](../plans/2026-10-18-clipline-master-plan.md) now owns product priorities, Creator Library/Kit ownership, caption-quality work, and delivery order. This document remains the technical baseline where the master plan does not supersede it.
Evidence baseline: Clipline commit `4b0822c0f36cb6335660ec83580fa836518e40d7`, plus the existing local Homebase delivery changes. Those local changes are not a release receipt.

## 1. Product decision

Rebuild Clipline into a local desktop video editor organized around Twitch moments. Start with the clips a streamer and their community already selected, turn each into an editable project, and automatically create landscape and portrait compositions from the same edit.

Use an existing open-source editing engine if it passes the evaluation below. Clipline owns ingestion, source identity, linked compositions, streamer layouts, preparation, review, and delivery. The selected engine owns editing mechanics, playback, composition, and rendering. Do not build new codecs or maintain several production engines.

The intended experience is: **pick the moment, make the edit, review both formats, export.** The full timeline remains available for music, overlays, cuts, titles, effects, and manual correction. Automation produces an editable starting point, never a flattened video that must be edited again.

This specification authorizes no release, upload, public publishing, or replacement of the current application. It defines the proposed rebuild and the checks that must precede a commitment to an engine.

### Required product invariants

- Twitch clips are the primary intake. VOD ranges, markers, manual timestamps, URLs, and local recordings are secondary inputs.
- One shared edit drives linked landscape and portrait compositions; framing and presentation can differ without making timing drift.
- Both compositions read the original available source, not another composition's rendered or cropped output.
- Media, projects, transcripts, and rendering stay local by default. Connecting Twitch and explicitly downloading models are network operations.
- A creator can correct every automated cut, caption, crop, and layout. Re-running automation preserves manual changes unless the creator resets them.
- A project remains editable after export and recoverable after interruption.
- Clipline completes media preparation. Homebase remains the destination for content operations and publishing workflows.

## 2. Scope of the first complete release

The first release must complete this sequence: connect Twitch, browse a date range, select multiple clips, import them, prepare both formats, edit, save/reopen, export a batch, and optionally deliver verified outputs locally to Homebase.

| Required | Included behavior |
| --- | --- |
| Clip inbox | Channel/date filters, thumbnails, title, game, duration, views, import state, search, chronological or popularity sort, multi-select, deduplication |
| Intake | Twitch clip URL, selected channel clips, local files, VOD ranges, markers/manual timestamps, optional VOD context with handles |
| Editing | Multiple sources, video/audio tracks, split, trim, ripple delete, move, snapping, undo/redo, basic transitions, titles, music gain, keyframed transforms |
| Compositions | Automatically create 16:9 and 9:16; shared timing plus composition-specific crops, facecam treatment, caption layout, and overlays |
| Captions | Local word timing, text correction, timing correction, optional speaker labels, karaoke styling, editable captions, SRT/ASS sidecars and burn-in |
| Persistence | Save/reopen, autosave, crash recovery, media relink, versioned schema, portable collection of media on request |
| Output | Individual clips, selected-clip compilation, durable queue, progress, cancel, retry, full-resolution rendering, output verification |
| Delivery | Existing Homebase lane selection and media-plus-metadata handoff, with independent render and delivery status |

Defer a public web editor, cloud storage, collaboration, plugin marketplace, camera RAW, an After Effects replacement, automatic public posting, and whole-stream AI highlight discovery. These are not prerequisites for the Twitch clip workflow.

Automatic subject tracking is an evaluated enhancement, not the sole framing method. Saved source regions and editable keyframes must work when tracking fails or is unavailable.

## 3. Current implementation and reuse boundary

The following findings are source-confirmed, not runtime verification:

| Current owner | Finding | Rebuild treatment |
| --- | --- | --- |
| `native/ui/project_state.py` | One source path, marked ranges, and project-wide style/format/caption state | Replace with a persistent, multiple-source project and linked composition model |
| `native/services/twitch_api.py` | Lists clips/VODs; normalizes a small subset of metadata and does not preserve VOD association/offset or expose pagination | Preserve useful service behavior; extend source tracking and complete date-window pagination |
| `native/services/twitch_auth.py` | Public client ID, stored session, device and loopback auth paths | Audit active callers and preserve a working desktop flow; do not assume the comments define the live route |
| `native/services/composite.py` | Normalized facecam geometry and real stacked composition | Port the geometry, saved guides, and behavior tests to the selected renderer |
| `native/services/captions.py`, `whisper_cpp.py`, `diarize.py` | Local caption/transcription and speaker services | Reuse verified processing behavior and timing contracts; replace UI coupling |
| `native/services/export_presets.py` | Style crop is applied before the output format | Replace with independent source-based compositions; a landscape output must never inherit a portrait crop |
| `native/workers.py`, `native/ui/window.py` | Background operations, per-clip export, and single-source compilation | Replace scheduling/state ownership; preserve observable behavior where it meets the new requirements |
| `native/services/homebase_delivery.py` | Local delivery with metadata revealed before the completed media file | Preserve the handoff contract; current file is untracked local work, not shipped behavior |
| `native/ui/theme.py`, `static/img/app-icon.svg` | Existing brand colors, typography roles, and canonical icon | Preserve and port the recognizable Clipline visual language |

Older migration documents and the README contain claims that exceed this model or describe retired entry points. They are historical context, not acceptance evidence. Inventory real saved formats and settings before writing an importer; no current persistent project format was established in this source inspection.

## 4. Creator workflow and interface

### A. Inbox

Open to the last project's inbox, or a useful empty inbox with Connect Twitch and Import File actions. A creator chooses a channel and date window, then selects moments. Fetching metadata does not start full-video downloads, whole-VOD analysis, or transcription.

Import Selected shows a concrete preparation recipe: landscape plus portrait, selected channel layout, caption setting, and optional context handles. Starting this action authorizes the displayed download and preparation jobs. Remember that recipe for the project; do not start expensive jobs merely because a format or preset changed.

Retain rejected and already imported entries as visible states when the filters include them. A refresh updates metadata without resetting a decision. Missing VOD context does not prevent editing an available clip.

### B. Edit

Use an editor workspace with a source/bin panel, a large program monitor, a timeline, and a contextual inspector. Place Shared Edit, Landscape, Portrait, and Compare next to the monitor. Keep one clearly labeled edit scope active.

Shared Edit contains timing, source selection, common audio, shared titles/effects, and caption text. Landscape and Portrait expose their presentation controls. Compare shows the same moment in both compositions with one transport and a shared playhead. At laptop widths, use a toggle when two monitors would become too small.

Users can split, trim, rearrange, add music or overlays, edit captions, and adjust keyframes without returning through a forced sequence of setup stages. The inbox remains accessible beside the editor. The earlier five-stage funnel is superseded for this proposed rebuild; the existing application is not changed by this document.

### C. Review and export

Review flags identify an uncertain crop, missing media, an obsolete transcript, clipped caption text, or an incomplete preparation job. Flags identify a correction in the current composition. Do not require a ceremonial approval click for an otherwise valid local export.

Export Selected lists the exact compositions, sizes, frame rates, caption treatment, destination, and optional Homebase lane. Exporting landscape only must remain possible if portrait preparation failed. A successful render and a failed Homebase copy produce separate results and separate retry actions.

### Interface constraints

Port the existing Clipline brand instead of inheriting the foundation's logo, colors, or marketing copy. Use panel boundaries for actual editing regions; avoid nested cards around every setting. Keep the timeline and video dominant.

Keyboard editing includes play/pause, source In/Out, split, undo/redo, save, timeline zoom, and frame stepping. Text editing must receive its own keys. Controls require accessible names, visible focus, logical focus order, scaling at 150% and 200%, and a non-drag way to enter crop coordinates and keyframes. Status cannot rely on color alone.

## 5. Twitch-first ingestion

### Source metadata and download policy

Preserve the clip ID, broadcaster ID, creator ID, game ID, title, creation time, duration, views, source URL, optional `video_id`, and nullable `vod_offset`. Preserve raw precision; round only for display. Clip ID is the inbox deduplication key. Two clips with overlapping footage remain distinct selected moments.

Twitch's [Get Clips](https://dev.twitch.tv/docs/api/reference/#get-clips) returns a start offset, which can be null. Its [Get Clips Download](https://dev.twitch.tv/docs/api/reference/#get-clips-download) supplies temporary landscape/portrait URLs, accepts up to ten clip IDs, and is limited to 100 requests/minute. The documented download scopes are `channel:manage:clips` or `editor:manage:clips`; the editor identity must match the authenticated user. These facts require live account validation during implementation.

For the initial product, support the connected broadcaster's channel. Request additional scope only when the creator chooses official downloading; do not confuse browsing authorization with downloading authorization. Multi-channel delegated editor management is deferred, while local-file and URL imports remain available.

Prefer an authorized official landscape download as the editable source. A Twitch portrait download is an optional reference/alternate asset, not the canonical landscape source. Refresh temporary URLs at download time; never put them in persistent project identity. A null URL produces an unavailable state, not a successful empty download.

Keep the existing yt-dlp URL route where tested and permitted by available access. It cannot bypass a permission rejection. On an authorization failure, offer reconnect or local-file import. Do not expose signed URLs, tokens, or auth query strings in logs or project files.

### VOD context

Context retrieval is opt-in, with a proposed default of five seconds before and after the selected clip. Clamp the requested range to available media. Null offsets or absent VODs produce a clip-only project with clearly bounded trim handles.

The downloaded VOD slice becomes a different source asset. Store its VOD-relative origin and a verified correspondence to the selected clip. Metadata supplies a candidate location; validate audiovisual alignment before swapping a clip for its VOD slice. If alignment cannot be established, keep the clip and offer a manual alignment action. Do not silently shift the moment.

Record source quality from the actual media probe: dimensions, rational frame rate, timestamps, audio streams, duration, codec, and color metadata. A downloaded clip is not assumed to have the original recording's resolution or frame rate. Optional local recording replacement uses an explicit alignment step and keeps the original asset recoverable.

### Intake lifecycle

An entry moves through discovered, selected, queued, downloading, probing, ready, or failed. Missing context is a capability flag, not an intake failure. A media asset becomes ready only after its completed file passes the probe and receives a content fingerprint.

Write downloads to partial files. Resume only when the source supports verified continuation; otherwise restart that download. Pagination must exhaust the selected date window, with overlap/deduplication between windows rather than an arbitrary first-page limit. Handle rate limits, reconnect, cancellation, disk-full failures, and unavailable media as distinct outcomes.

## 6. Project ownership and linked composition semantics

### One editing authority

The chosen engine's native editable document is the authority for media placement, source ranges, tracks, transitions, effects, and keyframes. Clipline adds a small manifest for source provenance, stable links, saved layout profiles, composition relationships, preparation state, and delivery receipts. Do not create a second complete neutral timeline schema alongside the native engine document.

All Clipline UI actions and automation enter the same command gateway. In the production application, raw engine editing widgets must also use that gateway for operations affecting a linked composition. Unobserved changes to a separately opened native project cannot silently become shared edits: import them as an explicit independent edit or reject the merge with a useful explanation.

The foundation evaluation must prove that shared-edit changes and manifest link updates can form one undoable operation. If an engine cannot expose the required command/event boundary without a broad rewrite, it fails this foundation requirement.

### Objects and identifiers

| Object | Required meaning |
| --- | --- |
| Project | Stable ID, schema version, selected engine/version, persisted edit revision, settings, media assets, edits, compositions, job references |
| SourceAsset | Stable ID, original/cached location, fingerprint, media probe, provenance, available trim range, optional proxy, optional VOD correspondence |
| Moment | Stable ID, Twitch/local intake identity, title/notes, selected source range, inbox decisions, associated shared edit |
| SharedEdit | Native sequence ID, stable edit-unit links, track/audio/caption content, exact time base, revision |
| EditUnit | Stable identity for a temporal placement, native clip IDs, source association, split/merge lineage; the engine owns its timing |
| Composition | Stable ID, shared edit ID, aspect ratio, native output sequence ID, presentation overrides, linked/independent mode, generated-from revision |
| LayoutProfile | Channel/layout identity, normalized facecam and gameplay regions, portrait policy, safe-area guides, caption defaults, profile version |
| Transcript | Stable word IDs, source-asset timing, corrected text, speaker assignment, enabled state, analysis version |
| ExportJob | Immutable project revision, composition ID, media fingerprints, output recipe, progress, cancellation and completion state |
| DeliveryReceipt | Export identity, destination/lane, delivered file fingerprint, metadata revision, completion/failure details |

Native timeline timing uses the selected engine's exact integer/rational types. Manifest source correspondences use rational timestamps, not rounded milliseconds. Frame-rate display such as 59.94 must preserve the actual fraction. Each project defines a sequence frame rate; mixed-rate inputs are sampled according to their presentation timestamps. Unsupported variable-rate inputs require explicit conforming with recorded mapping, not silent frame-rate relabeling.

### Linked changes

| Action | Result |
| --- | --- |
| Trim, split, move, reorder, change speed, or ripple delete in Shared Edit | Both linked compositions update in one transaction |
| Correct caption text or source word timing | Both linked compositions use the correction with independent line wrapping/placement |
| Change common music/gain or a shared title | Both compositions update |
| Change portrait crop, tracking keyframes, facecam placement, caption styling, or an output-only overlay | Only Portrait changes |
| Change landscape framing or caption placement | Only Landscape changes |
| Attempt a timing edit inside a linked output | Route to Shared Edit, or offer Make Independent Copy before the change |
| Reset an output's automatic framing | Reset the selected framing property/range only; leave caption and other overrides intact |

Link overrides to stable edit-unit IDs and source-local time. Splitting creates child IDs with lineage and maps the applicable overrides to each child. Moving a clip never changes source-local framing keyframes. Deleting a clip removes its active overrides; undo restores them. Merging incompatible overrides keeps separate edit units instead of guessing. Source replacement validates its correspondence; unresolved overrides become review flags rather than attaching to unrelated footage.

One master timeline does not need to become a flattened landscape nested sequence. Generate both presentations from the master's source placements and applicable shared effects, so portrait can isolate facecam and gameplay from the same original frame. The selected engine remains the owner of the resulting editable layers. The regeneration algorithm changes linked timing and generated properties only; it cannot delete manual presentation overrides.

Advanced output-specific retiming or restructuring uses an explicit independent copy. Preserve the parent revision and show that automatic timing updates have stopped. Ordinary crop and caption changes never require independence.

Compilations contain references to selected shared edits in a chosen order and generate both output compositions. They use original assets, not exported shorts. An updated child edit marks an existing compilation stale; Update Compilation applies changes as one undoable action, without altering an already queued export snapshot.

## 7. Automatic framing and captions

### Framing policies

Landscape defaults to fit the full available landscape frame into 16:9, with padding where needed. Filling/cropping is an explicit presentation choice. Never render landscape by widening a previously cropped portrait.

Portrait offers Gameplay Focus, Facecam Top, and Full Frame with Background. Facecam Top preserves the existing composition behavior: blurred/dimmed backdrop, gameplay region, isolated rounded facecam region, and captions in the reserved space. A missing guide must not silently imply that a facecam was isolated.

Profiles are keyed by channel and layout identity, not a single last-used guide. Detect a dimensions/layout mismatch and require selecting or correcting the profile. Profiles store normalized source regions so a matching 1080p and 4K layout share geometry. Changing a profile produces a new version; existing manually corrected compositions do not silently adopt it.

Automatic crop proposals prioritize an explicit gameplay region and creator keyframes. Tracking may suggest a source-local crop path with smoothing, movement limits, and confidence scores for each time range. Low-confidence ranges retain a safe static/full-frame composition and receive review flags. Fast cuts reset tracking. Do not chase every face, cursor, HUD animation, or subtitle.

Expose editable keyframes, a crop reset, and a preview of the entire selected range. Frame-in-frame baked facecam cannot reveal gameplay it already covered. If the source lacks recoverable image area, show the actual available composition.

### Caption model

Transcribe selected clips or acquired context ranges, never an entire VOD merely to prepare one clip. Cache raw analysis by source fingerprint/range, model, language, and analysis version. Preserve corrected text separately from regenerated analysis. Removing a caption word does not cut the video unless the creator invokes a text-based cut.

Resolve source word times through each edit unit's trim and speed mapping into timeline time. Include a word when its source-time midpoint belongs to the selected range, clamp its displayed interval to that range, and omit zero-duration results. Adjacent half-open ranges cannot duplicate the boundary word. Caption line wrapping and positions depend on each output's canvas and safe area. Store optional speaker assignments separately so relabeling speakers does not require transcription again.

Use one caption rendering definition for preview and export. If the candidate cannot reproduce Clipline's word highlighting, font, outline, and placement through its renderer, solve the gap before selection or fail that requirement. Export editable text/timing and sidecars even when burn-in is enabled. SRT contains text/timing; do not claim it preserves ASS visual styling.

Keep font resolution deterministic and offer a clear replacement when a font is missing. A 4K canvas is an output size, not a claim of newly recovered source detail. Proposed defaults are 1920x1080 landscape and 1080x1920 portrait, with source-matched sequence rate and 48 kHz audio. Optional 3840x2160 and 2160x3840 exports clearly identify upscaling when applicable.

## 8. Engine evaluation and selection

Evaluate one reusable foundation, not the number of advertised features. The descriptions below are upstream-reported capabilities, checked on 2026-10-14, not Clipline test results.

| Candidate | Reason to evaluate | Decisive evidence needed |
| --- | --- | --- |
| [Filmcraft](https://github.com/storytold/filmcraft) | Headless Rust engine, command system, replaceable UI | Windows media/playback/export, captions, presentation regeneration, sustainable integration boundary; its [gap list](https://github.com/storytold/filmcraft/blob/main/docs/gaps.md) is an evaluation input |
| [Drift](https://github.com/CutWire-Studios/Drift) | Qt/FFmpeg desktop editor; documents local captions, reframing, and agent/headless access | Real footage framing, command coverage, deterministic output, editing/manifest transactions, Windows packaging |
| [Concat](https://github.com/jub0t/concat) | Rust/GPU editor; documents local captions, multiple timelines and editing APIs; beta | Engine/UI separation, linked output behavior, export recovery and native document persistence |
| [MLT](https://mltframework.org/) | Established multitrack engine used by existing editors | Playback/rendering suitability and the full cost of building the timeline interface and command/undo owner |

[OpenCut](https://github.com/OpenCut-app/OpenCut) remains a watch candidate because its new API, MCP, and headless facilities are described as upcoming during a rewrite. Do not select it on roadmap promises. Kdenlive and Shotcut are useful reference editors and possible MLT handoff targets. libopenshot is a reserve engine candidate if the primary shortlist cannot satisfy the integration boundary.

### Identical evaluation exercise

Pin an upstream commit/release and record its license/notice/dependency obligations. Perform a clean Windows build or verify the pinned downloaded artifact. Use the same local fixtures and the same named target machine for every candidate. Retain the commands, versions, media fingerprints, logs, project files, preview captures, and exported files.

Each candidate must import a real Twitch clip, create both compositions, add an isolated facecam layer, create/edit karaoke captions, apply shared trims and speed changes, preserve a manual portrait crop, undo/redo, save/reopen, relink moved media, cancel/retry an export, and produce full-resolution landscape and portrait files. The intake downloader is shared across trials; candidates receive the same verified local media. Do not implement four Twitch clients.

First use a thin script/control bridge to prove commands. Then test a minimal embedded/custom workspace to establish that manual edits use the same observable command path. A side-by-side launch of an unrelated editor is not proof of the final Clipline UX.

| Hard gate | Required result |
| --- | --- |
| Source integrity | Landscape preserves original image area; portrait reads the same original available asset |
| Editing integration | Shared/manual/automated commands are observable; native document plus link metadata can undo, save, and recover together |
| Linked outputs | Shared changes update both outputs; portrait overrides survive trims, splits, moves, and reopen |
| Preview/export | Same composition semantics, no missing layers, matching captions/fonts/crops at sampled frames |
| Timing | Exact rational frame handling; expected output frame count; audiovisual alignment within one output frame and no cumulative drift |
| Native runtime | Real Windows playback, seek, audio, rendering, Unicode paths, spaces, moved files, and no-audio media work |
| Recovery | Crash recovery, export cancellation/retry, disk-full handling, missing media, and model unavailability preserve editable work |
| Distribution | Reproducible desktop packaging, redistributable dependencies/models/fonts, notices, and no credentials in the build |

After hard gates pass, compare measured seek/playback behavior, export throughput, memory, packaging effort, required patches, and upstream maintenance. Use a 1080p60 edit and a 4K60 source with a proxy. Proposed responsiveness target: p95 paused scrub-to-preview at most 250 ms at half resolution on the recorded target machine; record playback drops and audio underruns during a 60-second run. These are targets, not measurements. A target miss requires a documented corrective path before selection, not a fabricated passing score.

Choose one candidate in a short ADR containing the measured evidence and integration cost. If none passes, preserve the current application and report the failing requirements. Do not start a general engine framework or quietly lower the product requirements.

## 9. Runtime, persistence, and automation architecture

Use the selected foundation's native engine and UI technology where that avoids unnecessary bridging. Port the Clipline workspace into that application boundary. Reuse existing Python services behind a local process boundary during migration only where they earn their packaging cost; port them directly when their behavior and tests can be preserved. The selection ADR decides the concrete language/runtime.

The production structure has five actual responsibilities: intake services; project/command ownership; linked composition generation; the selected engine's preview/export implementation; and durable jobs/delivery. The implementation may combine modules that share an owner. It must not maintain two competing preview/export pipelines.

The command gateway provides typed operations for import, prepare, apply shared edit, set presentation override, inspect, render preview, enqueue/cancel export, save/open, and deliver. Each mutation checks expected revision and returns new revision, affected IDs, and structured errors. Repeated requests use operation IDs where they create assets/jobs. Automation and manual editing use the same gateway and undo boundaries.

Expose a local CLI/MCP interface after the core workspace works. Prefer stdio or authenticated local IPC; do not expose an unauthenticated public listener. Credentials belong to the OS credential store, not manifests, logs, or automation arguments. No global keyboard/mouse simulation is required to edit a timeline.

### Project storage

Use a `.clipline` project directory containing versioned manifest/native engine document snapshots, transcripts, media references, generated proxies/cache, and a job database. Media may stay in its original location; downloaded sources have owned project locations. Collect Media copies chosen originals into a portable project and verifies the copies. Do not duplicate large media on every save.

Save native document and manifest as one committed generation: write and validate the new small-document snapshot, then atomically advance the current-generation pointer. Retain the previous generation for recovery. An incomplete generation never becomes the active project. Autosave/recovery must use the same transaction boundary; an engine-only recovery journal is insufficient if link metadata has changed. Prototype crash injection before committing to this storage shape.

Schema upgrades create a recoverable copy and never overwrite the only readable version. A project from an unsupported newer schema opens read-only with an explanation. Media relinking verifies fingerprints and correspondence before silently accepting a replacement. Fingerprint computation is a cancellable job and is cached for unchanged owned assets.

### Jobs and immutable render snapshots

Persist jobs in a project-local SQLite queue, separate from undo history. A job records input fingerprints, project/composition revision, dependencies, recipe, and attempts. Dependencies include intake, probe, proxy, selected-range transcription, preparation, export verification, and delivery. Cheap composition generation and expensive media processing have separate states.

Use queued, running, succeeded, failed, cancelled, and interrupted states. After restart, running jobs become interrupted until reconciled. Retry only failed/interrupted work. Start with at most two downloads and one expensive transcription/export job; measure before increasing concurrency. Editing remains responsive while background work runs.

An export freezes the document and media dependencies when queued. Later edits create a newer revision and mark the queued/completed output as older; they cannot change a running render. A retry keeps the same snapshot. Render Latest creates a new job. Read originals for final rendering, never proxies; missing originals block that output instead of silently exporting proxy quality.

Write renders to unique partial paths. Completion requires a successful encoder exit, a media probe, expected dimensions/rate/duration/audio, and an independent decode check. Only then reveal the final file and mark it succeeded. Never overwrite another completed output merely because the title matches. Include moment, composition, and revision identity in output names/receipts.

Cancellation stops owned processes and preserves completed outputs. A failed verification retains diagnostics and a retry path. Generated cache can be removed without losing media, edits, manual captions, or the sole recovery snapshot.

### Homebase delivery

Preserve the current `*.homebase.json` contract and existing lanes. Add a stable export/delivery identity to local receipt tracking; extend the consumer's schema only in a separately coordinated change. Revealing metadata before the completed video must remain atomic from the consumer's perspective. A retry for an already delivered export must not create a duplicate publication candidate.

Do not assume two resolutions of the same moment should both be posted. The export plan explicitly selects which composition goes to a Homebase lane. The other composition can remain in Clipline's output folder. Delivery failure preserves the completed render and offers Retry Delivery without rendering again. A local copy receipt proves delivery to the folder, not Homebase ingestion or a platform upload.

## 10. Migration and rollout

Implement in a new isolated checkout once implementation is authorized. Preserve the current dirty Homebase changes and `.zcode/`; do not move or delete them. Keep the old application usable until the complete replacement passes its release gate. Do not publish a half-migrated application under the current release name.

| Phase | Deliverable | Exit evidence |
| --- | --- | --- |
| 0: foundation proof | Pinned candidate trials, fixture corpus, evidence bundle, selection ADR | Hard-gate results and a recorded choice; no engine selected by this specification |
| 1: persistent editing core | Native project, manifest links, command gateway, undo, transactional save/recovery | Multiple-source edit survives save/reopen, crash injection, and relink |
| 2: Twitch intake | Date-window inbox, source provenance, downloads, dedupe, VOD/marker/manual intake, optional context alignment | Live connected-account import plus local fixtures; unavailable VOD and expired URL handled correctly |
| 3: dual composition workspace | Shared Edit, Landscape, Portrait, Compare; saved guides, overrides, captions | Real clip automatically creates both outputs; editing/transcription/framing corrections survive regeneration and reopen |
| 4: complete outputs | Individual/batch exports, original-source compilation, durable jobs, verification, Homebase delivery | Batch interruption/retry and a compilation with several sources pass; delivery receipt distinct from render receipt |
| 5: compatibility and release | Settings migration, explicit legacy import where real formats exist, clean Windows installer/artifact | Downloaded packaged build passes the full creator workflow on a clean account/profile |

Import existing facecam guides, caption styles, output folders, and Homebase lane choices through a versioned settings migration. Move compatible credentials to secure storage through the app; never export them into a project. If legacy project files exist, implement a read-only parser and write a new project, reporting unsupported fields. Never fabricate a project importer for a format that exists only in an old plan.

Retire old runtime paths only after every owned consumer has moved and rollback has been exercised. The current app can reopen its existing state; the rebuild must not upgrade that state in place. Releases and public announcements require their own authorization after the reviewed build exists.

## 11. Acceptance matrix

Each check below is required evidence, not a claim that it has run. Automated fixtures cover deterministic behavior; real footage and the packaged Windows build cover decoder, GPU, audio, and product behavior.

| ID | Scenario | Passing evidence |
| --- | --- | --- |
| A01 | Import selected Twitch clips across a paginated date range twice | Exactly one inbox identity per clip; original decisions and both edits preserved |
| A02 | Download URL expires; token expires; download permission is missing | Correct refresh/reconnect/unavailable states; local work retained; no permission bypass |
| A03 | VOD absent, offset null, context shorter than handles, or slice misaligned | Available clip remains editable; no invented footage or silent time shift |
| A04 | Generate landscape and portrait from wide footage with marked edge features | Landscape retains both edge features; portrait crop uses the original source |
| A05 | Facecam layout matches, changes mid-clip, or is missing | Valid layered composition or explicit review flag; no falsely isolated facecam |
| A06 | Manual portrait keyframes followed by shared split/trim/reorder/undo | Both timings match; applicable overrides survive with correct source-local timing |
| A07 | Shared speed change and caption correction, then save/reopen | Caption words follow retimed content in both outputs; text and independent layouts retained |
| A08 | Different caption safe areas, long text, missing font, speaker relabeling | Readable previews and exports with matching font/layout; sidecars preserve supported data |
| A09 | 30/60 and 30000/1001 or 60000/1001 sources; supported VFR input | Expected output frames/timestamps, no cumulative drift, alignment within one output frame |
| A10 | Source has no audio, unusual dimensions, Unicode path, or moved location | Valid silent output or clear relink/error; no crash or silent replacement |
| A11 | Save crash between native document and manifest publication | Last committed pair recovers; no manifest/timeline revision mismatch |
| A12 | Export with proxies active, then originals missing | Originals used when present; missing originals block export rather than reduce quality silently |
| A13 | Queue both outputs, edit while exporting, cancel one, restart, retry | Immutable revision receipts; completed file retained; correct per-output resume |
| A14 | Disk fills during download/render/delivery | Failure is visible; no partial file appears as completed; retry does not lose edits |
| A15 | Build a compilation from at least three distinct original assets | Correct order/audio/captions; no re-encoded shorts used as the source |
| A16 | Homebase delivery fails, then retries twice | Render remains valid; one intended delivered asset; separate receipts and no publishing claim |
| A17 | Preview/export sampled at cut boundaries and animated crop/caption frames | Matching geometry, text, effects and alpha; decode/probe passes |
| A18 | Keyboard editing at laptop width and 150%/200% scale | All primary tasks complete; visible focus, legible controls, text fields receive text keys |
| A19 | Downloaded release artifact on clean Windows state | Twitch connection, local import, model provisioning, both edits, save/reopen and verified exports work |
| A20 | Import VOD markers/manual timestamps near media boundaries | Correct source ranges, explicit authorization/unavailability, clamped handles, deduplication by source marker identity |

The minimum real-media corpus contains gameplay with baked facecam/HUD, gameplay without facecam, a conversation clip, changing OBS scenes, muted/no-audio footage, mixed frame rates, a 4K60 source, and a clip whose VOD is unavailable. Use authorized local samples; do not invent media or benchmark results. Record actual source characteristics and distinguish fixture, source, runtime, rendered, live-account, and released-artifact evidence.

## 12. Decisions still open and next work

The engine, final implementation language, upstream fork strategy, native sequence identifiers, model packaging, and measured performance remain open until Phase 0. They are technical decisions to resolve through evidence, not reasons to postpone the product specification.

The first implementation unit is Phase 0's identical candidate exercise. Its output is an evidence-backed foundation ADR and one retained editable dual-format prototype. Do not begin the production rewrite until that exercise establishes a suitable engine and the linked-edit transaction boundary.

Completion of this specification means the product behavior and evaluation gates are reviewable. It does not mean the proposed rebuild works, upstream claims were validated, or the current application was replaced.
