"""Real Qt JobRunner delivery through the existing companion action token."""
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication
from native.ui.creator_library import CreatorLibraryWindow
from native.ui.workspace_editor import EditorCompanionBinding
from native.services.workspace_exports import ExportDelivery
from tests.test_workspace_editor_ui import Window,Controller,state


def pump(app,predicate):
    deadline=time.monotonic()+5
    while not predicate() and time.monotonic()<deadline:
        app.processEvents();time.sleep(.005)
    assert predicate()


def test_export_dialog_uses_same_action_jobrunner_and_shows_both_paths(tmp_path):
    app=QApplication.instance() or QApplication([])
    for name in ['segoeui.ttf','segoeuib.ttf']:
        path=Path('C:/Windows/Fonts')/name
        if path.is_file():QFontDatabase.addApplicationFont(str(path))
    class Host(Window):
        action_allowed=CreatorLibraryWindow.action_allowed
        acquire_action=CreatorLibraryWindow.acquire_action
        release_action=CreatorLibraryWindow.release_action
    window=Host();window._action_owner=None;window._action_busy=False
    controller=Controller();controller.value=state('open',owner=controller.owner)
    started,release=threading.Event(),threading.Event();threads=[]
    class Service:
        unsettled=False;blocked=False
        def run(self,folder,*,cancel,progress):
            threads.append(threading.current_thread());started.set();assert release.wait(5)
            progress('Copying both verified files…')
            paths=tuple(str(tmp_path/name) for name in ('landscape.mp4','portrait.mp4'))
            return ExportDelivery('completed','Landscape and portrait are ready.',paths,evidence=str(tmp_path/'evidence'))
    controller._exports=Service()
    binding=EditorCompanionBinding(window,controller);window.editor_binding=binding
    window.resize(760,600);window.show();binding._export();dialog=binding.export_dialog
    dialog.folder.setText(str(tmp_path));dialog.start.click()
    try:
        pump(app,started.is_set)
        assert binding.busy and not binding.save.isEnabled() and not binding.export.isEnabled()
        assert not window.acquire_action(object()) and not dialog.done.isEnabled()
        assert threads and threads[0] is not threading.main_thread()
        release.set();pump(app,lambda:not binding.busy and not window.runner._jobs)
        assert 'landscape.mp4' in dialog.results.toPlainText() and 'portrait.mp4' in dialog.results.toPlainText()
        assert dialog.status.text()=='Landscape and portrait are ready.' and binding.save.isEnabled()
        assert dialog.width()<=760 and dialog.start.geometry().right()<dialog.width()
        render=Path(__file__).resolve().parents[2]/'renders';render.mkdir(exist_ok=True)
        dialog.grab().save(str(render/'export-result.png'))
    finally:
        release.set();pump(app,lambda:not window.runner._jobs)
        dialog.hide();window.removeEventFilter(binding);window.hide();window.deleteLater();app.processEvents()
