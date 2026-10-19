"""Download the selected cached source through the existing durable intake owner."""
import threading

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from native.services.workspace_intake import SourceView


class IntakeCompanionBinding(QObject):
    """Keep the controller and its actual retained capabilities alive with this window."""
    def __init__(self, window, controller):
        super().__init__(window)
        self.window, self.controller = window, controller
        self.busy = False
        self.cancel = None
        self.panel = QWidget(window)
        body = QVBoxLayout(self.panel)
        body.setContentsMargins(0, 0, 0, 0)
        self.status = QLabel(controller.status().message)
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setAccessibleName('Clip source status')
        body.addWidget(self.status)
        self.task_identity = QLabel()
        self.task_identity.setWordWrap(True)
        self.task_identity.setTextFormat(Qt.TextFormat.PlainText)
        self.task_identity.setAccessibleName('Retained source task identity')
        body.addWidget(self.task_identity)
        actions = QHBoxLayout()
        self.download = QPushButton('Download source')
        self.download.setProperty('primary', True)
        self.download.clicked.connect(self._download)
        self.authorize = QPushButton('Allow clip downloads')
        self.authorize.clicked.connect(self._authorize)
        self.resume = QPushButton('Resume queued source')
        self.resume.clicked.connect(lambda: self._start(
            lambda job: self.controller.resume(cancel=self.cancel), 'Checking the queued source slot…', cancellable=True))
        self.cancel_action = QPushButton('Cancel source task')
        self.cancel_action.clicked.connect(self._cancel)
        self.cleanup = QPushButton('Stop retained source work')
        self.cleanup.clicked.connect(lambda: self._start(
            lambda job: self.controller.cleanup(), 'Stopping retained source resources…'))
        for button in (self.download, self.authorize, self.resume, self.cancel_action, self.cleanup):
            actions.addWidget(button)
        actions.addStretch()
        body.addLayout(actions)
        layout = window.content_layout
        layout.insertWidget(layout.indexOf(window.twitch) + 1, self.panel)
        window.intake_binding = self
        window.installEventFilter(self)
        self._refresh()

    def _selected_clip(self):
        return self.window.selected_entry() if self.window.kind == 'clips' else None

    def _gate_changed(self):
        view = self.controller.status()
        available = self.window.action_allowed(self) and not self.busy
        selected = self._selected_clip() is not None
        # This identity belongs to the admitted task, not the current table row.
        self.task_identity.setText('Source task: ' + view.clip_id if view.clip_id is not None else '')
        self.task_identity.setVisible(view.clip_id is not None)
        self.panel.setVisible(self.window.kind == 'clips' or not view.app_close_allowed)
        self.download.setEnabled(available and selected and view.can_download)
        self.authorize.setEnabled(available and selected and view.can_authorize)
        self.resume.setVisible(view.can_resume)
        self.resume.setEnabled(available and view.can_resume)
        self.cleanup.setVisible(view.can_cleanup)
        self.cleanup.setEnabled(available and view.can_cleanup)
        self.cancel_action.setVisible(self.cancel is not None or view.can_cancel)
        self.cancel_action.setEnabled((self.cancel is not None and not self.cancel.is_set())
                                      or available and view.can_cancel)

    def _refresh(self):
        view = self.controller.status()
        if not self.busy and view.app_close_allowed:
            self.window.release_action(self)
        self._gate_changed()
        self.window._controls()

    def _download(self):
        entry = self._selected_clip()
        if entry is not None and self.controller.status().can_download:
            clip_id = entry.id
            self._start(lambda job: self.controller.download(clip_id, cancel=self.cancel),
                        'Downloading the selected clip source…', cancellable=True)

    def _authorize(self):
        entry = self._selected_clip()
        if entry is not None and self.controller.status().can_authorize:
            clip_id = entry.id
            self._start(lambda job: self.controller.authorize(clip_id, job, cancel=self.cancel),
                        'Allow source downloads in your browser for the selected channel.', cancellable=True)

    def _start(self, operation, message, *, cancellable=False):
        if self.busy or not self.window.acquire_action(self):
            return
        self.busy = True
        self.cancel = threading.Event() if cancellable else None
        self.status.setText(message)
        self._refresh()
        try:
            self.window.runner.run(operation, on_finished=self._finished, on_error=self._unexpected_error)
        except Exception:
            # No child starts when the existing runner rejects admission.
            self._unexpected_error('Source work could not start.')

    def _cancel(self):
        if self.cancel is not None:
            self.cancel.set()
            self.status.setText('Cancelling source work. Waiting for its owned resources to stop…')
            self._refresh()
        elif self.controller.status().can_cancel:
            self._start(lambda job: self.controller.cancel_pending(), 'Cancelling the queued source task…')

    def _finished(self, view):
        self.busy = False
        self.cancel = None
        if type(view) is not SourceView:
            self._unexpected_error('Source work returned an unsupported result.')
            return
        # Retention and canonical settlement happened in the controller before this callback.
        self.status.setText(view.message)
        self._refresh()

    def _unexpected_error(self, _):
        self.busy = False
        self.cancel = None
        view = self.controller.status()
        self.status.setText(view.message if not view.app_close_allowed
                            else 'Source work did not finish. Check its saved state before another action.')
        self._refresh()

    def eventFilter(self, watched, event):
        if watched is self.window and event.type() == QEvent.Type.Close:
            if self.busy or not self.controller.status().app_close_allowed:
                if self.cancel is not None:
                    self.cancel.set()
                self.status.setText('Finish or cancel the source task, then stop any retained source resources before closing Clipline.')
                event.ignore()
                return True
        return super().eventFilter(watched, event)
