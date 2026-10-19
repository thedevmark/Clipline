"""Controller boundaries with real SQLite/files/ProjectStore, mocked native owner."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, FrozenInstanceError
import copy
import gc
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import weakref

from native.core.creator_library import CreatorLibrary
from native.core.project_store import ProjectStore
from native.services.workspace_library import WorkspaceLibrary
from native.services.native_checkpoint import checkout
from native.services.native_moment import CreatedNativeMoment, NativeMomentCreationError, QualifiedCreatorKitHelper
from native.services.filmcraft_session import QualifiedEditor, SessionError
from native.services.filmcraft_bridge import OutcomeUnknown
from native.services import workspace_editor as subject

from tests.fixtures import workspace_editor as fixture
from tests.fixtures.workspace_editor import FakeSession, CreationOwner, stopped

@unittest.skipUnless(os.name == 'nt', 'Windows native editor asset guards')
class ControllerTests(fixture.ControllerCase):
    def test_main_thread_rejected_without_native_work(self):
        outcome = self.controller.open(self.moment.id, 1, profile_id='selected', scene_key='game-scene')
        self.assertEqual(outcome.state, 'blocked')
        self.mock_start.assert_not_called(); self.mock_create.assert_not_called()

    def test_existing_store_checks_out_and_does_not_create_or_launch_twice(self):
        opened = self.open()
        self.assertEqual(opened.state, 'open'); self.assertTrue(opened.can_checkpoint)
        self.assertFalse(opened.app_close_allowed); self.mock_create.assert_not_called()
        self.assertEqual(opened.working.path.read_bytes(), self.seed.document)
        refused = self.open()
        self.assertEqual(refused.state, 'blocked'); self.assertEqual(self.controller.status().state, 'open')
        self.assertEqual(self.mock_start.call_count, 1); self.assertEqual(self.sessions[0].stops, 0)
        self.assert_protected()

    def test_missing_store_uses_product_creation_once_with_exact_selection(self):
        # A second canonical Moment has no initial store; no committed file is removed.
        with CreatorLibrary(self.workspace.database) as library:
            self.moment = library.create_moment(title='New', source_asset_id=self.asset.id, source_in_us=1_000_000,
                source_out_us=3_000_000, kit_id=self.kit_id, kit_version=1)
        self.store = ProjectStore(self.workspace.directory / 'moments' / self.moment.id)
        result = self.open()
        self.assertEqual(result.state, 'open'); self.assertEqual(self.mock_create.call_count, 1)
        library, moment, revision, params = self.created[0]
        self.assertEqual((library, moment, revision), (self.workspace.database, self.moment.id, 1))
        self.assertEqual((params['profile_id'], params['scene_key']), ('selected', 'game-scene'))
        self.assertEqual(params['store'].directory, self.store.directory)

    def test_stale_revision_and_wrong_scene_fail_before_native_launch(self):
        wrong = self.open(scene_key='wrong')
        self.assertEqual(wrong.state, 'failed')
        with CreatorLibrary(self.workspace.database) as library: library.set_overrides(self.moment.id, {}, expected_revision=1)
        stale = self.open(); self.assertEqual(stale.state, 'failed')
        self.mock_start.assert_not_called(); self.mock_create.assert_not_called()

    def test_later_kit_version_does_not_replace_pinned_version(self):
        with CreatorLibrary(self.workspace.database) as library:
            library.revise_kit(self.kit_id, self.descriptor | {'name': 'New name'}, expected_version=1)
        self.assertEqual(self.open().state, 'open')

    def test_changed_source_refuses_before_any_native_launch(self):
        self.source_path.write_bytes(b'foreign bytes')
        outcome = self.open()
        self.assertEqual(outcome.state, 'failed'); self.assertTrue(outcome.app_close_allowed)
        self.mock_start.assert_not_called(); self.assert_writable()

    def test_wrong_native_creation_identity_stops_before_usable_state(self):
        self.start_hook = lambda session: session.metadata['kit'].update(descriptorSha256='wrong')
        outcome = self.open()
        self.assertEqual(outcome.state, 'reconciliation_required'); self.assertFalse(outcome.can_checkpoint)
        self.assertTrue(outcome.app_close_allowed); self.assertEqual(self.sessions[0].stops, 1)
        self.assertEqual(self.store.load(), self.seed); self.assert_writable()

    def test_other_native_creation_pins_are_checked_before_session_admission(self):
        mutations = [lambda body: body.update(momentId='wrong'),
                     lambda body: body['kit'].update(version=2),
                     lambda body: body['sourceAsset'].update(path='wrong-source'),
                     lambda body: body['templateAsset'].update(sha256='wrong-template'),
                     lambda body: body['sceneProfile'].update(id='wrong-profile'),
                     lambda body: body['requestedSourceRangeUs'].update(end=4_000_000)]
        config = self.controller
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                self.controller = subject.WorkspaceEditorController(self.workspace, helper=config.helper,
                    editor=config.editor, python=config.python)
                self.start_hook = lambda session, change=mutate: change(session.metadata)
                outcome = self.open()
                self.assertTrue(outcome.requires_reconciliation); self.assertFalse(outcome.can_checkpoint)
                self.assertTrue(outcome.app_close_allowed); self.assertEqual(self.sessions[-1].stops, 1)
                self.assert_writable()

    def test_canonical_change_during_start_is_not_a_usable_session(self):
        def change(_session):
            with CreatorLibrary(self.workspace.database) as library: library.set_overrides(self.moment.id, {}, expected_revision=1)
        self.start_hook = change
        outcome = self.open()
        self.assertTrue(outcome.requires_reconciliation); self.assertFalse(outcome.can_checkpoint)
        self.assertEqual(self.sessions[0].stops, 1); self.assert_writable()

    def test_native_change_during_read_admission_is_stopped(self):
        self.start_hook = lambda session: setattr(session, 'mode', 'changing')
        outcome = self.open()
        self.assertTrue(outcome.requires_reconciliation); self.assertEqual(self.sessions[0].stops, 1)

    def test_unknown_creation_owner_is_retained_and_cleanup_never_clears_document_barrier(self):
        native_owner = CreationOwner()
        error = NativeMomentCreationError('Unknown fixture creation', self.root, state='outcome_unknown', owner=native_owner)
        self.mock_create.side_effect = error
        with CreatorLibrary(self.workspace.database) as library:
            self.moment = library.create_moment(title='New', source_asset_id=self.asset.id, source_in_us=1_000_000,
                source_out_us=3_000_000, kit_id=self.kit_id, kit_version=1)
        self.store = ProjectStore(self.workspace.directory / 'moments' / self.moment.id)
        outcome = self.open(); self.assertIs(outcome.owner, native_owner)
        self.assertFalse(outcome.app_close_allowed); self.assertTrue(outcome.can_cleanup); self.assert_protected()
        self.assertEqual(self.open().state, 'blocked'); self.assertEqual(self.mock_create.call_count, 1)
        self.assertTrue(self.cleanup().requires_reconciliation); self.assert_protected()
        native_owner.result = {'disposition': 'stopped', 'active_processes': 0}
        cleaned = self.cleanup(); self.assertTrue(cleaned.requires_reconciliation)
        self.assertTrue(cleaned.app_close_allowed); self.assertFalse(cleaned.can_cleanup); self.assertTrue(native_owner.closed)
        self.assertIsNone(cleaned.owner)
        self.assert_writable(); self.assertEqual(self.open().state, 'blocked')

    def test_session_error_owner_is_strongly_retained_before_worker_conversion(self):
        native_owner = FakeSession(self, checkout(self.store))
        native_owner.stop_result = {'scope': {'disposition': 'unknown', 'active_processes': None}, 'streams_closed': True}
        ref = weakref.ref(native_owner)
        self.mock_start.side_effect = SessionError('Unknown startup', native_owner.directory, owner=native_owner)
        outcome = self.open(); self.assertIs(outcome.owner, native_owner)
        self.assertFalse(outcome.app_close_allowed); self.assert_protected()
        self.mock_start.side_effect = None
        del native_owner, outcome
        gc.collect(); self.assertIsNotNone(ref())
        ref().stop_result = stopped(); self.assertTrue(self.cleanup().app_close_allowed); self.assert_writable()

    def test_publication_unknown_without_a_live_owner_blocks_creation_retry(self):
        with CreatorLibrary(self.workspace.database) as library:
            self.moment = library.create_moment(title='New', source_asset_id=self.asset.id, source_in_us=1_000_000,
                source_out_us=3_000_000, kit_id=self.kit_id, kit_version=1)
        self.store = ProjectStore(self.workspace.directory / 'moments' / self.moment.id)
        def uncertain(*args, **kwargs):
            self.create(*args, **kwargs)
            raise NativeMomentCreationError('Publication boundary unknown', self.root, state='publication_unknown')
        self.mock_create.side_effect = uncertain
        outcome = self.open()
        self.assertTrue(outcome.requires_reconciliation); self.assertTrue(outcome.app_close_allowed)
        self.assertFalse(outcome.can_cleanup); self.assertIsNotNone(self.store.load())
        self.assertEqual(self.open().state, 'blocked'); self.assertEqual(self.mock_create.call_count, 1)
        self.assert_writable()

    def test_unexpected_exception_owner_is_retained_in_typed_failure(self):
        native_owner = FakeSession(self, checkout(self.store))
        error = RuntimeError('Unexpected owner-bound fixture failure'); error.owner = native_owner
        self.mock_start.side_effect = error
        outcome = self.open()
        self.assertIs(outcome.owner, native_owner); self.assertEqual(outcome.error_type, 'RuntimeError')
        self.assertTrue(outcome.requires_reconciliation); self.assertFalse(outcome.app_close_allowed)
        self.assert_protected(); self.assertTrue(self.cleanup().app_close_allowed)

    def test_known_stopped_startup_error_does_not_claim_an_active_process(self):
        native_owner = FakeSession(self, checkout(self.store)); native_owner.stop_receipt = stopped()
        self.mock_start.side_effect = SessionError('Known startup failure', native_owner.directory, owner=native_owner)
        outcome = self.open()
        self.assertEqual(outcome.state, 'failed'); self.assertTrue(outcome.app_close_allowed)
        self.assertIsNone(outcome.owner); self.assertEqual(outcome.stop_receipt['scope']['active_processes'], 0)
        self.assertEqual(outcome.error_type, 'SessionError'); self.assert_writable()

    def test_checkpoint_error_owner_transfers_active_session_to_cleanup(self):
        self.open()
        native_owner = self.sessions[0]
        error = RuntimeError('Unexpected current-owner checkpoint failure')
        error.owner = native_owner
        with patch.object(subject, 'publish_checkpoint', side_effect=error):
            outcome = self.checkpoint()
        self.assertIs(outcome.owner, native_owner)
        self.assertTrue(outcome.requires_reconciliation)
        self.assertFalse(outcome.app_close_allowed)
        cleaned = self.cleanup()
        self.assertTrue(cleaned.app_close_allowed)
        self.assertTrue(cleaned.requires_reconciliation)
        self.assertFalse(cleaned.can_close_editor)
        self.assertIsNone(cleaned.owner)
        self.assertEqual(native_owner.stops, 1)
        self.assert_writable()

    def test_checkpoint_uses_existing_owner_and_close_requires_explicit_cleanup(self):
        opened = self.open(); saved = self.checkpoint()
        self.assertEqual(saved.state, 'open'); self.assertTrue(saved.can_checkpoint)
        self.assertFalse(saved.requires_reconciliation)
        self.assertNotEqual(saved.snapshot.generation, opened.snapshot.generation)
        self.assertEqual(saved.snapshot.document, b'OPAQUE CAPTURE FIXTURE 1')
        self.assertEqual(saved.working.base_generation, saved.snapshot.generation)
        self.assertEqual(self.sessions[0].working, saved.working)
        closed = self.pool.submit(self.controller.close).result(10)
        self.assertEqual(closed.state, 'blocked'); self.assertFalse(closed.app_close_allowed)
        self.assertEqual(self.sessions[0].stops, 0)
        self.assertEqual(self.sessions[0].snapshots, 1); self.assert_protected()
        cleaned = self.cleanup()
        self.assertTrue(cleaned.app_close_allowed); self.assertTrue(cleaned.requires_reconciliation)
        self.assertEqual(self.sessions[0].stops, 1); self.assert_writable()

    def test_lost_snapshot_reply_blocks_retry_and_cleanup_keeps_uncertainty(self):
        self.open(); self.sessions[0].mode = 'lost'
        outcome = self.checkpoint()
        self.assertTrue(outcome.requires_reconciliation); self.assertFalse(outcome.app_close_allowed)
        self.assertEqual(self.store.load(), self.seed); self.assertEqual(self.checkpoint().state, 'blocked')
        self.assertEqual(self.sessions[0].snapshots, 1)
        cleaned = self.cleanup(); self.assertTrue(cleaned.requires_reconciliation); self.assertTrue(cleaned.app_close_allowed)

    def test_observed_pin_change_after_publication_retains_returned_snapshot_without_rollback(self):
        self.open(); publish = ProjectStore.publish
        def changed(store, *args, **kwargs):
            result = publish(store, *args, **kwargs)
            with CreatorLibrary(self.workspace.database) as library: library.set_overrides(self.moment.id, {}, expected_revision=1)
            return result
        with patch.object(ProjectStore, 'publish', changed): outcome = self.checkpoint()
        self.assertTrue(outcome.requires_reconciliation); self.assertIsNotNone(outcome.snapshot)
        self.assertEqual(outcome.snapshot, self.store.load()); self.assertNotEqual(outcome.snapshot.generation, self.seed.generation)
        self.assertEqual(outcome.working.base_generation, outcome.snapshot.generation)
        self.assertEqual(self.checkpoint().state, 'blocked')

    def test_competing_publication_is_not_overwritten_or_adopted(self):
        self.open(); self.sessions[0].mode = 'competitor'
        outcome = self.checkpoint()
        self.assertTrue(outcome.requires_reconciliation)
        self.assertEqual(self.store.load().document, b'OPAQUE COMPETING SNAPSHOT')
        self.assertEqual(outcome.snapshot, self.seed)

    def test_direct_close_refuses_and_explicit_unknown_cleanup_keeps_guards(self):
        self.open(); self.sessions[0].stop_result = {'scope': {'disposition': 'unknown', 'active_processes': 1}, 'streams_closed': True}
        outcome = self.pool.submit(self.controller.close).result(10)
        self.assertEqual(outcome.state, 'blocked'); self.assertEqual(self.sessions[0].stops, 0)
        self.assertFalse(outcome.app_close_allowed); self.assertTrue(outcome.can_cleanup); self.assert_protected()
        self.assertEqual(self.store.load(), self.seed); self.assertEqual(self.sessions[0].snapshots, 0)
        uncertain = self.cleanup()
        self.assertFalse(uncertain.app_close_allowed); self.assertTrue(uncertain.requires_reconciliation)
        self.assertEqual(self.sessions[0].stops, 1); self.assert_protected()
        self.sessions[0].stop_result = stopped()
        cleaned = self.cleanup(); self.assertTrue(cleaned.app_close_allowed); self.assertTrue(cleaned.requires_reconciliation)
        self.assertEqual(self.sessions[0].stops, 2)

    def test_concurrent_operation_is_refused_without_a_second_native_launch(self):
        entered, release = threading.Event(), threading.Event()
        def hold(_session): entered.set(); self.assertTrue(release.wait(5))
        self.start_hook = hold
        first = self.pool.submit(self.controller.open, self.moment.id, 1, profile_id='selected', scene_key='game-scene')
        self.assertTrue(entered.wait(5))
        try:
            self.assertFalse(self.controller.status().app_close_allowed)
            second = self.pool.submit(self.controller.close).result(5)
            self.assertEqual(second.state, 'busy'); self.assertEqual(self.mock_start.call_count, 1)
        finally: release.set()
        self.assertEqual(first.result(10).state, 'open')

    def test_outcome_is_detached_immutable_and_status_does_no_library_io(self):
        self.open(); closed = self.cleanup()
        with self.assertRaises(FrozenInstanceError): closed.state = 'open'
        with self.assertRaises(TypeError): closed.stop_receipt['scope']['disposition'] = 'unknown'
        with patch.object(subject, 'CreatorLibrary', side_effect=AssertionError('GUI database access')):
            self.assertEqual(self.controller.status(), closed)

    def test_noncanonical_moment_path_refused_without_native_launch(self):
        outcome = self.pool.submit(self.controller.open, '../foreign', 1,
            profile_id='selected', scene_key='game-scene').result(10)
        self.assertEqual(outcome.state, 'failed'); self.mock_start.assert_not_called()
