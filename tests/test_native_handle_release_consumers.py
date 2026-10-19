"""Real owner methods with in-memory process/Job/guard boundaries; no child execution."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

from native.core.creator_library import CreatorLibrary
from native.core.project_store import ProjectStore
from native.services import filmcraft_session as session
from native.services import native_moment as moment
from native.services import native_export_client as export

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Job Object release contract")
REAL_OWNED_PROCESS = export.OwnedProcess


class FakeProcess:
    pid = 123
    def __init__(self, code=0):
        self.returncode = code
        self.stdin, self.stdout, self.stderr = io.BytesIO(), io.BytesIO(b'{}'), io.BytesIO()
        self.waits = self.kills = 0
    def poll(self): return self.returncode
    def wait(self, timeout):
        self.waits += 1
        return self.returncode
    def kill(self):
        self.kills += 1
        raise AssertionError('A proven empty scope must not terminate a process')


class FakeScope:
    def __init__(self, release=False):
        self.job = object()
        self.original_job = self.job
        self.scope_id = 'exact-fixture-job'
        self.release = release
        self.stops = self.closes = self.observations = 0
    def observe(self):
        self.observations += 1
        return {'disposition': 'stopped', 'active_processes': 0, 'scope_id': self.scope_id}
    def stop(self):
        self.stops += 1
        return {'disposition': 'stopped', 'active_processes': 0, 'scope_id': self.scope_id}
    def close(self):
        self.closes += 1
        if isinstance(self.release, Exception): raise self.release
        if self.release:
            self.job = None
        return self.release


@pytest.fixture(autouse=True)
def prohibit_real_launch(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('No actual process may launch in these owner tests')
    monkeypatch.setattr(session.subprocess, 'Popen', forbidden)
    monkeypatch.setattr(ctypes, 'get_last_error', lambda: 377)


def session_owner(tmp_path, monkeypatch, release=False):
    import msvcrt
    unlocks = []
    monkeypatch.setattr(msvcrt, 'locking', lambda *args: unlocks.append(args))
    owner = session.NativeSession.__new__(session.NativeSession)
    owner.directory = tmp_path
    owner.path = tmp_path / 'working.fcproj'; owner.path.write_bytes(b'opaque dirty working bytes')
    owner._stop_final = owner._stopped = False
    owner._bridge = object()
    owner._files = [io.BytesIO()]
    owner._input_guards = ExitStack()
    guards = []
    owner._input_guards.callback(guards.append, 'released')
    owner._lock = SimpleNamespace(seek=lambda offset: None, fileno=lambda: 123, close=lambda: unlocks.append('closed'))
    owner.tree, owner.process = FakeScope(release), FakeProcess()
    marker = tmp_path / 'editor-owner-uncertain.json'
    marker.write_text(json.dumps({'run': str(tmp_path)}))
    return owner, guards, unlocks, marker


@pytest.mark.parametrize('release', [False, OSError(991, 'synthetic CloseHandle exception')], ids=['false', 'exception'])
def test_observation_failure_retains_exact_scope_guards_lease_and_does_not_retry(tmp_path, monkeypatch, release):
    owner, guards, unlocks, marker = session_owner(tmp_path, monkeypatch, release)
    tree, lease = owner.tree, owner._lock
    result = owner.observe_exit()
    assert result['scope']['disposition'] == 'unknown' and result['scope_released'] is False
    assert not owner._stop_final and owner.tree is tree and tree.job is tree.original_job
    assert marker.exists() and owner._lock is lease and not guards and not unlocks
    assert tree.closes == tree.observations == 1 and tree.stops == 0
    tree.release = True
    assert owner.observe_exit() is result
    assert tree.closes == tree.observations == 1
    final = owner.stop()
    assert owner._stop_final and final['scope_released'] and final['scope']['disposition'] == 'stopped'
    assert tree.stops == 0 and tree.closes == 2 and owner.process.kills == 0
    assert not marker.exists() and owner._lock is None and guards == ['released'] and unlocks
    assert owner.stop() is final and tree.closes == 2


@pytest.mark.parametrize('release', [False, OSError(992, 'synthetic CloseHandle exception')], ids=['false', 'exception'])
def test_explicit_stop_failure_never_sets_final_or_drops_lease(tmp_path, monkeypatch, release):
    owner, guards, unlocks, marker = session_owner(tmp_path, monkeypatch, release)
    tree, lease = owner.tree, owner._lock
    result = owner.stop()
    assert result['scope']['disposition'] == 'unknown' and result['scope_released'] is False
    assert not owner._stop_final and tree.job is tree.original_job
    assert marker.exists() and owner._lock is lease and not guards and not unlocks
    assert tree.stops == tree.closes == 1
    tree.release = True
    final = owner.stop()
    assert final['scope_released'] and owner._stop_final
    assert tree.stops == 1 and tree.closes == 2 and owner.process.kills == 0
    assert guards == ['released'] and not marker.exists()


def test_successful_observation_preserves_working_bytes_and_canonical_cleanup(tmp_path, monkeypatch):
    owner, guards, unlocks, marker = session_owner(tmp_path, monkeypatch, True)
    original = owner.path.read_bytes()
    result = owner.observe_exit()
    assert result['scope']['disposition'] == 'stopped' and result['scope_released']
    assert owner._stop_final and guards == ['released'] and not marker.exists()
    assert owner.path.read_bytes() == original and owner.tree.stops == 0


@pytest.mark.parametrize('release', [False, OSError(993, 'synthetic CloseHandle exception')], ids=['false', 'exception'])
def test_retained_moment_owner_keeps_guards_until_handle_release(tmp_path, release):
    guards, closed = ExitStack(), []
    guards.callback(closed.append, 'released')
    tree, process = FakeScope(release), FakeProcess()
    owner = moment.RetainedNativeMomentOwner(tree, process, guards)
    proof = owner.stop()
    assert proof['disposition'] == 'stopped' and owner.settled and not closed
    with pytest.raises(OSError): owner.close()
    assert owner.scope is tree and owner.job is tree.original_job and not closed
    tree.release = True
    assert owner.stop() is proof and tree.stops == 1
    assert owner.close() is True and owner.job is None and closed == ['released']
    assert tree.closes == 2 and process.kills == 0
    assert owner.close() is True and tree.closes == 2


def moment_fixture(tmp_path):
    source, template, helper = [tmp_path / n for n in ['source.bin', 'template.fcproj', 'helper.fixture']]
    for path in [source, template, helper]: path.write_bytes(b'opaque synthetic fixture')
    database = tmp_path / 'library.sqlite'
    with CreatorLibrary(database) as library:
        asset, tpl = library.register_asset(source), library.register_asset(template)
        kit = library.create_kit({'name': 'Fixture', 'asset_refs': {'native_template': tpl.id}, 'properties': {},
            'layout_profiles': [{'id': 'exact', 'scene_key': 'OBS', 'source_width': 1920, 'source_height': 1080,
                'regions': {'gameplay': {'x': 0, 'y': 0, 'w': 1, 'h': 1}}}]})
        selected = library.create_moment(title='Pinned', source_asset_id=asset.id, source_in_us=0, source_out_us=1000000,
                                        kit_id=kit, kit_version=1)
    return database, selected, moment.QualifiedCreatorKitHelper(helper, moment.digest(helper), 'fixture-only')


def synthetic_creation_result(directory, process):
    """Authored protocol bytes, not evidence of a native template or engine result."""
    request_path = directory / 'request.json'
    raw = request_path.read_bytes(); request = json.loads(raw)
    output = directory / 'moment.fcproj'; output.write_bytes(b'OPAQUE SYNTHETIC DOCUMENT')
    sequences = {'shared': 5, 'landscape': 13, 'portrait': 21}
    compositions = [{'name': name, 'sequence': sequence,
        'width': 1080 if name == 'portrait' else 1920, 'height': 1920 if name == 'portrait' else 1080,
        'slots': [{'role': 'gameplay', 'video_track': 100 + i, 'prototype_clip': 200 + i,
                   'audio_track': 300 + i, 'prototype_audio': 400 + i}]}
        for i, (name, sequence) in enumerate(sequences.items())]
    role = {'role': 'gameplay', 'audio': 'inherit',
        'clips': {name: 1000 + i for i, name in enumerate(sequences)},
        'nativeAudioClips': {name: [2000 + i] for i, name in enumerate(sequences)},
        'sourceItem': 50, 'resolvedMediaItem': 50, 'resolvedMediaPath': request['source']['path'],
        'storedMediaIdentity': {'size': request['source']['bytes'], 'fingerprint': 'a' * 16}}
    selected = request['moment']
    receipt = {'status': 'pass', 'project': str(output), 'projectBytes': output.stat().st_size,
        'projectSha256': hashlib.sha256(output.read_bytes()).hexdigest(),
        'momentId': selected['id'], 'momentRevision': selected['revision'],
        'requestedSourceRangeUs': {'start': selected['source_in_us'], 'end': selected['source_out_us']},
        'profileId': request['profile_id'], 'sceneKey': request['scene_key'],
        'requestSha256': hashlib.sha256(raw).hexdigest(),
        'kit': {'id': request['kit']['id'], 'version': request['kit']['version'],
                'descriptorSha256': request['kit']['descriptor_sha256']},
        'sourceAsset': request['source']['id'], 'templateAsset': request['template']['id'],
        'mediaCopies': 0, 'nativeImportCalls': 1, 'exactSaveReopen': True,
        'nativeEffectsAndAudioPreserved': True,
        'nativeInterval': {'sourceInTicks': 1000000, 'durationTicks': 2000000,
                          'frames': 120, 'frameRate': {'num': 60, 'den': 1}},
        'compositions': compositions,
        'validation': {'valid': True, 'revision': 10, 'savedRevision': 10,
            'owners': [{'owner': 5, 'schema': 2, 'linksRevision': 0, 'sequences': sequences,
                'units': [{'id': selected['id'], 'parent': None, 'roles': [role]}]}]}}
    process.stdout = io.BytesIO(json.dumps(receipt).encode())


@pytest.mark.parametrize('code', [0, 1], ids=['normal-finalization', 'error-finalization'])
@pytest.mark.parametrize('release', [False, OSError(994, 'synthetic CloseHandle exception')], ids=['false', 'exception'])
def test_creation_release_failure_retains_typed_owner_and_blocks_publication(tmp_path, monkeypatch, code, release):
    database, selected, helper = moment_fixture(tmp_path)
    process, tree, closed = FakeProcess(code), FakeScope(release), []
    launch = []
    def fake_launch(*args, **kwargs):
        launch.append(args)
        synthetic_creation_result(Path(kwargs['cwd']), process)
        return process
    class GuardAPI:
        def CloseHandle(self, handle): closed.append(handle); return True
    api = GuardAPI()
    monkeypatch.setattr(moment.subprocess, 'Popen', fake_launch)
    monkeypatch.setattr(moment, 'ProcessTree', lambda value: tree)
    monkeypatch.setattr(moment, '_source_read_guard', lambda path: (api, object()))
    store = ProjectStore(tmp_path / 'canonical')
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(moment.create_native_moment, database, selected.id, 1,
            profile_id='exact', scene_key='OBS', helper=helper, python=Path(sys.executable),
            store=store, runs=tmp_path / 'runs')
        with pytest.raises(moment.NativeMomentCreationError) as raised: future.result(timeout=10)
    error = raised.value
    assert error.state == 'outcome_unknown' and type(error.owner) is moment.RetainedNativeMomentOwner
    assert error.owner.scope is tree and error.owner.job is tree.original_job and not closed
    assert error.details['process']['disposition'] == 'unknown' and error.details['process']['scope_released'] is False
    assert error.details['cleanup_error'] and tree.stops == tree.closes == len(launch) == 1
    assert process.poll() is not None and process.stdout.closed and process.stderr.closed
    assert store.load() is None and not (error.directory / 'publish-intent.json').exists()
    tree.release = True
    error.owner.stop(); error.owner.close()
    assert tree.stops == 1 and tree.closes == 2 and closed and process.kills == 0
    assert store.load() is None and len(launch) == 1


def export_owner(tmp_path, release=False):
    child = REAL_OWNED_PROCESS.__new__(REAL_OWNED_PROCESS)
    child.directory = tmp_path; child._closing = threading.Lock(); child.close_attempts = 0
    child.tree, child.process, child.threads = FakeScope(release), FakeProcess(), []
    child.proof, child.closed, child.gate_released = {}, False, True
    return child


@pytest.mark.parametrize('release', [False, OSError(995, 'synthetic CloseHandle exception')], ids=['false', 'exception'])
def test_export_release_failure_retains_execution_guards_and_same_job(tmp_path, release):
    child = export_owner(tmp_path, release)
    tree = child.tree
    execution = export.ExportExecution({'kind': 'synthetic retained owner'})
    execution.register(child)
    closed = []
    class GuardAPI:
        def CloseHandle(self, handle): closed.append(handle); return True
    handle = object()
    execution.guards[tmp_path / 'guarded'] = ((GuardAPI(), handle), 'fixture-digest')
    proof = execution.stop()
    assert proof['closed'] is False and proof['processes_stopped'] is False
    assert not child.closed and child.proof['disposition'] == 'unknown' and child.tree is tree
    assert tree.job is tree.original_job and execution.guards and not closed
    tree.release = True
    final = execution.stop()
    assert final['closed'] and child.closed and not execution.guards and closed == [handle]
    assert tree.stops == 1 and tree.closes == 2 and child.process.kills == 0
    assert execution.stop() is final and tree.closes == 2


def test_export_success_preserves_receipt_and_idempotence(tmp_path):
    child = export_owner(tmp_path, True)
    proof = child.close()
    assert child.closed and proof['disposition'] == 'stopped' and proof['active_processes'] == 0
    assert proof['streams_closed'] and child.close() is proof
    assert child.tree.stops == child.tree.closes == 1


def test_false_release_captures_error_before_receipt_formatting(tmp_path, monkeypatch):
    child = export_owner(tmp_path)
    events = []
    original = child.tree.close
    def close(): events.append('close'); return original()
    def last_error(): events.append('error'); return 377
    monkeypatch.setattr(child.tree, 'close', close)
    monkeypatch.setattr(ctypes, 'get_last_error', last_error)
    proof = child.close()
    assert events == ['close', 'error'] and '377' in proof['reason'] and not child.closed


def test_session_parent_wait_failure_keeps_final_cleanup_blocked(tmp_path, monkeypatch):
    owner, guards, unlocks, marker = session_owner(tmp_path, monkeypatch, True)
    original_wait = owner.process.wait
    def delayed(timeout): raise session.subprocess.TimeoutExpired('synthetic parent', timeout)
    monkeypatch.setattr(owner.process, 'wait', delayed)
    result = owner.stop()
    assert not owner._stop_final and result['scope']['disposition'] == 'unknown'
    assert marker.exists() and owner._lock is not None and not guards and not unlocks
    monkeypatch.setattr(owner.process, 'wait', original_wait)
    final = owner.stop()
    assert owner._stop_final and final['scope']['disposition'] == 'stopped' and guards
    assert owner.tree.stops == owner.tree.closes == 1


@pytest.mark.parametrize('boundary', ['parent', 'reader'])
def test_export_unsettled_parent_or_reader_prevents_handle_release(tmp_path, monkeypatch, boundary):
    child = export_owner(tmp_path, True)
    if boundary == 'parent':
        original = child.process.wait
        def delayed(timeout): raise export.subprocess.TimeoutExpired('synthetic parent', timeout)
        monkeypatch.setattr(child.process, 'wait', delayed)
    else:
        reader = SimpleNamespace(join=lambda timeout: None, is_alive=lambda: True)
        child.threads = [reader]
    proof = child.close()
    assert not child.closed and child.tree.closes == 0 and proof['disposition'] == 'unknown'
    if boundary == 'parent': monkeypatch.setattr(child.process, 'wait', original)
    else: reader.is_alive = lambda: False
    final = child.close()
    assert child.closed and final['streams_closed']
    assert child.tree.stops == child.tree.closes == 1


def test_retained_moment_parent_wait_failure_preserves_scope_and_guards(tmp_path, monkeypatch):
    guards, closed = ExitStack(), []
    guards.callback(closed.append, 'released')
    tree, process = FakeScope(True), FakeProcess()
    owner = moment.RetainedNativeMomentOwner(tree, process, guards)
    owner.stop()
    original = process.wait
    def delayed(timeout): raise moment.subprocess.TimeoutExpired('synthetic parent', timeout)
    monkeypatch.setattr(process, 'wait', delayed)
    with pytest.raises(moment.subprocess.TimeoutExpired): owner.close()
    assert owner.job is tree.original_job and tree.closes == 0 and not closed
    monkeypatch.setattr(process, 'wait', original)
    assert owner.close() is True and closed
    assert tree.stops == tree.closes == 1


def test_failed_tool_release_returns_no_successor_input(tmp_path, monkeypatch):
    program, gate = tmp_path / 'program.fixture', tmp_path / 'gate.fixture'
    program.write_bytes(b'not executable'); gate.write_bytes(b'not executed')
    qualified = export.QualifiedProgram(program, export.hash_file(program))
    client = export.NativeExportClient(qualified, qualified, qualified, qualified, gate,
        engine_version='synthetic', source_pin_sha256='a' * 64)
    execution = export.ExportExecution({'fixture': 'tool gate'})
    guards = []
    class GuardAPI:
        def CloseHandle(self, handle): guards.append(handle); return True
    monkeypatch.setattr(export, '_source_read_guard', lambda path: (GuardAPI(), object()))
    children = []
    def launch(argv, directory, cancel, deadline, **kwargs):
        directory.mkdir()
        child = export_owner(directory, False)
        child.send = lambda command: None
        child.wait = lambda: 0
        kwargs['execution'].register(child)
        children.append(child)
        return child
    monkeypatch.setattr(export, 'OwnedProcess', launch)
    with pytest.raises(export.ExportRuntimeError, match='no further tool will launch'):
        client._tool(qualified, ['synthetic-only'], tmp_path / 'probe', threading.Event(), 9999999999, execution)
    assert len(children) == 1 and not children[0].closed and execution.guards and not guards
    # An explicit same-owner cleanup remains unknown while release still fails.
    assert execution.stop()['closed'] is False and not guards
    assert children[0].tree.stops == 1 and children[0].tree.closes == 2
    children[0].tree.release = True
    assert execution.stop()['closed'] and guards and len(children) == 1


@pytest.mark.parametrize('release', [False, OSError(996, 'synthetic CloseHandle exception')], ids=['false', 'exception'])
def test_export_startup_failure_retains_owner_on_original_exception(tmp_path, monkeypatch, release):
    process, tree = FakeProcess(), FakeScope(release)
    monkeypatch.setattr(export.subprocess, 'Popen', lambda *args, **kwargs: process)
    monkeypatch.setattr(export, 'ProcessTree', lambda value: tree)
    class Reader:
        ident = 1
        def __init__(self, **kwargs): pass
        def start(self): raise RuntimeError('synthetic reader startup failure')
        def is_alive(self): return False
        def join(self, timeout): pass  # This fixture models an already completed started reader.
    monkeypatch.setattr(export.threading, 'Thread', Reader)
    with pytest.raises(RuntimeError, match='synthetic reader startup failure') as raised:
        REAL_OWNED_PROCESS(['fixture-only'], tmp_path / 'child', threading.Event(), 9999999999)
    child = raised.value.owner
    assert type(child) is REAL_OWNED_PROCESS and child.tree is tree and child.process is process
    assert tree.job is tree.original_job and not child.closed
    tree.release = True
    assert child.close()['disposition'] == 'stopped' and child.closed
    assert tree.stops == 1 and tree.closes == 2 and process.kills == 0


def test_creation_success_after_release_keeps_canonical_publication(tmp_path, monkeypatch):
    database, selected, helper = moment_fixture(tmp_path)
    process, tree, closed = FakeProcess(), FakeScope(True), []
    def launch(*args, **kwargs):
        synthetic_creation_result(Path(kwargs['cwd']), process)
        return process
    class GuardAPI:
        def CloseHandle(self, handle): closed.append(handle); return True
    monkeypatch.setattr(moment.subprocess, 'Popen', launch)
    monkeypatch.setattr(moment, 'ProcessTree', lambda value: tree)
    monkeypatch.setattr(moment, '_source_read_guard', lambda path: (GuardAPI(), object()))
    store = ProjectStore(tmp_path / 'canonical')
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(moment.create_native_moment, database, selected.id, 1,
            profile_id='exact', scene_key='OBS', helper=helper, python=Path(sys.executable),
            store=store, runs=tmp_path / 'runs').result(timeout=10)
    assert type(result) is moment.CreatedNativeMoment and store.load() == result.snapshot
    assert result.snapshot.path.read_bytes() == b'OPAQUE SYNTHETIC DOCUMENT'
    assert result.snapshot.engine == 'filmcraft' and result.snapshot.revision == 1
    assert (result.directory / 'published.json').exists() and closed
    assert tree.job is None and tree.stops == tree.closes == 1


@pytest.mark.parametrize('boundary', ['wait-error', 'stdin-error'])
def test_session_finally_cannot_finalize_after_unsettled_parent_operation(tmp_path, monkeypatch, boundary):
    owner, guards, unlocks, marker = session_owner(tmp_path, monkeypatch, True)
    if boundary == 'wait-error':
        original = owner.process.wait
        def failed(timeout): raise OSError('synthetic wait failure')
        monkeypatch.setattr(owner.process, 'wait', failed)
    else:
        original = owner.process.stdin
        def failed(): raise OSError('synthetic pipe close failure')
        owner.process.stdin = SimpleNamespace(closed=False, close=failed)
    with pytest.raises(OSError): owner.stop()
    assert not owner._stop_final and owner._lock is not None and marker.exists()
    assert not guards and not unlocks
    if boundary == 'wait-error': monkeypatch.setattr(owner.process, 'wait', original)
    else: owner.process.stdin = original
    result = owner.stop()
    assert owner._stop_final and result['parent_reaped'] and result['streams_closed'] and guards
    assert owner.tree.stops == owner.tree.closes == 1


@pytest.mark.parametrize('fault', [RuntimeError, KeyboardInterrupt], ids=['exception', 'interrupt'])
def test_export_registers_actual_reader_before_start_aborts(tmp_path, monkeypatch, fault):
    process, tree = FakeProcess(), FakeScope(True)
    entered, release = threading.Event(), threading.Event()
    started = []
    original_start, original_join = threading.Thread.start, threading.Thread.join
    def reader(self, pipe, name, limit):
        entered.set()
        release.wait(10)
    def abort_after_start(thread):
        original_start(thread)
        started.append(thread)
        assert entered.wait(2)
        raise fault('start aborted after actual reader began')
    def short_join(thread, timeout=None): return original_join(thread, .01)
    monkeypatch.setattr(export.subprocess, 'Popen', lambda *args, **kwargs: process)
    monkeypatch.setattr(export, 'ProcessTree', lambda value: tree)
    monkeypatch.setattr(REAL_OWNED_PROCESS, '_read', reader)
    monkeypatch.setattr(threading.Thread, 'start', abort_after_start)
    monkeypatch.setattr(threading.Thread, 'join', short_join)
    child = None
    try:
        with pytest.raises(fault, match='start aborted after actual reader began') as raised:
            REAL_OWNED_PROCESS(['fixture-only'], tmp_path / 'child', threading.Event(), 9999999999)
        child = raised.value.owner
        assert len(child.threads) == 1 and child.threads[0] is child._starting_reader is started[0]
        assert child.threads[0].ident is not None and child.threads[0].is_alive()
        assert not child.closed and child.proof['streams_closed'] is False
        assert tree.job is tree.original_job and tree.closes == 0
    finally:
        release.set()
        for thread in started: original_join(thread, 2)
    assert child.close()['streams_closed'] and child.closed
    assert tree.stops == tree.closes == 1 and process.kills == 0


def test_export_never_started_nonpending_thread_does_not_join_or_run(tmp_path):
    child = export_owner(tmp_path, True)
    ran = []
    thread = threading.Thread(target=ran.append, args=('must not run',), daemon=True)
    child.threads = [thread]
    assert thread.ident is None and not thread.is_alive()
    assert child.close()['streams_closed'] and child.closed and not ran
    assert thread.ident is None and child.tree.stops == child.tree.closes == 1


def test_export_ambiguous_start_retains_exact_thread_until_observable(tmp_path, monkeypatch):
    process, tree = FakeProcess(), FakeScope(True)
    monkeypatch.setattr(export.subprocess, 'Popen', lambda *args, **kwargs: process)
    monkeypatch.setattr(export, 'ProcessTree', lambda value: tree)
    class Reader:
        ident = None
        def __init__(self, **kwargs): self.joins = 0
        def start(self): raise RuntimeError('start outcome unconfirmed')
        def is_alive(self): return False
        def join(self, timeout):
            self.joins += 1
            if self.ident is None: raise RuntimeError('cannot join thread before it is started')
    monkeypatch.setattr(export.threading, 'Thread', Reader)
    with pytest.raises(RuntimeError, match='start outcome unconfirmed') as raised:
        REAL_OWNED_PROCESS(['fixture-only'], tmp_path / 'child', threading.Event(), 9999999999)
    child = raised.value.owner
    thread = child.threads[0]
    assert thread is child._starting_reader and thread.joins == 1
    assert not child.closed and not child.proof['streams_closed'] and tree.closes == 0
    assert child.close()['disposition'] == 'unknown' and tree.stops == 1
    # The same fixture object later reports that its start and completion occurred.
    thread.ident = 17
    assert child.close()['streams_closed'] and child.closed
    assert tree.stops == tree.closes == 1 and thread.joins == 3
