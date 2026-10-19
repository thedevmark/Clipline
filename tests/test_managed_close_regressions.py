"""Independent boundaries: real private store/SQLite/guards; native doubles only."""
import copy
import json
from pathlib import Path
import threading
import uuid
from unittest.mock import patch
import unittest

from tests import test_managed_close as safe

class TestIndependent(unittest.TestCase):
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

    def receipt(self):
        path = self.sessions[0].managed_exit['directory'] / 'receipt.json'
        return path, json.loads(path.read_text())

    def test_late_completion_after_stop_waiting(self):
        self.assertEqual(self.open().state, 'open')
        session = self.sessions[0]
        session.quit_mode = 'pending'
        stop = threading.Event(); stop.set()
        self.assertEqual(self.save_close(cancel=stop).state, 'close_pending')
        checkpoint = self.store.load()
        session.saved = session.content = b'INDEPENDENT LATE NATIVE SAVE'
        session.path.write_bytes(session.saved)
        session.emit('saved')
        outcome = self.check()
        self.assertEqual(outcome.state, 'closed', outcome.message)
        self.assertEqual(self.store.load().document, session.saved)
        self.assertEqual(self.store.load().revision, checkpoint.revision + 1)
        self.assertEqual((session.save_calls, session.snapshots, session.quit_calls, session.stops), (1, 1, 1, 0))

    def test_cancel_then_further_edits_and_fresh_close(self):
        self.assertEqual(self.open().state, 'open')
        session = self.sessions[0]; session.quit_mode = 'cancel'
        self.assertEqual(self.save_close().state, 'open')
        old_id = session.request
        self.assert_protected()
        session.content = b'INDEPENDENT EDITS AFTER CANCEL'
        session.quit_mode = 'saved'
        outcome = self.save_close()
        self.assertEqual(outcome.state, 'closed', outcome.message)
        self.assertNotEqual(session.request, old_id)
        self.assertEqual(self.store.load().document, b'INDEPENDENT EDITS AFTER CANCEL')
        self.assertEqual(session.stops, 0)

    def test_valid_stale_cancel_uuid_cannot_unlock_new_request(self):
        self.open(); session = self.sessions[0]; session.quit_mode = 'cancel'
        self.assertEqual(self.save_close().state, 'open')
        old_id = session.request
        session.quit_mode = 'pending'
        original = session.call
        def call(method, params):
            if method == 'clipline.managedExitStatus':
                return {'schema': 1, 'nonce': session.managed_exit['nonce'], 'requestId': old_id,
                        'decisionSerial': 99, 'pending': False, 'decision': 'cancel'}
            return original(method, params)
        with patch.object(session, 'call', side_effect=call):
            outcome = self.save_close()
        self.assertTrue(outcome.requires_reconciliation)
        self.assertFalse(outcome.can_close_editor)
        self.assertNotEqual(old_id, session.request)
        self.assertEqual(session.quit_calls, 2)
        self.assert_protected()

    def test_foreign_save_as_final_path_requires_explicit_restore(self):
        self.open(); session = self.sessions[0]; session.emit('saved')
        path, valid = self.receipt()
        foreign = copy.deepcopy(valid)
        foreign['savedContent']['identity']['projectPath'] = str(self.root / 'foreign-save-as.fcproj')
        path.write_text(json.dumps(foreign))
        self.assertTrue(self.check().requires_reconciliation)
        self.assertEqual(self.store.load(), self.seed)
        self.assert_protected()
        self.assertTrue((self.store.directory / 'editor-lifecycle.json').exists())
        path.write_text(json.dumps(valid))
        outcome = self.pool.submit(self.controller.recover_working).result(10)
        self.assertEqual(outcome.state, 'closed', outcome.message)
        self.assertEqual(self.store.load().document, session.saved)
        self.assertEqual((session.save_calls, session.snapshots, session.quit_calls, session.stops), (0, 0, 0, 0))

    def test_direct_bypass_receipt_cannot_publish_saved_content(self):
        self.open(); session = self.sessions[0]; session.emit('saved')
        path, value = self.receipt(); value['directBypass'] = True
        path.write_text(json.dumps(value))
        outcome = self.check()
        self.assertTrue(outcome.requires_reconciliation)
        self.assertFalse(outcome.app_close_allowed)
        self.assertEqual(self.store.load(), self.seed)
        self.assertEqual(session.stops, 0)
        self.assert_protected()

    def test_direct_dont_save_retains_separately_saved_document(self):
        self.open(); session = self.sessions[0]
        session.saved = b'INDEPENDENT EARLIER NATIVE SAVE'
        session.content = b'INDEPENDENT LATER DISCARDED MEMORY'
        session.emit('discarded')
        outcome = self.check()
        self.assertEqual(outcome.state, 'closed', outcome.message)
        self.assertEqual(self.store.load().document, session.saved)
        self.assertNotEqual(self.store.load().document, session.content)
        self.assertEqual((session.save_calls, session.snapshots, session.quit_calls, session.stops), (0, 0, 0, 0))

    def test_final_unadmitted_dependency_blocks_and_retains_artifact(self):
        self.open(); session = self.sessions[0]; session.emit('saved')
        path, value = self.receipt()
        value['savedContent']['identity']['mediaDependencies'].append({'path': str(self.root / 'other.mp4')})
        path.write_text(json.dumps(value))
        self.assertTrue(self.check().requires_reconciliation)
        self.assertEqual(self.store.load(), self.seed)
        self.assertEqual((session.managed_exit['directory'] / 'saved.fcproj').read_bytes(), session.saved)
        self.assert_protected()

    def test_checkpoint_success_then_exception_cannot_recover_or_repeat_publication(self):
        self.open(); session = self.sessions[0]
        publish = self.controller._store.publish; calls = []
        def lost(*args, **kwargs):
            calls.append(1); publish(*args, **kwargs)
            raise OSError('independent checkpoint publication reply lost')
        with patch.object(self.controller._store, 'publish', side_effect=lost):
            self.assertTrue(self.save_close().requires_reconciliation)
            outcome = self.pool.submit(self.controller.recover_working).result(10)
            self.assertTrue(outcome.requires_reconciliation)
            self.assertEqual(self.save_close().state, 'blocked')
        self.assertEqual(len(calls), 1)
        self.assertEqual((session.save_calls, session.snapshots, session.quit_calls, session.stops), (1, 1, 0, 0))
        self.assertTrue((self.store.directory / 'editor-lifecycle.json').exists())
        self.assert_protected()

    def test_settlement_failure_after_final_publication_never_retries(self):
        self.open(); session = self.sessions[0]; session.emit('saved')
        publish = self.controller._store.publish
        with patch.object(self.controller._store, 'publish', wraps=publish) as observed:
            with patch.object(self.controller._lifecycle, 'settle', side_effect=OSError('independent settlement failure')):
                self.assertTrue(self.check().requires_reconciliation)
            self.assertTrue(self.check().requires_reconciliation)
            outcome = self.pool.submit(self.controller.recover_working).result(10)
            self.assertEqual(outcome.state, 'blocked')
        self.assertEqual(observed.call_count, 1)
        self.assertEqual(self.store.load().document, session.saved)
        self.assertTrue((self.store.directory / 'editor-lifecycle.json').exists())
        self.assert_protected()

    def test_pending_close_rejects_checkpoint_mutation(self):
        self.open(); session = self.sessions[0]; session.quit_mode = 'pending'
        self.assertEqual(self.save_close().state, 'close_pending')
        prior = self.store.load()
        self.assertFalse(self.controller.status().can_checkpoint)
        outcome = self.checkpoint()
        actual = self.store.load()
        details = {'outcome_state': outcome.state, 'outcome_message': outcome.message,
            'save_calls': session.save_calls, 'snapshot_calls': session.snapshots,
            'quit_calls': session.quit_calls, 'stop_calls': session.stops,
            'prior_generation': prior.generation, 'actual_generation': actual.generation,
            'prior_revision': prior.revision, 'actual_revision': actual.revision,
            'lifecycle_state': json.loads((self.store.directory / 'editor-lifecycle.json').read_text())['state'],
            'quit_attempted': self.controller._quit_attempted,
            'can_checkpoint': outcome.can_checkpoint}
        (self.root / 'pending-checkpoint-evidence.json').write_text(json.dumps(details, indent=2))
        print(json.dumps(details))
        self.assertEqual(outcome.state, 'blocked', outcome.message)
        self.assertEqual(session.snapshots, 1)
        self.assertEqual(self.store.load(), prior)
