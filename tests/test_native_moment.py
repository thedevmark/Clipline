import concurrent.futures
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from native.core.creator_library import CreatorLibrary
from native.core.project_store import ProjectStore
from native.services.caption_worker.process_tree import ProcessTree
from native.services import native_moment as owner

ROOT = Path(__file__).parent


@unittest.skipUnless(os.name == 'nt', 'Windows native helper ownership qualification')
class NativeMomentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='native-moment-', dir=os.environ.get('CLIPLINE_MOMENT_TEST_ROOT'))
        self.root = Path(self.temp.name)
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.library = self.root / 'library.sqlite'
        self.source_path = self.root / 'source.bin'
        self.template_path = self.root / 'template.fcproj'
        self.source_path.write_bytes(b'authored source protocol fixture')
        self.template_path.write_bytes(b'opaque template protocol fixture')
        self.config = {'mode': 'success'}
        self.launched = []
        with CreatorLibrary(self.library) as library:
            self.source = library.register_asset(self.source_path)
            self.template = library.register_asset(self.template_path)
            self.descriptor = {
                'name': 'Streamer – 世界', 'asset_refs': {'native_template': self.template.id},
                'properties': {}, 'vocabulary': ['Some_Handle'],
                'layout_profiles': [{'id': 'main', 'scene_key': 'game-and-face',
                    'source_width': 1920, 'source_height': 1080,
                    'regions': {'gameplay': {'x': 0, 'y': 0, 'w': 1, 'h': .75},
                                'facecam': {'x': 0, 'y': .75, 'w': .3, 'h': .25}}}],
            }
            self.kit_id = library.create_kit(self.descriptor)
            self.kit = library.kit_record(self.kit_id, 1)
            self.moment = library.create_moment(title='Selected clip', source_asset_id=self.source.id,
                source_in_us=1000000, source_out_us=3000000, kit_id=self.kit_id, kit_version=1)
        self.store = ProjectStore(self.root / 'moment')
        self.helper = owner.QualifiedCreatorKitHelper(Path(sys.executable).resolve(),
                    owner.digest(sys.executable), 'protocol-fixture-only')
        original_popen = subprocess.Popen

        def spawn(*args, **kwargs):
            staging = Path(kwargs['cwd'])
            shutil.copyfile(ROOT / 'fixtures/native_factory_peer.py', staging / 'instantiate')
            (staging / 'fixture-config.json').write_text(json.dumps(self.config), encoding='utf8')
            process = original_popen(*args, **kwargs)
            self.launched.append((staging, process))
            return process

        self.spawn_patch = patch.object(owner.subprocess, 'Popen', spawn)
        self.spawn_patch.start()

    def tearDown(self):
        self.spawn_patch.stop()
        self.pool.shutdown(wait=True)
        for directory, process in self.launched:
            self.assertIsNotNone(process.poll(), 'The owned gate is still running')
        retained = os.environ.get('CLIPLINE_MOMENT_KEEP_EVIDENCE')
        if retained:
            destination = Path(retained) / self._testMethodName
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copytree(self.root, destination / self.root.name)
        self.temp.cleanup()

    def call(self, **kwargs):
        values = {'profile_id': 'main', 'scene_key': 'game-and-face', 'helper': self.helper,
                  'python': Path(sys.executable), 'store': self.store,
                  'runs': self.root / 'runs', 'timeout_seconds': 4}
        values.update(kwargs)
        return owner.create_native_moment(self.library, self.moment.id, 1, **values)

    def background(self, **kwargs):
        return self.pool.submit(self.call, **kwargs).result(timeout=15)

    def wait_ready(self):
        deadline = time.monotonic() + 5
        while True:
            if (self.launched and self.launched[-1][1].poll() is None
                    and (self.launched[-1][0] / 'fixture-ready').exists()):
                return self.launched[-1][0]
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.01)

    def assert_refused(self, **kwargs):
        with self.assertRaises(owner.NativeMomentCreationError) as caught:
            self.background(**kwargs)
        self.assertIsNone(self.store.load())
        return caught.exception

    def test_exact_canonical_descriptor_and_single_opaque_initial_publication(self):
        result = self.background()
        request = json.loads((result.directory / 'request.json').read_text(encoding='utf8'))
        self.assertEqual(request['kit']['descriptor_json'], self.kit.descriptor_json)
        self.assertEqual(request['kit']['descriptor_sha256'], self.kit.descriptor_sha256)
        self.assertNotEqual(json.dumps(json.loads(self.kit.descriptor_json), ensure_ascii=False), self.kit.descriptor_json)
        self.assertEqual(request['moment']['id'], self.moment.id)
        self.assertEqual(request['source']['id'], self.source.id)
        self.assertEqual(result.request_sha256, owner.digest(result.directory / 'request.json'))
        self.assertEqual(self.store.load(), result.snapshot)
        self.assertTrue(result.snapshot.document.startswith(b'OPAQUE FIXTURE DOCUMENT'))
        process = json.loads(result.process_receipt.read_text())
        self.assertEqual(process['scope']['disposition'], 'stopped')
        self.assertEqual(process['scope']['active_processes'], 0)
        self.assertEqual(process['exit_code'], 0)
        self.assertEqual(len(self.launched), 1)

    def test_newer_kit_does_not_replace_the_moments_explicit_pinned_version(self):
        with CreatorLibrary(self.library) as library:
            library.revise_kit(self.kit_id, self.descriptor | {'name': 'New kit'}, expected_version=1)
        result = self.background()
        self.assertEqual(json.loads((result.directory / 'request.json').read_text())['kit']['version'], 1)

    def test_existing_committed_store_refuses_without_launch(self):
        prior = self.store.publish(b'prior opaque document', expected_generation=None, revision=1,
                                  engine='filmcraft', engine_version='fixture', suffix='.fcproj')
        with self.assertRaises(owner.NativeMomentCreationError):
            self.background()
        self.assertEqual(self.store.load(), prior)
        self.assertFalse(self.launched)

    def test_stale_moment_or_wrong_scene_refuses_without_launch(self):
        self.assert_refused(scene_key='another-scene')
        with CreatorLibrary(self.library) as library:
            library.set_overrides(self.moment.id, {}, expected_revision=1)
        self.assert_refused()
        self.assertFalse(self.launched)

    def test_source_and_template_fingerprint_change_refuse_before_launch(self):
        for path in (self.source_path, self.template_path):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(b'changed')
                self.assert_refused()
                path.write_bytes(original)
        self.assertFalse(self.launched)

    def test_moment_or_kit_pin_change_during_helper_work_refuses_after_stop(self):
        for change in ('moment', 'kit'):
            with self.subTest(change=change):
                # Each subcase uses a fresh canonical moment with expected revision 1.
                with CreatorLibrary(self.library) as library:
                    self.moment = library.create_moment(title='Selected clip', source_asset_id=self.source.id,
                        source_in_us=1000000, source_out_us=3000000, kit_id=self.kit_id, kit_version=1)
                self.config = {'mode': 'hold'}
                future = self.pool.submit(self.call)
                directory = self.wait_ready()
                with CreatorLibrary(self.library) as library:
                    if change == 'moment':
                        library.set_overrides(self.moment.id, {}, expected_revision=1)
                    else:
                        library.revise_kit(self.kit_id, self.descriptor, expected_version=1)
                        library.apply_kit_update(self.moment.id, 2, expected_revision=1)
                (directory / 'fixture-proceed').touch()
                with self.assertRaises(owner.NativeMomentCreationError):
                    future.result(timeout=10)
                self.assertIsNone(self.store.load())

    def test_registration_relink_during_helper_refuses_even_with_same_bytes(self):
        self.config = {'mode': 'hold'}
        future = self.pool.submit(self.call)
        directory = self.wait_ready()
        relocated = self.root / 'relocated.bin'
        relocated.write_bytes(self.source_path.read_bytes())
        with CreatorLibrary(self.library) as library:
            library.relocate_asset(self.source.id, relocated, expected_path=self.source.path)
        (directory / 'fixture-proceed').touch()
        with self.assertRaises(owner.NativeMomentCreationError):
            future.result(timeout=10)
        self.assertIsNone(self.store.load())

    def test_windows_read_guards_hold_source_template_and_request_during_child(self):
        self.config = {'mode': 'hold'}
        future = self.pool.submit(self.call)
        directory = self.wait_ready()
        try:
            for path in (self.source_path, self.template_path, directory / 'request.json'):
                original = path.read_bytes()
                with self.assertRaises(OSError):
                    path.write_bytes(b'must not replace admitted bytes')
                self.assertEqual(path.read_bytes(), original)
        finally:
            (directory / 'fixture-proceed').touch()
        result = future.result(timeout=10)
        self.assertEqual(self.store.load(), result.snapshot)

    def test_launch_spec_overwrite_is_denied_before_spawn_and_through_cleanup(self):
        fixture_spawn = owner.subprocess.Popen
        real_stop = ProcessTree.stop
        checks = []

        def deny_replacement(directory, phase):
            launch = directory / 'launch.json'
            original = launch.read_bytes()
            replacement = json.loads(original)
            replacement['argv'] = [sys.executable, '-c',
                "from pathlib import Path; Path('unexpected-started.txt').touch()"]
            with self.assertRaises(OSError):
                launch.write_text(json.dumps(replacement), encoding='utf8')
            self.assertEqual(launch.read_bytes(), original)
            checks.append(phase)

        def guarded_spawn(*args, **kwargs):
            deny_replacement(Path(kwargs['cwd']), 'before_spawn')
            return fixture_spawn(*args, **kwargs)

        def guarded_stop(scope):
            deny_replacement(self.launched[-1][0], 'cleanup')
            return real_stop(scope)

        for mode in ('success', 'failure'):
            with self.subTest(mode=mode):
                self.config = {'mode': mode}
                self.store = ProjectStore(self.root / ('guarded-' + mode))
                with patch.object(owner.subprocess, 'Popen', guarded_spawn), \
                        patch.object(ProcessTree, 'stop', guarded_stop):
                    if mode == 'success':
                        result = self.background()
                        self.assertEqual(self.store.load(), result.snapshot)
                    else:
                        error = self.assert_refused()
                        self.assertIn('staged output is retained', str(error))
                directory = self.launched[-1][0]
                self.assertFalse((directory / 'unexpected-started.txt').exists())
                launch = directory / 'launch.json'
                original = launch.read_bytes()
                launch.write_bytes(original)
                self.assertEqual(launch.read_bytes(), original)
        self.assertEqual(checks, ['before_spawn', 'cleanup'] * 2)
        self.assertEqual(len(self.launched), 2)

    def test_written_request_or_launch_changed_before_guard_refuses_without_spawn(self):
        real_guard = owner._source_read_guard

        for name in ('request.json', 'launch.json'):
            with self.subTest(name=name):
                def replace_before_guard(path):
                    if path.name == name:
                        path.write_bytes(path.read_bytes() + b' ')
                    return real_guard(path)

                with patch.object(owner, '_source_read_guard', replace_before_guard):
                    error = self.assert_refused()
                expected = 'Native request' if name == 'request.json' else 'Native launch intent'
                self.assertIn(expected + ' changed before protection', str(error))
                self.assertFalse(self.launched)

    def test_competing_initial_publication_wins_without_adoption_or_retry(self):
        self.config = {'mode': 'hold'}
        future = self.pool.submit(self.call)
        directory = self.wait_ready()
        other = self.store.publish(b'other creator document', expected_generation=None, revision=1,
                                   engine='filmcraft', engine_version='other', suffix='.fcproj')
        (directory / 'fixture-proceed').touch()
        with self.assertRaises(owner.NativeMomentCreationError) as caught:
            future.result(timeout=10)
        self.assertEqual(caught.exception.state, 'failed')
        self.assertEqual(self.store.load(), other)
        self.assertEqual(len(self.launched), 1)

    def test_receipt_mismatches_never_publish(self):
        tampering = {
            'momentId': 'another moment', 'momentRevision': True, 'requestSha256': '0' * 64,
            'requestedSourceRangeUs.start': 0, 'profileId': 'other', 'sceneKey': 'other',
            'kit.descriptorSha256': '0' * 64, 'sourceAsset': 'other', 'templateAsset': 'other',
            'project': 'different.fcproj', 'projectBytes': 1, 'projectSha256': '0' * 64,
            'exactSaveReopen': False, 'nativeEffectsAndAudioPreserved': False,
            'validation.valid': False, 'nativeInterval.durationTicks': 0,
            'compositions.0.slots.0.role': 'other', 'validation.owners.0.units.0.id': 'other',
        }
        for field, value in tampering.items():
            with self.subTest(field=field):
                self.config = {'tamper': {field: value}}
                error = self.assert_refused()
                self.assertTrue((error.directory / 'moment.fcproj').is_file())
        self.assertEqual(len(self.launched), len(tampering))

    def test_failed_partial_output_is_retained(self):
        self.config = {'mode': 'failure'}
        error = self.assert_refused()
        self.assertEqual((error.directory / 'moment.fcproj').read_bytes(), b'partial opaque fixture output')
        self.assertIn('staged output is retained', str(error))

    def test_cancel_before_launch_and_during_child_retains_partial(self):
        cancel = threading.Event()
        cancel.set()
        self.assertEqual(self.assert_refused(cancel=cancel).state, 'cancelled')
        self.assertFalse(self.launched)
        cancel.clear()
        self.config = {'mode': 'hold'}
        future = self.pool.submit(self.call, cancel=cancel)
        directory = self.wait_ready()
        cancel.set()
        with self.assertRaises(owner.NativeMomentCreationError) as caught:
            future.result(timeout=10)
        self.assertEqual(caught.exception.state, 'cancelled')
        self.assertTrue((directory / 'moment.fcproj').exists())
        self.assertIsNone(self.store.load())

    def test_deadline_and_log_overflow_stop_owned_scope_without_publication(self):
        for mode in ('hang', 'stdout-limit', 'stderr-limit'):
            with self.subTest(mode=mode):
                self.config = {'mode': mode}
                error = self.assert_refused(timeout_seconds=.3 if mode == 'hang' else 4)
                receipt = json.loads((error.directory / 'process.json').read_text())
                self.assertEqual(receipt['scope']['disposition'], 'stopped')
                self.assertEqual(receipt['scope']['active_processes'], 0)
                self.assertLessEqual((error.directory / 'stdout.json').stat().st_size, owner.MAX_RECEIPT)
                self.assertLessEqual((error.directory / 'stderr.log').stat().st_size, owner.MAX_LOG)

    def test_unknown_cleanup_retains_scope_and_prevents_publication_without_retry(self):
        real_stop = ProcessTree.stop

        def unknown(scope):
            result = real_stop(scope)
            return result | {'disposition': 'unknown', 'active_processes': None}

        with patch.object(ProcessTree, 'stop', unknown):
            error = self.assert_refused()
        self.assertEqual(error.state, 'outcome_unknown')
        self.assertIsNotNone(error.owner)
        self.assertIsNotNone(error.owner.job)
        self.assertEqual(len(self.launched), 1)
        paths = (self.source_path, self.template_path,
                 error.directory / 'request.json', error.directory / 'launch.json')
        for path in paths:
            with self.assertRaises(OSError):
                path.write_bytes(path.read_bytes())
        with self.assertRaises(RuntimeError):
            error.owner.close()
        for path in paths:
            with self.assertRaises(OSError):
                path.write_bytes(path.read_bytes())
        proof = error.owner.stop()
        error.owner.close()
        self.assertEqual(proof['disposition'], 'stopped')
        for path in paths:
            original = path.read_bytes()
            path.write_bytes(original)
            self.assertEqual(path.read_bytes(), original)

    def test_hashes_are_outside_writer_lock_and_publish_readmission_is_inside(self):
        observations = []
        verify = CreatorLibrary.verify_asset
        publish = ProjectStore.publish

        def guarded_verify(library, asset_id):
            observations.append(('verify', library.connection.in_transaction,
                                 threading.current_thread() is threading.main_thread()))
            return verify(library, asset_id)

        def guarded_publish(store, *args, **kwargs):
            connection = sqlite3.connect(self.library, timeout=0)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute('BEGIN IMMEDIATE')
            finally:
                connection.close()
            observations.append(('publish', True, threading.current_thread() is threading.main_thread()))
            return publish(store, *args, **kwargs)

        with patch.object(CreatorLibrary, 'verify_asset', guarded_verify), patch.object(ProjectStore, 'publish', guarded_publish):
            self.background()
        self.assertEqual([entry[:2] for entry in observations],
                         [('verify', False)] * 4 + [('publish', True)])
        self.assertTrue(all(not entry[2] for entry in observations))

    def test_wrong_helper_hash_refuses_without_launch(self):
        self.assert_refused(helper=replace(self.helper, sha256='0' * 64))
        self.assertFalse(self.launched)

    def test_attachment_failure_never_releases_the_native_start_gate(self):
        with patch.object(owner, 'ProcessTree', side_effect=OSError('injected attach failure')):
            error = self.assert_refused()
        self.assertEqual(len(self.launched), 1)
        self.assertFalse((error.directory / 'child.json').exists())
        self.assertFalse((error.directory / 'fixture-consumed.json').exists())
        self.assertFalse((error.directory / 'moment.fcproj').exists())
        self.assertIsNotNone(self.launched[0][1].poll())

    def test_cleanup_exception_keeps_original_failure_and_queryable_scope(self):
        self.config = {'mode': 'failure'}
        with patch.object(ProcessTree, 'stop', side_effect=OSError('injected scope query failure')):
            error = self.assert_refused()
        self.assertEqual(error.state, 'outcome_unknown')
        self.assertIn('Native helper failed', str(error))
        self.assertIn('injected scope query failure', error.details['cleanup_error'])
        self.assertIsNotNone(error.owner.job)
        proof = error.owner.stop()
        error.owner.close()
        self.assertEqual(proof['disposition'], 'stopped')

    def test_publication_survives_database_or_final_receipt_failure_without_atomicity_claim(self):
        original_write = CreatorLibrary._write
        original_receipt = owner.write_once
        for failure in ('database-commit', 'published-receipt'):
            with self.subTest(failure=failure):
                self.store = ProjectStore(self.root / failure)
                count = []

                @contextmanager
                def transaction(library):
                    count.append(True)
                    with original_write(library):
                        yield
                        if failure == 'database-commit' and len(count) == 3:
                            raise sqlite3.OperationalError('injected final transaction failure')

                def receipt(path, value):
                    if failure == 'published-receipt' and path.name == 'published.json':
                        raise OSError('injected final receipt failure')
                    return original_receipt(path, value)

                with patch.object(CreatorLibrary, '_write', transaction), patch.object(owner, 'write_once', receipt):
                    with self.assertRaises(owner.NativeMomentCreationError) as caught:
                        self.background()
                error = caught.exception
                self.assertEqual(error.state, 'publication_unknown')
                snapshot = self.store.load()
                self.assertIsNotNone(snapshot)
                self.assertEqual(snapshot.document, (error.directory / 'moment.fcproj').read_bytes())
                self.assertTrue((error.directory / 'publish-intent.json').exists())
                self.assertFalse((error.directory / 'published.json').exists())
                with CreatorLibrary(self.library) as library:
                    self.assertEqual(library.moment(self.moment.id).revision, 1)

    def test_main_thread_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'background owner'):
            self.call()


if __name__ == '__main__':
    unittest.main(verbosity=2)
