"""Public join is required for a pending reader, even when ident is visible."""
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import threading
import pytest
from native.services import native_moment as moment
from tests import test_native_handle_release_consumers as t
from tests import test_native_moment_reader_retention as previous

REAL_THREAD = threading.Thread

class StartWindowReader:
    """Controlled public observations of a pending Thread.start window."""
    def __init__(self):
        self.release = threading.Event()
        self.thread = REAL_THREAD(target=self.release.wait, daemon=True)
        self.thread.start()
        self.pending = True
        self.joins = 0
        self.ident = self.thread.ident
        assert self.ident is not None
    def is_alive(self): return False if self.pending else self.thread.is_alive()
    def join(self, timeout):
        self.joins += 1
        if self.pending: raise RuntimeError('cannot join thread before it is started')
        self.thread.join(timeout)
    def make_joinable(self):
        self.pending = False
        self.release.set()
        self.thread.join(2)
        assert not self.thread.is_alive()
    def cleanup(self):
        self.release.set(); self.thread.join(2)


def test_moment_pending_visible_ident_requires_same_reader_join(tmp_path):
    reader = StartWindowReader()
    guards, released = ExitStack(), []
    guards.callback(released.append, 'released')
    scope, process = t.FakeScope(True), t.FakeProcess()
    owner = moment.RetainedNativeMomentOwner(scope, process, guards,
        readers=[reader], pending_reader=reader)
    owner.stop()
    try:
        try: owner.close()
        except RuntimeError: pass
        assert owner.scope is scope and owner.job is scope.original_job
        assert scope.closes == 0 and not released and reader.joins == 1
        reader.make_joinable()
        assert owner.close() is True and reader.joins == 2
        assert scope.closes == scope.stops == 1 and released == ['released']
    finally: reader.cleanup()


def test_export_pending_visible_ident_requires_same_reader_join(tmp_path, monkeypatch):
    reader = StartWindowReader()
    child = t.export_owner(tmp_path, True)
    child.threads = [reader]; child._starting_reader = reader
    execution, released = t.export.ExportExecution({'fixture': 'pending-join'}), []
    class GuardAPI:
        def CloseHandle(self, handle): released.append(handle); return True
    monkeypatch.setattr(t.export, '_source_read_guard', lambda path: (GuardAPI(), object()))
    guarded = tmp_path / 'guarded.fixture'; guarded.write_bytes(b'opaque private fixture')
    execution.hold(guarded, t.export.hash_file(guarded)); execution.register(child)
    try:
        execution.stop()
        proof = child.proof
        assert not child.closed and child.tree.job is child.tree.original_job
        assert proof['disposition'] == 'unknown' and not proof['streams_closed']
        assert child.tree.closes == 0 and reader.joins == 1
        assert execution.guards and not released and not execution.closed
        reader.make_joinable()
        execution.stop(); final = child.proof
        assert child.closed and final['streams_closed'] and reader.joins == 2
        assert child.tree.stops == child.tree.closes == 1
        assert execution.closed and not execution.guards and released
    finally: reader.cleanup()


@pytest.mark.parametrize('fault', [RuntimeError, KeyboardInterrupt], ids=['exception', 'interrupt'])
def test_creation_pending_visible_ident_cannot_publish_or_drop_reader(tmp_path, monkeypatch, fault):
    process, scope, launches, guards, store, create = previous.scenario(tmp_path, monkeypatch)
    made = []
    class Reader(StartWindowReader):
        def __init__(self, **kwargs): super().__init__(); made.append(self)
        def start(self): raise fault('controlled pending start with visible ident')
    def invoke():
        with patch.object(moment.threading, 'Thread', Reader): return create()
    retained = None
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            try: pool.submit(invoke).result(timeout=10)
            except BaseException as captured: error = captured
            else: pytest.fail('Pending start must preserve the retained creation owner')
        reader = made[0]; retained = error.owner
        assert type(error) is moment.NativeMomentCreationError and error.state == 'outcome_unknown'
        assert isinstance(error.__cause__, fault) and type(retained) is moment.RetainedNativeMomentOwner
        assert retained.pending_reader is reader and retained.readers == [reader]
        assert retained.job is scope.original_job and not guards and scope.closes == 0
        assert reader.joins == 1 and store.load() is None and len(launches) == 1
        with pytest.raises(RuntimeError): retained.close()
        assert scope.closes == 0 and not guards and reader.joins == 2
        reader.make_joinable()
        retained.close()
        assert reader.joins == 3 and scope.closes == scope.stops == 1 and guards
        assert store.load() is None and len(launches) == 1
    finally:
        for reader in made: reader.cleanup()
