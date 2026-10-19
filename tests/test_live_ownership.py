"""Owned fixture processes only. No decoder, model, native editor or network."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid
import wave
from unittest.mock import patch

import pytest

from native.core.creator_library import CreatorLibrary
from native.services import caption_audio as audio_module
from native.services.caption_worker import client as worker_module
from native.services.caption_worker.client import CaptionClient, ExecutionUncertain
from native.services.caption_worker.evidence import hash_file, digest
from native.services.caption_worker.process_tree import ProcessTree
from native.services.durable_captions import DurableCaptionRunner, CaptionJobError

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures'
pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Actual Windows JobObject qualification')


@pytest.fixture
def payload(tmp_path):
    source = tmp_path / 'source.wav'
    with wave.open(str(source), 'wb') as stream:
        stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\0\0' * 160)
    profile = tmp_path / 'profile.json'
    profile.write_text('{}')
    return {'schema_version': 1, 'run_id': str(uuid.uuid4()), 'task': 'transcribe', 'language': 'en',
            'profile_path': str(profile), 'profile_sha256': hash_file(profile),
            'source': {'asset_id': str(uuid.uuid4()), 'path': str(source), 'sha256': hash_file(source),
                       'audio_stream_index': 0, 'channels': [0], 'mix': 'unknown'},
            'audio': {'path': str(source), 'wav_sha256': hash_file(source),
                      'pcm_sha256': hashlib.sha256(b'\0\0' * 160).hexdigest(),
                      'sample_rate': 16000, 'sample_width': 2, 'sample_count': 160, 'channels': 1,
                      'sample_zero_source_us': 0,
                      'extraction': {'source_sha256': hash_file(source), 'argv': ['fixture'],
                                     'tool_version': 'authored fixture'}},
            'range': {'start_us': 0, 'end_us': 10000}}


@pytest.fixture
def owners():
    owned = []
    yield owned
    # Each test exits all fault-injection contexts before this teardown.
    for owner in reversed(owned):
        if owner.has_unsettled:
            proof = owner.cleanup_execution(wait_seconds=5)
            assert proof['ceased'] and not owner.has_unsettled


def client(owners, mode='success'):
    owner = CaptionClient(sys.executable, 'unused', command=[sys.executable, '-u', '-B', '-S',
                          str(FIXTURES / 'caption_fake_worker.py'), mode])
    owners.append(owner)
    return owner


def test_process_tree_close_preserves_exact_handle_after_false_then_success():
    tree = object.__new__(ProcessTree)
    tree.job = object()
    handle = tree.job
    calls = []
    tree.kernel = SimpleNamespace(CloseHandle=lambda value: calls.append(value) or False)
    assert tree.close() is False
    assert tree.job is handle and calls == [handle]
    tree.kernel.CloseHandle = lambda value: calls.append(value) or True
    assert tree.close() is True and tree.job is None
    assert tree.close() is True and calls == [handle, handle]


def test_process_tree_close_exception_does_not_erase_handle():
    tree = object.__new__(ProcessTree)
    tree.job = object()
    handle = tree.job
    def fail(_):
        raise OSError('injected CloseHandle failure')
    tree.kernel = SimpleNamespace(CloseHandle=fail)
    with pytest.raises(OSError):
        tree.close()
    assert tree.job is handle


def test_live_unknown_retains_original_process_scope_and_observation_does_not_stop(payload, tmp_path, owners):
    owner = client(owners, 'sleep')
    with patch.object(ProcessTree, 'stop', return_value={'disposition': 'unknown', 'active_processes': None}):
        result = owner.run(payload, tmp_path / 'runs', timeout=.1)
    captured = owner._captured
    assert result.owner is owner and owner.has_unsettled
    assert captured.process.poll() is None and captured.tree.job
    receipt = (result.directory / 'receipt.json').read_bytes()
    with patch.object(captured.tree, 'stop', side_effect=AssertionError('observation must not stop')):
        proof = owner.observe_execution()
    assert not proof['ceased'] and owner._captured is captured
    with pytest.raises(ExecutionUncertain) as caught:
        owner.run({**payload, 'run_id': str(uuid.uuid4())}, tmp_path / 'successor')
    assert caught.value.owner is owner and not (tmp_path / 'successor').exists()
    proof = owner.cleanup_execution(wait_seconds=5)
    assert proof['ceased'] and proof['parent_reaped'] and proof['streams_closed']
    assert not owner.has_unsettled and captured.tree.job is None
    assert (result.directory / 'receipt.json').read_bytes() == receipt
    # Later explicit launch is allowed only after actual settlement.
    owner.command[-1] = 'success'
    assert owner.run({**payload, 'run_id': str(uuid.uuid4())}, tmp_path / 'later', timeout=5).state == 'complete'


@pytest.mark.parametrize('failure', ['false', 'raise'])
def test_client_release_failure_retains_same_scope_and_recovers_without_stopping(payload, tmp_path, owners, failure):
    owner = client(owners)
    injected = {'return_value': False} if failure == 'false' else {'side_effect': OSError('release fault')}
    with patch.object(ProcessTree, 'close', **injected):
        with pytest.raises(ExecutionUncertain) as caught:
            owner.run(payload, tmp_path / 'runs', timeout=5)
    assert caught.value.owner is owner
    captured = owner._captured
    assert captured.process.poll() == 0 and captured.tree.job
    receipt = (captured.directory / 'receipt.json').read_bytes()
    with patch.object(captured.tree, 'stop', side_effect=AssertionError('read-only recovery')):
        proof = owner.observe_execution(wait_seconds=.1)
    assert proof['ceased'] and proof['resources_released'] and not owner.has_unsettled
    assert (captured.directory / 'receipt.json').read_bytes() == receipt


@pytest.mark.parametrize('artifact', ['receipt.json', 'live-release-'])
def test_client_diagnostic_failure_retains_capability_until_fresh_observation(payload, tmp_path, owners, artifact):
    owner = client(owners)
    original = worker_module.write_once
    def fault(path, value):
        if path.name == artifact or path.name.startswith(artifact):
            raise OSError('injected diagnostic failure')
        return original(path, value)
    with patch.object(worker_module, 'write_once', side_effect=fault):
        with pytest.raises(ExecutionUncertain) as caught:
            owner.run(payload, tmp_path / 'runs', timeout=5)
    assert caught.value.owner is owner and owner.has_unsettled
    captured = owner._captured
    initial = {p.name: p.read_bytes() for p in captured.directory.iterdir() if p.is_file()}
    assert owner.observe_execution(wait_seconds=.1)['ceased']
    assert not owner.has_unsettled
    assert all((captured.directory / name).read_bytes() == content for name, content in initial.items())


def test_stop_exception_and_unformattable_diagnostic_retain_original_owner(payload, tmp_path, owners):
    owner = client(owners)
    class BrokenMessage(OSError):
        def __str__(self):
            raise RuntimeError('diagnostic formatting failed')
    with patch.object(ProcessTree, 'stop', side_effect=BrokenMessage()):
        result = owner.run(payload, tmp_path / 'runs', timeout=5)
    assert result.owner is owner and owner.has_unsettled
    captured = owner._captured
    with patch.object(worker_module, 'write_once', side_effect=BrokenMessage()):
        with pytest.raises(ExecutionUncertain) as caught:
            owner.observe_execution()
    assert caught.value.owner is owner and str(caught.value) == 'BrokenMessage'
    assert owner._captured is captured and captured.tree.job
    assert owner.observe_execution(wait_seconds=.1)['ceased']


def test_failed_readonly_scope_query_keeps_live_queryable_handle(payload, tmp_path, owners):
    owner = client(owners)
    with patch.object(ProcessTree, 'close', return_value=False):
        with pytest.raises(ExecutionUncertain): owner.run(payload, tmp_path / 'runs', timeout=5)
    captured = owner._captured
    handle = captured.tree.job
    with patch.object(captured.tree, 'observe', side_effect=OSError('query failed')):
        proof = owner.observe_execution()
    assert not proof['ceased'] and captured.tree.job == handle
    assert owner.observe_execution(wait_seconds=.1)['ceased']


def test_busy_operation_errors_keep_original_owners_for_ui(tmp_path, owners):
    owner = client(owners)
    owner._busy.acquire()
    try:
        with pytest.raises(ExecutionUncertain) as caught: owner.run({}, tmp_path / 'unused')
        assert caught.value.owner is owner
    finally:
        owner._busy.release()
    preparer, selection, source = audio_case(tmp_path, owners)
    preparer._busy.acquire()
    try:
        with pytest.raises(ExecutionUncertain) as caught: preparer.prepare(selection, tmp_path / 'unused')
        assert caught.value.owner is preparer
    finally:
        preparer._busy.release()


@pytest.mark.parametrize('phase', ['scope', 'parent', 'reader', 'streams'])
def test_observation_fault_never_releases_exact_captured_objects(tmp_path, owners, phase):
    process = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(), stderr=io.BytesIO(), poll=lambda: 0)
    tree = SimpleNamespace(job=object(), observe=lambda: {'disposition': 'stopped', 'active_processes': 0},
                           close=lambda: True)
    thread = type('FixtureThread', (), {'is_alive': lambda self: False})()
    captured = worker_module._CapturedProcess(tmp_path, process, tree, [thread])
    owner = CaptionClient(sys.executable, 'unused')
    owner._captured = captured
    def fail():
        raise OSError('injected observation failure')
    if phase == 'scope': tree.observe = fail
    if phase == 'parent': process.poll = fail
    if phase == 'reader': thread.is_alive = fail
    if phase == 'streams': process.stderr = SimpleNamespace(closed=False, close=fail)
    proof = owner.observe_execution()
    assert not proof['ceased'] and proof['faults'] and owner._captured is captured
    assert tree.job is not None
    # These are memory-only boundary doubles; no process exists to tear down.


@pytest.mark.parametrize('wait', [float('nan'), float('inf'), -1, 6, True])
def test_observation_wait_is_finite_bounded_and_no_mutation(wait):
    owner = CaptionClient(sys.executable, 'unused')
    with pytest.raises(ValueError): owner.observe_execution(wait_seconds=wait)
    assert not owner.has_unsettled


def audio_case(tmp_path, owners):
    source = tmp_path / 'stereo.wav'
    with wave.open(str(source), 'wb') as stream:
        stream.setparams((2, 2, 16000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\0\0\0\0' * 64000)
    ffmpeg = tmp_path / 'fake-ffmpeg.exe'; ffmpeg.write_bytes(b'fake decoder fixture')
    ffprobe = tmp_path / 'fake-ffprobe.exe'; ffprobe.write_bytes(b'fake probe fixture')
    gate = tmp_path / 'gate.py'; gate.write_bytes((FIXTURES / 'caption_audio_fake_gate.py').read_bytes())
    gate.with_name('mode.json').write_text(json.dumps({'mode': 'normal'}))
    profile = tmp_path / 'profile.json'
    profile.write_text(json.dumps({'schema_version': 1, 'ffmpeg': str(ffmpeg),
                                 'assets': [{'role': 'ffmpeg', 'sha256': hash_file(ffmpeg)}]}))
    selection = {'asset_id': str(uuid.uuid4()), 'source_path': str(source), 'source_sha256': hash_file(source),
                 'stream_index': 0, 'channels': [1], 'mix': 'unknown', 'start_us': 1500000,
                 'end_us': 2500000, 'context_start_us': 1000000, 'context_end_us': 3000000,
                 'profile_path': str(profile), 'profile_sha256': hash_file(profile)}
    owner = audio_module.CaptionAudioPreparer(ffmpeg, ffprobe, python=sys.executable, gate=gate)
    owners.append(owner)
    return owner, selection, source


@pytest.mark.parametrize('failure', ['release', 'receipt', 'source_release'])
def test_audio_retains_actual_scope_or_source_guard_on_failure(tmp_path, owners, failure):
    owner, selection, source = audio_case(tmp_path, owners)
    original_bytes = source.read_bytes()
    if failure == 'release':
        context = patch.object(ProcessTree, 'close', return_value=False)
    elif failure == 'source_release':
        context = patch.object(owner, '_release_source', side_effect=OSError('source release fault'))
    else:
        original = audio_module.write_once
        def fault(path, value):
            if path.name == 'receipt.json': raise OSError('receipt write fault')
            return original(path, value)
        context = patch.object(audio_module, 'write_once', side_effect=fault)
    with context:
        if failure == 'release':
            result = owner.prepare(selection, tmp_path / 'runs', timeout=5)
            assert result.owner is owner and result.state == 'failed'
        else:
            with pytest.raises(ExecutionUncertain) as caught:
                owner.prepare(selection, tmp_path / 'runs', timeout=5)
            assert caught.value.owner is owner
    assert owner.has_unsettled
    guard = owner._source_guard
    assert guard is not None
    with pytest.raises(OSError): source.write_bytes(b'cannot replace guarded source')
    with pytest.raises(ExecutionUncertain): owner.prepare(selection, tmp_path / 'successor')
    assert not (tmp_path / 'successor').exists()
    initial = {p.name: p.read_bytes() for p in owner._directory.iterdir() if p.is_file()}
    proof = owner.observe_execution(wait_seconds=.2)
    assert proof['ceased'] and not owner.has_unsettled and owner._source_guard is None
    assert all((owner._directory / name).read_bytes() == content for name, content in initial.items())
    assert source.read_bytes() == original_bytes
    source.write_bytes(original_bytes + b'guard released after settlement')


def test_live_audio_unknown_keeps_source_guard_until_same_scope_stops(tmp_path, owners):
    owner, selection, source = audio_case(tmp_path, owners)
    (tmp_path / 'mode.json').write_text(json.dumps({'mode': 'blocked'}))
    with patch.object(ProcessTree, 'stop', return_value={'disposition': 'unknown', 'active_processes': None}):
        result = owner.prepare(selection, tmp_path / 'runs', timeout=5)
    assert result.owner is owner and result.error_code == 'CLEANUP_FAILED'
    captured, guard = owner._captured, owner._source_guard
    assert captured.process.poll() is None and guard is not None
    initial = (result.directory / 'receipt.json').read_bytes()
    with patch.object(captured.tree, 'stop', side_effect=AssertionError('observation must not stop')):
        assert not owner.observe_execution()['ceased']
    assert owner._source_guard is guard and owner._captured is captured
    with pytest.raises(OSError): source.write_bytes(b'blocked')
    assert owner.cleanup_execution(wait_seconds=1)['ceased']
    assert not owner.has_unsettled and owner._source_guard is None
    assert (result.directory / 'receipt.json').read_bytes() == initial


def test_audio_source_guard_close_failure_preserves_exact_handle(tmp_path):
    owner = audio_module.CaptionAudioPreparer('unused', 'unused', python=sys.executable)
    handle = object()
    kernel = SimpleNamespace(CloseHandle=lambda value: False)
    guard = (kernel, handle)
    owner._source_guard = guard
    with pytest.raises(OSError): owner._release_source()
    assert owner._source_guard is guard and owner.has_unsettled
    kernel.CloseHandle = lambda value: value is handle
    owner._release_source()
    assert owner._source_guard is None and not owner.has_unsettled


def test_returned_observation_cannot_mutate_internal_settlement(payload, tmp_path, owners):
    owner = client(owners)
    assert owner.run(payload, tmp_path / 'runs', timeout=5).state == 'complete'
    proof = owner.observe_execution()
    proof['scope']['disposition'] = 'invented'
    assert owner.observe_execution()['scope']['disposition'] == 'stopped'


@pytest.fixture
def durable(tmp_path, payload, owners):
    with CreatorLibrary(tmp_path / 'library.sqlite') as library:
        asset = library.register_asset(payload['source']['path'])
        payload['source']['asset_id'] = asset.id
        directory = tmp_path / 'prepared'; directory.mkdir()
        worker_module.write_once(directory / 'request.json', payload)
        worker_module.write_once(directory / 'receipt.json', {'request_sha256': digest(payload)})
        prepared = audio_module.PreparedAudio('complete', None, directory, directory / 'request.json')
        tid = library.captions.create({key: payload[key] for key in ('source', 'audio', 'range')})
        original_client = client(owners)
        runner = DurableCaptionRunner(library, tmp_path / 'jobs', original_client)
        def submit():
            return runner.submit(prepared, transcript_id=tid, idempotency_key=str(uuid.uuid4()))
        yield library, runner, submit, original_client


def test_exact_job_filter_selects_second_and_default_preserves_oldest(durable):
    library, runner, submit, owner = durable
    first, second = submit(), submit()
    assert runner.run_next(job_id=second['id'], timeout=5)['id'] == second['id']
    assert library.jobs.job(first['id'])['state'] == 'queued'
    assert runner.run_next(job_id=second['id'], timeout=5) is None
    assert runner.run_next(timeout=5)['id'] == first['id']


def test_foreign_or_invalid_job_filter_cannot_claim_another_job(durable):
    library, runner, submit, owner = durable
    first = submit()
    assert runner.run_next(job_id=str(uuid.uuid4()), timeout=5) is None
    with pytest.raises(ValueError): runner.run_next(job_id='../invalid', timeout=5)
    assert library.jobs.job(first['id'])['state'] == 'queued'


def test_durable_interruption_keeps_exact_owner_and_db_holds_after_live_cleanup(durable):
    library, runner, submit, owner = durable
    first, second = submit(), submit()
    with patch.object(ProcessTree, 'stop', return_value={'disposition': 'unknown', 'active_processes': None}):
        interrupted = runner.run_next(job_id=first['id'], timeout=5)
    assert interrupted['state'] == 'interrupted' and runner.retained_owner is owner
    assert runner.interrupted_job['id'] == first['id'] and runner.has_unsettled
    receipt_path = owner._captured.directory / 'receipt.json'
    receipt = receipt_path.read_bytes()
    holds = library.jobs.resources()
    assert holds and runner.cleanup_execution(wait_seconds=.2)['ceased']
    assert not runner.has_unsettled and library.jobs.resources() == holds
    assert library.jobs.job(first['id'])['state'] == 'interrupted'
    assert library.jobs.job(second['id'])['state'] == 'queued'
    assert receipt_path.read_bytes() == receipt
    with pytest.raises(CaptionJobError) as caught: runner.run_next(job_id=second['id'], timeout=5)
    assert caught.value.owner is owner
    # Cleanup is not a durable settlement or a proposal import.
    assert library.jobs.settlement(first['id']) is None


def test_durable_diagnostic_failure_cannot_drop_retained_owner(durable):
    library, runner, submit, owner = durable
    first = submit()
    original = runner._write
    def fail(category, key, value):
        if category == 'uncertain': raise OSError('uncertain artifact fault')
        return original(category, key, value)
    with patch.object(ProcessTree, 'stop', return_value={'disposition': 'unknown', 'active_processes': None}), patch.object(runner, '_write', side_effect=fail):
        with pytest.raises(OSError) as caught: runner.run_next(job_id=first['id'], timeout=5)
    assert caught.value.owner is owner and runner.retained_owner is owner
    assert runner.interrupted_job['id'] == first['id']


def test_unexpected_abort_keeps_claim_blocked_after_actual_scope_cleanup(durable):
    library, runner, submit, owner = durable
    first, second = submit(), submit()
    original_run = owner.run
    def interrupted(*args, **kwargs):
        original_run(*args, **kwargs)
        raise KeyboardInterrupt('injected unexpected abort')
    with patch.object(ProcessTree, 'stop', return_value={'disposition': 'unknown', 'active_processes': None}), patch.object(owner, 'run', side_effect=interrupted):
        with pytest.raises(KeyboardInterrupt) as caught:
            runner.run_next(job_id=first['id'], timeout=5)
    assert caught.value.owner is owner and runner.interrupted_job['id'] == first['id']
    assert runner.cleanup_execution(wait_seconds=.2)['ceased']
    assert library.jobs.resources() and library.jobs.settlement(first['id']) is None
    with pytest.raises(CaptionJobError): runner.run_next(job_id=second['id'], timeout=5)
    assert library.jobs.job(second['id'])['state'] == 'queued'
