"""Load bounded durable caption artifacts before recording an explicit choice."""
from pathlib import Path

from native.core.moment_captions import CaptionSelectionError, _evidence, _uuid


class MomentCaptionSelector:
    def __init__(self, library, artifacts_directory):
        self.library = library
        self.directory = Path(artifacts_directory).resolve(strict=True)
        if not self.directory.is_dir():
            raise CaptionSelectionError('The existing caption artifact directory is required')

    def _read(self, *parts):
        path = self.directory.joinpath(*parts)
        if path.resolve(strict=True) != path or not path.is_file():
            raise CaptionSelectionError('Caption evidence must remain in its original artifact directory')
        with path.open('rb') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise CaptionSelectionError('Caption selection evidence exceeds 64 KiB')
        return raw

    def select(self, moment_id, *, expected_moment_revision, expected_selection_revision,
               job_id, transcript_id, hypothesis_id, alignment_id):
        """Call on the background thread after explicit proposal selection, not after generation."""
        job = self.library.jobs.job(job_id)
        settlement = self.library.jobs.settlement(job_id)
        if job['kind'] != 'caption' or job['state'] != 'completed' or settlement is None:
            raise CaptionSelectionError('Select a verified completed caption job')
        reference = job['input']['request_ref']
        request_bytes = self._read('requests', _uuid(reference['id']) + '.json')
        envelope = _evidence(request_bytes, reference['sha256'])
        try:
            run_id = _uuid(envelope['request']['run_id'])
            result_id = _uuid(settlement['result_ref']['id'])
            output_set = _uuid(job['output_set_id'])
        except (KeyError, TypeError) as error:
            raise CaptionSelectionError('Incomplete caption execution references') from error
        result_bytes = self._read('results', result_id + '.json')
        receipt_bytes = self._read('executions', output_set, run_id, 'receipt.json')
        return self.library.caption_selections.select_completed(
            moment_id, expected_moment_revision=expected_moment_revision,
            expected_selection_revision=expected_selection_revision, job_id=job_id,
            transcript_id=transcript_id, hypothesis_id=hypothesis_id, alignment_id=alignment_id,
            request_bytes=request_bytes, result_bytes=result_bytes, receipt_bytes=receipt_bytes)
