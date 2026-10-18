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

Each run gets a new UUID and immutable hypothesis/alignment files. Canonical hypothesis words retain original ASR pieces and available probabilities. Alignment references those word IDs; missing or rejected boundaries remain null. A successful result proposes machine evidence. It does not accept words, change authored corrections, establish caption accuracy, or control timeline timing.

The parent bounds JSONL output and logs, handles crash/timeout/cancellation, and owns the Windows worker tree through a Job Object. A valid hypothesis survives later failure as partial evidence. Read artifacts through `RunResult.load` to verify their recorded hashes. A reused run directory fails rather than replacing earlier work.

The qualified evaluation profile uses isolated Python3.12, cached faster-whisper small on CPU and WhisperX English alignment. It accepts English ranges up to120 seconds, up to180 seconds of prepared mono16kHz PCM, and original clip files up to2GiB. Endpoints must lie on exact prepared samples. These are explicit profile limits, not a complete long-VOD pipeline or a selected quality default.

Optional imports occur only in the worker. Profile preflight checks approved asset hashes and package versions. The worker uses explicit local model paths, offline settings and Python socket guards. Those guards are not an OS network sandbox. This package does not install/download models or upload media.

See the [caption evidence contract](../../../docs/evaluation/caption-evidence-contract.md). Accepted corrections, partial-span realignment, native subtitle projection, managed profile installation, global scheduling/resource budgets, quality comparison and repair-time acceptance remain separate work.
