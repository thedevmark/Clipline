from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import threading
import pytest
from native.services.workspace_creative import WorkspaceCreative,TitleDraft,CreativeOutcome
from native.services.workspace_editor import WorkspaceEditorController,EditorOutcome
from native.services.workspace_library import WorkspaceLibrary
from native.services.native_moment import QualifiedCreatorKitHelper
from native.services.filmcraft_session import QualifiedEditor
from native.services.native_checkpoint import WorkingCopy
from native.services.filmcraft_bridge import OutcomeUnknown,NativeCommandError

class Bridge:
    def __init__(self):self.revision=10;self.calls=[];self.fault=None
    def execute(self,command,params):
        self.calls.append((command,params))
        if command=='clipline.validate':return {'owners':[{'owner':11,'sequences':{'shared':11,'landscape':12,'portrait':13}}]}
        if command=='metadata.get':return {'metadata':{}}
        if command=='sequence.inspect':return {'settings':{'width':1920 if params['item']==12 else 1080,'height':1080 if params['item']==12 else 1920},'video':[]}
        if command=='clipline.addCreativeTitle':
            if self.fault:raise self.fault
            self.revision+=1
            return {**params,'clip':99,'startTicks':0,'durationTicks':1,'scope':'one explicit composition'}
        raise AssertionError(command)

def background(fn):
    out=[];errors=[]
    def run():
        try:out.append(fn())
        except BaseException as error:errors.append(error)
    thread=threading.Thread(target=run);thread.start();thread.join(10);assert not thread.is_alive()
    if errors:raise errors[0]
    return out[0]

@pytest.fixture
def service(tmp_path):
    editor=WorkspaceEditorController(WorkspaceLibrary(tmp_path/'workspace'),
        helper=QualifiedCreatorKitHelper(Path('C:/fixture/helper.exe'),'a'*64,'test'),
        editor=QualifiedEditor(Path('C:/fixture/editor.exe'),'b'*64,'test'),python=Path('C:/fixture/python.exe'))
    bridge=Bridge();editor._session=SimpleNamespace(bridge=bridge)
    editor._facts=SimpleNamespace(moment=SimpleNamespace(id='moment',revision=1,title='Authored moment'),
        kit=SimpleNamespace(descriptor_sha256='c'*64),source=SimpleNamespace(sha256='d'*64))
    editor._view=EditorOutcome('open','Open',working=WorkingCopy(tmp_path/'owned.fcproj','00000000-0000-4000-8000-000000000001',1))
    editor._assert_facts=Mock()
    editor._inspect=lambda:(bridge.revision,[{'owner':11,'revision':0}])
    return WorkspaceCreative(editor),bridge,editor

def draft():return TitleDraft('hook','H04','i picked the worst time to reload',True)

@pytest.mark.parametrize('value',[TitleDraft('hook','none','',False),replace(draft(),evidence_confirmed=False),
    replace(draft(),text=''),replace(draft(),text='x'*161),replace(draft(),duration=float('nan')),
    TitleDraft('cta','stream','come make the next bad decision with chat',True)],ids=['none','evidence','empty','long','finite','destination'])
def test_invalid_or_none_draft_never_requests_a_native_edit(service,value):
    creative,bridge,editor=service
    with pytest.raises(ValueError):creative.apply('unused','portrait',value)
    assert bridge.calls==[]

def test_context_then_explicit_portrait_command_uses_captured_guard_inventory(service):
    creative,bridge,editor=service
    context=background(creative.context)
    assert context.state=='ready'
    outcome=background(lambda:creative.apply(context.token,'portrait',draft()))
    assert outcome.state=='applied' and 'portrait only' in outcome.message
    commands=[p for c,p in bridge.calls if c=='clipline.addCreativeTitle']
    assert len(commands)==1
    assert commands[0]['sequence']==13 and commands[0]['owner']==11
    assert commands[0]['expectedRevision']==10
    assert commands[0]['expectedProjectPath']==str(editor._view.working.path)
    assert commands[0]['expectedLinksRevisions']==[{'owner':11,'revision':0}]
    assert creative._context is None
    assert not any('caption' in c for c,p in bridge.calls)

def test_stale_context_does_not_issue_a_title_or_retarget(service):
    creative,bridge,editor=service
    context=background(creative.context);bridge.revision+=1
    outcome=background(lambda:creative.apply(context.token,'landscape',draft()))
    assert outcome.state=='blocked' and 'Reload' in outcome.message
    assert not any(c=='clipline.addCreativeTitle' for c,p in bridge.calls)
    assert editor._session is not None

def test_received_native_refusal_preserves_open_editor_and_draft_context(service):
    creative,bridge,editor=service
    context=background(creative.context)
    bridge.fault=NativeCommandError('request','engine.execute','This title slot already exists')
    outcome=background(lambda:creative.apply(context.token,'portrait',draft()))
    assert outcome.state=='blocked' and 'already exists' in outcome.message
    assert creative._context['token']==context.token
    assert editor._session is not None and editor._retained_owner is None

def test_unknown_reply_retains_actual_editor_owner_and_never_retries(service):
    creative,bridge,editor=service
    context=background(creative.context);actual=editor._session
    bridge.fault=OutcomeUnknown('request','engine.execute','authored disconnect')
    outcome=background(lambda:creative.apply(context.token,'portrait',draft()))
    assert outcome.state=='blocked'
    assert editor._retained_owner is actual
    assert editor.status().requires_reconciliation and not editor.status().app_close_allowed
    assert len([c for c,p in bridge.calls if c=='clipline.addCreativeTitle'])==1

def test_cta_keeps_one_goal_and_explicit_destination():
    result=TitleDraft('cta','stream','come make the next bad decision with chat',True,
        destination='https://twitch.tv/authored').command_fields()
    assert result['kind']=='cta' and result['recipe']=='stream'
    assert result['destination']=='https://twitch.tv/authored'

def test_recipe_dialog_keeps_words_editable_and_requires_explicit_evidence(tmp_path):
    from PySide6.QtWidgets import QApplication,QWidget
    from native.ui.workspace_creative import CreativeDialog
    app=QApplication.instance() or QApplication([])
    calls=[];window=QWidget()
    binding=SimpleNamespace(host=SimpleNamespace(window=window,busy=False),load=lambda:None,
        apply=lambda *args:calls.append(args))
    dialog=CreativeDialog(binding)
    context=CreativeOutcome('ready','Choose one composition.',token='context',title='Authored moment',
        compositions=({'role':'landscape','width':1920,'height':1080,'sequence':12,'existing':()},
                      {'role':'portrait','width':1080,'height':1920,'sequence':13,'existing':()}))
    dialog.refresh(context)
    assert not dialog.add.isEnabled()
    dialog.pattern.setCurrentIndex(1);dialog.text.setText('my exact approved wording')
    assert not dialog.add.isEnabled()
    dialog.confirm.setChecked(True);dialog.composition.setCurrentIndex(1)
    assert dialog.add.isEnabled();dialog.add.click()
    assert len(calls)==1 and calls[0][1]=='portrait'
    assert calls[0][2].text=='my exact approved wording'
    dialog.close();window.close();app.processEvents()
