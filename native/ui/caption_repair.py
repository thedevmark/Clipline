"""Detached accepted-word panel. Persistence and native calls belong to its binding."""
from PySide6.QtCore import Qt, Signal, QObject, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QDialog,
    QDialogButtonBox,
    QInputDialog,
    QMessageBox,
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


class SpellingMatchDialog(QDialog):
    """Review exact occurrences from one detached accepted revision."""
    def __init__(self, view, word_id, parent=None):
        super().__init__(parent)
        self.setObjectName('stage')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle('Replace matching words')
        self.setStyleSheet(theme.GLOBAL_QSS)
        self.resize(840,540)
        self.source_text = next(word.text for word in view.words if word.id==word_id)
        body=QVBoxLayout(self)
        heading=QLabel(f'Replace “{self.source_text}”')
        heading.setTextFormat(Qt.TextFormat.PlainText);heading.setWordWrap(True)
        heading.setProperty('title',True);body.addWidget(heading)
        note=QLabel('Choose the occurrences to change in this accepted transcript. Matching is exact and case-sensitive. Source timing stays the same.')
        note.setWordWrap(True);body.addWidget(note)
        row=QHBoxLayout();label=QLabel('New spelling')
        self.replacement=QLineEdit();self.replacement.setMaxLength(512)
        self.replacement.setAccessibleName('Replacement spelling for checked occurrences');label.setBuddy(self.replacement)
        row.addWidget(label);row.addWidget(self.replacement,1);body.addLayout(row)
        matches=[(index,word) for index,word in enumerate(view.words) if word.text==self.source_text]
        self.table=QTableWidget(len(matches),3)
        self.table.setHorizontalHeaderLabels(['Use','Source time','Words around the match'])
        self.table.setAccessibleName('Exact matching accepted-word occurrences')
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeMode.Stretch)
        for row,(index,word) in enumerate(matches):
            check=QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsUserCheckable|Qt.ItemFlag.ItemIsSelectable)
            check.setData(Qt.ItemDataRole.UserRole,word.id)
            check.setCheckState(Qt.CheckState.Checked if word.id==word_id else Qt.CheckState.Unchecked)
            self.table.setItem(row,0,check)
            self.table.setItem(row,1,QTableWidgetItem(source_time(word.start_us)))
            text=' '.join(('['+view.words[i].text+']') if i==index else view.words[i].text
                for i in range(max(0,index-3),min(len(view.words),index+4)))
            self.table.setItem(row,2,QTableWidgetItem(text))
        body.addWidget(self.table,1)
        selection=QHBoxLayout()
        self.select_all=QPushButton('Select all matches')
        self.clear=QPushButton('Clear selection')
        self.select_all.clicked.connect(lambda:self._select(Qt.CheckState.Checked))
        self.clear.clicked.connect(lambda:self._select(Qt.CheckState.Unchecked))
        selection.addWidget(self.select_all);selection.addWidget(self.clear);selection.addStretch()
        body.addLayout(selection)
        self.buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Save|QDialogButtonBox.StandardButton.Cancel)
        self.save=self.buttons.button(QDialogButtonBox.StandardButton.Save);self.save.setProperty('primary',True)
        self.buttons.accepted.connect(self.accept);self.buttons.rejected.connect(self.reject)
        body.addWidget(self.buttons)
        self.table.itemChanged.connect(self._actions);self.replacement.textChanged.connect(self._actions)
        self._actions();self.replacement.setFocus()

    def selected_ids(self):
        return [self.table.item(row,0).data(Qt.ItemDataRole.UserRole) for row in range(self.table.rowCount())
                if self.table.item(row,0).checkState()==Qt.CheckState.Checked]

    def _select(self,state):
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):self.table.item(row,0).setCheckState(state)
        self.table.blockSignals(False)
        self._actions()

    def _actions(self,*args):
        text=self.replacement.text();count=len(self.selected_ids())
        self.save.setText(f'Replace selected ({count})')
        self.save.setEnabled(count>0 and bool(text) and text!=self.source_text
            and not any(c.isspace() or ord(c)<32 for c in text))

    def accept(self):
        if self.save.isEnabled():super().accept()

class CaptionRepairPanel(QWidget):
    save_requested = Signal(object, str, str)
    apply_requested = Signal(object)
    check_requested = Signal(object)
    reload_requested = Signal(str)
    structure_requested = Signal(object, str, object, object)
    matching_requested = Signal(object, str, str, object)

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
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
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
        self.matching = QPushButton('Replace matching…')
        self.matching.clicked.connect(self._matching)
        row.addWidget(self.matching)
        layout.addLayout(row)
        structural = QHBoxLayout()
        self.split = QPushButton('Split word…')
        self.merge = QPushButton('Merge words…')
        self.delete = QPushButton('Delete…')
        for button, action in ((self.split, 'split'), (self.merge, 'merge'), (self.delete, 'delete')):
            button.clicked.connect(lambda checked=False, operation=action: self._structure(operation))
            structural.addWidget(button)
        structural.addStretch()
        layout.addLayout(structural)
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
        words = self.selected_words()
        return words[0] if len(words) == 1 else None

    def selected_words(self):
        if self.view is None:
            return ()
        rows = sorted(index.row() for index in self.table.selectionModel().selectedRows())
        return tuple(self.view.words[row] for row in rows if 0 <= row < len(self.view.words))

    def contiguous_selection(self):
        rows = sorted(index.row() for index in self.table.selectionModel().selectedRows())
        return bool(rows) and rows == list(range(rows[0], rows[-1] + 1))

    def has_draft(self):
        word = self.selected_word()
        return bool(word and self.spelling.text() != word.text)

    def _selected(self):
        word = self.selected_word()
        self.spelling.setText(word.text if word else '')
        if word:
            self.detail.setText(
                f'Source {source_time(word.start_us)}–{source_time(word.end_us)} · {word.provenance}',
            )
        else:
            count = len(self.selected_words())
            self.detail.setText((f'{count} adjacent words selected. Merge or delete this selection.'
                if self.contiguous_selection() else 'Select adjacent rows to merge or delete these words.') if count > 1
                                else 'Select one word to correct its spelling or split it. Shift-select adjacent words to merge.')
        self._actions()

    def _actions(self):
        word = self.selected_word()
        text = self.spelling.text()
        editable = bool(self.view and self.view.can_edit and (not self.busy) and (not self._error))
        self.spelling.setEnabled(editable and word is not None)
        self.save.setEnabled(
            editable and word is not None and (text != word.text) and bool(text) and (not any((c.isspace() or ord(c) < 32 for c in text))),
        )
        self.matching.setEnabled(editable and word is not None and not self.has_draft())
        self.reload.setEnabled(self.view is not None and (not self.busy))
        self.apply.setVisible(bool(self.view and self.view.state == 'saved_pending'))
        self.apply.setEnabled(
            bool(self.view and self.view.can_apply and (not self.busy) and (not self._error)),
        )
        self.check.setVisible(bool(self.view and self.view.state == 'saved_uncertain'))
        self.check.setEnabled(not self.busy)
        self.table.setEnabled(not self.busy)
        count = len(self.selected_words())
        structural = bool(self.view and self.view.can_structure and not self.busy and not self._error and not self.has_draft())
        self.split.setEnabled(structural and count == 1)
        self.merge.setEnabled(structural and count >= 2 and self.contiguous_selection())
        self.delete.setEnabled(structural and count >= 1 and self.contiguous_selection())
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

    def _matching(self):
        if not self.matching.isEnabled():return
        dialog=SpellingMatchDialog(self.view,self.selected_word().id,self)
        if dialog.exec()==QDialog.DialogCode.Accepted:
            self.matching_requested.emit(self.view,dialog.source_text,dialog.replacement.text(),dialog.selected_ids())

    def _structure(self, action):
        button = {'split': self.split, 'merge': self.merge, 'delete': self.delete}.get(action)
        if button is None or not button.isEnabled():
            return
        words = self.selected_words()
        if action == 'delete':
            confirm = QMessageBox(self)
            confirm.setObjectName('stage')
            confirm.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            confirm.setWindowTitle('Delete selected words?')
            confirm.setTextFormat(Qt.TextFormat.PlainText)
            confirm.setText(f'Delete {len(words)} selected word' + ('s?' if len(words) != 1 else '?'))
            confirm.setInformativeText('Previous accepted revisions remain available. The source footage does not change.')
            confirm.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
            confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
            if confirm.exec() != QMessageBox.StandardButton.Yes:
                return
            texts = []
        else:
            dialog = QInputDialog(self)
            dialog.setObjectName('stage')
            dialog.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            dialog.setInputMode(QInputDialog.InputMode.TextInput)
            dialog.setWindowTitle('Split word' if action == 'split' else 'Merge words')
            dialog.setLabelText('Enter the new words, separated by spaces. Set their source times next.'
                if action == 'split' else 'Enter one merged word. Known outer source times are retained; missing times still need review.')
            dialog.setTextValue(words[0].text if action == 'split' else ''.join(word.text for word in words))
            dialog.setOkButtonText('Save split' if action == 'split' else 'Save merge')
            dialog.findChild(QLineEdit).setMaxLength(4096)
            if dialog.exec() != QInputDialog.DialogCode.Accepted:
                return
            texts = dialog.textValue().split() if action == 'split' else [dialog.textValue().strip()]
            if (action == 'split' and len(texts) < 2) or any(not text or len(text) > 512 or any(c.isspace() or ord(c) < 32 for c in text) for text in texts):
                self.message.setText('Enter at least two words for a split, or one word without spaces for a merge.')
                return
        self.structure_requested.emit(self.view, action, [word.id for word in words], texts)

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
        panel.structure_requested.connect(self._structure)
        panel.matching_requested.connect(self._matching)

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

    @Slot(object, str, object, object)
    def _structure(self, view, action, word_ids, texts):
        self._dispatch(self.controller.save_structure, view, action, word_ids, texts)

    @Slot(object, str, str, object)
    def _matching(self,view,source_text,text,word_ids):
        self._dispatch(self.controller.save_matching_spelling,view,source_text,text,word_ids)

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
