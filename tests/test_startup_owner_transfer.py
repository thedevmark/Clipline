"""Startup error transfer retains the existing capability before secondary work."""
from pathlib import Path
import sys
import threading
from unittest.mock import patch

import pytest
from native.core.project_store import ProjectStore
from native.services.native_checkpoint import checkout
from native.services import filmcraft_session as session
from native.services import native_export_client as export
from tests import test_native_handle_release_consumers as t


@pytest.mark.parametrize('interrupt', [False, True], ids=['settled-cleanup','interrupted-cleanup'])
def test_export_primary_has_exact_owner_before_single_cleanup_attempt(tmp_path, monkeypatch, interrupt):
    primary=RuntimeError('original reader construction failed')
    process,scope=t.FakeProcess(),t.FakeScope(True)
    owners=[]; original=export.OwnedProcess.close
    monkeypatch.setattr(export.subprocess,'Popen',lambda *args,**kwargs:process)
    monkeypatch.setattr(export,'ProcessTree',lambda child:scope)
    def reader(**kwargs): raise primary
    secondary=KeyboardInterrupt('secondary cleanup interruption')
    def close(owner):
        owners.append(owner)
        assert primary.owner is owner and owner.process is process and owner.tree is scope
        if interrupt: raise secondary
        return original(owner)
    try:
        with patch.object(export.threading,'Thread',reader),patch.object(export.OwnedProcess,'close',close):
            with pytest.raises(RuntimeError) as raised:
                export.OwnedProcess(['fixture-only'],tmp_path/'child',threading.Event(),9999999999)
        assert raised.value is primary and len(owners)==1
        owner=owners[0]
        if interrupt:
            assert primary.owner is owner and primary.cleanup_error is secondary
            assert not owner.closed and scope.job is scope.original_job and scope.closes==0
        else:
            assert owner.closed and not hasattr(primary,'owner') and scope.job is None
    finally:
        for owner in owners:
            if not owner.closed: original(owner)


@pytest.mark.parametrize('phase',['primary-format','cleanup','cleanup-diagnostic'])
def test_session_startup_secondary_failure_keeps_typed_error_owner_and_primary_cause(tmp_path,monkeypatch,phase):
    store=ProjectStore(tmp_path/'store')
    store.publish(b'opaque fixture document',expected_generation=None,revision=1,
                  engine='filmcraft',engine_version='fixture',suffix='.fcproj')
    working=checkout(store)
    editor=session.QualifiedEditor(Path(sys.executable),session.digest(Path(sys.executable)),'fixture')
    process,scope=t.FakeProcess(),t.FakeScope(True)
    captured=[]; released=[]; attempts=[]
    original_lease=session.NativeSession._lease; original_stop=session.NativeSession.stop
    original_write=session.write_once; original_poll=process.poll
    diagnostic=KeyboardInterrupt('secondary diagnostic interrupted')
    cleanup=KeyboardInterrupt('secondary cleanup interrupted') if phase=='cleanup' else OSError('single cleanup attempt failed')
    class UnformattablePrimary(RuntimeError):
        def __str__(self): raise diagnostic
    primary=UnformattablePrimary()
    def poll(): raise primary
    if phase=='primary-format': process.poll=poll
    def lease(owner,path): captured.append(owner); return original_lease(owner,path)
    def guard(owner,path): owner._input_guards.callback(released.append,str(path))
    def stop(owner): attempts.append(owner); raise cleanup
    def write(path,value):
        if phase=='cleanup-diagnostic' and path.name=='launch-cleanup-error.json': raise diagnostic
        return original_write(path,value)
    monkeypatch.setattr(session.subprocess,'Popen',lambda *args,**kwargs:process)
    monkeypatch.setattr(session,'ProcessTree',lambda child:scope)
    def start_error():
        try:
            session.NativeSession.start(store,working,editor,python=Path(sys.executable),runs=tmp_path/'runs',readiness_seconds=1)
        except BaseException as error: return error
        pytest.fail('Readiness failure must return the typed existing owner')
    try:
        with patch.object(session.NativeSession,'_lease',lease),patch.object(session.NativeSession,'_guard_input',guard),patch.object(session,'write_once',write):
            if phase=='primary-format': error=start_error()
            else:
                with patch.object(session.NativeSession,'stop',stop): error=start_error()
        owner=captured[0]
        assert type(error) is session.SessionError
        assert error.owner is owner and owner.process is process and owner.tree is scope
        assert scope.job is scope.original_job and scope.closes==0 and not released and not owner._stop_final
        assert owner._lock is not None and (working.path.parent/'editor-owner-uncertain.json').exists()
        if phase=='primary-format':
            assert error.__cause__ is primary and error.diagnostic_errors==[diagnostic] and not attempts
        else:
            assert isinstance(error.__cause__,session.SessionError) and attempts==[owner]
            assert error.cleanup_exception is cleanup and error.cleanup_error
            if phase=='cleanup-diagnostic': assert diagnostic in error.diagnostic_errors
    finally:
        process.poll=original_poll
        for owner in captured:
            proof=original_stop(owner)
            assert proof['scope_released'] and owner._stop_final and scope.job is None
