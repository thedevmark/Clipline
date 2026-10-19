"""Startup aborts must return the exact still-unsettled owner before diagnostics."""
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


def test_export_cleanup_interrupt_cannot_replace_primary_without_owner(tmp_path, monkeypatch):
    process, scope = t.FakeProcess(), t.FakeScope(True)
    captured = []
    monkeypatch.setattr(export.subprocess, 'Popen', lambda *a, **k: process)
    monkeypatch.setattr(export, 'ProcessTree', lambda value: scope)
    original_close = export.OwnedProcess.close
    def reader_constructor(**kwargs):
        raise RuntimeError('reader constructor failed after actual Popen capture')
    def interrupt_close(owner):
        captured.append(owner)
        raise KeyboardInterrupt('cleanup interrupted with actual ownership still live')
    error = None
    try:
        with patch.object(export.threading, 'Thread', reader_constructor), patch.object(export.OwnedProcess, 'close', interrupt_close):
            try:
                export.OwnedProcess(['fixture-only'], tmp_path / 'child', threading.Event(), 9999999999)
            except BaseException as exc:
                error = exc
        assert captured and scope.job is scope.original_job
        assert getattr(error, 'owner', None) is captured[0], 'startup escape lost the exact actual OwnedProcess'
    finally:
        for owner in captured:
            original_close(owner)
            assert owner.closed and scope.job is None


def test_session_diagnostic_interrupt_keeps_the_existing_session_capability(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path / 'store')
    store.publish(b'opaque native bytes', expected_generation=None, revision=1,
                  engine='filmcraft', engine_version='fixture', suffix='.fcproj')
    working = checkout(store)
    editor = session.QualifiedEditor(Path(sys.executable), session.digest(Path(sys.executable)), 'fixture')
    process, scope = t.FakeProcess(), t.FakeScope(True)
    captured, released = [], []
    lease, write = session.NativeSession._lease, session.write_once
    def retain_fixture_reference(owner, path):
        captured.append(owner)
        return lease(owner, path)
    def guard(owner, path):
        owner._input_guards.callback(released.append, str(path))
    def diagnostic_failure(path, value):
        if path.name == 'launch-error.json':
            raise KeyboardInterrupt('diagnostic write interrupted')
        return write(path, value)
    monkeypatch.setattr(session.subprocess, 'Popen', lambda *a, **k: process)
    monkeypatch.setattr(session, 'ProcessTree', lambda value: scope)
    error = None
    try:
        with patch.object(session.NativeSession, '_lease', retain_fixture_reference), \
             patch.object(session.NativeSession, '_guard_input', guard), \
             patch.object(session, 'write_once', diagnostic_failure):
            try:
                session.NativeSession.start(store, working, editor, python=Path(sys.executable),
                                            runs=tmp_path / 'runs', readiness_seconds=1)
            except BaseException as exc:
                error = exc
        assert captured and scope.job is scope.original_job and not released
        assert getattr(error, 'owner', None) is captured[0], 'launch diagnostics lost the exact NativeSession'
    finally:
        for owner in captured:
            proof = owner.stop()
            assert proof['scope_released'] and proof['parent_reaped'] and scope.job is None
