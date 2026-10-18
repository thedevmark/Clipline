# Clipline implementation evidence and handoff

Updated: 2026-10-18. State: W0 executing. Product implementation is now authorized by Mark's instruction to execute the full master plan with agents. Public release, publishing, and paid model use remain separate boundaries.

## Ownership and preserved baseline

- Product checkout: `C:/Users/mark/.codex/worktrees/clipline-rebuild/clipline`.
- Branch: `codex/clipline-rebuild`; base: `4b0822c0f36cb6335660ec83580fa836518e40d7`.
- Original checkout: `D:/Documents/GitHub/clipline`; preserve its dirty files and `.zcode/`.
- Copied input snapshot: the three dirty Homebase UI/state files, `homebase_delivery.py`, its test, the master plan, technical spec, and hook/CTA playbook. Copying these establishes the implementation baseline, not authorship or release verification.
- Local-only large/private artifacts: `%LOCALAPPDATA%/clipline/evaluation/2026-10-18/`.
- Runtime evidence belongs beside pinned candidate builds and fixtures. Do not commit real source media, credentials, signed URLs, or private Premiere projects.

Local implementation commits (not pushed): `21942a3` pinned Filmcraft source/combined native corrections; `33847a1` source-preserving landscape exports; `9d0ed3d` shared assets/versioned Creator Kits; `b698571` committed native generations. The copied Homebase work remains unstaged and byte-identical to the original checkout. Parent and independent checks are recorded below.

## Team and write boundaries

| Agent | Role | Write boundary |
| --- | --- | --- |
| `/root` | Controller, shared fixtures/harness, product integration, acceptance ledger | Product checkout and shared local evaluation manifests |
| `/root/creator_kit_architecture` | Filmcraft executable evaluation | Local evaluation `filmcraft/` only |
| `/root/caption_decision_review` | Drift executable evaluation and reserve screening | Local evaluation `drift/` only |
| `/root/creative_workflow_review` | Premiere/current-product baseline and real-media inventory | Local evaluation `baseline/` and owned Premiere UI only |

Agents inherit the session model. The exact model identifier is not exposed in their tool results. Parent integrates files after inspecting artifacts; there is one writer in the product checkout.

## Work-package ledger

| Package | State | Evidence / next gate |
| --- | --- | --- |
| W0: baseline and engine proof | Running; executable trials and Windows native proof complete | Filmcraft advances to integration; editor widget/cancellation/resource gates remain. Premiere functional template baseline complete; matched workflow benchmark remains |
| W1: Creator Library / Kit | Persistence slice integrated; incomplete | Shared identity, versioned Kits, conflict handling and bounded metadata pages pass tests. Working-copy/native-store wiring, jobs, retention, complete migration and UI remain |
| W2: Twitch intake | Metadata persistence/API slice integrated; incomplete | Stable provenance, bounded pages and deduplication pass. Native inbox UI, durable preparation, downloads and connected account remain |
| W3: editor / linked outputs | Pending W0/W1 | Native document authority, manual override preservation, performance |
| W4: caption quality | Local worker integrated; incomplete | Source-bound immutable hypotheses/alignment and cancellation pass. Accepted corrections, native projection, managed installation and measured repair effort remain |
| W5: creative recipes | Policy ready; implementation pending | Hooks, subtitles, transitions, music/SFX, CTA |
| W6: optional Jev | Evaluation design ready; no API access used | Opt-in data/cost boundary; local product does not depend on it |
| W7: outputs / Homebase / results | Pending W3-W5 | Immutable jobs, verified exports, idempotent delivery, real analytics |
| W8: migration / release candidate | Pending | Clean Windows package, rollback, matched workflow benchmark |

## Active native integration checkpoint

The controller is still executing the full goal. The native corrections below pass their bounded Windows suites and are packaged as reproducible source inputs. Full native editor integration remains incomplete. Reuse Filmcraft's native editor. Do not replace it with the fixed W0 demo API or a restricted PySide timeline.

- Publication owner: `drift/gui-transaction-patch/filmcraft-edit-group-publication.patch`. Sixteen observer tests cover committed LiveMix/autosave, failure/unwind/nesting, journal overflow, events, source history and direct undo/redo. Original native failure assertions confirmed rejected state reached LiveMix and recovery before shutdown. Shutdown later replaced that recovery file with baseline; do not claim its final retained file still contains the rejected state. A future replay preserves the rejected bytes separately.
- Historical single-video linked baseline: `baseline/linked-engine-integration/filmcraft-linked-edit.patch`. Eighteen tests now cover native razor and regular trim, repeated cuts, UUID branch identities, source-local effects/audio/UI preservation, rollback, stale/unsupported requests, dynamic trim rejection, missing audio ownership, silent sources and malformed lineage. The shared native command entry point is `Session::execute`; the History panel also calls direct undo/redo. Unsupported move/reorder/speed/full-editor paths remain future work.
- Independent review found two linked-patch issues: `clip.link` could disconnect audio and subsequent shared edits would leave it behind; lineage validation accepted cycles/live-node parents. The author corrected both in the frozen combined source. Both corrections passed the eighteen-test Windows public command suite. Parent also caught and corrected unrelated UTF-8 corruption in the first publication patch; the refreshed patch preserves untouched Unicode exactly.
- Historical v1 combined source, test commands and hashes: `baseline/linked-engine-integration/frozen-source.json`. There is one frozen source and one shared native target cache. The observer suite ran first: sixteen passed, zero failed. The linked command suite then ran: eighteen passed, zero failed. Both returned exit zero and unchanged source hashes. The controller read both raw logs. No overlapping compiler/source writes.
- WSL stopped starting with `HCS_E_CONNECTION_TIMEOUT` before the correction compiler ran. No global WSL restart/settings change occurred. The original WSL proof remains historical evidence; this was a WSL build-runtime failure; the native Windows fallback subsequently ran the correction tests.
- Fallback: isolated official Rust 1.95 Windows GNU compiler/Cargo/std/rust-mingw, verified component hashes, total downloaded archive payload 143,935,324 bytes. A real native Windows EXE compiled and ran. The UUID/getrandom raw-dylib build later required the verified official LLVM-MinGW archive (190,836,485 bytes, SHA256 `317492c456aa27ee607a5919f1d2d38dcdc1112516a24d0bf4b00d078f52d17a`). The launcher uses its absolute `llvm-dlltool.exe` while keeping bundled GNU GCC first. A real raw-dylib Windows EXE then compiled and ran. Launcher and exact provenance: `filmcraft/windows-toolchain/run_cargo.py`, `download-plan.json`, `setup-result.json`, `README.md`. No global installation/PATH change.
- Full-workspace offline resolution pulled unrelated automation dependencies. Tests now use external harnesses against the frozen engine path. A bounded selected-graph cache fill added exact UUID 1.26.1/getrandom 0.4.3 with matching Cargo checksums. The observer build has returned to offline/locked mode, at most two compiler jobs. Resolver failures before compiler startup are retained; do not count them as executed tests.
- Experimental fixed-split process package: `filmcraft/integration-package/result.json` records twelve passing tests, including two native Windows tests. Keep this package as W0 evidence. It is not the production timeline API and is not wired into Clipline.

Next controller action: finish the patched GUI build/launch, close native save/export validation, connect matching-generation working copies, and expose the Twitch/Kit workflow through the editor. Do not claim a complete GUI, autosave integration or W3 completion from engine-only tests.

### Final layered native bundle source

The canonical patch now contains schema2 gameplay/facecam role bindings. Both roles follow shared native razor and regular trim across all three sequences. Gameplay owns native audio once; facecam adds no duplicate audio. Schema1 remains supported. The final Windows source passed **26 public command tests and16 publication observer tests**, with unchanged source hashes. The controller read both final raw logs.

A real save/reopen check initially found two one-ULP changes in portrait scale values. Enabling serde_json's exact float round-trip parser fixed them. The final strict test compares the complete native project and effects without a tolerance; split, trim, undo, redo and save/reopen all pass. Cargo.lock stays unchanged. The original differing snapshots and reports remain retained.

The final native document is24,421 bytes and references the27,801,100-byte original without copying it. Streamed import again refused whole-file reads. Published Windows renderer outputs both passed complete decode:1920x1080 and1080x1920, square pixels,60/1,210frames,3.5video seconds, stereo48kHz AAC with3.52-second padding. Source audio correlation was0.9997648513 at zero best shift and RMS ratio0.9982318. Root viewed both compositions; gameplay and the separate facecam remain visible. This is an illustrative proof layout, not final Kit styling.

Final source receipt: `baseline/linked-engine-integration/bundle-roundtrip-frozen-source.json`; native logs: `results/windows-native-roundtrip/attempt-001/` and `results/windows-native-roundtrip-observer/attempt-001/` beneath that owner; real document/exports: `real-v2.1-final/`. The one canonical product patch SHA256 is `e42ec1ed6a0717465b6414a48c3ab54b3001c5b6223b7c4571db883a9e210343`. Fresh archive preparation `filmcraft/product-source-preparation-005-v2.1/` matches all seven declared output hashes. The seventh file retains the exact tested public-command source as an upstream integration test; that newly packaged upstream test target has not yet run. The actual checks used isolated external harnesses.

The full patched GUI now builds from a fresh private copy matching all seven product pins, with two compiler jobs, the pinned lock and no optional model/downloader features. Build start is not success. Explicit save/export validation, remaining editor mutation paths, working-copy/ProjectStore wiring, full GUI acceptance, resource limits and general linked move/insert/reorder/speed support remain open. W3 is incomplete.

### Real footage through the corrected native engine

The unchanged v1 engine imported the 27,801,100-byte real-2 Twitch source through the native random-access reader. Whole-source reads were forbidden; none occurred. The reader consumed 125,480 bytes in calls no larger than 65,536 bytes, plus 2,097,152 bytes of native identity ranges. This is bounded import behavior for this source, not a large-VOD peak-memory result. Shared split/trim, undo/redo and native save/reopen passed.

Both published Windows renderer outputs decoded completely: 1920x1080 and 1080x1920, square pixels, 60/1, 210 video frames and 3.5 video seconds. Stereo 48 kHz AAC lasted 3.52 seconds including padding. The source-window audio comparison reported correlation 0.9997648513 and zero best shift at 8 kHz; this is limited alignment evidence. The controller inspected the frames: landscape preserves the source frame and embedded camera; portrait crops gameplay and lacks a separate camera. Receipts: baseline/linked-engine-integration/real-v1/. A separate schema2 bundle source is now under test for gameplay plus facecam; v1 remains frozen.

### Twitch metadata persistence slice

The library now migrates schema1 to schema2 transactionally and stores bounded, deduplicated clip metadata separately from moments/media. Stable clip identity retains broadcaster, creator and exact creation instant; a conflicting refresh rolls back the whole page. Title, views and nullable VOD availability remain refreshable. Decimal durations must be exactly representable in source microseconds. Library cursors are bounded to SQLite's signed integer range.

The new `twitch_api.clips_page` retains complete clip provenance, explicit date windows and the opaque provider cursor. The existing `list_clips` UI behavior and thumbnails remain unchanged. Tests cover migration, 257 clips across bounded pages, duplicate refresh, immutable identity conflicts (including duplicates in a new page), equivalent timezone representations, exact fractional timestamps, malformed input and forbidden media reads. Final controller check: **42 passed, 37 subtests passed** across the Twitch and Creator Library suites. Independent review reproduced three defects, verified their corrections and found no material remaining issue. Local review/probes: `baseline/twitch-metadata-review.md` and its neighboring receipts.

The read-only existing-account check stopped with `TwitchAuthError: Not connected to Twitch.` It changed no scopes/settings and downloaded no media. Receipt: `twitch-inbox/live-metadata-result.json`. The native inbox UI, preparation scheduling and connected-account acceptance remain unfinished; this slice does not close W2.

The [Twitch reference](https://dev.twitch.tv/docs/api/reference/#get-clips) confirms view-count ordering, up to100 results per page, date filters, approximately1000 results per query, and nullable VOD offsets. Future inbox sync must paginate date windows rather than label the first page “latest.” The separate [download endpoint](https://dev.twitch.tv/docs/api/reference/#get-clips-download) requires clip-management scope and returns temporary URLs. This change does not request that scope or persist those URLs.

### Integrated local caption worker

`native/services/caption_worker/` now owns the optional local machine-evidence subprocess boundary. The parent imports only standard-library code. It validates source/audio/profile bindings, bounds protocol output/logs and deadlines, owns the Windows child tree, preserves partial hypotheses, checks canonical word order/non-overlap, and verifies artifact hashes on readback. ASR pieces/probabilities and alignment scores remain separate; missing timings stay null. It has no API that overwrites accepted corrections.

Root integrated the six frozen candidate files byte for byte. The 38 protocol tests passed in the product checkout. Independent review reproduced an alignment-order/overlap hole; the corrected probe rejects it with partial evidence retained. Four earlier root review findings were also reproduced and fixed: invalid-request receipts, audio-path type, finite deadlines, and buffered short stderr messages. Original failed receipts remain retained.

The final product runtime replay registered the real source through CreatorLibrary in a private evaluation catalog, then completed ASR/alignment with the final integrated worker build `3aa9fefae6b298423ce7898ecca7f7edd826b62d84a611bcdf9634665c22f049`. Thirteen canonical words retain sixteen ASR pieces; all thirteen align to their original IDs in source order within the selected 0..18-second span. Exit0, full artifact hashes and the actual source UUID are recorded in `caption-product-integration/runs/4a2ccf91-2680-4c7c-8563-8e8abfca25c1/`. The replay took102.039 seconds under concurrent compilation; it is not an uncontended performance result. The production library remained untouched.

The profile still uses the qualified cached CPU small model and WhisperX alignment. It is an evaluation profile, not a selected quality winner. No listened reference, WER or repair-time improvement exists. Model installation, accepted transcript revisions/corrections, partial-span realignment, native subtitle projection, global scheduling/resource limits and caption UI remain unfinished. The accepted-transcript persistence prototype is the next independent W4 unit.

Parent-read combined product check after the Twitch and caption worker integrations: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q` — **167 passed, 861 subtests passed**. This does not run optional model inference; the separate real runtime receipt above supplies that bounded check.

### Pinned source preparation

`vendor/filmcraft/pin.json` now binds the verified upstream archive, combined patch and all six changed-file hashes. `scripts/engine/prepare_filmcraft.py` prepares only a fresh destination and leaves diagnostic failures intact. The first actual attempt exposed missing new-file mode headers; the second exposed inherited Windows Git autocrlf conversion. Both failures remain retained. The corrected third attempt applied cleanly, with all six file hashes identical to the Windows-tested frozen source. New-file, Unicode and exact-byte tests now pass: nine tests and seven subtests. Git attributes retain LF patch bytes. Independent source-package review approved the current boundary without P0/P1/P2 findings. Its independent hashes confirm archive, patch and all six changed files. The ZIP comment equals the pinned upstream commit; the archive endpoint is reconstructed rather than a retained original request.

Local raw tests: `drift/gui-transaction-patch/results/windows-native-external/attempt-002/` and `baseline/linked-engine-integration/results/windows-native-linked/attempt-001/`. Source receipts: `filmcraft/product-source-preparation-003/` and the final helper replay `filmcraft/product-source-preparation-004/` (embedded output-hash receipt). These inputs are not yet a complete built patched GUI. The next native slice must include gameplay and facecam roles; the v1 single-video manifest is insufficient for a layered Kit.

## Cumulative acceptance ledger

| Requirement | Current implementation evidence |
| --- | --- |
| R01 Twitch stream-to-post direction | Master plan; runtime not complete |
| R02 clip-first / context / local sources | Metadata persistence, explicit API pages/date windows and nullable VOD context pass. Live check found no saved account. Downloads/native inbox remain |
| R03 linked landscape/portrait | Native Filmcraft grouped split/trim, undo, rollback, metadata and crop/keyframe preservation pass on Windows. General editor integration remains open |
| R04 capable open-source editor | Filmcraft chosen for integration after executable Windows proof and Mark's clarification. Reuse its capable native editor; integration is unfinished |
| R05 alternative foundation coverage | Filmcraft/Drift runtime complete; Concat/MLT screened with documented automation/distribution gaps; OpenCut watch |
| R06 rebuild-versus-extend | Selective core rebuild accepted; Filmcraft chosen for integration |
| R07 lag / small projects | 10,000 moment metadata test uses bounded keyset pages without media access. UI latency and Premiere comparison remain unmeasured |
| R08 Creator Kit / asset reuse | SQLite asset/Kit/moment core integrated; Filmcraft native template instantiated and rebound to another source. No Creator Kit UI yet |
| R09 fewer caption repairs | Actual baseline corpus and manual repairs unmeasured |
| R10 WhisperX / useful caption reuse | Isolated CPU ASR+WhisperX alignment and cached-assets replay pass; quality default, correction persistence and managed packaging remain |
| R11 Jev / clipping decisions | Primary-source research; no paid requests or outbound media |
| R12 hook document / measured winners | Playbook exists; no fabricated performance rows |
| R13 editable subtitles | Candidate native SRT/title/caption renders tested; Filmcraft sampled preview/export checked. Full styled-word parity remains open |
| R14 transitions / creative assets | Filmcraft centered dissolve sampled endpoints/midpoint and unchanged frame count pass. Drift transition command/render exercised. Full recipe set remains open |
| R15 CTAs | Playbook policy; editable application pending |
| R16 reuse / suitable open source | Source reuse map and executable foundation evaluation |
| R17 exports / Homebase / learning | Copied unfinished Homebase contract as baseline; runtime verification pending |
| R18 orchestration | Isolated branch, three bounded agent units, one integration owner, evidence ledger |

Carry forward technical-spec A01-A20. A command-line import/export cannot close UI, caption accuracy, human repair-time, linked-edit transaction, or live publishing checks.

## Baseline and measurement rules

Synthetic fixtures verify media behavior only. They contain test patterns, colored edge/facecam regions, and tones; no real speech. They cannot establish gaming quality, ASR accuracy, or creator preference. Real media stays local, with fingerprints and probe receipts.

Concurrent functional tests record automation wall time with uncontrolled resource contention. They are not exclusive performance benchmarks or Mark's hands-on editing times. Schedule an uncontended run before judging latency, playback, or comparative throughput.

Do not overwrite original Premiere projects. Use an evaluation copy or a new project. Warm/cold cache conditions, versions, project scope, media fingerprints, output specification, UI automation versus observed human time, and failures must accompany each baseline result.

## Initial checks and failures

- `python -m pytest -q` failed before collection because an unrelated globally installed xonsh pytest plugin requires a Windows console. This is an environment failure, not a passing test or a demonstrated product failure. Retry with plugin autoload disabled for this process only.
- Four reproducible fixture files and probes were generated successfully: 1080p60 with audio, 1080p60000/1001 with audio, 1080x1920 without audio, and 4K60 with audio. A separate authored SRT is a rendering fixture, not a transcript.
- Candidate trials, baseline receipts, test outcomes, and the foundation ADR are added only after outputs are inspected.

## Observed results: first execution checkpoint

### Executable foundation trials

Filmcraft v0.9.0 (`4a69a038d32761f604b3c7e4845b21fb40b3cbe4`) and Drift v0.8.0 (`e4a500c38002e73a161ebcc24b2cad5cf4fe2c8e`) portable Windows downloads match both publisher checksums and GitHub asset digests. Local reports and raw receipts are in `filmcraft/` and `drift/` beneath the evidence root. This pins what ran; it does not certify dependencies or release licensing.

Filmcraft passed native import/edit/save/reopen, razor/undo/redo, dual exports, source crop layers, editable captions/titles/SRT, explicit caption burn-in, template source replacement, fingerprint-verified Unicode relink, NTSC output, silent input, UHD preview and real gameplay exports. A centered dissolve retained 240 frames; sampled red/midpoint/blue values confirmed the blend. Its CLI batch is not one undo action. Native `Session::edit_group` and supported sequence metadata are now being tested as the shared edit gateway. A separate GUI integration and crash-recovery proof still matter.

Drift passed headless Windows commands, native persistence and layered gameplay/facecam renders. Its failed compound command retained the successful prefix; `undo_to` restored it. SRT import initially extended a four-second sequence to thirty seconds until duration was explicit. Changing the canvas alone left subtitle geometry wrong. The explicit `60000/1001` string and denominator field failed schema validation; numeric 59.94 produced exact packet spacing in the tested export, with unusual average-rate metadata. These are integration obligations, not issues to hide behind a successful export.

Filmcraft's Windows native export queue cancellation/retry gate subsequently passed through published MCP. The job was encoding with 16/240 frames before cancel; cancellation completed at 32/240 and the native job reported finished with `cancelled`. Its partial output was retained and stayed unchanged during a one-second check. Retrying the same queue item produced a complete 240-frame, four-second 1920x1080 square-pixel output with successful video/audio decoding. A deliberate I/O failure followed by retry also completed a thirty-frame half-second export. The owned MCP process exited normally. Receipts: `filmcraft/cancellation/report.md` and `filmcraft/cancellation/attempt-2/`. This does not establish GUI cancellation, queue persistence across restart, uncontended speed or OS thread instrumentation.

Source inspection also confirms native `file.import` uses `import_streamed` and a host random-access `Services::reader` when available. The earlier byte-import proof is not a large-file memory test. Preserve the streaming path for long originals and measure active decode/cache bounds before closing R07.

Concat v0.2.7 has Windows packages, but its documented release automation does not match a shipped MCP claim; native GUI Remote settings or a source build are required. MLT v7.42.0 release screening found source distribution rather than a ready Windows GitHub binary. Neither reserve was executed or integrated. Filmcraft is the selected integration direction.

Mark then clarified that Filmcraft supplies the Premiere-style editor. Filmcraft is now the chosen integration direction; Drift remains reserve. Work now concentrates on Clipline's Twitch workflow and the real native integration boundaries, rather than more generic editor comparisons. Mark can provide Premiere workflow context; reported time estimates remain distinct from instrumented measurements.

### Premiere and existing Clipline

Premiere 25.1.0 is installed. A private evaluation copy of the existing project preserves the original fingerprint. The original has 78 unique referenced paths, all present. XML element counts describe structure; they are not a count of user-visible projects or proof of lag. Windows automation initially failed dialog activation. Mark opened the copy, and the agent restored the minimized window through its observed Maximize control. The existing `_TEMPLATE Copy 22` portrait gameplay/facecam/text composition played with visible audio activity. Its bounded export completed: H.264 2160x3840, 60000/1001, 1081 frames, 18.034683 seconds, AAC stereo 48 kHz. The evaluation copy was saved; the original fingerprint remains unchanged. This project/source differs from the shared gameplay fixture, so it is a functional template baseline, not a matched comparison. Open time, manual caption repair and uncontended playback metrics remain unknown.

Mark's reported routine baseline is **two hours per one minute of finished clip**. This is his workflow estimate, not an instrumented editing/export duration. The breakdown among hands-on edits, waiting, captions and rebuilding templates remains unknown. The initial engineering target is **ten minutes per finished minute** for a routine clip with an established Kit, including preparation and both outputs. Record end-to-end and hands-on time separately; do not exclude caption repairs or setup failures to reach the target. Measure one-time Kit onboarding separately. Ten minutes is a target, not a demonstrated result.

Current Clipline's whisper.cpp `base.en-q5_1` completed the 34.916-second real gameplay source with a two-thread evaluation wrapper. It produced 23 word records in 10.878 seconds including extraction. This is an uncontrolled automation duration. The transcript has no reviewed reference, so accuracy and repair savings remain unknown. Different recognition runs produced different wording. A stronger local caption evaluation is now assigned.

The bounded caption trial has now completed on matching 0..18-second real gameplay audio. Current whisper.cpp and cached faster-whisper small hypotheses were retained. An isolated Python 3.12 / WhisperX 3.8.6 / CPU Torch profile ran faster-whisper ASR plus English forced alignment; cached-assets replay passed. All 13 aligned word records have in-span bounds with interpolation disabled. No listening reference exists, so recognition/timing accuracy and repair savings remain unmeasured. This trial does not qualify large-v3, CUDA, diarization, or WhisperX's complete default ASR CLI. Python 3.14 is unsupported by this profile. PyAV 19 file-path decoding conflicted with faster-whisper 1.2.1; prepared FFmpeg-to-NumPy PCM avoided that path without vendor patches. The optional installed package payload measured about 2.02 GB plus 377.7 MB alignment weights. Pin and qualify a managed optional profile before shipping. The [caption evidence contract](caption-evidence-contract.md) records W4 ownership; local raw results are in `drift/caption-evaluation/`.

The legacy Clipline render baseline reproduced two defects: landscape applied a portrait crop first, discarding both source-edge signatures; both formats had non-square sample aspect ratios. The baseline receipts remain unchanged in `current-clipline-v2/`, including the stronger final audit that rejects pixel geometry.

The product fix fits the complete original display frame into landscape and normalizes square pixels before subtitle rendering. A rendered regression failed for all three styles before the fix and passes after it. Fresh six-second 1080p60 exports in `current-clipline-fixed/` pass dimensions, square pixels, video duration, frame count, expected audio and full decoding. Landscape retains both five-color source-edge signatures. Portrait retains the intentional crop. These tests do not establish uniform cadence, A/V synchronization or whole-timeline visual parity.

### Integrated persistence slice

`native/core/creator_library.py` owns stable asset UUIDs keyed by SHA256, verified explicit relocation, immutable Kit versions and bindings, pinned moment references, semantic property overrides, explicit three-way update conflicts and revision guards. It copies no source media. The parent reviewed the returned prototype and strengthened the SQL binding guard to reject extra historical roles. Metadata listing uses keyset pagination and cannot load media. The 10,000-moment test reads 73 bounded pages with media opens forbidden; this is correctness evidence, not an editor latency benchmark.

Parent-run combined check after source-package integration: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q` — **115 passed, 828 subtests passed**. The final source preparation helper separately passed nine tests and seven subtests, and its actual archive replay verified all six output hashes.

Earlier parent-run combined check after ProjectStore integration: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q` — **105 passed, 821 subtests passed**. After adding an anamorphic landscape regression, the affected export/store tests passed: **8 passed, 13 subtests passed**. These checks include copied baseline Homebase tests in the combined run; they do not prove live delivery. The product currently exposes no library UI. Durable jobs, backups, retention, migration and engine wiring are unfinished.

`native/core/project_store.py` now publishes immutable native documents through one atomic current pointer on Windows. Actual child-process interruption, competing writer, stale publication and corrupt-document tests pass. Parent also stored the real Filmcraft FCProj, reloaded identical bytes, and independently reopened/rendered it through the Windows release. Publication revisions advance independently of undoable native metadata and engine session counters. This proves process-interruption behavior, not power-loss durability or a wired autosave path.

See the [foundation evaluation](2026-10-18-foundation-evaluation.md) for the current decision and evidence. Filmcraft's native transaction proof now runs as a real Windows EXE. Parent independently replayed shared split, stale rejection and undo. The proof's fixed-unit assumptions and snapshot boundary require refinement before product integration. The agent is preparing that bounded package; no raw native widget bypass is accepted as a complete linked editor.

Independent review closed the fixture and export findings. Six independently rendered landscape/portrait cases and one anamorphic case passed; twelve selected persistence/store tests also passed. Receipt: `baseline/final-review-receipt.md`. The native editor integration review then identified a different issue: `Session::execute` publishes persistence/LiveMix after each nested command even within `edit_group`. Rollback restores the document but not those external observations. A concrete repro and minimal engine-owner correction are the next gate; the proof's unarmed autosave must not hide that production obligation.

### Next dependency gates

1. Preserve the reviewed source package and passing native bundle/real-media evidence. Finish the patched GUI build/launch and native save/export validation, then wire the editor. Preserve the full native editor; leave the fixed W0 demo API outside product code.
2. Export/Creator Library reviews passed with their findings corrected. Continue W1 native wiring, jobs and recovery.
3. Premiere functional template baseline is complete. Run matched measurements under exclusive resource conditions before acceptance. Preserve unknown human-time and caption-accuracy fields.
4. Wire the chosen native engine and matching-generation persistence before connecting moment editing to the new library. Complete W1 jobs/retention/recovery, then W2 intake and W3 editor.
5. Carry R01–R18 and technical A01–A20 through W4–W8. Local caption preparation can proceed independently; Jev remains optional and unused.


### Integrated accepted captions and native control connection

The final accepted-transcript candidate was integrated into `native/core/accepted_transcript.py` and `caption_schema.sql` without changing its reviewed core/DDL bytes. CreatorLibrary applies the additive caption-domain migration and exposes `library.captions` on the same SQLite connection. Catalog `user_version` remains 2; caption schema version is 1. There is no second database owner. Closing the library closes the caption store. The PyInstaller data list now includes the schema beside its module; a frozen package has not been built for this change.

Machine hypotheses and alignments remain immutable proposals. Only explicit acceptance/correction advances accepted revisions, their event history and the CAS head in one transaction. Accepted word UUIDs differ from machine IDs. Replace/split/merge preserve lineage; retime preserves the accepted ID. A spelling replacement retains its usable source interval with inherited provenance pointing to the prior word/revision. This is neither a new alignment measurement nor a new manual timing claim. Splits with unknown internal boundaries remain unresolved; a merge inherits outer bounds only when all selected words have usable timing. Missing, unresolved and interpolated timing block final projection input. New proposals cannot silently replace accepted text.

The independent reviewer inspected the frozen correction delta and ran three named Windows tests. Root then ran the adapted product tests, migration tests, existing catalog/Twitch/worker/store tests and the native-bridge tests together: **165 passed, 87 subtests passed**, exit 0, 31.14 seconds reported by pytest. This is the selected affected suite, not a new full-repository total or a latency benchmark. Receipts and exact source hashes: `accepted-caption-product-integration/product-source-receipt.json` and `combined-tests-002.log`.

The first combined run failed. Its migration test prohibited reading the newly added SQL schema as if it were media; the revised guard permits only that schema file. Several fake-worker protocol calls hit their short deadlines during compilation. Cancellation-test assertion failures then left their owner threads active during temporary-directory cleanup. The test workers now use the same isolated -S launch policy as the product, and cancellation probes always cancel/join in finally, with separate readiness and job deadlines. The final passing suite retains the strict timeout, hostile protocol, descendant termination, immutable artifact and partial-evidence checks. No production job deadline or process cleanup guarantee was weakened. The initial failures do not establish that resource contention is solved.

Root replayed the verified real 13-word worker evidence through `library.captions` using a SQLite backup from a read-only evaluation catalog. Inherited spelling timing, explicit unresolved blocking, later manual resolution, proposal replay protection, pinned revision inputs and exact database reopen all passed. The synthetic authored word REVIEWED exercises ownership; it is not a verified transcript correction. No model inference or media decoding ran. Native composition projection and listening accuracy remain unmeasured. Receipt: `accepted-caption-product-integration/product-replay-001/result.json`; raw output: `replay-raw.txt`. Production databases were not opened.

`native/services/filmcraft_bridge.py` now uses the native localhost JSONL protocol. It sends once per fresh connection, bounds finite UTF-8 JSON and reply size, enforces a total deadline, checks reply IDs, and preserves unknown outcomes with the request UUID after a lost/late/malformed response. It never automatically retries an edit. Native server timeout also means unknown, because queued UI work may still execute. All native commands continue through Session.execute. The client rejects blocking export wait=true through both engine.execute and ui.menu.invoke aliases; background exports retain native job IDs. Independent review reproduced a bypass in the first client version, and the correction probe verifies all four alias/key combinations plus native command-before-id precedence. The initial 70 ms second-call fixture was fragile under compiler contention; its replacement uses an explicit release event and a separate deadline. Controlled-clock coverage checks the total deadline without a timing-performance claim.

The patched GUI build has reached the native engine and application crates without a reported compiler error. Successful linking, actual product-client GUI smoke and native frames remain pending. A separate source-reviewed persistence candidate adds document-wide validation, guarded native save/export owners and current-project autosave protection while retaining damaged recovery copies. Its 40 public command cases and 16 observer cases await the compiler slot. Root also identified a TOCTOU gap between separate validate/save calls; the next source delta must capture validated native revision/owner inventory and save one snapshot in a single native command before ProjectStore publication. None of these pending engine changes is promoted as a tested product capability.


### Native checkpoint client and full GUI startup diagnosis

`native/services/native_checkpoint.py` now creates unique editable working copies from immutable ProjectStore generations and publishes opaque captures through generation CAS. The capture call requires native clipline.saveSnapshot, the exact working path, native revision and complete linked-owner inventory. It writes an immutable intent before the sole native call, checks captured evidence and bounded bytes, retains uncertain or competing captures, and returns an advancing working-copy token. It never supplies a committed generation path to the live editor, retries a lost command, substitutes ordinary saveCopy, marks native history clean, or claims a cross-process transaction. Automatic working-session recovery, asset/Kit resolution, GUI launch and application UI wiring remain open. Its native command is still a separate source-reviewed candidate, not part of the current qualified product pin.

Root ran the final checkpoint/store suite: **35 passed, 10 subtests passed**, exit0. This includes stale generations, a competing publication after native capture, lost replies, wrong working paths/revisions/owner inventories, empty/oversize/missing/unreadable/in-read-mutated captures, retained evidence and immutable prior files. The transport/native capture responses in these Python tests are contract fixtures. They are not a real GUI or native saveSnapshot runtime claim. Independent review found no material gateway defect; the subsequently added named capture-fault cases close its non-blocking test gaps. Exact root source/test receipt: accepted-caption-product-integration/checkpoint-source-receipt.json.

The final source-reviewed snapshot command requires exactly path, expectedProjectPath, expectedRevision and expectedLinksRevisions. It refuses an open native edit group or active trim playback, validates native identity/CAS/inventory and serializes through the native saveCopy owner in one synchronous call. It preserves working path and dirty state. Latest source: baseline/linked-engine-integration/snapshot-source; source report: snapshot-boundary-report.md. Its44 public command cases and16 observer cases were then assigned the released compiler slot. They remain pending until the root reads actual completed logs.

The full patched Filmcraft v2.1 Windows GNU GUI build completed with Cargo exit0 and unchanged Cargo.lock. The 2320.844-second duration occurred under contention and is not a product performance result. Binary SHA256:54ace29424fecc58b02e9cd3df7b4441ebb6a862838e31e01af70d4f42c8cfa5. Runtime acceptance failed: the EXE immediately exited0xC0000005 before its control port, main/logger initialization or data directory. The successful build is therefore not a working patched editor result.

The published MSVC GUI opened the same real project under the same process environment and intended private data directory. Its control channel responded and Program showed the portrait gameplay/facecam composition. Root viewed the screenshot. The process quit exit0 and the input project hash stayed unchanged. This comparison qualifies that published GUI view only; it does not qualify our linked patch or prove playback performance. Receipt/frame: filmcraft/gui-integration-build/published-comparison.{json,png}.

The first-exception LLDB trace identifies Winit's pre-main .CRT$XCU main-thread initializer. Its GetCurrentThreadId call jumps to the import-name RVA0x030B7F6C rather than a loaded function address, causing DEP. The raw import name exists at that RVA, but its older static-library import table is missing from the parsed directory. Loaded/imported DLL inspection shows Windows system libraries, without a private libgcc/libwinpthread dependency. This is concrete mixed import/linkage evidence before editor/document/graphics initialization; it is not yet a completed linker fix. Failed binary, PE imports, WER evidence, LLDB trace and fault-import-rva.json remain local under gui-integration-build/. A tiny GNU ld versus LLVM LLD mixed-import reproduction is the next diagnostic, before another broad GUI build.

Audio preparation review separately reproduced a packet-only origin gap. The source correction now validates original decoded origin and an explicit continuous sample clock before resampling/trimming, using constant-memory streamed clock evidence. Independent source review passed; the author's corrected32-test suite and real AAC extraction passed. Actual AAC priming packets begin negative while decoded PTS begins zero; the retained decoded evidence covers the prepared end. Product integration and the explicit configured-interpreter correction remain pending. No caption recognition accuracy or native caption projection claim follows from this extraction proof.
