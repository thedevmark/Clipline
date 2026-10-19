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

TEST_ROOT = Path(os.environ.get('CLIPLINE_CONTROLLER_TEST_ROOT', Path(tempfile.gettempdir()) / 'clipline-controller-tests')).resolve()
TEST_ROOT.mkdir(parents=True, exist_ok=True)


def stopped():
    return {'scope': {'disposition': 'stopped', 'active_processes': 0}, 'streams_closed': True}


class FakeSession:
    """Explicit protocol double, not a native engine or process fixture."""

    def __init__(self, case, working):
        self.case, self.working = case, working
        self.directory = case.root / ('editor-' + str(len(case.sessions)))
        self.directory.mkdir()
        self.bridge = self
        self.revision, self.project_reads, self.snapshots, self.stops = 1, 0, 0, 0
        self.stop_result = stopped()
        self.mode = 'normal'
        self.metadata = case.provenance()

    def execute(self, command, params):
        if command == 'project.inspect':
            self.project_reads += 1
            revision = 2 if self.mode == 'changing' and self.project_reads > 1 else self.revision
            return {'path': str(self.working.path), 'revision': revision, 'dirty': False}
        if command == 'metadata.get':
            return {'item': 5, 'metadata': {'clipline.creatorKit.v1': json.dumps(self.metadata)}}
        if command == 'clipline.validate':
            self.case.assertEqual(params['expectedRevision'], self.revision)
            return self.validation()
        if command == 'clipline.saveSnapshot':
            self.snapshots += 1
            self.case.assertEqual(params['expectedProjectPath'], str(self.working.path))
            self.case.assertEqual(params['expectedRevision'], self.revision)
            self.case.assertEqual(params['expectedLinksRevisions'], [{'owner': 5, 'revision': 0}])
            capture = Path(params['path'])
            capture.write_bytes(b'OPAQUE CAPTURE FIXTURE ' + str(self.snapshots).encode())
            if self.mode == 'lost':
                raise OutcomeUnknown('fixture-request', 'engine.execute', 'injected lost snapshot reply')
            if self.mode == 'competitor':
                prior = self.case.store.load()
                self.case.store.publish(b'OPAQUE COMPETING SNAPSHOT', expected_generation=prior.generation,
                    revision=prior.revision + 1, engine='filmcraft', engine_version='fixture', suffix='.fcproj')
            value = self.validation()
            value['projectPath'] = str(self.working.path)
            return {'path': str(capture), 'validation': value}
        raise AssertionError(command)

    def validation(self):
        return {'valid': True, 'revision': self.revision, 'owners': [{'owner': 5, 'linksRevision': 0,
            'units': [{'roles': [{'role': 'gameplay', 'resolvedMediaPath': self.case.asset.path,
                'storedMediaIdentity': self.metadata['nativeImportedIdentity']}]}]}]}

    def stop(self):
        self.stops += 1
        self.stop_receipt = copy.deepcopy(self.stop_result)
        return self.stop_receipt


class CreationOwner:
    def __init__(self):
        self.stops, self.closed = 0, False
        self.result = {'disposition': 'unknown', 'active_processes': None}

    def stop(self):
        self.stops += 1
        return dict(self.result)

    def close(self):
        self.closed = True


@unittest.skipUnless(os.name == 'nt', 'Windows native editor asset guards')
class ControllerCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='controller-', dir=TEST_ROOT)
        self.root = Path(self.temp.name)
        self.workspace = WorkspaceLibrary(self.root / 'workspace')
        self.workspace.directory.mkdir()
        self.source_path = self.root / 'source.bin'; self.source_path.write_bytes(b'owned source fixture')
        self.template_path = self.root / 'template.bin'; self.template_path.write_bytes(b'owned opaque template')
        with CreatorLibrary(self.workspace.database) as library:
            self.asset = library.register_asset(self.source_path)
            self.template = library.register_asset(self.template_path)
            self.descriptor = {'name': 'Pinned Kit', 'asset_refs': {'native_template': self.template.id},
                'properties': {}, 'layout_profiles': [{'id': 'selected', 'scene_key': 'game-scene',
                    'source_width': 1920, 'source_height': 1080,
                    'regions': {'gameplay': {'x': 0, 'y': 0, 'w': 1, 'h': 1}}}], 'vocabulary': []}
            self.kit_id = library.create_kit(self.descriptor)
            self.kit = library.kit_record(self.kit_id, 1)
            self.moment = library.create_moment(title='Selected', source_asset_id=self.asset.id,
                source_in_us=1_000_000, source_out_us=3_000_000, kit_id=self.kit_id, kit_version=1)
        self.store = ProjectStore(self.workspace.directory / 'moments' / self.moment.id)
        self.seed = self.store.publish(b'OPAQUE COMMITTED FIXTURE', expected_generation=None, revision=1,
            engine='filmcraft', engine_version='fixture', suffix='.fcproj')
        executable = Path(sys.executable).resolve()
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        self.controller = subject.WorkspaceEditorController(self.workspace,
            helper=QualifiedCreatorKitHelper(executable, digest, 'fixture-helper'),
            editor=QualifiedEditor(executable, digest, 'fixture-editor'), python=executable)
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.sessions, self.created = [], []
        self.start_hook = None
        self.start_patch = patch.object(subject.NativeSession, 'start', side_effect=self.start)
        self.create_patch = patch.object(subject.native_moment, 'create_native_moment', side_effect=self.create)
        self.mock_start, self.mock_create = self.start_patch.start(), self.create_patch.start()

    def tearDown(self):
        if self.controller._retained_owner is not None:
            owner = self.controller._retained_owner
            if isinstance(owner, FakeSession): owner.stop_result = stopped()
            else: owner.result = {'disposition': 'stopped', 'active_processes': 0}
            self.pool.submit(self.controller.cleanup).result(10)
        if self.controller._session is not None:
            self.controller._session.stop_result = stopped()
            self.pool.submit(self.controller.cleanup).result(10)
        self.pool.shutdown(wait=True)
        self.start_patch.stop(); self.create_patch.stop()
        evidence = os.environ.get('CLIPLINE_CONTROLLER_KEEP_EVIDENCE')
        if evidence:
            shutil.copytree(self.root, Path(evidence) / self._testMethodName)
        self.assertTrue(self.root.resolve().is_relative_to(TEST_ROOT))
        self.temp.cleanup()

    def provenance(self):
        profile = copy.deepcopy(self.descriptor['layout_profiles'][0])
        for rect in profile['regions'].values():
            for key in rect: rect[key] = float(rect[key])
        return {'schema': 1, 'momentId': self.moment.id, 'momentRevision': self.moment.revision,
            'kit': {'id': self.kit_id, 'version': 1, 'descriptorSha256': self.kit.descriptor_sha256},
            'sourceAsset': asdict(self.asset), 'templateAsset': asdict(self.template),
            'assetRefs': self.descriptor['asset_refs'], 'sceneProfile': profile,
            'requestedSourceRangeUs': {'start': 1_000_000, 'end': 3_000_000},
            'nativeImportedIdentity': {'size': self.asset.bytes, 'fingerprint': 'opaque-native-fixture'}}

    def create(self, library, moment_id, revision, **kwargs):
        self.created.append((library, moment_id, revision, kwargs))
        snapshot = kwargs['store'].publish(b'OPAQUE NEW FACTORY FIXTURE', expected_generation=None, revision=1,
            engine='filmcraft', engine_version='fixture-helper', suffix='.fcproj')
        directory = self.root / 'factory'; directory.mkdir()
        return CreatedNativeMoment(snapshot, directory, 'fixture-digest', directory / 'native.json', directory / 'process.json')

    def start(self, store, working, editor, **kwargs):
        self.assertEqual(store.directory, self.store.directory)
        self.assertEqual(working.base_generation, store.load().generation)
        session = FakeSession(self, working)
        self.sessions.append(session)
        if self.start_hook: self.start_hook(session)
        return session

    def open(self, **kwargs):
        args = {'profile_id': 'selected', 'scene_key': 'game-scene'}; args.update(kwargs)
        return self.pool.submit(self.controller.open, self.moment.id, 1, **args).result(10)

    def checkpoint(self):
        return self.pool.submit(self.controller.checkpoint).result(10)

    def cleanup(self):
        return self.pool.submit(self.controller.cleanup).result(10)

    def assert_protected(self):
        for path in (self.source_path, self.template_path):
            with self.assertRaises(OSError): path.write_bytes(path.read_bytes())

    def assert_writable(self):
        for path in (self.source_path, self.template_path): path.write_bytes(path.read_bytes())
