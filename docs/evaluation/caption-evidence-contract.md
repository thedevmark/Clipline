# W4 hypothesis, correction, and source-time evidence contract

This is the narrow transcript contract needed by the master plan. It does not define an editing timeline or another source of native edit authority.

## Source and audio identity

- Stable Creator Library asset ID plus the original SHA256 fingerprint.
- Original audio stream index and selected channels. Record whether the stream is an isolated microphone or a mixed game/Discord/music track; unknown remains unknown.
- Source interval and context handles in integer source microseconds. Record exact sample count/rate and extracted PCM hash.
- Extraction/normalization command and its version. Do not assume the selected clip begins at source time zero.
- A mapping from extracted audio sample zero to the original source clock. Preserve this when aligned intervals are combined or deduplicated.

## Immutable hypothesis run

- Run ID, creation time, source/audio identity, requested language/task, vocabulary/Kit revision, and processing state: queued/running/complete/partial/failed/cancelled.
- Backend/package versions, ASR and VAD model identity/revision/SHA256, effective settings, hardware/device, compute type, worker build, and raw result hash.
- Preserve original segment text and source-relative segment bounds. Retain available average log probability and no-speech probability under their actual metric names.
- Word records contain a stable ID within this hypothesis, original text, optional source start/end, and available ASR probability. Missing evidence is null, not zero and not a fabricated score.
- Preserve token-level evidence when provided. If tokens are merged into words, record that normalization and do not call an arbitrary mean token score calibrated word confidence.

## Alignment revision

- Alignment run ID, target hypothesis/corrected-transcript revision, language-specific model identity/SHA256, package versions, settings, and raw result hash.
- Bind timing to canonical word IDs. Keep ASR probability and phoneme/alignment score separate.
- Word bounds use source microseconds. Explicit statuses distinguish aligned, missing, interpolated, manually timed, and unresolved/overlapping evidence.
- Retain character alignment or a reference to it when needed for repair. Missing/zero-duration/out-of-range intervals require review.
- This evaluation uses `interpolate_method="ignore"`. If a later profile interpolates, preserve that fact per word and never present interpolation as measured precise timing.
- Forced alignment estimates where the supplied text occurs. It does not verify that the text was spoken correctly.

## Accepted transcript and correction ownership

- Accepted transcript revision, parent revision, and stable accepted word IDs are separate from immutable machine runs.
- A correction event contains its own ID, expected base revision, target word IDs/source span, operation, before/after text, optional manual timing, origin, and resulting revision.
- Replacement/split/merge operations create new word identities with lineage to the affected words. Never reuse an ordinal from a new ASR run as if it were the same word.
- A rerun produces a proposal. It cannot overwrite accepted corrections. Changed or ambiguous correspondence requires review.
- Explicitly chosen spelling rules and vocabulary entries belong to a versioned Kit. They guide or propose changes; they must not insert unheard words silently.
- Speaker identity remains unknown unless supported. Diarization labels do not identify real people automatically or separate mixed voices.

## Engine projection

- Native edits remain authoritative for timing/cuts/speed. Project source words into each composition through native edit-unit/source mappings.
- Both outputs reference the same accepted text revision. Each output may own wrapping, position, font/style, and animation overrides.
- Caption layout and line grouping are derived presentation. They cannot replace word IDs with mutable line-start timestamps as identity.
- Authored hooks and CTAs are separate text layers. They are not spoken subtitles.
- Immutable render receipts pin the accepted transcript, alignment, native revision, Kit version, font/effect identities, and output recipe.

## Quality evidence

Keep recognition, timing, speaker labels, readability, and manual repair effort as separate dimensions. Store a versioned held-out ground-truth reference when one exists. Without listening-groundtruth, WER, name accuracy, timing error, and repair savings remain unmeasured. Differences between two hypotheses do not identify which one is correct.
