"""Real SQLite/ProjectStore with explicit native/session boundary doubles.

Opaque fixture bytes are not Filmcraft documents. No native process is launched.
"""
import copy
from contextlib import ExitStack
import ctypes
from dataclasses import replace
import hashlib
import json
import os
import io
from pathlib import Path
import sys
import threading
import tempfile
from types import SimpleNamespace
import uuid
import unittest
from unittest.mock import patch


from native.services.caption_worker import process_tree as tree_module
from native.services import native_checkpoint as checkpoint
from native.services import filmcraft_session as session_module
from native.services import managed_exit as evidence
from native.services import workspace_editor as subject
from native.services import editor_runtime as runtime
from tests.fixtures import workspace_editor as fixture


def sha(data):
    return hashlib.sha256(data).hexdigest()


class ManagedSession(fixture.FakeSession):
    def __init__(self, case, working, nonce):
        super().__init__(case, working)
        self.path = working.path.resolve(strict=True)
        self.initial_hash = sha(self.path.read_bytes())
        self.managed_exit = {'nonce': nonce, 'manifest_sha256': 'a' * 64,
                             'directory': self.directory / 'managed-exit'}
        self.managed_exit['directory'].mkdir()
        self.content = self.saved = b'OPAQUE ACCEPTED EDIT FIXTURE'
        self.closed = False
        self.unknown_scope = False
        self.save_calls = self.quit_calls = 0
        self.quit_mode = 'saved'
        self.request = None
        self.initial_identity = self.identity(self.path.read_bytes())

    def identity(self, data, *, saved=False):
        value = {'projectPath': str(self.path), 'projectContentSHA256': sha(data),
                 'revision': self.revision, 'savedRevision': self.revision, 'dirty': False,
                 'validation': self.validation(),
                 'ownerMetadata': {'5': {'clipline.creatorKit.v1': json.dumps(self.metadata)}},
                 'mediaDependencies': [{'item': 1, 'path': self.case.asset.path}]}
        if saved:
            value.update(identityOrigin='native_decoded_saved_file', revision=None, savedRevision=None, dirty=None)
            value['validation'].update(revision=None, savedRevision=None)
        return value

    def execute(self, command, params):
        if command == 'file.save':
            self.save_calls += 1
            self.saved = self.content
            self.path.write_bytes(self.saved)
            if self.quit_mode == 'save_lost':
                raise fixture.OutcomeUnknown('save-fixture', 'engine.execute', 'lost save reply')
            return {'path': str(self.path)}
        if command == 'clipline.saveSnapshot':
            self.snapshots += 1
            Path(params['path']).write_bytes(self.content)
            value = self.validation()
            value.update(projectPath=str(self.path), projectContentSHA256=sha(self.content))
            return {'path': params['path'], 'validation': value}
        return super().execute(command, params)

    def call(self, method, params):
        if method == 'ui.menu.invoke':
            self.quit_calls += 1
            self.case.assertEqual(params['id'], 'app.quit')
            self.case.assertEqual(params['managedExitNonce'], self.managed_exit['nonce'])
            self.request = params['managedExitRequestId']
            if self.quit_mode in ('pending', 'cancel', 'wrong_cancel'):
                return None
            if self.quit_mode == 'late_save':
                self.saved = self.content = b'OPAQUE LATE HUMAN SAVE FIXTURE'
                self.path.write_bytes(self.saved)
                self.revision += 1
            self.emit('discarded' if self.quit_mode == 'discard' else 'saved')
            if self.quit_mode == 'quit_lost':
                raise fixture.OutcomeUnknown('quit-fixture', method, 'lost quit reply')
            return None
        if method == 'clipline.managedExitStatus':
            return {'schema': 1, 'nonce': self.managed_exit['nonce'],
                    'requestId': 'wrong' if self.quit_mode == 'wrong_cancel' else self.request,
                    'decisionSerial': 1, 'pending': self.quit_mode == 'pending',
                    'decision': None if self.quit_mode == 'pending' else 'cancel'}
        raise AssertionError(method)

    def observe_exit(self):
        return {'schema': 1, 'scope': {'disposition': 'unknown' if self.unknown_scope else
                    'stopped' if self.closed else 'running',
                    'active_processes': None if self.unknown_scope else 0 if self.closed else 1},
                'parent_reaped': self.closed, 'exit_code': 0 if self.closed else None,
                'streams_closed': self.closed}

    def emit(self, state):
        directory = self.managed_exit['directory']

        def artifact(name, role, data, identity):
            (directory / name).write_bytes(data)
            return {'artifact': name, 'role': role, 'sha256': sha(data), 'bytes': len(data), 'identity': identity}

        saved = artifact('saved.fcproj', 'saved_content', self.saved, self.identity(self.saved, saved=True))
        saved.update(present=True, path=str(self.path), nativeProjectEqualsMemory=state == 'saved')
        memory_bytes = self.saved if state == 'saved' else b'OPAQUE CLEARED PROJECT FIXTURE'
        memory_identity = self.identity(memory_bytes)
        memory = artifact('final.fcproj', 'final_memory', memory_bytes, memory_identity)
        decision_evidence = post = None
        if state == 'discarded':
            discarded = artifact('discard.fcproj', 'discarded_memory', b'OPAQUE UNSAVED FIXTURE',
                                 self.identity(b'OPAQUE UNSAVED FIXTURE'))
            post = artifact('post.fcproj', 'post_discard', memory_bytes, memory_identity)
            decision_evidence = {'memory': discarded, 'saved': saved}
        value = {'schema': 1, 'sealedFinalExit': True, 'state': state, 'nativeGenerator': 'FilmCraft fixture',
                 'projectContentFingerprint': {'version': 1, 'viewIncluded': False, 'nativeSchema': 15},
                 'nonce': self.managed_exit['nonce'], 'launchManifestSHA256': self.managed_exit['manifest_sha256'],
                 'workingPath': str(self.path), 'initialWorkingSHA256': self.initial_hash,
                 'initialIdentity': self.initial_identity, 'decision': 'save' if state == 'saved' else 'dont_save',
                 'directBypass': False, 'fault': None, 'decisionEvidence': decision_evidence,
                 'postDiscard': post, 'finalMemory': memory, 'savedContent': saved, 'authority': 'fixture only',
                 'managedRequest': None if self.request is None else {'schema': 1,
                     'nonce': self.managed_exit['nonce'], 'requestId': self.request, 'decisionSerial': 1,
                     'pending': False, 'decision': 'save' if state == 'saved' else 'dont_save'}}
        (directory / 'receipt.json').write_text(json.dumps(value), encoding='utf8')
        self.closed = True


@unittest.skipUnless(os.name == 'nt', 'Windows read guard fixture')
class SafeCloseTests(unittest.TestCase):
    provenance = fixture.ControllerCase.provenance
    create = fixture.ControllerCase.create
    open = fixture.ControllerCase.open
    checkpoint = fixture.ControllerCase.checkpoint
    cleanup = fixture.ControllerCase.cleanup
    assert_protected = fixture.ControllerCase.assert_protected
    assert_writable = fixture.ControllerCase.assert_writable

    def setUp(self):
        fixture.ControllerCase.setUp(self)
        self.controller.managed_exit = True
        self.controller.close_wait_seconds = 0.01

    def tearDown(self):
        owner = self.controller._session or self.controller._retained_owner
        if owner is not None:
            owner.stop_result = fixture.stopped()
            self.pool.submit(self.controller.cleanup).result(10)
        fixture.ControllerCase.tearDown(self)

    def start(self, store, working, editor, **kwargs):
        barrier = store.directory / 'editor-lifecycle.json'
        self.assertTrue(barrier.is_file())
        value = json.loads(barrier.read_text())
        self.assertEqual(value['nonce'], kwargs['managed_exit_nonce'])
        self.assertEqual(value['generation'], self.store.load().generation)
        session = ManagedSession(self, working, kwargs['managed_exit_nonce'])
        self.sessions.append(session)
        if self.start_hook:
            self.start_hook(session)
        return session

    def save_close(self, **kwargs):
        return self.pool.submit(self.controller.save_and_close, **kwargs).result(10)

    def check(self):
        return self.pool.submit(self.controller.check_close).result(10)

    def test_normal_close_saves_once_quits_once_never_terminates(self):
        self.assertEqual(self.open().state, 'open')
        outcome = self.save_close()
        self.assertEqual(outcome.state, 'closed', outcome.message)
        self.assertTrue(outcome.app_close_allowed)
        session = self.sessions[0]
        self.assertEqual((session.save_calls, session.snapshots, session.quit_calls, session.stops), (1, 1, 1, 0))
        self.assertEqual(self.store.load().document, session.saved)
        self.assertFalse((self.store.directory / 'editor-lifecycle.json').exists())
        self.assert_writable()

    def test_late_prompt_save_publishes_exact_final_saved_artifact(self):
        self.open()
        self.sessions[0].quit_mode = 'late_save'
        outcome = self.save_close()
        self.assertEqual(outcome.state, 'closed', outcome.message)
        self.assertEqual(self.store.load().revision, self.seed.revision + 2)
        self.assertEqual(self.store.load().document, b'OPAQUE LATE HUMAN SAVE FIXTURE')
        self.assertEqual(self.sessions[0].stops, 0)

    def test_native_dont_save_publishes_no_discarded_memory(self):
        self.open()
        self.sessions[0].quit_mode = 'discard'
        outcome = self.save_close()
        self.assertEqual(outcome.state, 'closed', outcome.message)
        self.assertIn('discarded', outcome.message)
        self.assertEqual(self.store.load().document, self.sessions[0].saved)
        self.assertNotEqual(self.store.load().document, b'OPAQUE UNSAVED FIXTURE')

    def test_cancel_requires_matching_native_request_and_can_close_again(self):
        self.open()
        session = self.sessions[0]
        session.quit_mode = 'cancel'
        outcome = self.save_close()
        self.assertEqual(outcome.state, 'open', outcome.message)
        self.assertTrue(outcome.can_close_editor)
        first = session.request
        session.quit_mode = 'saved'
        self.assertEqual(self.save_close().state, 'closed')
        self.assertNotEqual(first, session.request)
        self.assertEqual(session.quit_calls, 2)
        self.assertEqual(session.stops, 0)

    def test_foreign_cancel_does_not_unlock_another_request(self):
        self.open()
        self.sessions[0].quit_mode = 'wrong_cancel'
        outcome = self.save_close()
        self.assertTrue(outcome.requires_reconciliation)
        self.assertFalse(outcome.can_close_editor)
        self.assertEqual(self.save_close().state, 'blocked')
        self.assertEqual(self.sessions[0].quit_calls, 1)
        self.assert_protected()

    def test_stop_waiting_retains_live_owner_and_guards(self):
        self.open()
        self.sessions[0].quit_mode = 'pending'
        cancel = threading.Event()
        cancel.set()
        outcome = self.save_close(cancel=cancel)
        self.assertEqual(outcome.state, 'close_pending')
        self.assertFalse(outcome.app_close_allowed)
        self.assertIs(outcome.owner, self.sessions[0])
        self.assertIs(outcome.owner.companion_asset_guards, self.controller._guards)
        self.assertIs(self.controller._session, self.sessions[0])
        self.assertEqual(self.sessions[0].stops, 0)
        self.assertEqual(self.save_close().state, 'blocked')
        self.assert_protected()

    def test_lost_quit_ack_can_be_resolved_without_second_quit(self):
        self.open()
        self.sessions[0].quit_mode = 'quit_lost'
        outcome = self.save_close()
        self.assertTrue(outcome.requires_reconciliation)
        self.assertEqual(self.check().state, 'closed')
        self.assertEqual((self.sessions[0].quit_calls, self.sessions[0].stops), (1, 0))

    def test_user_closes_native_without_companion_close_request(self):
        self.open()
        session = self.sessions[0]
        session.path.write_bytes(session.saved)
        session.emit('saved')
        self.assertEqual(self.check().state, 'closed')
        self.assertEqual(session.quit_calls, 0)
        self.assertEqual(self.store.load().document, session.saved)

    def test_missing_receipt_after_real_scope_zero_keeps_barrier(self):
        self.open()
        self.sessions[0].closed = True
        outcome = self.check()
        self.assertTrue(outcome.requires_reconciliation)
        self.assertFalse(outcome.app_close_allowed)
        self.assertTrue((self.store.directory / 'editor-lifecycle.json').is_file())
        self.assertEqual(self.store.load(), self.seed)

    def test_native_receipt_cannot_prove_unknown_process_scope(self):
        self.open()
        self.sessions[0].emit('saved')
        self.sessions[0].unknown_scope = True
        outcome = self.check()
        self.assertEqual(outcome.state, 'close_pending')
        self.assertEqual(self.store.load(), self.seed)
        self.assert_protected()
        self.assertEqual(self.sessions[0].stops, 0)

    def test_foreign_saved_path_nonce_hash_and_validation_are_rejected(self):
        self.open()
        session = self.sessions[0]
        session.emit('saved')
        path = session.managed_exit['directory'] / 'receipt.json'
        original = json.loads(path.read_text())
        changes = [lambda value: value.update(nonce='wrong'),
                   lambda value: value['savedContent'].update(path=str(self.root / 'foreign')),
                   lambda value: value['savedContent'].update(artifact='../escape.fcproj'),
                   lambda value: value['savedContent'].update(sha256='0' * 64),
                   lambda value: value['savedContent']['identity']['validation'].update(valid=False),
                   lambda value: value['savedContent']['identity']['ownerMetadata']['5'].update(
                       {'clipline.creatorKit.v1': '{}'})]
        for change in changes:
            with self.subTest(change=change):
                value = copy.deepcopy(original)
                change(value)
                path.write_text(json.dumps(value))
                outcome = self.check()
                self.assertTrue(outcome.requires_reconciliation)
                self.assertEqual(self.store.load(), self.seed)
                self.assert_protected()

    def test_changed_canonical_row_and_generation_are_not_followed(self):
        self.open()
        session = self.sessions[0]
        session.emit('saved')
        with fixture.CreatorLibrary(self.workspace.database) as library:
            library.set_overrides(self.moment.id, {}, expected_revision=1)
        self.assertTrue(self.check().requires_reconciliation)
        self.assertEqual(self.store.load(), self.seed)

    def test_uncertain_publication_is_not_retried(self):
        self.open()
        session = self.sessions[0]
        session.emit('saved')
        publish = self.store.publish
        calls = []

        def lost(*args, **kwargs):
            calls.append(1)
            result = publish(*args, **kwargs)
            raise OSError('injected failure after CURRENT publication')

        with patch.object(self.controller._store, 'publish', side_effect=lost):
            self.assertTrue(self.check().requires_reconciliation)
            self.assertTrue(self.check().requires_reconciliation)
            self.pool.submit(self.controller.recover_working).result(10)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.load().document, session.saved)
        self.assertTrue((self.store.directory / 'editor-lifecycle.json').exists())

    def test_legacy_close_never_terminates_and_schema1_has_no_safe_close(self):
        self.open()
        outcome = self.pool.submit(self.controller.close).result(10)
        self.assertEqual(outcome.state, 'blocked')
        self.assertEqual(self.sessions[0].stops, 0)
        self.controller.managed_exit = False
        self.assertEqual(self.save_close().state, 'blocked')
        self.assertEqual(self.sessions[0].save_calls, 0)

    def test_cold_lifecycle_barrier_refuses_another_launch(self):
        self.open()
        other = subject.WorkspaceEditorController(self.workspace, helper=self.controller.helper,
            editor=self.controller.editor, python=self.controller.python, managed_exit=True)
        outcome = self.pool.submit(other.open, self.moment.id, 1,
                                   profile_id='selected', scene_key='game-scene').result(10)
        self.assertEqual(outcome.state, 'failed')
        self.assertEqual(self.mock_start.call_count, 1)

    def test_controller_can_open_again_after_verified_settlement(self):
        self.open()
        self.assertEqual(self.save_close().state, 'closed')
        reopened = self.open()
        self.assertEqual(reopened.state, 'open', reopened.message)
        self.assertTrue(reopened.can_close_editor)
        self.assertTrue(reopened.can_checkpoint)

    def test_projectstore_generation_cas_conflict_preserves_final_artifact(self):
        self.open()
        session = self.sessions[0]
        session.emit('saved')
        other = self.store.publish(b'OPAQUE COMPETING GENERATION', expected_generation=self.seed.generation,
                                  revision=2, engine='filmcraft', engine_version='fixture', suffix='.fcproj')
        outcome = self.check()
        self.assertTrue(outcome.requires_reconciliation)
        self.assertEqual(self.store.load(), other)
        self.assertEqual((session.managed_exit['directory'] / 'saved.fcproj').read_bytes(), session.saved)

    def test_lost_save_stays_recoverable_without_automatic_retry(self):
        self.open()
        session = self.sessions[0]
        session.quit_mode = 'save_lost'
        outcome = self.save_close()
        self.assertTrue(outcome.requires_reconciliation)
        self.assertEqual((session.save_calls, session.snapshots, session.quit_calls, session.stops), (1, 0, 0, 0))
        recovered = self.pool.submit(self.controller.recover_working).result(10)
        self.assertEqual(recovered.state, 'open', recovered.message)
        self.assertEqual((session.save_calls, session.snapshots, session.quit_calls), (1, 0, 0))
        self.assertTrue(recovered.can_close_editor)

    def test_changed_project_base_refuses_save_and_live_recovery_before_mutation(self):
        self.open()
        session = self.sessions[0]
        self.store.publish(b'OPAQUE OTHER BASE', expected_generation=self.seed.generation,
                           revision=2, engine='filmcraft', engine_version='fixture', suffix='.fcproj')
        outcome = self.save_close()
        self.assertTrue(outcome.requires_reconciliation)
        self.assertEqual(session.save_calls, 0)
        recovered = self.pool.submit(self.controller.recover_working).result(10)
        self.assertTrue(recovered.requires_reconciliation)
        self.assertEqual((session.save_calls, session.snapshots, session.quit_calls, session.stops), (0, 0, 0, 0))

    def test_native_save_as_can_be_restored_then_readmitted_without_saving(self):
        self.open()
        session = self.sessions[0]
        original = session.execute
        changed = True

        def inspect_path(command, params):
            result = original(command, params)
            if command == 'project.inspect' and changed:
                result['path'] = str(self.root / 'human-save-as.fcproj')
            return result

        with patch.object(session, 'execute', side_effect=inspect_path):
            self.assertTrue(self.save_close().requires_reconciliation)
            self.assertEqual(session.save_calls, 0)
            changed = False
            recovered = self.pool.submit(self.controller.recover_working).result(10)
            self.assertEqual(recovered.state, 'open', recovered.message)
            self.assertEqual((session.save_calls, session.snapshots, session.quit_calls), (0, 0, 0))

    def test_partial_receipt_and_artifact_collision_do_not_publish(self):
        self.open()
        session = self.sessions[0]
        session.emit('saved')
        path = session.managed_exit['directory'] / 'receipt.json'
        raw = path.read_bytes()
        path.write_bytes(raw[:20])
        self.assertTrue(self.check().requires_reconciliation)
        self.assertEqual(self.store.load(), self.seed)
        path.write_bytes(raw)
        artifact = session.managed_exit['directory'] / 'saved.fcproj'
        artifact.write_bytes(b'OPAQUE DIFFERENT FILE')
        self.assertTrue(self.check().requires_reconciliation)
        self.assertEqual(self.store.load(), self.seed)

    def test_checkpoint_refuses_pending_before_database_file_or_native_work(self):
        self.open()
        session = self.sessions[0]
        session.quit_mode = 'pending'
        self.assertEqual(self.save_close().state, 'close_pending')
        prior = self.store.load()
        barrier = (self.store.directory / 'editor-lifecycle.json').read_bytes()
        with patch.object(self.controller, '_assert_facts') as facts, \
                patch.object(self.controller, '_assert_publication_base') as publication, \
                patch.object(self.controller, '_inspect') as inspect, \
                patch.object(subject, 'publish_checkpoint') as capture:
            outcome = self.checkpoint()
        self.assertEqual(outcome.state, 'blocked')
        self.assertEqual(self.controller.status().state, 'close_pending')
        facts.assert_not_called()
        publication.assert_not_called()
        inspect.assert_not_called()
        capture.assert_not_called()
        self.assertEqual(self.store.load(), prior)
        self.assertEqual((self.store.directory / 'editor-lifecycle.json').read_bytes(), barrier)
        self.assertEqual((session.save_calls, session.snapshots, session.quit_calls, session.stops), (1, 1, 1, 0))

    def test_checkpoint_after_correlated_cancel_is_allowed(self):
        self.open()
        session = self.sessions[0]
        session.quit_mode = 'cancel'
        self.assertEqual(self.save_close().state, 'open')
        prior = self.store.load()
        session.content = b'OPAQUE EDIT AFTER NATIVE CANCEL'
        outcome = self.checkpoint()
        self.assertEqual(outcome.state, 'open', outcome.message)
        self.assertTrue(outcome.can_checkpoint)
        self.assertEqual(self.store.load().revision, prior.revision + 1)
        self.assertEqual(self.store.load().document, session.content)
        self.assertEqual((session.save_calls, session.snapshots, session.quit_calls, session.stops), (1, 2, 1, 0))

    def test_check_close_without_request_preserves_open_checkpoint_consumer(self):
        self.open()
        session = self.sessions[0]
        self.assertTrue(self.controller.status().can_check_close)
        self.assertEqual(self.check().state, 'open')
        self.assertTrue(self.controller.status().can_checkpoint)
        self.assertEqual(self.checkpoint().state, 'open')
        self.assertEqual((session.save_calls, session.snapshots, session.quit_calls, session.stops), (0, 1, 0, 0))

    def test_unknown_scope_then_running_scope_restores_read_only_consumer(self):
        self.open()
        session = self.sessions[0]
        session.unknown_scope = True
        self.assertEqual(self.check().state, 'close_pending')
        self.assertFalse(self.controller.status().can_checkpoint)
        self.assertEqual(self.checkpoint().state, 'blocked')
        self.assertEqual(session.snapshots, 0)
        session.unknown_scope = False
        self.assertEqual(self.check().state, 'open')
        self.assertTrue(self.controller.status().can_checkpoint)


class OwnerBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='safe-close-owner-')
        self.root = Path(self.temp.name)
        self.path = self.root / 'working.fcproj'
        self.path.write_bytes(b'OPAQUE OWNER FIXTURE')
        self.owner = session_module.NativeSession.__new__(session_module.NativeSession)
        self.owner.directory = self.root
        self.owner.path = self.path
        self.owner._stop_final = self.owner._stopped = False
        self.owner._bridge = object()
        self.owner._lock = None
        self.owner._files = [io.BytesIO()]
        self.owner._input_guards = ExitStack()
        self.released = []
        self.owner._input_guards.callback(self.released.append, 'inputs')
        self.process = SimpleNamespace(poll=lambda: 0)
        self.owner.process = self.process
        self.closed = []
        self.proof = {'disposition': 'stopped', 'active_processes': 0, 'scope_id': 'actual-double-scope'}
        self.owner.tree = SimpleNamespace(observe=lambda: copy.deepcopy(self.proof),
                                           close=lambda: self.closed.append('scope') or True)
        self.marker = self.root / 'editor-owner-uncertain.json'
        self.marker.write_text(json.dumps({'run': str(self.root)}))

    def tearDown(self):
        self.owner._input_guards.close()
        for stream in self.owner._files:
            stream.close()
        self.temp.cleanup()

    def test_zero_scope_plus_reaped_parent_releases_only_after_receipt(self):
        result = self.owner.observe_exit()
        self.assertTrue(result['parent_reaped'])
        self.assertTrue(result['streams_closed'])
        self.assertEqual(self.released, ['inputs'])
        self.assertEqual(self.closed, ['scope'])
        self.assertFalse(self.marker.exists())
        self.assertEqual(len(list(self.root.glob('observed-exit-*.json'))), 1)
        self.assertEqual(self.owner.observe_exit(), result)

    def test_parent_live_or_scope_unknown_never_releases_queryable_owner(self):
        for code, proof in [(None, {'disposition': 'stopped', 'active_processes': 0}),
                            (0, {'disposition': 'running', 'active_processes': 1}),
                            (0, {'disposition': 'unknown', 'active_processes': None})]:
            with self.subTest(code=code, proof=proof):
                self.owner.process = SimpleNamespace(poll=lambda: code)
                self.proof = proof
                self.owner.observe_exit()
                self.assertEqual(self.released, [])
                self.assertEqual(self.closed, [])
                self.assertTrue(self.marker.exists())
                self.assertFalse(self.owner._stop_final)

    def test_evidence_write_failure_retains_queryable_scope_and_input_guards(self):
        with patch.object(session_module, 'write_once', side_effect=OSError('receipt unavailable')):
            with self.assertRaises(OSError):
                self.owner.observe_exit()
        self.assertEqual(self.released, [])
        self.assertEqual(self.closed, [])
        self.assertTrue(self.marker.exists())
        self.assertTrue(self.owner.observe_exit()['parent_reaped'])

    def test_malformed_working_marker_retains_scope_and_input_guards(self):
        self.marker.write_text('null')
        with self.assertRaises(session_module.SessionError):
            self.owner.observe_exit()
        self.assertEqual(self.released, [])
        self.assertEqual(self.closed, [])
        self.assertTrue(self.marker.exists())

    def test_process_tree_observe_queries_without_termination_or_close(self):
        class Accounting(ctypes.Structure):
            _fields_ = [('active_processes', ctypes.c_ulong)]

        calls = []

        def query(job, kind, ptr, size, out):
            calls.append((job, kind))
            ptr._obj.active_processes = 2
            return True

        tree = tree_module.ProcessTree.__new__(tree_module.ProcessTree)
        tree.process, tree.scope_id, tree.job = SimpleNamespace(pid=17), 'owned-double', 123
        tree.accounting = Accounting
        tree.kernel = SimpleNamespace(QueryInformationJobObject=query)
        result = tree.observe()
        self.assertEqual(result['active_processes'], 2)
        self.assertEqual(result['disposition'], 'running')
        self.assertEqual(calls, [(123, 1)])
        self.assertEqual(tree.job, 123)


@unittest.skipUnless(os.name == 'nt', 'Windows launch guards')
class LaunchAndRuntimeTests(unittest.TestCase):
    def test_managed_manifest_is_bound_and_guarded_before_popen(self):
        with tempfile.TemporaryDirectory(prefix='safe-managed-launch-') as folder:
            root = Path(folder)
            store = fixture.ProjectStore(root / 'store')
            store.publish(b'OPAQUE LAUNCH FIXTURE', expected_generation=None, revision=1,
                          engine='filmcraft', engine_version='fixture', suffix='.fcproj')
            working = checkpoint.checkout(store)
            editor = session_module.QualifiedEditor(Path(sys.executable), session_module.digest(Path(sys.executable)), 'fixture')
            nonce = str(uuid.uuid4())
            seen = []

            def popen(argv, **kwargs):
                intent = json.loads(Path(argv[-1]).read_text())
                manifest = Path(intent['argv'][-1])
                self.assertEqual(intent['argv'][-2], '--managed-exit')
                raw = manifest.read_bytes()
                value = json.loads(raw)
                self.assertEqual(value['nonce'], nonce)
                self.assertEqual(value['workingPath'], str(working.path))
                self.assertEqual(intent['managed_exit']['manifest_sha256'], sha(raw))
                with self.assertRaises(PermissionError):
                    manifest.write_bytes(raw)
                seen.append(manifest)
                raise RuntimeError('stop before any process launch')

            with patch.object(session_module.subprocess, 'Popen', side_effect=popen):
                with self.assertRaisesRegex(session_module.SessionError, 'before any process') as caught:
                    session_module.NativeSession.start(store, working, editor, python=Path(sys.executable),
                        runs=root / 'runs', managed_exit_nonce=nonce)
            self.assertEqual(len(seen), 1)
            self.assertEqual(caught.exception.owner.stop_receipt['scope']['disposition'], 'never_started')
            self.assertEqual(working.path.read_bytes(), b'OPAQUE LAUNCH FIXTURE')

    def test_runtime_schema1_stays_legacy_and_schema2_is_explicit(self):
        with tempfile.TemporaryDirectory(prefix='safe-runtime-') as folder:
            root = Path(folder)
            program = {'path': str(root / 'uninstalled.exe'), 'sha256': 'a' * 64, 'version': 'fixture'}
            value = {'schema': 1, 'creator_kit_helper': program, 'editor': program, 'python': str(root / 'python.exe')}
            path = root / 'runtime.json'
            path.write_text(json.dumps(value))
            self.assertFalse(runtime.load_editor_runtime(path).managed_exit)
            value.update(schema=2, managed_exit_protocol=1)
            path.write_text(json.dumps(value))
            self.assertTrue(runtime.load_editor_runtime(path).managed_exit)
            for invalid in (True, 0, '1', None):
                value['managed_exit_protocol'] = invalid
                path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    runtime.load_editor_runtime(path)


if __name__ == '__main__':
    unittest.main(verbosity=2)
