# Clipline master plan: the Twitch stream-to-post editor

Date: 2026-10-18
Status: implementation authorized; Filmcraft is the chosen editor integration direction after Windows trials and Mark's clarification. W0 integration and baseline checks remain open. Track current evidence in the [execution ledger](../../evaluation/2026-10-18-execution.md).

## The goal

Make Clipline Mark's preferred editor for turning Twitch streams into finished clips: choose the moment, receive an accurate and branded editable starting point, refine one shared edit, review landscape and portrait, and export or deliver to Homebase. The next clip should reuse the Creator Kit, available sources, corrections, and assets without rebuilding a template or opening a huge session.

The ambition is to beat Mark's actual Premiere workflow on setup, caption repair, responsive editing, reuse, dual-format production, and delivery. Mark reports spending two hours per one minute of finished clip. The initial routine-workflow target is ten minutes per finished minute with an established Creator Kit, including preparation, repairs and both outputs. Measure end-to-end and hands-on time separately; record one-time Kit onboarding separately. The reported baseline is an estimate and the target is unproven. General feature-count parity with Premiere is not the success measure. Clipline still needs a capable timeline and creative control; a restricted automatic clip exporter does not satisfy the goal.

This plan consolidates the complete conversation, the [October 14 technical specification](../specs/2026-10-14-clipline-rebuild-design.md), current source inspection, primary-source research, and three independent read-only reviews. It supersedes the older specification where Creator Library ownership, Creator Kits, caption quality, and implementation order differ. Preserve the older specification's linked-edit, source-integrity, recovery, and export invariants unless this plan explicitly changes them.

Mark authorized execution of the full plan with agents on 2026-10-18. Begin with W0, then follow the dependency gates below. Paid API use, external publishing, and public release still require their applicable authorization; none is implied by a local test.

## 1. Cumulative requirements: nothing dropped

| ID | Requirement from the conversation | Plan owner and evidence |
| --- | --- | --- |
| R01 | Twitch streamer, from stream to posted clips, is the number-one direction | Inbox and preparation workflow; W2 and end-to-end benchmark |
| R02 | Twitch clip-first ingestion, optional surrounding VOD and local sources | Creator Library/source provenance; W2 |
| R03 | Automatically create editable landscape and portrait from one edit | Linked composition owner; W3 |
| R04 | A real editor using the best suitable open-source foundation | Foundation evaluation; W0; no winner inferred from a README |
| R05 | Consider Filmcraft and the alternative editors discussed | Screen Filmcraft, Drift, Concat and MLT; trial viable finalists; record elimination reasons in the ADR; OpenCut watch list |
| R06 | Decide whether to start over or build on Clipline | Selective core rebuild; section 2 |
| R07 | Huge sessions and severe lag must not follow Mark into Clipline | Archive/workset separation, proxy and cache budgets; W1/W3/performance gates |
| R08 | Stop manually recreating templates and importing the same assets | Persistent, versioned Creator Kit and shared library; W1 |
| R09 | Accurate captions with substantially less manual correction | Managed ASR/alignment, correction ownership and repair benchmark; W4 |
| R10 | Explore WhisperX and retain useful caption work | Best Local and Quick Local profiles; section 6 |
| R11 | Explore TypeSafe Jev and open-source auto-clipping workflows | Optional decision experiment, jevcut research pattern; section 7/W6 |
| R12 | A separate hook document, including top-performing hooks | [Hook and CTA playbook](../../creator/clipline-hooks-and-cta.md); measured-results rules; W5/W7 |
| R13 | Good-looking, editable subtitles | Caption design and rendering parity; W4 |
| R14 | Transitions that work, with useful creative assets | Small verified transition set and Creator Kit recipes; section 8/W5 |
| R15 | Calls to action | Per-clip goal, no CTA or one earned action; playbook/W5 |
| R16 | Reuse what we have and the best suitable open source | Reuse map and component decisions; sections 2/5 |
| R17 | Full workflow through export and content operations | Immutable export snapshots, Homebase handoff and results import; W7 |
| R18 | Orchestrate an ambitious coherent program | Single acceptance ledger, dependency-ordered work packages, review contracts; section 10 |

These requirements accumulate. A later message refines this table; it does not replace the other rows. A scope change needs an explicit recorded decision.

## 2. Rebuild versus extend: the recommendation

**Rebuild the editing core inside the Clipline product. Keep and verify the useful services, assets, behavior, and tests.** This is neither an in-place extension of the current single-source state nor a clean-slate recreation of everything.

| Option | Benefit | Structural cost | Decision |
| --- | --- | --- | --- |
| Extend the current five-stage app and ProjectState | Familiar code, quick isolated additions | Keeps one source, global caption/style state, in-memory jobs, and no durable linked-edit document | Suitable only for scoped maintenance while the replacement is developed |
| Delete Clipline and start a new editor from zero | Complete freedom | Recreates Twitch intake, facecam composition, caption tooling, identity, settings, and delivery; creates avoidable migration risk | Reject |
| New persistent core and editor, preserving fit services | Correct ownership without discarding useful work | Requires a staged migration and a tested engine integration | Recommended |

Source-confirmed reasons: `native/ui/project_state.py` owns one source and project-wide captions/formats; `native/workers.py` creates in-memory worker threads; `native/services/whisper_cpp.py` selects a small quantized English model and loses useful recognition evidence during normalization. These are concrete owners to replace or strengthen. They are not proof that all of Clipline is unsuitable.

Reuse Twitch identity/intake behavior, normalized facecam geometry, caption export behavior after timing checks, settings/assets, and Homebase delivery semantics. Port the visual language from `native/ui/theme.py` and the existing icon. Preserve source-level tests where the contract survives; rewrite tests that enforce a retired internal shape.

Keep the current app usable during development. Work in an isolated checkout when implementation begins. Preserve the existing dirty Homebase work and `.zcode/`; do not overwrite or treat them as released functionality.

## 3. Design around a streamer's working day

### The ordinary loop

1. Connect Twitch once, choose a channel, and create a Creator Kit from one representative OBS frame and the creator's existing assets.
2. After a stream, open a metadata inbox of Twitch clips and markers. Select moments; do not load the whole broadcast into an editor.
3. Prepare Selected downloads only needed media, resolves available context, generates a small moment project, applies the pinned Kit, and queues the selected caption profile.
4. Open one moment with ready landscape and portrait compositions. Review the opening, caption repair queue, payoff, framing, ending, and optional CTA.
5. Make shared timing/audio/text changes once. Keep each composition's crop, caption placement, and output-only graphics independent.
6. Export one or both outputs. Move to the next moment while verified background jobs run within the resource budget.
7. Deliver the chosen output to Homebase. Homebase owns publishing operations and post-performance records. Clipline retains the exact edit/export identity.
8. Reopen tomorrow with the same Kit, known sources, cached analysis, and saved work. No repeated template reconstruction or asset import.

The full timeline supports multiple sources/tracks, split/trim/ripple, snapping, undo/redo, titles, music, common effects, layered facecam/gameplay, keyframes, and transitions. Setup automation never removes these controls.

### Three different things must stay separate

| Owner | Persistent responsibility | Not its responsibility |
| --- | --- | --- |
| Creator Library | Catalog, original/source identity, source locations, reusable assets, provenance, analysis references, Kits | Loading every source into an active timeline |
| Stream inbox | A filter/group over moments and readiness | An ever-growing editing session |
| Moment project | One chosen moment's editable sequence, pinned Kit and source references, corrections, linked outputs | Copies of all historical footage and Kit files |

Compilation projects reference selected moment revisions and originals. They remain explicit assemblies, not an accumulating place for every daily edit. A changed child marks a compilation stale; updating it is an undoable action and cannot mutate a queued render.

## 4. Creator Kit and responsive-project architecture

### Creator Kit

A Kit contains versioned references to channel identity, fonts, logos, reusable overlays, music/SFX, optional stingers, facecam/gameplay layout profiles, subtitle styles, hook/CTA preferences, transition recipes, export/delivery defaults, and vocabulary for names/game terms. The creator imports shared assets once.

A recipe binds source media into a lightweight editable native-engine template. New projects receive a saved Kit version automatically. Editing an instance does not alter the template. Updating a Kit creates a new version; old projects and queued jobs remain pinned until explicitly updated. Applying an update preserves manual property overrides or reports a conflict.

The Kit supports several OBS scenes and games. Normalized regions can transfer across matching resolutions, but a different layout requires matching or correction. Do not identify a layout by width/height alone. Do not clone a stale facecam crop onto an unrelated scene.

If a shared asset moves, relink its stable identity once after fingerprint verification. Dependent projects resolve it through the library. Missing music or a font identifies the Kit asset and affected output; it does not make the creator manually reconstruct the template.

### Storage and edit authority

The library owns durable originals, downloaded sources, shared assets, reusable analysis, and the durable job queue. Users choose its location. Disposable proxies, waveforms, thumbnails, and decode caches have separate configurable budgets. Projects primarily store references and small edit documents. Do not copy the whole Kit or media library into each project.

The selected engine's native document remains the editing authority. A narrow Clipline manifest owns source provenance, Kit/version references, stable edit-unit links, accepted creative choices, composition relationships, and receipts. Save native document plus manifest transactionally as one generation; crash recovery must restore a matching pair. This is not a second universal timeline implementation.

Raw engine widgets and automation must enter the same observable edit/undo gateway. Stable IDs and source-local time preserve portrait overrides through shared split/trim/reorder/speed changes. Shared caption text updates both outputs; wrapping/style/position can differ. Explicit Make Independent Copy permits output-specific timing without pretending it still follows the shared edit.

Render snapshots pin project revision, original source fingerprints, Kit version, fonts, effects, transcript revision, and output recipe. Later changes cannot alter a running render. Shared-library retention considers saved/archive projects and queued renders, not only the current window. Collect Media packages the dependency closure where licensing permits. Cache cleanup cannot delete original assets, accepted corrections, or the only recovery copy.

### Prevent lag by controlling the workload

Virtualize the inbox and fetch metadata/pages lazily. Open decoders for active media and a small bounded prefetch window. Retain source proxies across projects by fingerprint/settings. Never predecode a six-hour stream because its metadata exists in the library.

Generate proxies only for needed ranges/sources and render finals from originals. Missing originals block a final rather than silently exporting proxies. Keep the editor process independent of speech-model workers. Prioritize playback, use bounded decode/frame caches, and allow only one heavy analysis/export GPU job initially. Measure contention; a thread pool alone does not protect the GPU or disk.

Hardware observed on 2026-10-18: Ryzen 7 5700X3D, RTX 4070 Ti SUPER, 32 GiB system RAM. This inventory is not a performance result. Record storage, driver, model, proxy resolution, and cold/warm cache state in the benchmark.

## 5. Open-source component decisions

These are role recommendations and evaluation priorities, not a claim that one project is universally best. Pin actual revisions, model manifests, notices, and dependency licenses before implementation or redistribution.

| Role | Recommended route | Why / qualification |
| --- | --- | --- |
| Editor foundation | Integrate [Filmcraft](https://github.com/storytold/filmcraft); retain tested [Drift](https://github.com/CutWire-Studios/Drift) as reserve | Filmcraft supplies the capable Premiere-style native editor. Clipline adds Twitch intake, Creator Library/Kits, linked outputs, caption repair and delivery. Windows native transaction proof passed; GUI interception and remaining integration gates stay explicit |
| Reserve foundations | [Concat](https://github.com/jub0t/concat), then [MLT](https://mltframework.org/) if needed | Concat documents a Rust/API editor; MLT offers a mature multitrack foundation but requires costing the editing UI/command work |
| Watch/reference | [OpenCut](https://github.com/OpenCut-app/OpenCut), Olive, Kdenlive, Shotcut, libopenshot | Prior discussion remains relevant; avoid choosing OpenCut on upcoming rewrite features or Olive on an unstable alpha promise |
| Lightweight transcription | Existing [whisper.cpp](https://github.com/ggml-org/whisper.cpp) service, improved model selection | Keep a managed low-dependency local fallback; do not preserve a small fixed model as the accuracy ceiling |
| Quality transcription/alignment | Managed [WhisperX](https://github.com/m-bain/whisperX) worker | Evaluate stronger ASR plus forced alignment and optional speaker labels against repair effort |
| Subtitle rendering | Selected engine's renderer; evaluate [libass](https://github.com/libass/libass) where needed | One definition for preview/export; do not bolt on visually different export-only captions |
| Cut proposals | Existing clip/marker anchors plus engine/FFmpeg signals; [PySceneDetect](https://github.com/Breakthrough/PySceneDetect) only if it fills a measured gap | Scene boundaries and silence are candidate evidence, not proof of a worthwhile gaming moment |
| Decision experiment | [TypeSafe Jev](https://docs.typesafe.ai/introduction), borrowing bounded-candidate ideas from [jevcut](https://github.com/VBS2004/jevcut) | Optional network model; not required for the local product |
| Additional transitions | Selected engine's built-ins first; licensed [GL Transitions](https://github.com/gl-transitions/gl-transitions) effects only after integration proof | A shader collection is not a second rendering engine |
| Hook generation | Existing Homebase local vision/report owner | Reuse evidence and creative policy instead of creating a competing Clipline generator |

Filmcraft is the chosen integration direction as of execution on October 18. Mark clarified that it supplies the Premiere-style editor; Clipline must build the streamer workflow around that editor. Reuse its native timeline and controls. Do not recreate generic editor features in a restricted PySide timeline. The executable trial supports this direction, but does not close raw-widget command ownership, autosave/preview atomicity, cancellation, caption parity, resource budgets or packaging. Continue those concrete integration checks. Further broad editor comparisons are reserve work if a material blocker changes the decision. The [foundation evaluation](../../evaluation/2026-10-18-foundation-evaluation.md) records what ran; the [existing technical spec](../specs/2026-10-14-clipline-rebuild-design.md#8-engine-evaluation-and-selection) retains the detailed acceptance checks.

Repository metadata inspected on October 18 reports BSD-2-Clause for WhisperX, MIT for jevcut, GPL-3.0 for Drift, and AGPL-3.0 for Concat. Filmcraft contains dual-license declarations that require inspecting actual files rather than relying on GitHub's single detected label. These observations do not resolve the licenses of bundled models, fonts, codecs, or effects.

## 6. Captions: accuracy and repair time before decoration

Expose Quick Local Captions and Best Local Captions. The quality profile evaluates WhisperX with a stronger model against a faster alternative on actual streams; whisper.cpp remains the lightweight fallback. Do not declare the larger model or alignment package the winner without the repair benchmark.

Pipeline:

1. Choose the speech track. Prefer an available isolated OBS microphone track over game/music/Discord mix when appropriate; preserve all original audio for the edit.
2. Extract only selected intervals with short context handles and deduplicate overlaps. Use VAD as a speech signal without deleting silent visual moments.
3. Run ASR with language selection and Kit vocabulary. Hints guide recognition; they cannot insert words that were never spoken.
4. Preserve the immutable hypothesis, model/settings, source times, stable word IDs, and available recognition evidence. Keep unknown evidence as unknown.
5. Run language-specific forced alignment. Alignment improves timing; it does not correct a wrong transcript. Mark unaligned words instead of fabricating exact karaoke timing.
6. Add optional speaker labeling. Unknown and overlapping speakers remain explicit. Diarization does not separate voices from mixed audio.
7. Present a focused repair queue: uncertain text, names, overlap, missing timing, and suspected silence hallucinations. Let the creator audition the actual span, correct one occurrence, and optionally apply an explicitly chosen spelling rule.
8. Reprocess or realign the affected span only. Preserve accepted corrections through model changes, regeneration, format changes, save/reopen, and batch export.
9. Project corrected words through trims/speed changes into each composition. Keep authored hook text and CTAs on separate text layers; never present them as spoken subtitles.

WhisperX documents problems with overlapping speech, imperfect diarization, and out-of-dictionary tokens that cannot align. Optional diarization requires separate model-access handling. These limitations must remain visible in the repair workflow. See the [upstream limitations and setup](https://github.com/m-bain/whisperX).

Package the quality worker and models through a managed, checksummed download. The streamer should not need Python/pip/CUDA setup. If the worker fails or runs out of memory, reduce batch size/release model memory, retain the edit, and offer fallback. Never replace corrected text silently. Run one heavy model job at a time until playback contention is measured.

Start with three editable subtitle recipes: clean readable captions, restrained word highlighting, and an optional reaction/emphasis treatment. Fonts, outline/background, maximum line length, colors, animation, profanity preference, safe areas, and speaker colors belong in the Kit. Render pause/gap timing correctly; do not accumulate karaoke durations as if every word were contiguous.

Export plain SRT/VTT for timing/text, styled ASS where supported, and optional burned captions. The source word/correction model remains the editing authority. The engine must show the same fonts, wrapping, timing, emphasis, and placement that it exports.

## 7. Jev: useful experiment with a bounded role

Jev returns typed choices/scores/other decisions through TypeSafe's API; it does not generate the final hook copy. An open-source integration or SDK does not establish an open-source/local model. No model weights or local Jev deployment were verified in this research. Keep the workflow complete without it. See [official introduction](https://docs.typesafe.ai/introduction).

The [October 14 Jev announcement](https://typesafe.ai/blog/jev-1-14) adds experimental image inputs and bounding boxes. The old absolute claim that Jev is text-only is outdated. These experimental modes are not the foundation for reliable gameplay tracking or caption correctness.

Useful questions: which proposed opening needs less missing context; whether a candidate contains its payoff; which approved hook fits observed evidence; whether a CTA fits the chosen goal. Code creates finite candidate IDs from real source boundaries. Jev selects among IDs; code resolves timestamps and validates ranges. Probabilities/confidence are model outputs, not measured virality or proof of correctness.

The [jevcut repository](https://github.com/VBS2004/jevcut) demonstrates transcript-based boundary judgment and reports its own evaluation. Its talking-content benchmark is not a gaming benchmark. Keep Twitch clips, manual markers, visual events, reactions, and silent clutches in the candidate pool. A transcript judge cannot reject a wordless event merely because it has no self-contained spoken sentence.

Make outbound evidence and cost explicit before use, send bounded approved evidence, pin the available model/version, cache decisions, and use typed validation. Do not call paid APIs or upload media as part of implementing this planning document. Benchmark later against deterministic proposals and creator choices on held-out gaming examples. Adopt only if selection quality or review time improves materially.

## 8. Hooks, transitions, music, and CTAs

The [companion playbook](../../creator/clipline-hooks-and-cta.md) owns hook families, examples, evidence requirements, endings, and the rules for a top-performer list. Match Mark's concise spoken voice. Give one recommendation and a small number of useful alternatives. Allow the footage or genuine reaction to be the hook with no added copy.

Source-confirmed existing work: Homebase's `lib/video/clip-hooks.ts` creates a transcript-and-frame-grounded local report. `app/api/clip-hooks/route.ts` accepts raw video, analyzes it, and removes temporary files. It is not an evidence-only API or a persistent canonical-edit service. Its current normalization requires at least five ideas, which can force filler on thin evidence.

Keep one creative-policy/report owner. A coordinated Homebase change should accept versioned evidence and return fingerprint/revision/policy provenance, optional CTA, and fewer ideas when warranted. Clipline owns accepting/applying the suggestion in its editable timeline. Do not assume sampled frames establish exact action timing. Validate placements against source time. Homebase service availability is optional; manual Kit hooks and templates work offline without a Homebase process.

Initial transition library: hard cut, short dissolve, dip, simple directional wipe/push, and a creator-owned stinger. Include music ducking, audio fades, volume controls, and reusable SFX with explicit asset references. Default to cuts where they preserve the moment; optional visual effects remain editable.

Every transition stores its boundary, duration, curve, source handles, effect version, and audio policy. The default centered transition uses available handles and preserves the intended shared timeline length. If handles are absent, offer a shorter/shifted transition or a cut. Do not silently freeze/duplicate frames, clip speech, or remove the payoff. An explicit overlap edit can shorten the timeline, but must move words/audio/markers and both outputs consistently in one undoable operation.

Verify transition endpoints/midpoint, alpha, color, audio, rational frame rates, portrait/landscape, short clips, save/reopen, and preview/export agreement. [FFmpeg xfade/acrossfade](https://ffmpeg.org/ffmpeg-filters.html#xfade) are implementation references where applicable, not a separate final-export path that can disagree with the editor. Native effect and shader support must be tested in the chosen engine.

CTA is a creator choice: none, a specific earned question, a valid continuation destination, a repeatable-series follow invitation, or a current stream/channel invitation. Use at most one action. Do not interrupt the payoff, invent a live schedule, or attach the same closing card to every clip. Pin the exact approved text and destination in the render snapshot.

## 9. What makes this better than Mark's Premiere workflow

These are proposed targets, not measured results. Use the same actual sources, intended edits, output formats, and usable Premiere template. Record hands-on time separately from download/model/render time. Do not compare a deliberately broken Premiere workflow with a warmed Clipline demo.

| Test | Ambitious release target |
| --- | --- |
| Second clip after restarting with an existing Kit | Zero manual imports of unchanged shared assets and zero template reconstruction |
| Open a recent 60-second edit | Usable preview within 3 seconds warm / 5 seconds cold, excluding missing source acquisition |
| Active clip proxy scrub | p95 preview response <=250 ms on the recorded machine/settings |
| Ordinary edit input | p95 UI acknowledgement <=100 ms; playback/audio observations recorded separately |
| Proxy playback | At least 99% of expected frames in a 60-second test; no unexplained audio gaps |
| Archive growth | 10,000 metadata moments degrade active-project open/scrub p95 by no more than 20%; source acquisition excluded and fixture inventory disclosed |
| Caption repair | At least 50% lower median repair time per finished minute than Mark's Premiere baseline, on a held-out real-stream corpus |
| Whole task | At least 50% lower median hands-on time for a matched stream-to-dual-output task, with all required creative corrections completed |
| Reuse/recovery | No corrected word, approved hook, crop override, Kit asset reference, or completed output lost across reanalysis/restart |
| Output integrity | Originals used, expected dimensions/rational frame rate/duration, sampled caption/transition parity, independent decode/probe pass |

Use at least 30 short samples spanning clean speech, game noise, shouting/laughter, Discord overlap, music, names/slang, silent action, and scene changes. Label a held-out subset before tuning. Include 1080p60 and 4K60 source cases and long-GOP local recordings. Compare current Clipline, Premiere, and candidate quality/quick profiles where applicable.

Caption evidence includes WER/proper-name errors, manually sampled onset/offset error, hallucinations during silence, missed speech, speaker errors, and seconds spent repairing. Set absolute accuracy thresholds after the baseline; a falsely precise score is not an improvement. Keep raw findings and failure cases, not only averages. Model/frame/render tests and live account/released-artifact tests establish different things.

## 10. Orchestrated delivery program

One controller owns the cumulative requirements, decision log, integration branch, and final evidence ledger. Delegates receive bounded units with inputs, outputs, dependency IDs, tests, and review scope. One writer owns each checkout. Reviewers work read-only or in separate checkouts. No delegate may change product scope, select an untested engine, weaken data preservation, spend money, or publish without the relevant authority.

| Work package | Owner | Depends on | Deliverable and exit gate |
| --- | --- | --- | --- |
| W0: baseline and foundation proof | Controller + engine engineer | None | Matched Premiere/current Clipline baseline; fixture corpus; screened foundations; one selected-engine ADR with a minimal evaluation implementation and executable Windows vertical slice |
| W1: Creator Library and Kit | Core/data engineer | W0 engine persistence boundary | Shared asset identity, Kit versioning, small project references, retention roots, transactional save/recovery; restart/relink/archive-scale tests pass |
| W2: stream inbox and intake | Intake engineer | W1 identities/job ownership | Real Twitch clip import, paging/dedupe, markers/manual inputs, local recording refs, context correspondence, partial/reconnect recovery |
| W3: editing and dual compositions | Editor engineer | W1, W2 local fixture intake | Kit instantiation, full core timeline controls, shared edits, overrides, Compare, proxy scheduling, native save/reopen/undo and performance gates |
| W4: caption quality and correction | Speech/caption engineer | W1 transcript identity; W3 preview integration | Managed Quick/Best profiles, source corrections, alignment/repair queue, styled subtitle parity, measured repair reduction |
| W5: creative recipes | Creative integration engineer | W3; W4 for subtitle styles | Hook/evidence choices, verified transitions/music/SFX, Kit CTA presets, no lost overrides; creative sample review |
| W6: optional decision experiment | Evaluation engineer | W2/W4 bounded evidence; opt-in API access | Jev versus deterministic/creator choices, cost/data receipts, silent-event coverage; adopt/reject decision, never a local release blocker |
| W7: outputs, Homebase and learning | Delivery/integration engineer | W3-W5 | Individual/batch/compilation exports, immutable snapshots, independent verify/delivery retry, versioned creative handoff, manual/authorized metrics import |
| W8: migration and release candidate | Controller + independent reviewer | W0-W5, W7 | Settings/Kit migration, recovery/rollback, clean-profile packaged Windows tests, all blocking findings resolved, matched workflow benchmark complete |

W2 and W4 can proceed on stable W1 contracts with fixtures. W5 policy work can proceed before engine effects are integrated. W6 is optional and cannot delay a complete local editor. Integration waits on actual dependencies; do not create parallel competing schemas or merge unverified branches merely because a delegate finished.

### The first vertical slice

Before a large migration, prove one real Twitch moment with one persistent Kit, known shared assets, accurate/corrected captions, landscape and portrait, a manual crop, a transition, an optional approved CTA, restart, and verified exports. Populate the archive while keeping the active edit small. The second moment must instantiate the same Kit without another asset import or template rebuild.

W0 uses a minimal persisted Kit descriptor, a fixture-backed library, an authorized existing local Twitch sample, and the candidate's native editing/persistence functions. Manually corrected evaluation captions are permitted; the managed WhisperX worker is not a W0 dependency. This proves the integration boundary without depending on the later production packages. W1-W4 complete production library ownership, migration, retention, transactional crash recovery, scheduling, and managed speech runtime. A passing prototype is not permission to ship its shortcuts as those completed systems.

Retain a single chosen prototype and its evidence. If no foundation satisfies command/undo ownership, caption/transition parity, performance and packaging, keep the existing application and return to the selection decision. Do not compensate by building a new codec stack or hiding missing behavior.

### Acceptance and handoff

For every R01-R18 row, retain implementation owner, check command/manual path, revision, evidence location, outcome, and unresolved limitations. Carry forward the technical specification's A01-A20 checks and add Kit/version/relink, archive-scale, caption-repair, silence/overlap, transition handles, creative-policy provenance, and CTA tests.

Independent reviews occur at schema/command boundaries, caption integration, creative-to-export integration, and the release candidate. A reviewer must distinguish source-confirmed from test-confirmed behavior. Do not call a locally verified media file a posted clip. Homebase folder delivery, Homebase ingestion, platform submission, and live published behavior need their own receipts.

No calendar estimate is asserted before W0 measures the integration cost. Each work package ends in working software and recorded evidence. The final product gate is a real stream-to-post working session that meets the workflow targets and makes the next day's session easier rather than larger.

## 11. Research and review record

Primary references, inspected 2026-10-18: [TypeSafe introduction](https://docs.typesafe.ai/introduction), [Jev 1.14 announcement](https://typesafe.ai/blog/jev-1-14), [jevcut](https://github.com/VBS2004/jevcut), [WhisperX](https://github.com/m-bain/whisperX), [whisper.cpp](https://github.com/ggml-org/whisper.cpp), [libass](https://github.com/libass/libass), [GL Transitions](https://github.com/gl-transitions/gl-transitions), [PySceneDetect](https://github.com/Breakthrough/PySceneDetect), and [FFmpeg filters](https://ffmpeg.org/ffmpeg-filters.html). Hook guidance is linked in the companion playbook and distinguished from channel performance evidence.

Three independent read-only reviews examined Creator Kit/library architecture, captions/Jev decisions, and hooks/transitions/CTA ownership. Their material corrections are included here: shared library above projects, pinned Kit dependencies, corrected-transcript authority, honest Jev scope, one Homebase creative owner, and explicit transition duration/handle semantics. No runtime performance, ASR accuracy, Jev account, engine build, or public publishing result is claimed.


### Current Homebase receiver boundary (source-verified 2026-10-18)

Current Homebase retired the Video Drop lane receiver. Its executable manual intake scans only `<videoDropRoot>/post`, ignores the old `.homebase.json` sidecar, and imports X/Threads items. Nonempty `<media>.txt` creates human-authored approved public revisions. Clipline must not adapt generated metadata into that TXT convention. The older copied Clipline lane delivery also has demonstrated retry/collision/sidecar ownership defects and remains preserved rather than wired into the native flow.

W7 requires a dedicated versioned native bundle receiver alongside the manual intake. It validates grouped landscape/portrait artifacts and provenance, atomically upserts the export-set and per-artifact identities, creates drafts, repairs partial imports idempotently and returns an ingestion acknowledgement. Clipline first needs a durable native export owner binding immutable snapshot, native jobs, selected compositions and independent artifact verification. A raw path or a checkpoint followed by a same-session evaluation export is insufficient. Until that owner and receiver pass, the product may report only a verified local folder delivery; it must not report Homebase ingestion or publication. No live receiver, database, watched-folder or platform action ran during this review. Source/test report: local evaluation `homebase-native-handoff/report.md`.
