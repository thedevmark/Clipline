"""Detached accepted-word panel. Persistence and native calls belong to its binding."""
from PySide6.QtCore import Qt, Signal, QObject, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from native.ui import theme
from native.workers import JobRunner
STATES = {
    'saved_applied': 'Saved and applied',
    'saved_pending': 'Saved · Not applied',
    'saved_uncertain': 'Saved · Editor outcome unknown',
}

def source_time(us):
    if us is None:
        return '—'
    seconds, remainder = divmod(us, 1000000)
    minutes, seconds = divmod(seconds, 60)
    return f'{minutes}:{seconds:02d}.{remainder // 1000:03d}'

class CaptionRepairPanel(QWidget):
    save_requested = Signal(object, str, str)
    apply_requested = Signal(object)
    check_requested = Signal(object)
    reload_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('stage')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            theme.GLOBAL_QSS + f'\nQPushButton[primary="true"]:disabled {{ background: {theme.BG_PANEL}; color: {theme.INK_DIM}; border-color: {theme.BORDER}; }}',
        )
        body_font = self.font()
        body_font.setPixelSize(16)
        self.setFont(body_font)
        self.setMinimumSize(560, 500)
        self.view = None
        self.busy = False
        self._error = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        heading = QLabel('Captions')
        heading.setProperty('title', True)
        layout.addWidget(heading)
        self.source = QLabel('Load an accepted transcript to correct its words.')
        self.source.setWordWrap(True)
        layout.addWidget(self.source)
        self.status = QLabel('No accepted words loaded')
        self.status.setAccessibleName('Caption save and application status')
        layout.addWidget(self.status)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['Source time', 'Word', 'Timing', 'Origin'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for i in (0, 2, 3):
            self.table.horizontalHeader().setSectionResizeMode(
                i,
                QHeaderView.ResizeMode.ResizeToContents,
            )
        self.table.setStyleSheet(
            f'QTableWidget {{ background: {theme.BG_PANEL}; color: {theme.INK_BRIGHT}; gridline-color: {theme.BORDER}; selection-background-color: {theme.ACCENT_DEEP}; selection-color: {theme.ACCENT_INK}; }} QHeaderView::section {{ background: {theme.BG_PANEL}; color: {theme.INK}; padding: 6px; border: 0; border-bottom: 1px solid {theme.BORDER}; }}',
        )
        self.table.setAccessibleName('Accepted source words')
        self.table.itemSelectionChanged.connect(self._selected)
        layout.addWidget(self.table, 1)
        self.detail = QLabel('Select one word to correct its spelling.')
        self.detail.setWordWrap(True)
        self.detail.setProperty('hint', True)
        layout.addWidget(self.detail)
        row = QHBoxLayout()
        label = QLabel('Spelling')
        self.spelling = QLineEdit()
        self.spelling.setMaxLength(512)
        label.setBuddy(self.spelling)
        self.spelling.setAccessibleName('Corrected spelling for the selected word')
        self.spelling.textChanged.connect(self._actions)
        self.spelling.returnPressed.connect(self._save)
        row.addWidget(label)
        row.addWidget(self.spelling, 1)
        self.save = QPushButton('Save correction')
        self.save.setProperty('primary', True)
        self.save.clicked.connect(self._save)
        row.addWidget(self.save)
        layout.addLayout(row)
        self.message = QLabel('The correction keeps the selected word’s source timing.')
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        actions = QHBoxLayout()
        self.reload = QPushButton('Reload saved words')
        self.reload.clicked.connect(self._reload)
        actions.addWidget(self.reload)
        actions.addStretch()
        self.check = QPushButton('Check editor')
        self.check.clicked.connect(lambda: self.check_requested.emit(self.view))
        actions.addWidget(self.check)
        self.apply = QPushButton('Apply saved revision')
        self.apply.clicked.connect(lambda: self.apply_requested.emit(self.view))
        actions.addWidget(self.apply)
        layout.addLayout(actions)
        self._actions()

    def set_view(self, view):
        previous_row = self.table.currentRow()
        selected = self.selected_word().id if self.selected_word() else None
        self.view = view
        self._error = False
        self.source.setText(f'{view.source_name} · Accepted revision {view.revision}')
        self.status.setText(STATES[view.state])
        self.message.setText(view.message)
        self.table.blockSignals(True)
        self.table.setRowCount(len(view.words))
        target = max(0, min(previous_row, len(view.words) - 1))
        for i, word in enumerate(view.words):
            if word.id == selected:
                target = i
            origin = 'Human' if word.text_origin == 'human' else 'Accepted ASR'
            values = (
                source_time(word.start_us),
                word.text,
                word.timing_status.replace('_', ' ').capitalize(),
                origin,
            )
            for column, text in enumerate(values):
                self.table.setItem(i, column, QTableWidgetItem(text))
        self.table.blockSignals(False)
        if view.words:
            self.table.selectRow(target)
        self._selected()
        self._actions()

    def selected_word(self):
        if self.view is None:
            return None
        row = self.table.currentRow()
        return self.view.words[row] if 0 <= row < len(self.view.words) else None

    def _selected(self):
        word = self.selected_word()
        self.spelling.setText(word.text if word else '')
        if word:
            self.detail.setText(
                f'Source {source_time(word.start_us)}–{source_time(word.end_us)} · {word.provenance}',
            )
        else:
            self.detail.setText('Select one word to correct its spelling.')
        self._actions()

    def _actions(self):
        word = self.selected_word()
        text = self.spelling.text()
        editable = bool(self.view and self.view.can_edit and (not self.busy) and (not self._error))
        self.spelling.setEnabled(editable and word is not None)
        self.save.setEnabled(
            editable and word is not None and (text != word.text) and bool(text) and (not any((c.isspace() or ord(c) < 32 for c in text))),
        )
        self.reload.setEnabled(self.view is not None and (not self.busy))
        self.apply.setVisible(bool(self.view and self.view.state == 'saved_pending'))
        self.apply.setEnabled(
            bool(self.view and self.view.can_apply and (not self.busy) and (not self._error)),
        )
        self.check.setVisible(bool(self.view and self.view.state == 'saved_uncertain'))
        self.check.setEnabled(not self.busy)
        self.table.setEnabled(not self.busy)
        if self.view and (not self.busy) and (not self._error):
            dirty = word is not None and text != word.text
            self.status.setText(
                'Spelling change not saved' if dirty and editable else STATES[self.view.state],
            )
            invalid = dirty and (not text or any((c.isspace() or ord(c) < 32 for c in text)))
            self.message.setText('Enter one word without spaces.' if invalid else self.view.message)

    def _save(self):
        if self.save.isEnabled():
            self.save_requested.emit(self.view, self.selected_word().id, self.spelling.text())

    def _reload(self):
        if self.view:
            self.reload_requested.emit(self.view.transcript_id)

    def set_busy(self, busy):
        self.busy = busy
        if busy:
            self.status.setText('Updating captions…')
        elif self.view:
            self.status.setText(STATES[self.view.state])
        self._actions()

    def show_error(self, message):
        self._error = True
        self.message.setText(message)
        self._actions()

class CaptionRepairBinding(QObject):
    """Reuse the application's JobRunner; bound Qt slots receive detached results."""

    def __init__(self, panel, controller, runner=None, *, host_dispatch=None):
        super().__init__(panel)
        self.panel = panel
        self.controller = controller
        self.runner = runner or JobRunner()
        self.host_dispatch = host_dispatch
        panel.save_requested.connect(self._save)
        panel.apply_requested.connect(self._apply)
        panel.check_requested.connect(self._check)
        panel.reload_requested.connect(self._reload)

    def _dispatch(self, fn, *args):
        if self.panel.busy:
            return
        if self.host_dispatch is not None:
            self.host_dispatch(fn,*args,on_finished=self._finished,on_error=self._failed)
            return
        self.panel.set_busy(True)
        self.runner.run(lambda job: fn(*args), on_finished=self._finished, on_error=self._failed)

    def load(self, transcript_id, revision):
        self._dispatch(self.controller.load, transcript_id, revision)

    @Slot(object, str, str)
    def _save(self, view, word_id, text):
        self._dispatch(self.controller.save_spelling, view, word_id, text)

    @Slot(object)
    def _apply(self, view):
        self._dispatch(self.controller.apply_saved, view)

    @Slot(object)
    def _check(self, view):
        self._dispatch(self.controller.check_editor, view)

    @Slot(str)
    def _reload(self, tid):
        self._dispatch(self.controller.reload_saved, tid)

    @Slot(object)
    def _finished(self, view):
        self.panel.set_view(view)
        self.panel.set_busy(False)

    @Slot(str)
    def _failed(self, error):
        self.panel.set_busy(False)
        self.panel.show_error(
            'Caption update stopped. Reload saved words before another correction.',
        )
        self.panel.setToolTip(error)
