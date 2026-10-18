# Local caption evidence worker

This package runs optional ASR and forced alignment outside the editor process. Importing the parent client loads only the standard library. Call it from the durable background-job owner; never block the GUI thread.

```python
from pathlib import Path
from native.services.caption_worker import client

worker = Path(client.__file__).with_name("worker.py")
runner = client.CaptionClient(managed_python, worker)
result = runner.run(request, owned_output_root, timeout=360, cancel=cancel_event)
hypothesis = result.load("hypothesis")
alignment = result.load("alignment")
```

Supply an explicit approved local profile and prepared-audio extraction receipt. The request binds the actual Creator Library asset UUID and original fingerprint, audio stream/channels/mix, WAV and PCM fingerprints, sample count, sample-zero source time, and the requested source range. A trusted extraction producer owns the mapping from original audio to prepared PCM. The worker rechecks source/PCM identities; hashes alone do not prove an arbitrary caller's extraction claim.

`native.services.caption_audio.CaptionAudioPreparer` supplies that bounded extraction path. Construct it with explicit FFmpeg, FFprobe and `python=` interpreter paths. The interpreter must be the configured helper runtime; a frozen application's `sys.executable` is the GUI executable. The helper does not provision an interpreter or model profile. Its gate source is included in the packaging data list, but packaged-app execution remains unqualified.

Pass the registered asset identity, original audio stream index, explicit channel indices and mix status, source range/context and cached profile identity to `prepare`. The result retains a failure receipt or exposes a hash-checked `load_request()` for `CaptionClient`. Selection never infers an isolated microphone. Successful preparation verifies both original decoded origin and continuous decoded samples before channel mixing, resampling and exact sample trimming. Negative AAC priming packets do not establish decoded sample zero. The streamed clock check keeps bounded first/last evidence and a hash; it does not accumulate a full frame log.

This preparation profile requires continuous audio starting at source zero. A prepared context can start later. Analysis endpoints must use the exact 16 kHz sample grid; callers must adapt analysis context without changing native edit boundaries. Nonzero/unknown decoded origins, discontinuities, missing streams, changed sources, unavailable channels and insufficient decoded samples fail explicitly. On Windows a share-read guard prevents source writes/replacement during preparation. Other platforms have pre/post fingerprints, without that qualified lock guarantee.

Each run gets a new UUID and immutable hypothesis/alignment files. Canonical hypothesis words retain original ASR pieces and available probabilities. Alignment references those word IDs; missing or rejected boundaries remain null. A successful result proposes machine evidence. It does not accept words, change authored corrections, establish caption accuracy, or control timeline timing.

The parent bounds JSONL output and logs, handles crash/timeout/cancellation, and owns the Windows worker tree through a Job Object. A valid hypothesis survives later failure as partial evidence. Read artifacts through `RunResult.load` to verify their recorded hashes. A reused run directory fails rather than replacing earlier work.

The qualified evaluation profile uses isolated Python3.12, cached faster-whisper small on CPU and WhisperX English alignment. It accepts English ranges up to120 seconds, up to180 seconds of prepared mono16kHz PCM, and original clip files up to2GiB. Endpoints must lie on exact prepared samples. These are explicit profile limits, not a complete long-VOD pipeline or a selected quality default.

Optional imports occur only in the worker. Profile preflight checks approved asset hashes and package versions. The worker uses explicit local model paths, offline settings and Python socket guards. Those guards are not an OS network sandbox. This package does not install/download models or upload media.

Accepted source words and correction history are available through `CreatorLibrary.captions` on the catalog's connection. Immutable proposals never advance its accepted head. Explicit replacement can inherit usable timing with prior-word/revision provenance; unknown split boundaries remain reviewable. Projection callers must pin an accepted revision. This does not supply the correction UI or native caption projection.

`native.services.durable_captions.DurableCaptionRunner` connects a prepared request to the catalog's admission store and this worker. Construct the library and runner on the same background thread. Give every controller for that catalog the same owned artifact directory. Create or choose the transcript with the exact source/audio/range identity, then call `submit(prepared, transcript_id=..., idempotency_key=...)`. Pin an existing accepted revision explicitly when it belongs to the request. The immutable request artifact includes the full channel/mix/profile/range selection; its reference and normalized retention pins must agree before launch. Source/profile verification also runs before the worker starts. This preflight performs synchronous local file IO on the background owner; the supplied timeout bounds the worker phase, not arbitrary filesystem stalls.

`run_next()` claims only caption jobs. Participating controllers share one catalog heavy slot; this does not govern the legacy app queue, other catalogs, compilers or unrelated native editor tasks. A read-only connection observes cancellation while the worker runs. The current controller requires its own completed process-scope and output evidence before settling. Unknown cleanup, a lost controller result or cancellation during final verification retains interrupted resource holds. A restarted controller never adopts a stored receipt as proof that an old process stopped. Restart/orphan reconciliation remains unfinished.

Failed or cancelled outputs stay in their exclusive execution namespace as review-only evidence. They cannot enter the completed-output import path. `import_completed(job_id)` rechecks completed artifact hashes and retains immutable hypothesis/alignment proposals idempotently. It never accepts words or advances a correction head. Successful tests with the hostile fixture worker prove the runtime contract, not transcription accuracy. The actual cached-model replay is recorded in the execution ledger. This service does not provision models, add UI controls, publish media or complete application-wide scheduling.

See the [caption evidence contract](../../../docs/evaluation/caption-evidence-contract.md). Partial-span realignment, native subtitle projection, managed profile installation, global scheduling/resource budgets, quality comparison and repair-time acceptance remain separate work.
