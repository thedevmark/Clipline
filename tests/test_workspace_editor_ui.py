"""Real Qt delivery and close/save boundaries around a detached editor owner."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import threading
import time
from types import SimpleNamespace
import unittest

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QPushButton, QVBoxLayout, QWidget

from native.services.workspace_library import MomentEditContext
from native.ui.workspace_editor import EditorCompanionBinding
from native.workers import JobRunner


def state(name='idle', *, owner=None, uncertain=False):
    opened = name == 'open'
    return SimpleNamespace(state=name, message=name, owner=owner, requires_reconciliation=uncertain,
        can_checkpoint=opened, can_close_editor=opened, can_cleanup=owner is not None,
        app_close_allowed=owner is None)


class Controller:
    def __init__(self):
        self.value = state()
        self.started, self.release = threading.Event(), threading.Event()
        self.open_calls = self.checkpoints = self.closes = 0
        self.cancel = None
        self.fail_save = False
        self.owner = object()

    def status(self):
        return self.value

    def open(self, moment, revision, *, profile_id, scene_key, cancel):
        self.open_calls += 1
        self.cancel = cancel
        self.started.set()
        if not self.release.wait(3):
            raise RuntimeError('Authored open fixture was not released')
        self.value = state('reconciliation_required', owner=self.owner, uncertain=True)
        return self.value

    def checkpoint(self):
        self.checkpoints += 1
        if self.fail_save:
            self.value = state('reconciliation_required', owner=self.owner, uncertain=True)
        return self.value

    def close(self):
        self.closes += 1
        self.value = state('closed')
        return self.value

    def save_and_close(self, *, cancel=None):
        saved = self.checkpoint()
        return saved if saved.requires_reconciliation else self.close()

    def cleanup(self):
        self.value = state('closed', uncertain=True)
        return self.value


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.runner = JobRunner()
        self.editor_available = True
        self.open = QPushButton('Open edit')
        self.setCentralWidget(QWidget())
        body = QVBoxLayout(self.centralWidget())
        body.addWidget(QLabel('Clipline'))
        body.addWidget(self.open)
        self.library = SimpleNamespace(moment_edit_context=lambda *a:
            MomentEditContext('Authored moment', 'Pinned Kit', 1, (('layout', 'scene'),)))

    def _controls(self):
        self.open.setEnabled(self.editor_available)


class EditorBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.window = Window()
        self.controller = Controller()
        self.binding = EditorCompanionBinding(self.window, self.controller,
            choose_layout=lambda context: context.profiles[0])

    def pump(self, predicate, message):
        deadline = time.monotonic() + 5
        while not predicate():
            self.app.processEvents()
            if time.monotonic() >= deadline:
                self.fail(message)
            time.sleep(.005)
        self.app.processEvents()

    def tearDown(self):
        self.controller.release.set()
        self.pump(lambda: not self.window.runner._jobs, 'Qt worker did not finish')
        self.controller.value = state('closed')
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_open_cancellation_and_unknown_owner_survive_real_worker_delivery(self):
        self.binding.open_moment('authored-moment', 1)
        self.pump(self.controller.started.is_set, 'Editor owner did not receive selection')
        self.assertFalse(self.window.open.isEnabled())
        self.binding.cancel_open.click()
        self.assertTrue(self.controller.cancel.is_set())
        self.controller.release.set()
        self.pump(lambda: not self.binding.busy, 'Typed editor outcome was not delivered')
        self.assertIs(self.controller.status().owner, self.controller.owner)
        self.assertFalse(self.window.editor_available)
        event = QCloseEvent()
        self.assertTrue(self.binding.eventFilter(self.window, event))
        self.assertFalse(event.isAccepted())
        self.binding.open_moment('another-moment', 2)
        self.assertEqual(self.controller.open_calls, 1)

    def test_failed_checkpoint_never_closes_unsaved_editor(self):
        self.controller.value = state('open', owner=self.controller.owner)
        self.controller.fail_save = True
        self.binding._refresh()
        self.binding.close_editor.click()
        self.pump(lambda: not self.binding.busy, 'Save failure was not delivered')
        self.assertEqual(self.controller.checkpoints, 1)
        self.assertEqual(self.controller.closes, 0)
        self.assertTrue(self.controller.status().requires_reconciliation)

    def test_save_and_close_uses_one_checkpoint_then_one_owned_close(self):
        self.controller.value = state('open', owner=self.controller.owner)
        self.binding._refresh()
        self.binding.close_editor.click()
        self.pump(lambda: not self.binding.busy, 'Saved editor did not close')
        self.assertEqual((self.controller.checkpoints, self.controller.closes), (1, 1))
        self.assertTrue(self.window.editor_available)

    def test_cancelled_layout_choice_never_starts_editor(self):
        self.binding.choose_layout = lambda context: None
        self.binding.open_moment('authored-moment', 1)
        self.pump(lambda: not self.binding.busy, 'Cancelled layout did not return')
        self.assertEqual(self.controller.open_calls, 0)
        self.assertTrue(self.window.editor_available)

    def test_legacy_force_close_is_never_a_save_and_close_fallback(self):
        self.controller.value = state('open', owner=self.controller.owner)
        self.controller.save_and_close = None
        self.binding._refresh()
        self.assertFalse(self.binding.close_editor.isEnabled())
        self.binding.close_editor.click()
        self.assertEqual((self.controller.checkpoints, self.controller.closes), (0, 0))
