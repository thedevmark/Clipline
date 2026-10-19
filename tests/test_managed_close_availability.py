"""Exercise the real NativeSession property and SessionBridge preflight.

Only process polling, listener/job queries and wire transport are boundary doubles.
No process or control socket is created.
"""
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

from tests import test_managed_close as safe

session_module = safe.session_module


def real_owner(directory, original=None):
    owner = session_module.NativeSession.__new__(session_module.NativeSession)
    if original is not None:
        owner.__dict__.update(original.__dict__)
        # These are the explicit process lifetime boundary doubles. Preflight,
        # the bridge property and SessionBridge.call remain the real methods.
        owner.observe_exit = original.observe_exit
        owner.stop = original.stop
    owner.directory = Path(directory)
    owner._stopped = owner._stop_final = False
    owner.port, owner.child_pid = 12345, 77
    owner.process = SimpleNamespace(poll=Mock(return_value=None))
    owner.tree = SimpleNamespace()
    owner._bridge = session_module.SessionBridge(owner)
    return owner


class TestRealSessionPreflight(unittest.TestCase):
    def setUp(self):
        self.owner = real_owner(Path.cwd() / 'not-launched')
        self.patches = ExitStack()
        self.listeners = self.patches.enter_context(patch.object(session_module, 'listener_pids', return_value={77}))
        self.membership = self.patches.enter_context(patch.object(session_module, 'in_job', return_value=True))
        self.transport = self.patches.enter_context(patch.object(session_module.FilmcraftBridge, 'call', return_value={'read': True}))

    def tearDown(self):
        self.patches.close()

    def assert_unavailable(self, operation):
        with self.assertRaises(session_module.SessionUnavailable) as raised:
            operation()
        self.assertIs(raised.exception.owner, self.owner)
        self.assertEqual(raised.exception.directory, self.owner.directory)
        self.transport.assert_not_called()

    def assert_ownership_error(self, operation):
        with self.assertRaises(session_module.SessionError) as raised:
            operation()
        self.assertNotIsInstance(raised.exception, session_module.SessionUnavailable)
        self.assertIs(raised.exception.owner, self.owner)
        self.assertEqual(raised.exception.directory, self.owner.directory)
        self.transport.assert_not_called()
        return raised.exception

    def test_both_property_and_call_keep_the_original_listener_and_job_checks(self):
        self.assertEqual(self.owner.bridge.call('ui.inspect', {}), {'read': True})
        self.assertEqual(self.listeners.call_count, 2)
        self.assertEqual(self.membership.call_count, 2)
        self.transport.assert_called_once()

    def test_empty_listener_between_property_and_call_is_typed_unavailable(self):
        self.listeners.side_effect = [{77}, set()]
        bridge = self.owner.bridge
        self.assert_unavailable(lambda: bridge.call('clipline.managedExitStatus', {}))
        self.assertEqual(self.listeners.call_count, 2)
        self.assertEqual(self.membership.call_count, 1)

    def test_parent_exit_between_property_and_call_is_typed_unavailable(self):
        self.owner.process.poll.side_effect = [None, 0]
        bridge = self.owner.bridge
        self.assert_unavailable(lambda: bridge.call('clipline.managedExitStatus', {}))
        self.assertEqual(self.listeners.call_count, 1)

    def test_parent_exit_before_property_is_typed_unavailable(self):
        self.owner.process.poll.return_value = 0
        self.assert_unavailable(lambda: self.owner.bridge)
        self.listeners.assert_not_called()

    def test_known_unavailable_session_state_is_not_scope_cessation(self):
        self.owner._stopped = True
        self.assert_unavailable(lambda: self.owner.bridge)
        self.assertFalse(self.owner._stop_final)
        self.owner._stopped = False
        self.owner._bridge = None
        self.assert_unavailable(lambda: self.owner.bridge)
        self.assertFalse(self.owner._stop_final)
        self.listeners.assert_not_called()

    def test_nonempty_foreign_listener_is_ownership_error(self):
        self.listeners.return_value = {88}
        self.assert_ownership_error(lambda: self.owner.bridge)
        self.membership.assert_not_called()

    def test_extra_foreign_listener_between_property_and_call_is_ownership_error(self):
        self.listeners.side_effect = [{77}, {77, 88}]
        bridge = self.owner.bridge
        self.assert_ownership_error(lambda: bridge.call('clipline.managedExitStatus', {}))
        self.assertEqual(self.membership.call_count, 1)

    def test_wrong_job_is_ownership_error(self):
        self.membership.return_value = False
        self.assert_ownership_error(lambda: self.owner.bridge)

    def test_listener_query_error_keeps_owner_and_cause(self):
        failure = OSError('listener query unavailable')
        self.listeners.side_effect = failure
        self.assertIs(self.assert_ownership_error(lambda: self.owner.bridge).__cause__, failure)

    def test_job_query_error_keeps_owner_and_cause(self):
        failure = OSError('job query unavailable')
        self.membership.side_effect = failure
        self.assertIs(self.assert_ownership_error(lambda: self.owner.bridge).__cause__, failure)

    def test_unknown_listener_inventory_is_not_known_absence(self):
        self.listeners.return_value = None
        self.assert_ownership_error(lambda: self.owner.bridge)
        self.membership.assert_not_called()


class TestControllerRealPreflight(unittest.TestCase):
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

    def attach_real_preflight(self):
        original = self.sessions[0]
        owner = real_owner(original.directory, original)
        self.controller._session = owner
        self.controller._view = replace(self.controller._view, owner=owner)
        return original, owner

    def pending(self):
        self.assertEqual(self.open().state, 'open')
        self.sessions[0].quit_mode = 'pending'
        stop = threading.Event()
        stop.set()
        self.assertEqual(self.save_close(cancel=stop).state, 'close_pending')
        return self.attach_real_preflight()

    def assert_pending(self, result, owner, checkpoint, barrier_bytes):
        self.assertEqual(result.state, 'close_pending', result.message)
        self.assertFalse(result.requires_reconciliation)
        self.assertFalse(result.app_close_allowed)
        self.assertTrue(result.can_check_close)
        self.assertIs(self.controller._session, owner)
        self.assertIs(result.owner, owner)
        self.assertEqual(self.store.load(), checkpoint)
        self.assertEqual((self.store.directory / 'editor-lifecycle.json').read_bytes(), barrier_bytes)
        self.assert_protected()

    def test_empty_listener_preflight_stays_pending_then_scope_zero_publishes_once(self):
        original, owner = self.pending()
        checkpoint = self.store.load()
        barrier = (self.store.directory / 'editor-lifecycle.json').read_bytes()
        with patch.object(session_module, 'listener_pids', return_value=set()), \
                patch.object(session_module.FilmcraftBridge, 'call') as transport:
            result = self.check()
        self.assert_pending(result, owner, checkpoint, barrier)
        transport.assert_not_called()
        original.saved = original.content = b'OPAQUE SAVED AFTER REAL PREFLIGHT ABSENCE'
        original.path.write_bytes(original.saved)
        original.emit('saved')
        publish = self.controller._store.publish
        with patch.object(self.controller._store, 'publish', wraps=publish) as publication:
            final = self.check()
        self.assertEqual(final.state, 'closed', final.message)
        self.assertTrue(final.app_close_allowed)
        self.assertEqual(publication.call_count, 1)
        self.assertEqual(self.store.load().document, original.saved)
        self.assertEqual((original.save_calls, original.snapshots, original.quit_calls, original.stops), (1, 1, 1, 0))

    def test_listener_disappears_between_property_and_call_stays_pending(self):
        original, owner = self.pending()
        checkpoint = self.store.load()
        barrier = (self.store.directory / 'editor-lifecycle.json').read_bytes()
        with patch.object(session_module, 'listener_pids', side_effect=[{77}, set()]) as listeners, \
                patch.object(session_module, 'in_job', return_value=True) as membership, \
                patch.object(session_module.FilmcraftBridge, 'call') as transport:
            result = self.check()
        self.assert_pending(result, owner, checkpoint, barrier)
        self.assertEqual((listeners.call_count, membership.call_count), (2, 1))
        transport.assert_not_called()
        self.assertEqual((original.save_calls, original.snapshots, original.quit_calls, original.stops), (1, 1, 1, 0))

    def test_parent_exit_after_scope_observation_still_waits_for_scope_zero(self):
        original, owner = self.pending()
        checkpoint = self.store.load()
        barrier = (self.store.directory / 'editor-lifecycle.json').read_bytes()
        owner.process.poll.return_value = 0
        # observe_exit still reports the retained scope running. Parent exit alone is insufficient.
        self.assert_pending(self.check(), owner, checkpoint, barrier)
        self.assertEqual((original.save_calls, original.snapshots, original.quit_calls, original.stops), (1, 1, 1, 0))

    def ownership_fault(self, kind):
        original, owner = self.pending()
        checkpoint = self.store.load()
        with ExitStack() as stack:
            listeners = stack.enter_context(patch.object(session_module, 'listener_pids', return_value={77}))
            membership = stack.enter_context(patch.object(session_module, 'in_job', return_value=True))
            transport = stack.enter_context(patch.object(session_module.FilmcraftBridge, 'call'))
            if kind == 'foreign': listeners.return_value = {88}
            elif kind == 'wrong_job': membership.return_value = False
            elif kind == 'query_error': listeners.side_effect = OSError('injected listener query fault')
            result = self.check()
        self.assertEqual(result.state, 'reconciliation_required')
        self.assertTrue(result.requires_reconciliation)
        self.assertFalse(result.can_close_editor)
        self.assertIs(self.controller._session, owner)
        self.assertIs(result.owner, owner)
        self.assertEqual(self.store.load(), checkpoint)
        transport.assert_not_called()
        self.assertEqual((original.save_calls, original.snapshots, original.quit_calls, original.stops), (1, 1, 1, 0))
        self.assert_protected()

    def test_foreign_listener_still_requires_reconciliation(self):
        self.ownership_fault('foreign')

    def test_wrong_job_still_requires_reconciliation(self):
        self.ownership_fault('wrong_job')

    def test_listener_query_error_still_requires_reconciliation(self):
        self.ownership_fault('query_error')

    def test_typed_availability_keeps_preexisting_reconciliation_and_barrier(self):
        original, owner = self.pending()
        original.quit_mode = 'cancel'
        status = original.call('clipline.managedExitStatus', {})
        status['nonce'] = 'foreign'
        with patch.object(session_module, 'listener_pids', return_value={77}), \
                patch.object(session_module, 'in_job', return_value=True), \
                patch.object(session_module.FilmcraftBridge, 'call', return_value=status):
            self.assertTrue(self.check().requires_reconciliation)
        reason = self.controller._blocked_reason
        barrier = (self.store.directory / 'editor-lifecycle.json').read_bytes()
        checkpoint = self.store.load()
        with patch.object(session_module, 'listener_pids', return_value=set()):
            final = self.check()
        self.assertEqual(final.state, 'close_pending')
        self.assertTrue(final.requires_reconciliation)
        self.assertEqual(self.controller._blocked_reason, reason)
        self.assertEqual((self.store.directory / 'editor-lifecycle.json').read_bytes(), barrier)
        self.assertEqual(self.store.load(), checkpoint)
        self.assertIs(final.owner, owner)
        self.assert_protected()

    def test_mutation_preflight_availability_keeps_failure_semantics(self):
        self.assertEqual(self.open().state, 'open')
        original, owner = self.attach_real_preflight()
        with patch.object(session_module, 'listener_pids', return_value=set()), \
                patch.object(session_module.FilmcraftBridge, 'call') as transport:
            result = self.checkpoint()
        self.assertEqual(result.state, 'reconciliation_required')
        self.assertTrue(result.requires_reconciliation)
        self.assertIs(result.owner, owner)
        self.assertIs(self.controller._session, owner)
        self.assertEqual((original.save_calls, original.snapshots, original.quit_calls, original.stops), (0, 0, 0, 0))
        transport.assert_not_called()
        self.assert_protected()
