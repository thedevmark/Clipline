"""Exact source-interval controls on the existing accepted-word panel."""
from dataclasses import dataclass
import re

from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import QGridLayout, QLabel, QLineEdit, QPushButton

from native.ui.caption_repair import CaptionRepairPanel, CaptionRepairBinding, STATES
from native.services.caption_timing import TimingInputError


SECONDS = re.compile(r'(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?')


def source_seconds(value):
    if value is None:
        return ''
    seconds, micros = divmod(value, 1_000_000)
    return f'{seconds}.{micros:06d}'


def parse_source_seconds(text):
    text = text.strip()
    if len(text) > 32 or not SECONDS.fullmatch(text):
        raise ValueError('Enter source seconds with up to six decimal places.')
    whole, _, fraction = text.partition('.')
    value = int(whole) * 1_000_000 + int(fraction.ljust(6, '0') or '0')
    if value > 2**63 - 1:
        raise ValueError('The source time exceeds the supported range.')
    return value


@dataclass(frozen=True)
class TimingValidation:
    message: str


class TimingRepairPanel(CaptionRepairPanel):
    timing_requested = Signal(object, str, object, object)

    def __init__(self, parent=None):
        self.start_time = None
        self.end_time = None
        self._timing_validation = None
        self._populating_timing = False
        super().__init__(parent)

        timing = QGridLayout()
        start_label = QLabel('Source start (seconds)')
        end_label = QLabel('Source end (seconds)')
        self.start_time = QLineEdit()
        self.end_time = QLineEdit()
        self.start_time.setMaxLength(32)
        self.end_time.setMaxLength(32)
        start_label.setBuddy(self.start_time)
        end_label.setBuddy(self.end_time)
        self.start_time.setAccessibleName('Selected word source start in seconds')
        self.end_time.setAccessibleName('Selected word source end in seconds')
        self.save_timing = QPushButton('Save timing')
        self.save_timing.setProperty('primary', True)
        self.save_timing.clicked.connect(self._save_timing)
        self.start_time.returnPressed.connect(self._save_timing)
        self.end_time.returnPressed.connect(self._save_timing)
        self.start_time.textChanged.connect(self._timing_changed)
        self.end_time.textChanged.connect(self._timing_changed)
        timing.addWidget(start_label, 0, 0)
        timing.addWidget(end_label, 0, 1)
        timing.addWidget(self.start_time, 1, 0)
        timing.addWidget(self.end_time, 1, 1)
        timing.addWidget(self.save_timing, 1, 2)
        timing.setColumnStretch(0, 1)
        timing.setColumnStretch(1, 1)
        self.timing_hint = QLabel('Select a word to repair its original source interval.')
        self.timing_hint.setWordWrap(True)
        self.timing_hint.setProperty('hint', True)
        self.layout().insertLayout(6, timing)
        self.layout().insertWidget(7, self.timing_hint)
        self.setMinimumHeight(600)
        self._actions()

    def _selected(self):
        super()._selected()
        if self.start_time is None:
            return
        word = self.selected_word()
        self._populating_timing = True
        self.start_time.setText(source_seconds(word.start_us) if word else '')
        self.end_time.setText(source_seconds(word.end_us) if word else '')
        self._populating_timing = False
        self._timing_validation = None
        self._actions()

    def _timing_changed(self):
        if self._populating_timing:
            return
        self._timing_validation = None
        self._actions()

    def _interval(self):
        start = parse_source_seconds(self.start_time.text())
        end = parse_source_seconds(self.end_time.text())
        if not self.view.source_start_us <= start < end <= self.view.source_end_us:
            raise ValueError('Use an increasing interval inside the accepted source range.')
        return start, end

    def _actions(self):
        super()._actions()
        if self.start_time is None:
            return
        word = self.selected_word()
        view = self.view
        editable = bool(
            view and getattr(view, 'can_time', False)
            and not self.busy and not self._error and word is not None
        )
        if view:
            self.timing_hint.setText(
                f'Accepted source range: {source_seconds(view.source_start_us)}–'
                f'{source_seconds(view.source_end_us)} seconds. {view.timing_message}'
            )
        timing_dirty = bool(
            word and (
                self.start_time.text() != source_seconds(word.start_us)
                or self.end_time.text() != source_seconds(word.end_us)
            )
        )
        spelling_dirty = bool(word and self.spelling.text() != word.text)
        self.start_time.setEnabled(editable and not spelling_dirty)
        self.end_time.setEnabled(editable and not spelling_dirty)
        self.spelling.setEnabled(self.spelling.isEnabled() and not timing_dirty)
        self.save.setEnabled(self.save.isEnabled() and not timing_dirty)
        problem = None
        try:
            self._interval()
        except (ValueError, AttributeError) as exc:
            problem = str(exc)
        self.save_timing.setEnabled(
            editable and timing_dirty and not spelling_dirty
            and problem is None and self._timing_validation is None
        )
        if not self.busy and not self._error and view:
            if self._timing_validation:
                self.status.setText('Timing change not saved')
                self.message.setText(self._timing_validation)
            elif timing_dirty and editable:
                self.status.setText('Timing change not saved')
                self.message.setText(
                    problem or (
                        'Reload saved words to discard both drafts. Then change spelling or timing.'
                        if spelling_dirty else 'Save this original source interval.'
                    )
                )
            elif not spelling_dirty:
                self.status.setText(STATES[view.state])

    def _save_timing(self):
        if self.save_timing.isEnabled():
            start, end = self._interval()
            self.timing_requested.emit(self.view, self.selected_word().id, start, end)

    def show_timing_error(self, message):
        self._timing_validation = message
        self._actions()
        self.start_time.setFocus()


class TimingRepairBinding(CaptionRepairBinding):
    def __init__(self, panel, controller, runner=None):
        super().__init__(panel, controller, runner)
        panel.timing_requested.connect(self._save_timing)

    @Slot(object, str, object, object)
    def _save_timing(self, view, word_id, start, end):
        def save():
            try:
                return self.controller.save_timing(view, word_id, start, end)
            except TimingInputError as exc:
                return TimingValidation(str(exc))
        self._dispatch(save)

    @Slot(object)
    def _finished(self, result):
        if isinstance(result, TimingValidation):
            self.panel.set_busy(False)
            self.panel.show_timing_error(result.message)
            return
        super()._finished(result)
