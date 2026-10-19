"""Read-only shutdown observation failures do not create document uncertainty."""
import json
import threading
import unittest
from unittest.mock import patch

from tests import test_managed_close as safe
from native.services.filmcraft_bridge import OutcomeUnknown, EditorUnavailable


class TestStatusObservation(unittest.TestCase):
    setUp = safe.SafeCloseTests.setUp
    tearDown = safe.SafeCloseTests.tearDown
    provenance = safe.SafeCloseTests.provenance
    create = safe.SafeCloseTests.create
    open = safe.SafeCloseTests.open
    checkpoint = safe.SafeCloseTests.checkpoint
    cleanup = safe.SafeCloseTests.cleanup
    assert_protected = safe.SafeCloseTests.assert_protected
    assert_writable = safe.SafeCloseTests.assert_writable
    start = safe.SafeCloseTests.start
    save_close = safe.SafeCloseTests.save_close
    check = safe.SafeCloseTests.check

    def pending_after(self, error):
        self.assertEqual(self.open().state, 'open')
        session = self.sessions[0]
        session.quit_mode = 'pending'
        original = session.call
        calls = []
        def unavailable(method, params):
            if method == 'clipline.managedExitStatus':
                calls.append((method, params))
                raise error
            return original(method, params)
        stop_waiting = threading.Event()
        stop_waiting.set()
        with patch.object(session, 'call', side_effect=unavailable):
            outcome = self.save_close(cancel=stop_waiting)
        self.assertEqual(outcome.state, 'close_pending', outcome.message)
        self.assertFalse(outcome.requires_reconciliation)
        self.assertFalse(outcome.app_close_allowed)
        self.assertTrue(outcome.can_check_close)
        self.assertFalse(outcome.can_checkpoint)
        self.assertFalse(outcome.can_close_editor)
        self.assertIs(outcome.owner, session)
        self.assertIs(self.controller._session, session)
        self.assertIsNone(self.controller._blocked_reason)
        self.assertEqual(len(calls), 1)
        self.assertEqual((session.save_calls, session.snapshots, session.quit_calls, session.stops), (1, 1, 1, 0))
        barrier = self.store.directory / 'editor-lifecycle.json'
        self.assertEqual(json.loads(barrier.read_bytes())['state'], 'quit_pending')
        self.assert_protected()
        return session, barrier, self.store.load()

    def test_status_timeout_then_scope_zero_publishes_saved_content_once(self):
        session, barrier, checkpoint = self.pending_after(
            OutcomeUnknown('status-request', 'clipline.managedExitStatus', 'native server stopped waiting'))
        # The next observation is still live. It neither resends a mutation nor publishes.
        self.assertEqual(self.check().state, 'close_pending')
        self.assertEqual(self.store.load(), checkpoint)
        session.saved = session.content = b'OPAQUE FINAL SAVE AFTER STATUS TIMEOUT'
        session.path.write_bytes(session.saved)
        session.emit('saved')
        publish = self.controller._store.publish
        with patch.object(self.controller._store, 'publish', wraps=publish) as publication:
            final = self.check()
        self.assertEqual(final.state, 'closed', final.message)
        self.assertTrue(final.app_close_allowed)
        self.assertFalse(final.requires_reconciliation)
        self.assertEqual(publication.call_count, 1)
        self.assertEqual(self.store.load().document, session.saved)
        self.assertEqual(self.store.load().revision, checkpoint.revision + 1)
        self.assertFalse(barrier.exists())
        self.assertEqual((session.save_calls, session.snapshots, session.quit_calls, session.stops), (1, 1, 1, 0))
        self.assert_writable()

    def test_status_unavailable_then_correlated_cancel_restores_open(self):
        session, barrier, checkpoint = self.pending_after(EditorUnavailable('native listener is closing'))
        before = barrier.read_bytes()
        session.quit_mode = 'cancel'
        final = self.check()
        self.assertEqual(final.state, 'open', final.message)
        self.assertFalse(final.requires_reconciliation)
        self.assertTrue(final.can_checkpoint)
        self.assertTrue(final.can_close_editor)
        self.assertEqual(self.store.load(), checkpoint)
        self.assertEqual(json.loads(barrier.read_bytes())['state'], 'open')
        self.assertNotEqual(before, barrier.read_bytes())
        self.assertEqual((session.save_calls, session.snapshots, session.quit_calls, session.stops), (1, 1, 1, 0))
        self.assert_protected()

    def test_status_unavailable_does_not_allow_foreign_nonce_to_restore_open(self):
        session, barrier, checkpoint = self.pending_after(EditorUnavailable('native listener unavailable'))
        original = session.call
        def foreign(method, params):
            value = original(method, params)
            if method == 'clipline.managedExitStatus':
                value['nonce'] = 'foreign'
            return value
        session.quit_mode = 'cancel'
        with patch.object(session, 'call', side_effect=foreign):
            final = self.check()
        self.assertTrue(final.requires_reconciliation)
        self.assertEqual(final.state, 'reconciliation_required')
        self.assertFalse(final.can_close_editor)
        self.assertEqual(self.store.load(), checkpoint)
        self.assertTrue(barrier.exists())
        self.assertEqual((session.save_calls, session.snapshots, session.quit_calls, session.stops), (1, 1, 1, 0))
        self.assert_protected()
