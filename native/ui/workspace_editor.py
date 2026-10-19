"""Bind one canonical native editor owner to the persistent library companion."""
from collections import Counter
import threading
from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton,
    QMessageBox, QVBoxLayout, QWidget,
)


class LayoutSelection(QDialog):
    def __init__(self, context, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Open edit')
        self.setMinimumWidth(400)
        body = QVBoxLayout(self)
        title = QLabel(context.title)
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        body.addWidget(title)
        kit = QLabel(f'{context.kit_name} · version {context.kit_version}')
        kit.setTextFormat(Qt.TextFormat.PlainText)
        kit.setWordWrap(True)
        body.addWidget(kit)
        self.layouts = QComboBox()
        self.layouts.setAccessibleName('Creator Kit layout')
        self.layouts.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.layouts.setMinimumContentsLength(24)
        scene_counts = Counter(scene for _,scene in context.profiles)
        for profile, scene in context.profiles:
            text = f'{profile} · {scene}' if scene_counts[scene] > 1 else scene
            self.layouts.addItem(text, (profile, scene))
            self.layouts.setItemData(self.layouts.count()-1, f'{scene}\nProfile: {profile}', Qt.ItemDataRole.ToolTipRole)
        selected = getattr(context, 'selected_layout', None)
        if selected is not None:
            for index in range(self.layouts.count()):
                if self.layouts.itemData(index) == selected:
                    self.layouts.setCurrentIndex(index)
                    break
        body.addWidget(QLabel('Layout'))
        body.addWidget(self.layouts)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Open | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        body.addWidget(buttons)

    def selected_layout(self):
        return self.layouts.currentData()


class EditorCompanionBinding(QObject):
    """The controller retains live owners; Qt receives detached typed outcomes."""
    def __init__(self, window, controller, *, choose_layout=None):
        super().__init__(window)
        self.window, self.controller = window, controller
        self.runner = window.runner
        self.choose_layout = choose_layout or self._choose_layout
        self.busy = False
        self.cancel = None
        self.cancel_label = 'Cancel opening'
        self.panel = QWidget(window)
        body = QVBoxLayout(self.panel)
        body.setContentsMargins(0, 0, 0, 0)
        self.status = QLabel('Select a Moment to open its edit.')
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setAccessibleName('Native editor status')
        body.addWidget(self.status)
        actions = QHBoxLayout()
        self.save = QPushButton('Save checkpoint')
        self.save.clicked.connect(lambda: self._run(self.controller.checkpoint, 'Saving the edit…'))
        actions.addWidget(self.save)
        self.close_editor = QPushButton('Save and close')
        self.close_editor.clicked.connect(lambda: self._run(self._save_and_close, 'Saving and closing the edit…',
                                                          cancellable=True, cancel_label='Stop waiting'))
        actions.addWidget(self.close_editor)
        self.check_close = QPushButton('Check close')
        self.check_close.clicked.connect(lambda: self._run(self.controller.check_close, 'Checking whether the editor closed…'))
        actions.addWidget(self.check_close)
        self.recover = QPushButton('Recover saved working edit')
        self.recover.clicked.connect(lambda: self._run(self.controller.recover_working, 'Recovering the saved working edit…'))
        actions.addWidget(self.recover)
        self.cleanup = QPushButton('Stop retained process')
        self.cleanup.clicked.connect(self._cleanup)
        actions.addWidget(self.cleanup)
        self.cancel_open = QPushButton('Cancel opening')
        self.cancel_open.clicked.connect(self._cancel_open)
        actions.addWidget(self.cancel_open)
        actions.addStretch()
        body.addLayout(actions)
        layout = getattr(window, 'content_layout', None)
        if layout is None:
            layout = window.centralWidget().layout()
        layout.insertWidget(2, self.panel)
        window.open_moment = self.open_moment
        window.installEventFilter(self)
        self._refresh()

    def _claim(self):
        return getattr(self.window, 'acquire_action', lambda owner: True)(self)

    def _release_if_safe(self):
        outcome = self.controller.status()
        if not ((outcome.requires_reconciliation or outcome.can_cleanup) and not outcome.app_close_allowed):
            getattr(self.window, 'release_action', lambda owner: None)(self)

    def _refresh(self):
        outcome = self._gate_changed()
        self.window._controls()
        return outcome

    def _gate_changed(self):
        outcome = self.controller.status()
        available = getattr(self.window, 'action_allowed', lambda owner: True)(self)
        self.save.setEnabled(available and not self.busy and outcome.can_checkpoint)
        self.close_editor.setEnabled(available and not self.busy and outcome.can_checkpoint and outcome.can_close_editor
                                     and callable(getattr(self.controller, 'save_and_close', None)))
        for button, capability in ((self.check_close, 'can_check_close'), (self.recover, 'can_recover_working')):
            allowed = getattr(outcome, capability, False)
            button.setVisible(allowed)
            button.setEnabled(available and allowed and not self.busy)
        self.cleanup.setVisible(outcome.can_cleanup)
        self.cleanup.setEnabled(available and not self.busy and outcome.can_cleanup)
        self.cancel_open.setVisible(self.cancel is not None)
        self.cancel_open.setText(self.cancel_label)
        self.cancel_open.setEnabled(self.cancel is not None and not self.cancel.is_set())
        self.window.editor_available = available and not self.busy and outcome.app_close_allowed and not outcome.requires_reconciliation
        return outcome

    def _choose_layout(self, context):
        dialog = LayoutSelection(context, self.window)
        return dialog.selected_layout() if dialog.exec() == QDialog.DialogCode.Accepted else None

    def open_moment(self, moment_id, revision):
        if self.busy or not self.window.editor_available or not self._claim():
            return
        self.busy = True
        self.status.setText('Loading the selected Creator Kit…')
        self._refresh()
        self.runner.run(
            lambda job: self.window.library.moment_edit_context(moment_id, revision),
            on_finished=lambda context: self._open_context(context, moment_id, revision),
            on_error=self._unexpected_error,
        )

    def _open_context(self, context, moment_id, revision):
        if not context.profiles:
            self.busy = False
            self._release_if_safe()
            self.status.setText('This Creator Kit needs a layout before its Moment can open.')
            self._refresh()
            return
        try:
            selected = self.choose_layout(context)
        except Exception as error:
            self._unexpected_error(str(error))
            return
        self.busy = False
        if selected is None:
            self._release_if_safe()
            self.status.setText('Opening cancelled.')
            self._refresh()
            return
        profile, scene = selected
        self._run(lambda: self.controller.open(moment_id, revision, profile_id=profile, scene_key=scene,
                                              cancel=self.cancel),
                  'Opening the selected edit…', cancellable=True)

    def _run(self, operation, message, *, cancellable=False, cancel_label='Cancel opening'):
        if self.busy or not self._claim():
            return
        self.busy = True
        self.cancel = threading.Event() if cancellable else None
        self.cancel_label = cancel_label
        self.status.setText(message)
        self._refresh()
        self.runner.run(lambda job: operation(), on_finished=self._finished, on_error=self._unexpected_error)

    def _save_and_close(self):
        return self.controller.save_and_close(cancel=self.cancel)

    def _cleanup(self):
        choice = QMessageBox.warning(self.window, 'Stop retained process',
            'Stopping the editor process can discard unsaved changes. Continue?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if choice == QMessageBox.StandardButton.Yes:
            self._run(self.controller.cleanup, 'Stopping the retained process…')

    def _cancel_open(self):
        if self.cancel is not None:
            self.cancel.set()
            self.status.setText('Stopping the wait. The editor can still finish closing.'
                                if self.cancel_label == 'Stop waiting' else 'Cancelling the editor opening…')
            self._refresh()

    def _finished(self, outcome):
        self.busy = False
        self.cancel = None
        self._release_if_safe()
        self.status.setText(outcome.message)
        self._refresh()

    def _unexpected_error(self, error):
        # The controller owns any live error.owner before JobRunner formats it.
        self.busy = False
        self.cancel = None
        self._release_if_safe()
        self.status.setText(error)
        self._refresh()

    def eventFilter(self, watched, event):
        if watched is self.window and event.type() == QEvent.Type.Close:
            current = self.controller.status()
            if self.busy or not current.app_close_allowed:
                self.status.setText('Save and close the editor before closing Clipline.' if current.can_checkpoint
                                    else 'The editor operation needs attention before Clipline can close.')
                event.ignore()
                return True
        return super().eventFilter(watched, event)
