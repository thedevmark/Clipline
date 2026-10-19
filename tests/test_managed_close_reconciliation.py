"""Named catch-boundary regression: transport loss must not erase a prior fault."""
import unittest
from unittest.mock import patch

from tests import test_managed_close as safe
from tests import test_managed_close_status as observation
from native.services.filmcraft_bridge import EditorUnavailable

class TestRetainedReconciliation(unittest.TestCase):
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
    pending_after = observation.TestStatusObservation.pending_after

    def test_transport_loss_preserves_existing_correlation_reconciliation(self):
        session, barrier, checkpoint = self.pending_after(EditorUnavailable('first transient fixture'))
        original = session.call
        session.quit_mode = 'cancel'
        def foreign(method, params):
            value = original(method, params)
            if method == 'clipline.managedExitStatus':
                value['nonce'] = 'foreign'
            return value
        with patch.object(session, 'call', side_effect=foreign):
            invalid = self.check()
        self.assertTrue(invalid.requires_reconciliation)
        self.assertEqual(invalid.state, 'reconciliation_required')
        reason = self.controller._blocked_reason
        before = barrier.read_bytes()
        def unavailable(method, params):
            if method == 'clipline.managedExitStatus':
                raise EditorUnavailable('second transient fixture')
            return original(method, params)
        with patch.object(session, 'call', side_effect=unavailable):
            final = self.check()
        self.assertEqual(final.state, 'close_pending')
        self.assertTrue(final.requires_reconciliation)
        self.assertEqual(self.controller._blocked_reason, reason)
        self.assertIs(final.owner, session)
        self.assertFalse(final.can_checkpoint)
        self.assertFalse(final.can_close_editor)
        self.assertFalse(final.app_close_allowed)
        self.assertEqual(barrier.read_bytes(), before)
        self.assertEqual(self.store.load(), checkpoint)
        self.assertEqual((session.save_calls, session.snapshots, session.quit_calls, session.stops), (1, 1, 1, 0))
        self.assert_protected()
