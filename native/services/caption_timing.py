"""Manual source-timing repair over the existing caption repair owner.

The inherited operation and binding lease span observation, canonical CAS,
native application and marker settlement. This module owns no sequence clock,
caption grouping, native hash, transcript schema or automatic timing estimate.
"""
from dataclasses import dataclass, fields, replace
import uuid

from native.core.accepted_transcript import StaleRevision, TranscriptError
from native.services.caption_repair import (
    CaptionRepairController,
    RepairBlocked,
    RepairView,
    write_once,
)


class TimingInputError(ValueError):
    """Canonical validation rejected the authored interval before commit."""


@dataclass(frozen=True)
class TimingRepairView(RepairView):
    can_time: bool = False
    source_start_us: int = 0
    source_end_us: int = 0
    timing_message: str = ''


def word_identity(word):
    """Fields a canonical retime must preserve exactly, in accepted order."""
    return {
        key: value
        for key, value in word.items()
        if key not in ('timing', 'timing_provenance')
    }


class TimingRepairController(CaptionRepairController):
    def _timing_admission(self, library, view):
        if self.barrier.exists() or view.state == 'saved_uncertain':
            return False, 'Check the unsettled editor application before changing timing.'
        if view.state not in ('saved_applied', 'saved_pending'):
            return False, 'Reload the saved words before changing timing.'
        if not view.native_pin_known:
            return False, 'Open the editor and reload the saved words to change timing.'

        pin, body = self._observe()
        if pin != view.native_pin:
            return False, 'The editor captions changed. Reload the saved words.'
        if pin is None:
            if body is not None:
                return False, 'The editor caption identity needs review.'
            return True, 'Enter the selected word’s original source interval.'

        if pin['transcript_id'] != view.transcript_id or pin['revision'] > view.revision:
            return False, 'The editor has another accepted transcript revision.'
        native_input = body['input'] if body is not None else None
        prior = library.captions.projection_input(
            view.transcript_id, revision=pin['revision']
        )
        if native_input != prior:
            return False, 'The editor payload differs from its canonical revision.'

        current = library.captions.revision(view.transcript_id, view.revision)
        old_words = prior['accepted']['words']
        current_words = current['words']
        if (
            len(old_words) != len(current_words)
            or [word_identity(word) for word in old_words]
            != [word_identity(word) for word in current_words]
        ):
            return False, 'Apply the saved text correction before changing timing.'
        return True, 'Enter the selected word’s start and end in the original source.'

    def _with_timing(self, library, view):
        identity = library.captions.identity(view.transcript_id)
        try:
            allowed, reason = self._timing_admission(library, view)
        except Exception:
            allowed = False
            reason = 'The editor could not be verified. Reload the saved words.'
        values = {field.name: getattr(view, field.name) for field in fields(RepairView)}
        return TimingRepairView(
            **values,
            can_time=allowed,
            source_start_us=identity['range']['start_us'],
            source_end_us=identity['range']['end_us'],
            timing_message=reason,
        )

    def _view(self, library, tid, revision, **kwargs):
        return self._with_timing(
            library, super()._view(library, tid, revision, **kwargs)
        )

    def _apply(self, library, view, operation):
        result = super()._apply(library, view, operation)
        result = self._with_timing(library, result)
        if result.state == 'saved_pending' and not result.can_apply and result.can_time:
            return replace(
                result,
                message='Timing saved. Other words need timing review before application.',
            )
        return result

    def _uncertain(self, view, operation, reason, native_receipt):
        result = super()._uncertain(view, operation, reason, native_receipt)
        return replace(
            result,
            can_time=False,
            timing_message='Check the unsettled editor application before changing timing.',
        )

    def check_editor(self, view):
        result = super().check_editor(view)
        return replace(
            result,
            can_time=result.state == 'saved_applied',
            timing_message=(
                'Enter the selected word’s original source interval.'
                if result.state == 'saved_applied'
                else 'Check the unsettled editor application before changing timing.'
            ),
        )

    def save_timing(self, view, word_id, start_us, end_us):
        """CAS-save one explicit human interval, then apply that exact revision once."""
        with self._operation() as library:
            if self.barrier.exists():
                raise RepairBlocked('Check the unsettled editor application first')
            if not isinstance(view, TimingRepairView) or not view.can_time:
                raise RepairBlocked('This saved revision is not ready for timing repair')
            allowed, reason = self._timing_admission(library, view)
            if not allowed:
                raise RepairBlocked(reason)
            if type(start_us) is not int or type(end_us) is not int:
                raise TimingInputError('Use exact integer source microseconds.')

            accepted = library.captions.revision(view.transcript_id, view.revision)
            selected = [word for word in accepted['words'] if word['id'] == word_id]
            if len(selected) != 1:
                raise TimingInputError('Select a word from this accepted revision.')
            word = selected[0]
            operation = self.directory / str(uuid.uuid4())
            operation.mkdir()
            write_once(
                operation / 'save-intent.json',
                {
                    'operation': 'retime',
                    'transcript_id': view.transcript_id,
                    'expected_revision': view.revision,
                    'word_id': word_id,
                    'source_start_us': start_us,
                    'source_end_us': end_us,
                    'expected_native_pin': view.native_pin,
                },
            )
            try:
                updated = library.captions.edit(
                    view.transcript_id,
                    expected_revision=view.revision,
                    operation='retime',
                    target_word_ids=[word_id],
                    replacements=[{
                        'text': word['text'],
                        'timing': {
                            'start_us': start_us,
                            'end_us': end_us,
                            'status': 'manually_timed',
                        },
                    }],
                    origin='human',
                )
            except StaleRevision:
                raise
            except TranscriptError as exc:
                raise TimingInputError(str(exc)) from exc

            saved = self._view(
                library,
                view.transcript_id,
                updated['revision'],
                pin=view.native_pin,
                known=view.native_pin_known,
                receipt=operation,
            )
            try:
                write_once(
                    operation / 'saved.json',
                    {
                        'transcript_id': view.transcript_id,
                        'revision': updated['revision'],
                        'event': library.captions.event(
                            view.transcript_id, updated['revision']
                        ),
                    },
                )
            except OSError:
                return replace(
                    saved,
                    message='Timing saved. Its application receipt could not be started.',
                )
            return self._apply(library, saved, operation)
