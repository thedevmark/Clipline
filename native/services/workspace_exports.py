"""Deliver both existing native exports from one saved active-Moment checkpoint."""
from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import re
import uuid

from native.core.creator_library import CreatorLibrary
from native.services.durable_native_exports import DurableNativeExports
from native.services.native_captions import read_native_pin


@dataclass(frozen=True)
class ExportDelivery:
    state: str = 'ready'
    message: str = 'Choose an output folder, then save and export both formats.'
    paths: tuple = ()
    originals: tuple = ()
    evidence: str = ''
    can_cleanup: bool = False


def copy_exclusive(source, destination, expected):
    """Publish a complete verified copy; preserve the completed backend media."""
    temporary = destination.with_name('.' + destination.name + '.' + str(uuid.uuid4()) + '.part')
    try:
        value = hashlib.sha256()
        with Path(source).open('rb') as incoming, temporary.open('xb') as outgoing:
            for block in iter(lambda: incoming.read(1024 * 1024), b''):
                outgoing.write(block); value.update(block)
            outgoing.flush(); os.fsync(outgoing.fileno())
        if value.hexdigest() != expected:
            raise ValueError('The verified export changed during copying.')
        if os.name == 'nt':
            os.rename(temporary, destination)  # Windows rename refuses an existing target.
        else:
            os.link(temporary, destination); temporary.unlink()
        return str(destination)
    finally:
        if temporary.exists(): temporary.unlink()


class WorkspaceExports:
    def __init__(self, editor, runtime):
        self.editor, self.runtime = editor, runtime
        self.directory = editor.workspace.directory / 'exports'
        self.runner = self.client = self.execution = self.cleanup_runner = None
        self.job = None
        self.blocked = False
        self._view = ExportDelivery()

    @property
    def unsettled(self):
        return bool(self.execution is not None and not self.execution.closed
                    or self.runner is not None and any(not owner.closed for owner in tuple(self.runner._executions.values())))

    def status(self):
        return replace(self._view, can_cleanup=self.unsettled)

    def _retain(self, error):
        # Keep the original runner/client and actual execution before diagnostics.
        owners = getattr(error, 'executions', ())
        if owners: self.execution = owners[0]
        if self.execution is None and self.runner is not None:
            self.execution = next((owner for owner in self.runner._executions.values() if not owner.closed), None)

    def run(self, folder, *, cancel, progress=lambda message: None):
        values = []
        def operation():
            if self.blocked:
                values.append(ExportDelivery('blocked', 'The previous interrupted export keeps its resource holds. Its files remain quarantined.'))
                return
            target = Path(folder)
            if not target.is_absolute() or not target.is_dir():
                values.append(ExportDelivery('blocked', 'Choose an existing local output folder.'))
                return
            if cancel.is_set():
                values.append(ExportDelivery('cancelled', 'Export cancelled before saving.'))
                return
            progress('Saving the current edit...')
            saved_identity = self.editor._checkpoint_open()
            try: values.append(self._render(target, cancel, progress, saved_identity))
            except BaseException as error:
                self._retain(error)
                self.blocked = self.job is not None
                try: message = str(error)
                except BaseException: message = type(error).__name__
                values.append(ExportDelivery('blocked', message, evidence=str(self.directory), can_cleanup=self.unsettled))
        result = self.editor._run(operation)
        self._view = values[0] if values else ExportDelivery('blocked', result.message, can_cleanup=self.unsettled)
        return self.status()

    def _render(self, target, cancel, progress, saved_identity):
        e = self.editor
        snapshot = e._view.snapshot
        revision, links = e._inspect()
        if (revision, links) != saved_identity or len(links) != 1:
            raise ValueError('The editor changed after saving. Save and export again.')
        pin = read_native_pin(e._session.bridge, links[0]['owner'])
        after, current_links = e._inspect()
        if after != revision or current_links != links:
            raise ValueError('The editor changed while reading caption bindings. Save and export again.')
        self.client = self.runtime.client()
        with CreatorLibrary(e.workspace.database) as library:
            self.runner = DurableNativeExports(library, self.directory, self.client)
            self.job = self.runner.submit(snapshot, project_id=e._facts.moment.id,
                kit_pin={'kit_id': e._facts.kit.kit_id, 'version': e._facts.kit.version},
                accepted_pin=pin, owners=links, compositions=['landscape', 'portrait'],
                idempotency_key=str(uuid.uuid4()))
            progress('Rendering landscape and portrait from the saved edit...')
            result = self.runner.run_next(job_id=self.job['id'], cancel=cancel)
            if result is None:
                if cancel.is_set():
                    self.job = self.runner.jobs.request_cancel(self.job['id'], expected_revision=self.job['revision'])
                    return ExportDelivery('cancelled', 'Export cancelled before rendering. No files were delivered.')
                self.blocked = True
                return ExportDelivery('blocked', 'This export could not claim its resource holds. No other render was started.', evidence=str(self.directory))
            self.execution = result.execution
            self.job = result.job
            if result.job['state'] != 'completed':
                self.blocked = result.job['state'] == 'interrupted'
                return ExportDelivery(result.job['state'],
                    'Export did not complete. The original files and evidence are retained; no files were delivered.',
                    evidence=str(self.directory / 'executions' / result.job['output_set_id']), can_cleanup=self.unsettled)
            progress('Verifying both completed files...')
            receipt = self.runner.completed_receipt(self.job['id'])
        title = re.sub(r'[^\w .()-]', '', e._facts.moment.title).strip(' .')[:70] or 'Moment'
        folder = target / ('Clipline - ' + title + ' - ' + str(uuid.uuid4())[:8])
        folder.mkdir(exist_ok=False)
        paths, errors = [], []
        outputs = receipt['runtime']['outputs']
        originals = tuple(row['path'] for row in outputs)
        for output in outputs:
            mode = output['composition']
            progress('Copying ' + mode + ' to the output folder...')
            try: paths.append(copy_exclusive(output['path'], folder / (mode + '.mp4'), output['sha256']))
            except Exception as error: errors.append(mode.capitalize() + ': ' + str(error))
        evidence = str(self.directory / 'executions' / self.job['output_set_id'])
        if errors:
            return ExportDelivery('partial' if paths else 'copy_failed',
                'Saved ' + str(len(paths)) + ' of 2 files. ' + ' '.join(errors) + ' Verified originals remain available below.',
                tuple(paths), originals, evidence)
        return ExportDelivery('completed', 'Landscape and portrait are ready.', tuple(paths), originals, evidence)

    def cleanup(self):
        values = []
        def operation():
            if self.execution is None:
                values.append(ExportDelivery('blocked', 'No retained export process is available.'))
                return
            try:
                with CreatorLibrary(self.editor.workspace.database) as library:
                    self.cleanup_runner = DurableNativeExports(library, self.directory, self.client)
                    current = self.cleanup_runner.jobs.job(self.job['id'])
                    if current['state'] == 'interrupted':
                        self.cleanup_runner.cleanup_pending(current['id'], execution=self.execution)
                    else:
                        self.execution.stop()
                values.append(ExportDelivery('stopped' if self.execution.closed else 'blocked',
                    'Export cleanup finished. Interrupted files remain quarantined and resource holds remain retained.'
                    if self.execution.closed else 'The original export process still needs cleanup.',
                    evidence=str(self.directory), can_cleanup=self.unsettled))
            except BaseException as error:
                self._retain(error)
                try: message = str(error)
                except BaseException: message = type(error).__name__
                values.append(ExportDelivery('blocked', message, evidence=str(self.directory), can_cleanup=self.unsettled))
        result = self.editor._run(operation, export_cleanup=True)
        self._view = values[0] if values else ExportDelivery('blocked', result.message, can_cleanup=self.unsettled)
        return self.status()
