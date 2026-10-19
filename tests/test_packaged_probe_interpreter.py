"""Interpreter propagation and refusal; no real probe, native process or packaged app runs."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
from unittest.mock import Mock

import pytest

import creator_desktop
from native.core.creator_library import CreatorLibrary, LibraryError
from native.services.editor_runtime import EditorRuntime
from native.services.local_video_probe import LocalVideoProbe
from native.ui.creator_library import CreatorLibraryWindow
from tests.test_source_moment_preparation import app, wait


def test_unfrozen_default_remains_current_python(monkeypatch):
    monkeypatch.delattr(sys, 'frozen', raising=False)
    assert LocalVideoProbe().python == sys.executable


def test_frozen_default_refuses_before_any_process(monkeypatch):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    popen = Mock(side_effect=AssertionError('No launch is permitted'))
    monkeypatch.setattr('native.services.caption_audio.subprocess.Popen', popen)
    with pytest.raises(LibraryError, match='explicit helper Python'):
        LocalVideoProbe()
    popen.assert_not_called()


@pytest.mark.parametrize('kind', ['relative', 'missing', 'directory', 'gui', 'gui-hardlink'])
def test_frozen_unusable_or_gui_alias_helper_refuses_before_launch(tmp_path, monkeypatch, kind):
    gui = tmp_path / 'clipline.exe'; gui.write_bytes(b'GUI fixture, not executable')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(gui))
    candidates = {'relative': Path('python.exe'), 'missing': tmp_path / 'missing.exe',
                  'directory': tmp_path, 'gui': gui}
    if kind == 'gui-hardlink':
        alias = tmp_path / 'helper-alias.exe'; os.link(gui, alias); candidates[kind] = alias
    popen = Mock(side_effect=AssertionError('No launch is permitted'))
    monkeypatch.setattr('native.services.caption_audio.subprocess.Popen', popen)
    with pytest.raises(LibraryError, match='helper Python'):
        LocalVideoProbe(python=candidates[kind])
    popen.assert_not_called()


def test_helper_is_rechecked_before_each_inspection(tmp_path, monkeypatch):
    helper = tmp_path / 'python.exe'; helper.write_bytes(b'Interpreter identity fixture')
    gui = tmp_path / 'clipline.exe'; gui.write_bytes(b'GUI identity fixture')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(gui))
    probe = LocalVideoProbe(python=helper)
    helper.unlink()
    tool = Mock(side_effect=AssertionError('No tool may start'))
    monkeypatch.setattr(probe, '_tool', tool)
    with pytest.raises(LibraryError, match='existing absolute local helper Python'):
        probe.inspect_video(SimpleNamespace(), tmp_path / 'runs')
    tool.assert_not_called()
    assert not probe.has_unsettled


def test_runtime_entrypoint_passes_helper_through_real_window_binding_service_and_gated_launch(tmp_path, monkeypatch, app):
    helper = Path(sys.executable).resolve()
    gui = tmp_path / 'clipline.exe'; gui.write_bytes(b'Packaged GUI fixture')
    ffprobe = tmp_path / 'ffprobe.exe'; ffprobe.write_bytes(b'Probe fixture, never executed')
    program = {'path': str(tmp_path / 'unexecuted-native.exe'), 'sha256': 'a' * 64, 'version': 'fixture'}
    runtime = tmp_path / 'runtime.json'
    runtime.write_text(json.dumps({'schema': 1, 'creator_kit_helper': program, 'editor': program, 'python': str(helper)}))
    fake_app = SimpleNamespace(setApplicationName=lambda _: None, setWindowIcon=lambda _: None, exec=lambda: 0)
    monkeypatch.setattr(creator_desktop, 'QApplication', SimpleNamespace(instance=lambda: fake_app))
    windows = []
    def window(*args, **kwargs):
        actual = CreatorLibraryWindow(*args, **kwargs); windows.append(actual); return actual
    monkeypatch.setattr(creator_desktop, 'CreatorLibraryWindow', window)
    monkeypatch.setattr(EditorRuntime, 'controller', lambda *args, **kwargs: object())
    monkeypatch.setattr('native.ui.workspace_editor.EditorCompanionBinding', lambda *args: SimpleNamespace())
    monkeypatch.setattr('native.ui.workspace_creative.CreativeCompanionBinding', lambda *args: object())
    monkeypatch.setattr('native.ui.workspace_transitions.TransitionCompanionBinding', lambda *args: object())
    monkeypatch.setattr('native.services.local_video_probe.TOOLS', SimpleNamespace(ffprobe=str(ffprobe)))
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(gui))
    assert creator_desktop.main(['--workspace', str(tmp_path / 'workspace'), '--runtime-manifest', str(runtime)]) == 0
    window = windows[0]
    try:
        wait(app, lambda: not window.runner._jobs)
        service = window.moment_preparation_binding.service
        assert service.python == helper
        source = tmp_path / 'source.mp4'; source.write_bytes(b'Registered identity fixture')
        with CreatorLibrary(window.library.database) as library: asset = library.register_asset(source)
        launched = []
        def intercept(argv, **kwargs):
            launched.append(argv)
            raise OSError('Test intercepted the original process boundary')
        monkeypatch.setattr('native.services.caption_audio.subprocess.Popen', intercept)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(OSError, match='intercepted'):
                pool.submit(service.inspect_local, asset.id).result(timeout=10)
        assert service.local_probe.python == helper
        assert len(launched) == 1 and launched[0][0] == str(helper)
        assert launched[0][0] != str(gui)
        assert '-S' in launched[0]  # Existing trusted stdin launch gate, unchanged owner.
        assert not service.has_unsettled and service.local_probe._source_guard is None
    finally:
        window.close()


def test_frozen_window_without_runtime_stays_open_but_refuses_local_probe(tmp_path, monkeypatch, app):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    window = CreatorLibraryWindow(tmp_path / 'workspace')
    try:
        wait(app, lambda: not window.runner._jobs)
        source = tmp_path / 'source.mp4'; source.write_bytes(b'Registered identity fixture')
        with CreatorLibrary(window.library.database) as library: asset = library.register_asset(source)
        popen = Mock(side_effect=AssertionError('No GUI subprocess is permitted'))
        monkeypatch.setattr('native.services.caption_audio.subprocess.Popen', popen)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(LibraryError, match='explicit helper Python'):
                pool.submit(window.moment_preparation_binding.service.inspect_local, asset.id).result(timeout=10)
        popen.assert_not_called()
        assert not window.moment_preparation_binding.service.has_unsettled
    finally:
        window.close()
