# Standalone native export service integration

The product now contains the reviewed native export client and its durable queue owner. A selected immutable native snapshot supplies the edit; the native engine owns composition ranges, clocks and render settings. The Python owner binds the snapshot, Moment, Creator Kit, source assets and accepted-caption revision, then verifies native capture, media streams and complete decode before durable completion can release resource holds.

This is service integration. Output controls and an admitted runtime manifest remain unfinished. Homebase is an optional later destination for completed files. The core Twitch → Creator Kit → editor → captions → landscape/portrait export workflow must work independently.

## Integrated source

- `native/services/native_export_client.py` is byte-identical to reviewed frozen071: SHA256 `fa4fa4ca8e07559f3e662b1190d8e040add4e61adbb984eb1582d05c6c333ecc`.
- `native/services/durable_native_exports.py` changes only the private client's import to the product namespace: SHA256 `73cbc9200628998c688e15750b040a7b3421a85e61e7f65b8afbccb97fae7396`.
- `ExportJobOutcome.execution` and `ExportOwnershipError.executions` return actual live owners. A caller must retain these capabilities before a generic background worker converts an exception to a string. Unknown cessation retains process handles, file guards and durable resource holds. A PID or persisted receipt cannot replace that owner.
- The caller must admit explicit helper, native source, Python, ffprobe, ffmpeg and gate identities. The initial gate bytes must match the runtime manifest before constructing the client. Tools are not selected through PATH discovery.
- The supported recipe is one linked Moment, full landscape/portrait compositions, software H.264, native burned captions and AAC 48 kHz stereo. The A/V policy verifies presented coverage within documented rounding/padding bounds; it does not prove packet-exact AAC-tail preservation.

The integration contains two services and three tests. It excludes the private test overlay and the separate completed-handoff implementation. Two delivery-only cases remain deferred explicitly. The relocation case retains its export-receipt assertions and excludes only its delivery assertion.

## Observed verification

Evaluation evidence is under `%LOCALAPPDATA%/clipline/evaluation/2026-10-18/filmcraft/native-export-product-integration-001/`.

1. Independent source review compared both services to frozen071 and found no service-promotion blocker. Its private candidate run passed **69 tests in 5.82 seconds**. Report: `independent-review.md`, SHA256 `764e9fbadeaaae660abd00c57eb1c4a4353c0c40f32fca23cd2feb08615dcf92`.
2. Root's initial installed test collection exposed a bare sibling import masked by the private test path. Root corrected it to `from tests.test_native_exports`. The corrected installed collection passed **69 tests in 4.84 seconds**. Evidence: `results/root-product-002/pytest.log`.
3. Root restored the two service files' exact reviewed LF bytes after Git patch application converted newlines. There was no logical code change. The complete product collection on those exact installed services then passed **593 tests and 1,035 subtests in 114.94 seconds**, exit 0. Root read the complete result. Evidence: `results/product-regression-001/pytest.log`.

The 69 focused cases include 12 real private Windows process/Job Object/read-guard cases, 28 durable storage/admission cases and 29 authored rational timing cases. Their media boundaries use fixtures. These runs do not execute a compiled native renderer or establish export performance, speech accuracy or final creative quality.

Historical actual standalone export002 remains separate evidence for its old qualified helper and frozen071 client. It produced verified real landscape and portrait files, with nine stopped owned scopes and zero remaining durable holds. This source integration does not transfer that qualification to a newly compiled binary. The unified two-helper build and its 31 factory plus seven export cases have a separate admission and receipt.

## Explicit runtime configuration

`native/services/export_runtime.py` now loads a strict, bounded schema-1 manifest with explicit helper, ffprobe, ffmpeg, Python and tool-gate identities, declared versions, engine version and native-only source-pin SHA256. Parsing reads at most 65,537 bytes and rejects oversized, duplicate, missing, extra, nonfinite or malformed settings. It performs no executable discovery or launch. Its source is exact private candidate SHA256 `82f8357059b1a57d49bf50d3942ab615869ab3ca5a53cb6a9f5e82410ce9690f`.

Client construction requires a background thread. It compares the existing client's captured gate hash to the manifest before returning the client; later execution still guards and rechecks that admitted identity. A restored file after a constructor-time swap cannot hide a captured wrong gate. The parser does not prove that an arbitrary source digest describes native code; the qualified source owner must supply that identity and its exact helper binding.

Root read the candidate source, tests and report, installed the unchanged service, and changed only the test import to the product namespace. The combined installed runtime/export collection passed **119 tests in 5.65 seconds**, exit 0. Root read the complete raw result at `filmcraft/native-export-runtime-001/results/root-product-001/pytest.log`. The 50 new manifest cases use temporary files and constructor doubles; the private omission control made both gate-swap cases fail when the comparison was removed. No actual helper, decoder or render ran. The preceding 593-test full collection predates this runtime module; it is not reported as a new full-suite result.

## Other active findings

The companion editor's old checkpoint-then-force-close path failed an independent late-edit reproduction. The Qt caller now refuses that path. A private graceful-close replacement passed 40 fixtures but then failed a focused Save As reproduction: the native app could adopt a new saved path after a checkpoint while the old working file still matched the canonical generation. That candidate remains unpromoted. Comparing the old file after process exit cannot prove the final native project identity. The actual companion replay remains disabled pending a complete native close contract.

Read-only export timing diagnosis separates **109.623 seconds in native render jobs** from the recorded **135.186-second** correctness-run interval. The current software recipe and concurrent work prevent an acceptable-speed claim. Existing engine performance counters can instrument the next controlled run before selecting a thread, NVENC or GPU-compositor change. Report: `filmcraft/native-export-performance-diagnosis-001.md`, SHA256 `f15785b18039b334428c4e05337c5ff196756b9fd1451ec1e3d53fc1068a6651`. No new render or benchmark ran for that diagnosis.
