"""Moment reader ownership with real threads and synthetic process/Job boundaries."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys
import threading
from unittest.mock import patch

import pytest

from native.core.project_store import ProjectStore
from native.services import native_moment as moment
from tests import test_native_handle_release_consumers as t

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows native owner contract')


def scenario(tmp_path, monkeypatch, code=0):
    database, selected, helper = t.moment_fixture(tmp_path)
    process, scope = t.FakeProcess(code), t.FakeScope(True)
    launches, guards = [], []
    def launch(*args, **kwargs):
        launches.append(args)
        t.synthetic_creation_result(Path(kwargs['cwd']), process)
        return process
    class GuardAPI:
        def CloseHandle(self, handle): guards.append(handle); return True
    monkeypatch.setattr(moment.subprocess, 'Popen', launch)
    monkeypatch.setattr(moment, 'ProcessTree', lambda child: scope)
    monkeypatch.setattr(moment, '_source_read_guard', lambda path: (GuardAPI(), object()))
    store = ProjectStore(tmp_path / 'canonical')
    def create():
        return moment.create_native_moment(database, selected.id, 1,
            profile_id='exact', scene_key='OBS', helper=helper, python=Path(sys.executable),
            store=store, runs=tmp_path / 'runs')
    return process, scope, launches, guards, store, create


@pytest.mark.parametrize('failed_reader', [1, 2], ids=['stdout', 'stderr'])
@pytest.mark.parametrize('fault', [RuntimeError, KeyboardInterrupt], ids=['exception', 'interrupt'])
def test_moment_start_abort_keeps_actual_live_reader_and_guards(tmp_path, monkeypatch, failed_reader, fault):
    process, scope, launches, guards, store, create = scenario(tmp_path, monkeypatch)
    original_read = moment._read_pipe
    original_start, original_join = threading.Thread.start, threading.Thread.join
    release, entered = threading.Event(), threading.Event()
    started, read_arguments = [], []
    # stdout is replaced with synthetic protocol bytes by launch; choose by file label.
    selected_name = 'stdout.json' if failed_reader == 1 else 'stderr.log'
    def read(pipe, path, limit, overflow, errors):
        read_arguments.append((overflow, errors))
        if path.name == selected_name:
            entered.set()
            assert release.wait(10)
        original_read(pipe, path, limit, overflow, errors)
    def start(reader):
        started.append(reader)
        original_start(reader)
        if len(started) == failed_reader:
            assert entered.wait(2)
            raise fault('Moment reader start aborted after OS start')
    def short_join(reader, timeout=None): return original_join(reader, .01)
    def invoke():
        with patch.object(moment, '_read_pipe', read), patch.object(threading.Thread, 'start', start), patch.object(threading.Thread, 'join', short_join):
            return create()
    retained = None
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            try:
                pool.submit(invoke).result(timeout=10)
            except BaseException as captured:
                error = captured
            else:
                pytest.fail('Reader start abort must return a retained creation error')
        assert type(error) is moment.NativeMomentCreationError
        retained = error.owner
        assert error.state == 'outcome_unknown' and type(retained) is moment.RetainedNativeMomentOwner
        assert isinstance(error.__cause__, fault)
        assert retained.scope is scope and retained.job is scope.original_job
        assert retained.readers == started and retained.pending_reader is started[-1]
        assert retained.pending_reader.ident is not None and retained.pending_reader.is_alive()
        assert any(retained.overflow is a and retained.read_errors is b for a, b in read_arguments)
        assert error.details['streams_closed'] is False and not guards
        assert scope.closes == 0 and scope.stops == len(launches) == 1 and store.load() is None
        with pytest.raises(RuntimeError): retained.close()
        assert scope.closes == 0 and not guards
    finally:
        release.set()
        for reader in started: original_join(reader, 2)
    proof = retained.stop()
    assert proof['disposition'] == 'stopped' and scope.stops == 1
    assert retained.close() is True and scope.closes == 1 and guards
    assert all(pipe.closed for pipe in (process.stdin, process.stdout, process.stderr))
    assert len(launches) == 1 and store.load() is None


@pytest.mark.parametrize('fault', [RuntimeError, KeyboardInterrupt], ids=['exception', 'interrupt'])
def test_moment_pending_ident_none_is_retained_without_invalid_join(tmp_path, monkeypatch, fault):
    process, scope, launches, guards, store, create = scenario(tmp_path, monkeypatch)
    made = []
    class PendingReader:
        ident = None
        def __init__(self, *, target, args, daemon):
            self.args = args; self.starts = self.joins = 0; made.append(self)
        def start(self): self.starts += 1; raise fault('Moment start outcome unconfirmed')
        def is_alive(self): return False
        def join(self, timeout):
            self.joins += 1
            if self.ident is None: raise RuntimeError('cannot join thread before it is started')
    def invoke():
        with patch.object(threading, 'Thread', PendingReader): return create()
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            pool.submit(invoke).result(timeout=10)
        except BaseException as captured:
            error = captured
        else:
            pytest.fail('Unconfirmed reader start must return a retained creation error')
    assert type(error) is moment.NativeMomentCreationError
    reader = made[0]
    retained = error.owner
    assert error.state == 'outcome_unknown' and isinstance(error.__cause__, fault)
    assert type(retained) is moment.RetainedNativeMomentOwner
    assert retained.readers == [reader] and retained.pending_reader is reader
    assert retained.overflow is reader.args[-2] and retained.read_errors is reader.args[-1]
    retained.read_errors.append('later synthetic diagnostic'); retained.overflow.set()
    assert reader.args[-1][-1] == 'later synthetic diagnostic' and reader.args[-2].is_set()
    assert retained.scope is scope and retained.job is scope.original_job
    assert error.details['pending_reader_start'] and not error.details['streams_closed']
    assert not guards and scope.closes == 0 and scope.stops == 1 and store.load() is None
    with pytest.raises(RuntimeError): retained.close()
    assert reader.joins == 2 and reader.starts == 1 and not guards
    # Only this same fixture object later reports an observed start and completion.
    reader.ident = 17
    retained.stop(); retained.close()
    assert scope.stops == scope.closes == 1 and guards and len(launches) == 1
    assert reader.starts == 1 and reader.joins == 3 and store.load() is None
    assert all(pipe.closed for pipe in (process.stdin, process.stdout, process.stderr))


@pytest.mark.parametrize('code', [0, 1], ids=['normal-finalization', 'error-finalization'])
def test_moment_releases_scope_only_after_real_readers_and_streams_settle(tmp_path, monkeypatch, code):
    process, scope, launches, guards, store, create = scenario(tmp_path, monkeypatch, code)
    original_read, original_join = moment._read_pipe, threading.Thread.join
    release = threading.Event(); registered = []
    original_close = scope.close
    def read(pipe, path, limit, overflow, errors):
        registered.append(threading.current_thread())
        assert release.wait(10)
        original_read(pipe, path, limit, overflow, errors)
    def join(reader, timeout=None):
        release.set()
        return original_join(reader, 2)
    def close():
        assert len(registered) == 2 and all(not reader.is_alive() for reader in registered)
        assert all(pipe.closed for pipe in (process.stdin, process.stdout, process.stderr))
        assert process.poll() is not None and not guards
        return original_close()
    monkeypatch.setattr(scope, 'close', close)
    def invoke():
        with patch.object(moment, '_read_pipe', read), patch.object(threading.Thread, 'join', join):
            return create()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(invoke)
            if code:
                with pytest.raises(moment.NativeMomentCreationError) as raised: future.result(timeout=10)
                assert raised.value.state == 'failed' and raised.value.owner is None
                assert store.load() is None
            else:
                result = future.result(timeout=10)
                assert store.load() == result.snapshot
        assert scope.stops == scope.closes == len(launches) == 1 and guards
    finally:
        release.set()
        for reader in registered: original_join(reader, 2)


def test_moment_error_with_live_registered_readers_stays_unknown(tmp_path, monkeypatch):
    process, scope, launches, guards, store, create = scenario(tmp_path, monkeypatch, 1)
    original_read, original_join = moment._read_pipe, threading.Thread.join
    release = threading.Event(); registered = []
    def read(pipe, path, limit, overflow, errors):
        registered.append(threading.current_thread())
        assert release.wait(10)
        original_read(pipe, path, limit, overflow, errors)
    def short_join(reader, timeout=None): return original_join(reader, .01)
    def invoke():
        with patch.object(moment, '_read_pipe', read), patch.object(threading.Thread, 'join', short_join):
            return create()
    retained = None
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(moment.NativeMomentCreationError) as raised:
                pool.submit(invoke).result(timeout=10)
        error, retained = raised.value, raised.value.owner
        assert error.state == 'outcome_unknown' and type(retained) is moment.RetainedNativeMomentOwner
        assert len(retained.readers) == 2 and set(retained.readers) == set(registered)
        assert retained.pending_reader is None and any(reader.is_alive() for reader in retained.readers)
        assert scope.closes == 0 and scope.stops == 1 and not guards and store.load() is None
    finally:
        release.set()
        for reader in registered: original_join(reader, 2)
    retained.stop(); retained.close()
    assert scope.stops == scope.closes == len(launches) == 1 and guards and store.load() is None


@pytest.mark.parametrize('boundary', ['wait-timeout', 'wait-error', 'stdin-close', 'output-close'])
def test_session_stop_keeps_handle_until_parent_and_stream_settlement(tmp_path, monkeypatch, boundary):
    owner, guards, unlocks, marker = t.session_owner(tmp_path, monkeypatch, True)
    tree, lease, process = owner.tree, owner._lock, owner.process
    original_wait = process.wait
    original_stdin = process.stdin
    output = owner._files[0]
    class InterruptedStream:
        closed = False
        def close(self): raise OSError('synthetic stream close failure')
    if boundary.startswith('wait'):
        def wait(timeout):
            if boundary == 'wait-timeout': raise t.session.subprocess.TimeoutExpired('fixture', timeout)
            raise OSError('synthetic parent wait failure')
        monkeypatch.setattr(process, 'wait', wait)
    elif boundary == 'stdin-close': process.stdin = InterruptedStream()
    else: owner._files = [InterruptedStream()]
    if boundary == 'wait-timeout':
        result = owner.stop()
        assert result['scope']['disposition'] == 'unknown'
    else:
        with pytest.raises(OSError): owner.stop()
    assert tree.job is tree.original_job and tree.closes == 0
    assert owner.tree is tree and owner._lock is lease and not owner._stop_final
    assert marker.exists() and not guards and not unlocks and tree.stops == 1
    # Cleanup reuses the same prior proven-empty receipt; it does not stop/query again.
    process.wait = original_wait
    if boundary == 'stdin-close': process.stdin = original_stdin
    if boundary == 'output-close': owner._files = [output]
    original_close = tree.close
    def close():
        assert process.waits > 0 and process.stdin.closed
        assert all(stream.closed for stream in owner._files) and not guards
        return original_close()
    monkeypatch.setattr(tree, 'close', close)
    final = owner.stop()
    assert final['scope_released'] and final['parent_reaped'] and final['streams_closed']
    assert owner._stop_final and tree.stops == 1 and tree.closes == 1 and tree.observations == 0
    assert tree.job is None and process.kills == 0 and guards == ['released']
    assert owner._lock is None and unlocks and not marker.exists()
    assert owner.stop() is final and tree.closes == 1
