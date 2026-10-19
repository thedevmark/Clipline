"""A successful Save followed by absent control is not a read-only pending case."""
import unittest
from unittest.mock import patch

from tests import test_managed_close as safe
from tests import test_managed_close_availability as availability

class TestMutationBoundary(unittest.TestCase):
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
    attach_real_preflight = availability.TestControllerRealPreflight.attach_real_preflight

    def test_availability_lost_after_save_does_not_retry_or_claim_pending_close(self):
        self.assertEqual(self.open().state, 'open')
        original, owner = self.attach_real_preflight()
        initial = self.store.load()
        session_module = availability.session_module
        with patch.object(session_module, 'listener_pids', return_value={77}) as listeners, \
                patch.object(session_module, 'in_job', return_value=True):
            def wire(bridge, method, params=None):
                self.assertEqual(method, 'engine.execute')
                result = original.execute(params['command'], params['params'])
                if params['command'] == 'file.save':
                    listeners.return_value = set()
                return result
            with patch.object(session_module.FilmcraftBridge, 'call', wire):
                result = self.save_close()
        self.assertEqual(result.state, 'reconciliation_required')
        self.assertTrue(result.requires_reconciliation)
        self.assertIs(result.owner, owner)
        self.assertIs(self.controller._session, owner)
        self.assertFalse(result.app_close_allowed)
        self.assertFalse(result.can_checkpoint)
        self.assertFalse(result.can_close_editor)
        self.assertEqual(self.store.load(), initial)
        self.assertEqual((original.save_calls, original.snapshots, original.quit_calls, original.stops), (1, 0, 0, 0))
        self.assertEqual(self.save_close().state, 'blocked')
        self.assertEqual((original.save_calls, original.snapshots, original.quit_calls, original.stops), (1, 0, 0, 0))
        self.assert_protected()
