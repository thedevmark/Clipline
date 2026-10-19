"""Canonical accepted-word repair followed by one native application.

All methods run on a background owner. Every operation opens its own library
connection there. Views are detached; no SQLite object or native timeline enters
Qt. A durable unsettled marker prevents a blind retry after uncertain mutation.
"""
from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
import json
import os
import threading
import uuid
from pathlib import Path
from native.core.creator_library import CreatorLibrary, FingerprintMismatch
from native.core.accepted_transcript import NeedsTimingReview
from native.services import native_captions
from native.services.filmcraft_bridge import EditorUnavailable, NativeCommandError

@dataclass(frozen=True)
class WordView:
    id: str
    text: str
    start_us: int | None
    end_us: int | None
    timing_status: str
    provenance: str
    text_origin: str

@dataclass(frozen=True)
class RepairView:
    transcript_id: str
    revision: int
    source_name: str
    words: tuple[WordView, ...]
    native_pin: dict | None
    native_pin_known: bool
    state: str
    message: str
    can_apply: bool
    can_edit: bool
    receipt_directory: str | None = None
    can_structure: bool = False

class RepairBusy(RuntimeError):
    pass

class RepairBlocked(RuntimeError):
    pass

def detached(value):
    return json.loads(json.dumps(value, allow_nan=False))

def write_once(path, value):
    with path.open('x', encoding='utf8') as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())

class CaptionRepairController:

    def __init__(
        self,
        library_path,
        bridge,
        working,
        *,
        owner,
        tracks,
        engine_version,
        receipts_directory,
    ):
        self.library_path = Path(library_path)
        self.bridge = bridge
        self.working = working
        self.owner = owner
        self.tracks = detached(tracks)
        self.engine_version = engine_version
        working_identity = os.path.normcase(str(working.path.resolve(strict=True)))
        binding = hashlib.sha256(f'{working_identity}:{owner}'.encode()).hexdigest()
        self.directory = Path(receipts_directory) / binding
        self.barrier = self.directory / 'unsettled-application.json'
        self._lock = threading.Lock()

    @contextmanager
    def _operation(self):
        if threading.current_thread() is threading.main_thread():
            raise RuntimeError('Caption persistence and native calls require a background owner')
        if not self._lock.acquire(blocking=False):
            raise RepairBusy('A caption operation is already running')
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self._binding_lease():
                with CreatorLibrary(self.library_path) as library:
                    yield library
        finally:
            self._lock.release()

    @contextmanager
    def _binding_lease(self):
        # Inspection and marker retirement form one operation across controllers.
        # A stale observer must never remove a newer application's barrier.
        with (self.directory / 'operation.lock').open('a+b') as lock:
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b'\0')
                lock.flush()
            lock.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RepairBusy('Another caption owner is operating on this project') from exc
            try:
                yield
            finally:
                if os.name == 'nt':
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _view(
        self,
        library,
        tid,
        revision,
        *,
        pin=None,
        known=False,
        state='saved_pending',
        message='Saved words are not applied to the editor.',
        receipt=None,
    ):
        accepted = library.captions.revision(tid, revision)
        words = tuple(
            (WordView(w['id'], w['text'], w['timing']['start_us'], w['timing']['end_us'], w['timing']['status'], self._provenance(w), w['text_origin']) for w in accepted['words']),
        )
        usable = all((w.timing_status in ('aligned', 'manually_timed', 'inherited') for w in words))
        return RepairView(
            tid,
            revision,
            Path(library.captions.identity(tid)['source']['path']).name,
            words,
            detached(pin),
            known,
            state,
            message,
            state == 'saved_pending' and known and usable,
            state == 'saved_applied',
            str(receipt) if receipt else None,
            state == 'saved_applied',
        )

    @staticmethod
    def _provenance(word):
        p = word['timing_provenance']
        if p['kind'] == 'inherited':
            if p.get('mode') == 'outer_span':
                return 'Outer interval from revision ' + str(p['from_revision'])
            return 'Same interval from revision ' + str(p['from_revision'])
        if p['kind'] == 'alignment_proposal':
            return 'Accepted alignment'
        if p['kind'] == 'human_manual':
            return 'Manually timed'
        return 'Timing needs review'

    def _observe(self):
        before = self.bridge.execute('project.inspect', {})
        if (
            not isinstance(before, dict)
            or before.get('path') != str(self.working.path.resolve(strict=True))
            or type(before.get('revision')) is not int
            or not 0 <= before['revision'] < 2 ** 64
        ):
            raise RepairBlocked('The editor has another project open')
        pin = native_captions.read_native_pin(self.bridge, self.owner)
        metadata = self.bridge.execute('metadata.get', {'item': self.owner})
        text = metadata.get('metadata', {}).get('clipline.acceptedCaptions.v1')
        body = None
        if text is not None:
            if not isinstance(text, str) or len(text.encode('utf8')) > 8 * 1024 ** 2:
                raise RepairBlocked('Native caption metadata is invalid')
            body = json.loads(text)
            if (
                not isinstance(body, dict)
                or body.get('schema') != 1
                or body.get('owner') != self.owner
                or body.get('pin') != pin
            ):
                raise RepairBlocked('Native caption identity changed during inspection')
        validation = self.bridge.execute('clipline.validate', {'expectedRevision': before['revision']})
        after = self.bridge.execute('project.inspect', {})
        if (
            not isinstance(validation, dict)
            or validation.get('valid') is not True
            or type(validation.get('revision')) is not int
            or validation['revision'] != before['revision']
            or not isinstance(after, dict)
            or after.get('path') != before['path']
            or type(after.get('revision')) is not int
            or after['revision'] != before['revision']
        ):
            raise RepairBlocked('Native project changed during inspection')
        return (pin, body)

    def load(self, transcript_id, revision):
        """Load only the revision explicitly selected by the caller."""
        with self._operation() as library:
            if self.barrier.exists():
                attempt = self._unsettled()
                return self._view(
                    library,
                    attempt['transcript_id'],
                    attempt['revision'],
                    state='saved_uncertain',
                    message='Correction saved. Showing the unsettled revision. Check the editor before another application.',
                )
            try:
                pin, body = self._observe()
                projection = library.captions.projection_input(transcript_id, revision=revision)
                applied = body is not None and body.get('input') == projection
                return self._view(
                    library,
                    transcript_id,
                    revision,
                    pin=pin,
                    known=True,
                    state='saved_applied' if applied else 'saved_pending',
                    message='Saved words match the editor.' if applied else 'Saved words are not applied to the editor.',
                )
            except NeedsTimingReview:
                return self._view(
                    library,
                    transcript_id,
                    revision,
                    pin=pin,
                    known=True,
                    message='Saved words need timing review before application.',
                )
            except Exception:
                return self._view(
                    library,
                    transcript_id,
                    revision,
                    message='Saved words loaded. The editor could not be verified.',
                )

    def reload_saved(self, transcript_id):
        """Explicit user reload; application still uses its exact detached revision."""
        with self._operation() as library:
            revision = library.captions.head(transcript_id)
        return self.load(transcript_id, revision)

    def save_spelling(self, view, word_id, text):
        """One human action: CAS-save spelling, then apply that saved revision once."""
        with self._operation() as library:
            if self.barrier.exists():
                raise RepairBlocked('Check the previous editor outcome before saving another correction')
            if not view.can_edit or view.state != 'saved_applied':
                raise RepairBlocked('Apply this saved revision before another correction')
            operation = self.directory / str(uuid.uuid4())
            operation.mkdir()
            write_once(
                operation / 'save-intent.json',
                {
                    'transcript_id': view.transcript_id,
                    'expected_revision': view.revision,
                    'word_id': word_id,
                    'text': text,
                },
            )
            accepted = library.captions.edit(
                view.transcript_id,
                expected_revision=view.revision,
                operation='replace',
                target_word_ids=[word_id],
                replacements=[{'text': text}],
                origin='human',
            )
            revision = accepted['revision']
            saved = self._view(
                library,
                view.transcript_id,
                revision,
                pin=view.native_pin,
                known=view.native_pin_known,
                receipt=operation,
            )
            try:
                write_once(
                    operation / 'saved.json',
                    {
                        'transcript_id': view.transcript_id,
                        'revision': revision,
                        'event': library.captions.event(view.transcript_id, revision),
                    },
                )
            except OSError:
                return replace(
                    saved,
                    message='Correction saved. Its application receipt could not be started.',
                )
            return self._apply(library, saved, operation)

    def apply_saved(self, view):
        """Explicit recovery after a known not-applied result; never follow head."""
        with self._operation() as library:
            if view.state != 'saved_pending' or not view.can_apply:
                raise RepairBlocked('This saved revision is not ready for application')
            operation = self.directory / str(uuid.uuid4())
            operation.mkdir()
            return self._apply(library, view, operation)

    def _structure_admission(self, library, view):
        if self.barrier.exists() or view.state != 'saved_applied' or not view.native_pin_known:
            return False, 'Apply or check the saved revision before changing words.'
        pin, body = self._observe()
        if pin != view.native_pin or body is None or body.get('input') != library.captions.projection_input(view.transcript_id, revision=view.revision):
            return False, 'The editor captions changed. Reload the saved words.'
        return True, ''

    def save_structure(self, view, action, word_ids, texts):
        """Save one authored structural edit, then use the existing projection owner once."""
        if action not in ('split', 'merge', 'delete'):
            raise RepairBlocked('Choose Split, Merge or Delete.')
        if not isinstance(word_ids, list) or not isinstance(texts, list):
            raise RepairBlocked('Select ordered words and explicit replacement text.')
        if not ((action == 'split' and len(word_ids) == 1 and len(texts) >= 2)
                or (action == 'merge' and len(word_ids) >= 2 and len(texts) == 1)
                or (action == 'delete' and word_ids and not texts)):
            raise RepairBlocked('The selected words do not fit this action.')
        with self._operation() as library:
            if not view.can_structure:
                raise RepairBlocked('This saved revision is not ready for word edits.')
            allowed, reason = self._structure_admission(library, view)
            if not allowed:
                raise RepairBlocked(reason)
            operation = self.directory / str(uuid.uuid4())
            operation.mkdir()
            write_once(operation / 'save-intent.json', {
                'operation': action, 'transcript_id': view.transcript_id,
                'expected_revision': view.revision, 'target_word_ids': word_ids,
                'replacement_text': texts, 'expected_native_pin': view.native_pin,
            })
            accepted = library.captions.edit(view.transcript_id,
                expected_revision=view.revision, operation=action,
                target_word_ids=word_ids, replacements=[{'text': text} for text in texts], origin='human')
            saved = self._view(library, view.transcript_id, accepted['revision'],
                pin=view.native_pin, known=view.native_pin_known, receipt=operation)
            try:
                write_once(operation / 'saved.json', {
                    'transcript_id': view.transcript_id, 'revision': accepted['revision'],
                    'event': library.captions.event(view.transcript_id, accepted['revision']),
                })
            except OSError:
                return replace(saved, message='Word edit saved. Its application receipt could not be started.')
            result = self._apply(library, saved, operation)
            if result.state == 'saved_pending' and not result.can_apply:
                return replace(result, message=('Split saved. Set each new word’s source interval before applying.'
                    if action == 'split' else 'Word edit saved. Finish the remaining source timing before applying.'))
            if result.state == 'saved_applied':
                return replace(result, message='Word edit saved and applied to the editor.')
            return result

    def _apply(self, library, view, operation):
        if self.barrier.exists():
            raise RepairBlocked('An earlier editor application needs checking')
        if not view.native_pin_known:
            return replace(
                view,
                message='Correction saved. Open the editor, then reload these saved words.',
            )
        try:
            library.captions.projection_input(view.transcript_id, revision=view.revision)
        except NeedsTimingReview:
            return replace(
                view,
                can_apply=False,
                message='Correction saved. Timing needs review before application.',
            )
        attempt = {
            'schema': 1,
            'transcript_id': view.transcript_id,
            'revision': view.revision,
            'expected_pin': view.native_pin,
            'working_path': str(self.working.path),
            'owner': self.owner,
            'operation': str(operation),
        }
        try:
            write_once(operation / 'apply-intent.json', attempt)
            write_once(self.barrier, attempt)
        except FileExistsError:
            return self._uncertain(view, operation, 'An unsettled application already exists', None)
        except OSError:
            return replace(
                view,
                can_apply=False,
                message='Correction saved. The application could not start.',
            )
        try:
            result = native_captions.apply_accepted_captions(
                library,
                self.bridge,
                self.working,
                owner=self.owner,
                transcript_id=view.transcript_id,
                revision=view.revision,
                tracks=detached(self.tracks),
                expected_pin=view.native_pin,
                receipts_directory=operation / 'native',
                engine_version=self.engine_version,
            )
        except native_captions.AppliedOutcomeUncertain as exc:
            return self._uncertain(view, operation, str(exc), getattr(exc, 'directory', None))
        except NativeCommandError as exc:
            return self._uncertain(view, operation, str(exc), None)
        except (
            native_captions.CaptionApplicationError,
            EditorUnavailable,
            FingerprintMismatch,
            NeedsTimingReview,
            OSError,
            ValueError,
        ) as exc:
            try:
                write_once(
                    operation / 'application.json',
                    {'state': 'saved_pending', 'reason': str(exc)[:2000]},
                )
                self.barrier.unlink()
            except OSError as receipt_error:
                return self._uncertain(view, operation, str(receipt_error), None)
            if isinstance(exc, FingerprintMismatch):
                message = 'Correction saved. The source file changed. Restore or relink it before applying.'
            elif isinstance(exc, EditorUnavailable):
                message = 'Correction saved. Open the Filmcraft editor, then apply this saved revision.'
            else:
                message = 'Correction saved. Check the source and caption setup before applying again.'
            return replace(
                view,
                state='saved_pending',
                can_apply=True,
                message=message,
                receipt_directory=str(operation),
            )
        except Exception as exc:
            return self._uncertain(view, operation, str(exc), None)
        try:
            write_once(
                operation / 'application.json',
                {
                    'state': 'saved_applied',
                    'accepted': result.accepted,
                    'native_revision': result.native_revision,
                    'native_receipt': str(result.receipt_directory),
                },
            )
            self.barrier.unlink()
        except OSError as exc:
            return self._uncertain(view, operation, str(exc), result.receipt_directory)
        return replace(
            view,
            native_pin=detached(result.accepted),
            native_pin_known=True,
            state='saved_applied',
            can_apply=False,
            can_edit=True,
            message='Correction saved and applied to the editor.',
            receipt_directory=str(operation),
        )

    def _uncertain(self, view, operation, reason, native_receipt):
        try:
            write_once(
                operation / 'uncertain.json',
                {
                    'state': 'saved_uncertain',
                    'reason': reason[:2000],
                    'native_receipt': str(native_receipt) if native_receipt else None,
                },
            )
        except OSError:
            pass
        return replace(
            view,
            state='saved_uncertain',
            can_apply=False,
            can_edit=False,
            can_structure=False,
            message='Correction saved. The editor outcome is unknown. Check the editor before another application.',
            receipt_directory=str(operation),
        )

    def _unsettled(self):
        try:
            with self.barrier.open('rb') as stream:
                raw = stream.read(16385)
            if len(raw) > 16384:
                raise ValueError('Application marker exceeds its bound')
            attempt = json.loads(raw)
            if not isinstance(attempt, dict) or attempt.get('schema') != 1:
                raise ValueError('Application marker is invalid')
            tid = attempt['transcript_id']
            if not isinstance(tid, str) or str(uuid.UUID(tid)) != tid:
                raise ValueError('Application marker transcript is invalid')
            revision = attempt['revision']
            if type(revision) is not int or not 1 <= revision < 2 ** 63:
                raise ValueError('Application marker revision is invalid')
            if (
                attempt.get('owner') != self.owner
                or attempt.get('working_path') != str(self.working.path)
            ):
                raise ValueError('Application marker belongs to another native binding')
            operation = Path(attempt['operation']).resolve(strict=True)
            if not operation.is_dir() or not operation.is_relative_to(self.directory.resolve()):
                raise ValueError('Application receipt is outside this repair owner')
            return attempt
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RepairBlocked('The unsettled application receipt needs manual review') from exc

    def check_editor(self, view):
        """Resolve uncertainty only by observing the exact saved projection in native metadata."""
        with self._operation() as library:
            if not self.barrier.exists():
                raise RepairBlocked('No uncertain application is waiting')
            attempt = self._unsettled()
            if (
                attempt.get('transcript_id') != view.transcript_id
                or attempt.get('revision') != view.revision
            ):
                raise RepairBlocked('Check the revision from the unsettled application')
            pin, body = self._observe()
            projection = library.captions.projection_input(view.transcript_id, revision=view.revision)
            if body is None or body.get('input') != projection:
                return replace(
                    view,
                    state='saved_uncertain',
                    can_apply=False,
                    can_edit=False,
                    message='The saved correction is not confirmed in the editor. No application was retried.',
                )
            operation = Path(attempt['operation'])
            write_once(
                operation / ('observed-' + str(uuid.uuid4()) + '.json'),
                {'state': 'saved_applied', 'pin': pin, 'input_matches_saved_revision': True},
            )
            self.barrier.unlink()
            return replace(
                view,
                state='saved_applied',
                native_pin=detached(pin),
                native_pin_known=True,
                can_apply=False,
                can_edit=True,
                can_structure=True,
                message='Saved correction confirmed in the editor.',
                receipt_directory=str(operation),
            )
