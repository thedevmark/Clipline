from dataclasses import replace
import threading
import time
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PySide6.QtGui import QCloseEvent, QFontDatabase
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

from native.services.workspace_library import LibraryPage, LibraryEntry
from native.services.workspace_intake import SourceView
from native.ui.creator_library import CreatorLibraryWindow
from native.ui.workspace_intake import IntakeCompanionBinding
from native.ui.workspace_editor import EditorCompanionBinding

@pytest.fixture(scope='module')
def app():
    app=QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    font=Path(os.environ.get('SystemRoot','/'))/'Fonts/segoeui.ttf'
    if font.is_file():
        # Offscreen Qt does not discover the Windows font automatically.
        assert QFontDatabase.addApplicationFont(str(font))>=0
    yield app

def pump(app,predicate):
    end=time.monotonic()+8
    while not predicate():
        app.processEvents()
        assert time.monotonic()<end,'Qt boundary fixture did not finish'
        time.sleep(.005)
    app.processEvents()

class Controller:
    def __init__(self):
        self.value=SourceView()
        self.calls=[];self.started=threading.Event();self.release=threading.Event()
        self.pending=False;self.actual=object();self.retained=[]
    def status(self):return self.value
    def download(self,clip_id,*,cancel):
        self.calls.append(('download',clip_id,threading.current_thread(),cancel))
        self.started.set()
        assert self.release.wait(5)
        if self.pending:
            self.retained.append(self.actual)
            self.value=SourceView(state='pending',message='Retained source work needs attention.',
                can_download=False,can_authorize=False,can_cleanup=True,app_close_allowed=False)
        else:
            self.value=SourceView(state='completed',message='Source downloaded.')
        return self.value
    def authorize(self,clip_id,job,*,cancel):
        self.calls.append(('authorize',clip_id,threading.current_thread(),cancel))
        self.value=SourceView(state='authorized',message='Choose Download source when ready.')
        return self.value
    def cleanup(self):
        self.value=SourceView(state='interrupted',message='Retained source stopped. No retry.')
        self.retained=[]
        return self.value

@pytest.fixture
def window(app,tmp_path):
    window=CreatorLibraryWindow(tmp_path/'workspace')
    pump(app,lambda:not window.runner._jobs)
    controller=Controller();binding=IntakeCompanionBinding(window,controller)
    entry=LibraryEntry('selected',None,('Authored clip','2026-10-18','4s','10'),'Selected cached clip')
    window.model.replace(LibraryPage('clips',0,None,(entry,)))
    window.table.selectRow(0);window._controls()
    yield window,binding,controller
    controller.release.set()
    pump(app,lambda:not window.runner._jobs)
    controller.value=SourceView()
    window.release_action(binding)
    window.close();window.deleteLater();app.processEvents()

def test_download_captures_selected_id_and_serializes_library_actions(app,window):
    window,binding,controller=window
    assert binding.download.text()=='Download source'
    binding.download.click()
    pump(app,controller.started.is_set)
    assert controller.calls[0][1]=='selected'
    assert controller.calls[0][2] is not threading.main_thread()
    assert not window.connect.isEnabled() and not window.refresh.isEnabled()
    window._connect_twitch() # Direct callback invocation still passes the shared admission gate.
    binding.download.click()
    assert len(controller.calls)==1
    event=QCloseEvent();QApplication.sendEvent(window,event)
    assert not event.isAccepted()
    assert controller.calls[0][3].is_set()
    controller.release.set();pump(app,lambda:not window.runner._jobs)
    assert not binding.busy and window._action_owner is None
    assert binding.status.text()=='Source downloaded.'

def test_unknown_owner_survives_callback_error_and_window_close(app,window):
    window,binding,controller=window
    controller.pending=True
    binding.download.click();pump(app,controller.started.is_set)
    controller.release.set();pump(app,lambda:not window.runner._jobs)
    assert controller.retained==[controller.actual]
    binding._unexpected_error('PRIVATE URL')
    assert 'PRIVATE' not in binding.status.text()
    assert window._action_owner is binding
    assert not window.connect.isEnabled()
    event=QCloseEvent();QApplication.sendEvent(window,event)
    assert not event.isAccepted()
    binding.cleanup.click();pump(app,lambda:not window.runner._jobs)
    assert window._action_owner is None
    assert controller.retained==[]
    assert binding.status.text()=='Retained source stopped. No retry.'

def test_diagnostic_callback_failure_cannot_release_pending_owner(app,window):
    window,binding,controller=window
    controller.pending=True
    binding.download.click();pump(app,controller.started.is_set)
    controller.release.set();pump(app,lambda:not window.runner._jobs)
    with patch.object(binding.status,'setText',side_effect=RuntimeError('authored Qt callback failure')):
        with pytest.raises(RuntimeError):binding._finished(controller.status())
    assert controller.retained==[controller.actual]
    assert window._action_owner is binding
    window._controls()
    assert binding.cleanup.isEnabled()
    event=QCloseEvent();QApplication.sendEvent(window,event)
    assert not event.isAccepted()
    binding.cleanup.click();pump(app,lambda:not window.runner._jobs)

def test_authorize_only_selected_clip_and_no_automatic_download(app,window):
    window,binding,controller=window
    binding.authorize.click();pump(app,lambda:not window.runner._jobs)
    assert [call[:2] for call in controller.calls]==[('authorize','selected')]
    assert controller.calls[0][2] is not threading.main_thread()
    assert window._action_owner is None

def editor_state():
    return SimpleNamespace(can_checkpoint=True,can_close_editor=True,can_cleanup=False,
        app_close_allowed=False,requires_reconciliation=False,message='Open')

def test_editor_and_source_share_action_gate(app,window):
    window,intake,controller=window
    started=threading.Event();release=threading.Event();calls=[]
    def checkpoint():
        calls.append('checkpoint');started.set();assert release.wait(5);return editor_state()
    editor_controller=SimpleNamespace(status=editor_state,checkpoint=checkpoint,save_and_close=lambda **kwargs:None,
        check_close=lambda:None,recover_working=lambda:None,cleanup=lambda:None)
    editor=EditorCompanionBinding(window,editor_controller)
    window.editor_binding=editor;window._controls()
    try:
        editor.save.click();pump(app,started.is_set)
        assert not intake.download.isEnabled()
        intake._download();assert not controller.calls
        release.set();pump(app,lambda:not window.runner._jobs)
        assert window._action_owner is None
        intake.download.click();pump(app,controller.started.is_set)
        assert not editor.save.isEnabled()
        editor._run(checkpoint,'saving')
        assert len(calls)==1
        controller.release.set();pump(app,lambda:not window.runner._jobs)
    finally:
        release.set()
        window.removeEventFilter(editor)

def test_both_cli_manifests_are_opt_in(app,tmp_path):
    import creator_desktop
    fake=SimpleNamespace(library=object(),show=lambda:None)
    intake=SimpleNamespace(client_id='public')
    editor=SimpleNamespace(controller=lambda library:object())
    with (patch.object(creator_desktop,'CreatorLibraryWindow',return_value=fake),
          patch.object(QApplication,'exec',return_value=0),
          patch('native.services.editor_runtime.load_editor_runtime',return_value=editor) as load_editor,
          patch('native.services.intake_runtime.load_intake_runtime',return_value=intake) as load_intake,
          patch('native.ui.workspace_editor.EditorCompanionBinding') as editor_binding,
          patch('native.services.workspace_intake.WorkspaceIntakeController') as intake_controller,
          patch('native.ui.workspace_intake.IntakeCompanionBinding') as intake_binding):
        assert creator_desktop.main(['--workspace',str(tmp_path),'--runtime-manifest',str(tmp_path/'editor.json'),
             '--intake-runtime-manifest',str(tmp_path/'intake.json')])==0
        load_editor.assert_called_once_with(tmp_path/'editor.json')
        load_intake.assert_called_once_with(tmp_path/'intake.json')
        intake_controller.assert_called_once_with(fake.library,intake)
        assert fake.intake_binding is intake_binding.return_value
        assert fake.editor_binding is editor_binding.return_value

def test_no_manifest_starts_no_intake(app,tmp_path):
    import creator_desktop
    fake=SimpleNamespace(show=lambda:None)
    with (patch.object(creator_desktop,'CreatorLibraryWindow',return_value=fake),
          patch.object(QApplication,'exec',return_value=0),
          patch('native.services.intake_runtime.load_intake_runtime') as loader):
        creator_desktop.main(['--workspace',str(tmp_path)])
        loader.assert_not_called()

@pytest.mark.parametrize('flag,module',[('--runtime-manifest','editor_runtime.load_editor_runtime'),
    ('--intake-runtime-manifest','intake_runtime.load_intake_runtime')],ids=['editor','intake'])
def test_invalid_runtime_precedes_window_worker_admission(app,tmp_path,flag,module,capsys):
    import creator_desktop
    with (patch.object(creator_desktop,'CreatorLibraryWindow') as window,
          patch('native.services.'+module,side_effect=ValueError('PRIVATE PAYLOAD'))):
        with pytest.raises(SystemExit) as error:creator_desktop.main([flag,str(tmp_path/'invalid.json')])
        assert error.value.code==2
        window.assert_not_called()
    assert 'PRIVATE' not in capsys.readouterr().err

def test_library_action_blocks_source_admission(app,window):
    window,binding,controller=window
    started=threading.Event();release=threading.Event()
    def operation(job):started.set();assert release.wait(5);return 'Authored library action'
    try:
        window._action(operation,'Authored action')
        pump(app,started.is_set)
        assert not binding.download.isEnabled()
        binding._download()
        assert not controller.calls
    finally:
        release.set()
        pump(app,lambda:not window.runner._jobs)

def test_queued_source_has_explicit_resume_cancel_without_automatic_retry(app,window):
    window,binding,controller=window
    pending=SourceView(state='pending',message='Source slot busy.',job_id='authored-job',
        can_download=False,can_authorize=False,can_resume=True,can_cancel=True,app_close_allowed=False)
    controller.value=pending
    window.acquire_action(binding);binding._refresh()
    calls=[]
    def resume(*,cancel):calls.append('resume');return pending
    def cancel_pending():
        calls.append('cancel');controller.value=SourceView(state='cancelled',message='Queued source cancelled.');return controller.value
    controller.resume=resume;controller.cancel_pending=cancel_pending
    assert binding.resume.isEnabled() and not binding.download.isEnabled()
    app.processEvents();assert not calls
    binding.resume.click();pump(app,lambda:not window.runner._jobs)
    assert calls==['resume'] and window._action_owner is binding
    binding.cancel_action.click();pump(app,lambda:not window.runner._jobs)
    assert calls==['resume','cancel'] and window._action_owner is None

def test_minimum_window_keeps_one_source_row_visible(app,window):
    window,binding,controller=window
    controller.value=SourceView(state='pending',message='The selected source is queued. Resume when a source slot is available, or cancel this task.',
        can_download=False,can_authorize=False,can_resume=True,can_cancel=True,app_close_allowed=False)
    window.hint.hide()
    window.acquire_action(binding);binding._refresh()
    window.resize(760,600);window.show();app.processEvents()
    assert window.width()==760 and window.height()==600
    assert window.table.viewport().height()>=window.table.rowHeight(0)
    assert window.table.geometry().bottom()<window.count.geometry().top()

@pytest.mark.parametrize('opened',[False,True],ids=['idle-editor','open-editor'])
def test_combined_bindings_scroll_without_overlap_and_keep_task_identity(app,window,opened):
    window,intake,controller=window
    state=SimpleNamespace(can_checkpoint=opened,can_close_editor=opened,can_cleanup=False,
        app_close_allowed=not opened,requires_reconciliation=False,message='Open editor' if opened else 'No editor open')
    editor_controller=SimpleNamespace(status=lambda:state,save_and_close=lambda **kwargs:None,
        checkpoint=lambda:state,check_close=lambda:state,recover_working=lambda:state,cleanup=lambda:state)
    editor=EditorCompanionBinding(window,editor_controller);window.editor_binding=editor
    entries=tuple(LibraryEntry(identity,None,(title,'2026-10-18','4s','10'),'Authored metadata fixture')
        for identity,title in [('clipA','Clip A queued'),('clipB','Clip B selected')])
    window._page(LibraryPage('clips',0,None,entries),window._request)
    window.table.selectRow(1)
    pending=SourceView(state='pending',message='The selected source is queued.',clip_id='clipA',job_id='jobA',
        can_download=False,can_authorize=False,can_resume=True,can_cancel=True,app_close_allowed=False)
    controller.value=pending
    calls=[]
    def resume(*,cancel):calls.append(('resume','clipA','jobA'));return pending
    def cancel_pending():
        calls.append(('cancel','clipA','jobA'))
        controller.value=SourceView(state='cancelled',message='Queued source cancelled.',clip_id='clipA')
        return controller.value
    controller.resume=resume;controller.cancel_pending=cancel_pending
    try:
        window.acquire_action(intake);intake._refresh()
        window.resize(760,600);window.show();app.processEvents()
        assert (window.width(),window.height())==(760,600)
        assert window.selected_entry().id=='clipB'
        assert intake.task_identity.text()=='Source task: clipA'
        assert window.table.viewport().height()>=window.table.rowHeight(0)
        assert window.table.geometry().bottom()<window.count.geometry().top()
        for widget in (intake.resume,intake.cancel_action,window.table,window.count,editor.save):
            center=widget.mapTo(window.content,widget.rect().center())
            window.scroll.ensureVisible(center.x(),center.y(),widget.width()//2+8,widget.height()//2+8)
            app.processEvents()
            viewport=window.scroll.viewport()
            top=widget.mapTo(viewport,QPoint(0,0))
            assert viewport.rect().contains(top),widget.objectName() or type(widget).__name__
            assert viewport.rect().contains(widget.mapTo(viewport,widget.rect().bottomRight()))
        assert not editor.save.isEnabled()
        intake.resume.click();pump(app,lambda:not window.runner._jobs)
        assert calls==[('resume','clipA','jobA')]
        assert intake.task_identity.text()=='Source task: clipA'
        intake.cancel_action.click();pump(app,lambda:not window.runner._jobs)
        assert calls==[('resume','clipA','jobA'),('cancel','clipA','jobA')]
        assert window._action_owner is None
        assert editor.save.isEnabled()==opened
    finally:
        window.removeEventFilter(editor)
