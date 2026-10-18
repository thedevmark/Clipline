# Foundation evaluation: Clipline's Twitch editor

Date: 2026-10-18. Base Clipline revision: `4b0822c0f36cb6335660ec83580fa836518e40d7`.

## Current decision

Use Filmcraft as the editor integration direction. Mark clarified that Filmcraft supplies the Premiere-style editor and Clipline should add the Twitch workflow around it. Keep Drift as a tested reserve. This is the implementation choice, not certification of a finished integration or completion of W0. Do not recreate Filmcraft's generic timeline features in a restricted PySide editor.

Both engines rendered actual local Twitch gameplay into landscape and portrait on this machine. Filmcraft additionally exposed a native compound transaction that rolled back rejected linked edits and retained composition-specific effects. Drift's tested batch retained a successful prefix after rejection. Clipline needs one atomic shared edit and one undo action, so that boundary carries more weight than feature count.

The full selection gate remains open: generalizing the proof-only Windows gateway, editor widgets entering that same gateway, cancellation/retry, recovery with autosave, representative caption styling, and bounded playback/resource measurements. A matched Premiere comparison also remains incomplete. Do not convert this decision into a Premiere-superiority claim.

## What actually ran

| Area | Filmcraft | Drift |
| --- | --- | --- |
| Pinned Windows release | v0.9.0, `4a69a038d32761f604b3c7e4845b21fb40b3cbe4` | v0.8.0, `e4a500c38002e73a161ebcc24b2cad5cf4fe2c8e` |
| Download integrity | Publisher SHA256 and GitHub digest match | Publisher SHA256 and GitHub digest match |
| Windows runtime | Native CLI, native GUI smoke/control/screenshot | Native headless stdio MCP, NVIDIA OpenGL |
| Real media | Real Twitch gameplay dual exports, native reopen | Real Twitch gameplay dual exports, facecam/gameplay layering |
| Native edit authority | Engine project; supported namespaced sequence metadata | Native project bundle |
| Captions | Editable track, SRT, title, explicit burn-in, sampled preview/export | SRT/title render; explicit clip duration and composition geometry required |
| Transitions | Centered dissolve sampled red/blend/blue, unchanged 240 frames | Native transition command accepted and rendered |
| Template reuse | New native project from template; different source replacement | Native bundle reuse/source references and authored streamer layout |
| Format edge cases | NTSC, silent input, UHD import/preview | NTSC packet spacing via numeric 59.94, silent portrait, UHD source |
| Compound linked edits | Native `Session::edit_group` split/trim, one undo each, rejection rollback; WSL and real Windows EXE | Tested batch leaves successful prefix after later failure; explicit `undo_to` restores it |
| Portrait overrides | Crop and source-local motion keyframes survive shared split/trim in native proof | Not proven through compound shared edits |
| Native proof persistence | Three native sequences and linked audio in one FCProj, supported metadata; Windows release reopens and renders it | Native reopen tested; one repeated export byte-identical |
| Crash recovery | Native proof process exits recover old/new committed revision; Windows product store tested separately | Not proven |

The tests have different coverage and are not a scorecard of equal benchmark runs. All candidate timings so far include uncontrolled contention. Candidate-specific bugs and failed attempts remain in their local receipts.

## Native authority and Windows publication

Clipline's manifest stays narrow: stable edit-unit links and lineage, source identity, Kit/version references, composition relationships and provenance. The engine owns timing, placements, effects, audio links, and its native history. The Filmcraft proof stores this manifest through `metadata.set` on the shared sequence. It does not maintain a second timing document.

The first native proof ran unmodified pinned engine source under isolated WSL Rust 1.95. Shared split and trim changed all three native sequences inside one `Session::edit_group`. Undo/redo restored the complete project. An injected rejection restored native data, metadata, active sequence and history. Process exits during mutation and before pointer publication recovered revision 2; an exit after publication recovered revision 3. Native autosave was never armed. Arbitrary reorder, speed changes and caption regeneration were not included.

The Windows published CLI reopened and rendered the resulting FCProj. Parent independently ran the native snapshot validator. Parent then published the actual revision-2 FCProj through `native/core/project_store.py` on Windows, reloaded identical bytes and SHA256, and inspected/rendered it through the independent Windows CLI. That proves native-document compatibility and Windows snapshot storage separately. The active engine session and UI are not wired to this store yet.

An isolated Rust 1.95 / verified LLVM-MinGW UCRT toolchain cross-built a real Windows engine-only executable for `x86_64-pc-windows-gnullvm`. The first launch exposed a missing `libunwind.dll`; the matching verified-toolchain DLL resolved it. The original failure and dependency hash remain in the receipts. No global compiler/runtime installation occurred. Its long-lived stdio session passed linked split/trim, single undo, redo, rejection rollback, stale revision rejection and native save/reopen. Parent independently replayed split, stale rejection and undo through the Windows executable. This proof still contains fixed one-source split/trim assumptions. The integration package must remove or clearly restrict them, require a snapshot revision, and validate the staging path before promotion. It is not a shipped production gateway.

`ProjectStore` uses immutable documents and an atomically replaced current pointer, full document fingerprints, stale-generation guards and a process writer lock. Actual child-process exits tested interruption after document write, after pending-pointer write, and after publication. A second process cannot save while another owns the lock. Old and abandoned documents remain available. These are process-interruption results; no hardware/power-loss guarantee is claimed.

## Reserve screening

- **Concat v0.2.7:** Windows installers and checksums exist. The released automation boundary requires checking GUI Remote settings or building documented source tools; a shipped MCP server was not verified. Screened, not executed.
- **MLT v7.42.0:** source release inspected; no ready GitHub Windows binary found in that screening. A practical Windows distribution/UI route remains to be costed. Screened, not executed.
- **OpenCut, Olive, Kdenlive, Shotcut, libopenshot:** retained references/watch list. No runtime comparison in this trial. Do not attribute current capabilities from the old discussion without a new check.

Filmcraft's source includes MIT and Apache license texts. Drift's repository declares GPL-3.0 and Concat declares AGPL-3.0. Those source observations do not resolve redistribution of bundled codecs, models, fonts or effects. Pin notices and dependency/model manifests before the Windows release candidate.

## Evidence

Private/local evidence root: `%LOCALAPPDATA%/clipline/evaluation/2026-10-18/`.

| Evidence | Location beneath root |
| --- | --- |
| Machine inventory | `machine.json` |
| Reproducible synthetic fixtures | `fixtures/`, stronger source-edge oracle in `fixtures-v2/` |
| Filmcraft release, commands, native projects and render checks | `filmcraft/report.md`, `filmcraft/result.json`, `filmcraft/execution.jsonl` |
| Native transaction source, build, process receipts | `filmcraft/native-transaction/` |
| Windows native gateway and parent replay | `filmcraft/native-transaction/windows-gateway-result.json`, `filmcraft/native-transaction/parent-windows-gateway-validation.json` |
| Drift release, native bundles and render checks | `drift/report.md`, `drift/result.json` |
| Parent Windows native snapshot round trip | `native-store-windows/result.json`, inspect/render command receipts |
| Premiere copy, visible-state evidence and baseline | `baseline/baseline.json`, `baseline/report.md` |
| Legacy Clipline render defects | `current-clipline-v2/`, `current-clipline-v2/final-audit/` |
| Fresh repaired renders, code fingerprint and full decoding | `current-clipline-fixed/result.json` |

Real footage and Premiere project files remain outside Git. The execution ledger preserves R01–R18 and the remaining W0–W8 work.
