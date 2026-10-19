"""Offscreen Qt checks using the real shared gate; no runtime/native/tool launches."""
from dataclasses import replace
from types import SimpleNamespace
import uuid
import pytest
from PySide6.QtWidgets import QApplication
from native.ui.creator_library import CreatorLibraryWindow
from native.ui.workspace_editor import EditorCompanionBinding
from native.ui.workspace_captions import CaptionDialog
from native.services.workspace_editor import EditorOutcome
from native.services.workspace_captions import CaptionOutcome
from native.services.caption_repair import WordView
from native.services.caption_timing import TimingRepairView


class QueueRunner:
    def __init__(self):self.calls=[]
    def run(self,fn,**callbacks):self.calls.append((fn,callbacks))


@pytest.fixture(scope='module')
def app():
    app=QApplication.instance() or QApplication([])
    from PySide6.QtGui import QFontDatabase,QFont
    from pathlib import Path
    font=Path('C:/Windows/Fonts/segoeui.ttf')
    if font.is_file():QFontDatabase.addApplicationFont(str(font));app.setFont(QFont('Segoe UI'))
    return app


def sample():
    words=(WordView(str(uuid.uuid4()),'Good',1000000,1200000,'aligned','Accepted alignment','machine_accepted'),
           WordView(str(uuid.uuid4()),'shot',1500000,1700000,'inherited','Same interval from revision 1','human'))
    view=TimingRepairView(str(uuid.uuid4()),2,'Authored gameplay fixture',words,None,True,'saved_applied',
        'Saved words match the editor.',False,True,can_time=True,source_start_us=1000000,source_end_us=3000000,timing_message='Enter an original source interval.')
    groups=tuple({'role':role,'sequence':seq,'tracks':[{'id':seq+40,'label':'C1','name':'Saved gameplay captions','language':'en','format':'WebVTT','style':{'size':36}}]} for role,seq in [('shared',211),('landscape',455),('portrait',799)])
    tracks=tuple({'sequence':g['sequence'],'track':g['tracks'][0]['id']} for g in groups)
    proposal={'job_id':str(uuid.uuid4()),'transcript_id':view.transcript_id,'hypothesis_id':str(uuid.uuid4()),'alignment_id':str(uuid.uuid4()),
              'text':'Good shot','word_count':2,'unresolved':0}
    return CaptionOutcome('proposal','Review these generated words. Accepted corrections remain unchanged.',
        context_token=str(uuid.uuid4()),title='Authored fixture · The last shot',source_range=(1000001,2999999),analysis_range=(1000000,3000000),
        profile_name='Small English CPU · Evaluation only',streams=({'stream_index':7,'channels':[0,1]},),track_choices=groups,tracks=tracks,
        proposal=proposal,repair=view,can_generate=True,can_accept=True)


@pytest.fixture
def mounted(app,tmp_path):
    runner=QueueRunner();window=CreatorLibraryWindow(tmp_path/'workspace',runner=runner)
    state=EditorOutcome('open','The active Moment is open.',moment_id=str(uuid.uuid4()),moment_revision=1,
                        can_checkpoint=True,can_close_editor=True,can_cleanup=True,app_close_allowed=False)
    caption=sample()
    controller=SimpleNamespace(_captions=object(),status=lambda:state,caption_status=lambda:caption,
        checkpoint=lambda:None,check_close=lambda:None,recover_working=lambda:None,cleanup=lambda:None)
    host=EditorCompanionBinding(window,controller);window.editor_binding=host
    dialog=CaptionDialog(host);host.caption_dialog=dialog;dialog.refresh(caption)
    yield window,host,dialog,runner,caption
    dialog.close();window.removeEventFilter(host);window.deleteLater();app.processEvents()


def test_caption_dispatch_uses_existing_editor_token_and_excludes_host_actions(mounted):
    window,host,dialog,runner,caption=mounted
    assert window.acquire_action(host)
    before=len(runner.calls)
    assert host.run_caption(lambda:caption,'Generating…',on_finished=dialog.refresh,cancellable=True)
    assert window._action_owner is host and host.busy
    assert not window.acquire_action(object())
    assert not host.save.isEnabled() and not host.close_editor.isEnabled()
    assert not dialog.generate.isEnabled() and not dialog.accept.isEnabled()
    assert not host.run_caption(lambda:caption,'Duplicate',on_finished=dialog.refresh)
    assert len(runner.calls)==before+1
    host._cancel_open();assert host.cancel.is_set()
    runner.calls[-1][1]['on_finished'](caption)
    assert not host.busy and window._action_owner is host
    assert dialog.generate.isEnabled()


def test_host_dispatch_failure_preserves_spelling_and_timing_drafts(mounted):
    _,host,dialog,runner,caption=mounted
    dialog.repair.table.selectRow(1);dialog.repair.spelling.setText('Custom')
    assert dialog.repair.spelling.text()=='Custom'
    assert not dialog.generate.isEnabled() and not dialog.accept.isEnabled()
    assert not dialog.repair.table.isEnabled()
    dialog.repair_binding._save(caption.repair,caption.repair.words[1].id,'Custom')
    assert host.busy and dialog.repair.busy
    runner.calls[-1][1]['on_error']('Injected service refusal')
    assert dialog.repair.spelling.text()=='Custom' and not host.busy
    dialog.repair.set_view(caption.repair);dialog.repair.table.selectRow(1)
    dialog.repair.start_time.setText('1.600000')
    assert not dialog.repair.save.isEnabled() and dialog.repair.save_timing.isEnabled()
    dialog.repair_binding._save_timing(caption.repair,caption.repair.words[1].id,1600000,1700000)
    runner.calls[-1][1]['on_error']('Injected timing refusal')
    assert dialog.repair.start_time.text()=='1.600000'


def test_generated_text_and_human_context_precede_hidden_ids(mounted):
    _,_,dialog,_,caption=mounted
    assert dialog.title.text()==caption.title
    assert 'Moment:' in dialog.range.text() and 'Analysis:' in dialog.range.text()
    assert dialog.text.toPlainText()=='Good shot'
    assert not dialog.details.isVisible() and caption.proposal['job_id'] in dialog.details.toPlainText()
    assert all(box.currentData() is not None for box in dialog.track_boxes)


def test_real_jobrunner_caption_delivery_keeps_shared_gate_and_returns_on_gui_thread(mounted,app):
    import threading,time
    from native.workers import JobRunner
    window,host,dialog,_,caption=mounted
    host.runner=JobRunner();entered=threading.Event();delivered=[]
    def work():
        assert threading.current_thread() is not threading.main_thread()
        entered.set()
        assert host.cancel.wait(2)
        return replace(caption,state='cancelled',message='Authored cancellation confirmed')
    def finished(outcome):
        assert threading.current_thread() is threading.main_thread()
        delivered.append(outcome);dialog.refresh(outcome)
    assert host.run_caption(work,'Generating…',on_finished=finished,cancellable=True)
    deadline=time.monotonic()+3
    while not entered.is_set() and time.monotonic()<deadline:app.processEvents();time.sleep(.001)
    assert entered.is_set() and host.busy and window._action_owner is host
    host._cancel_open()
    while (not delivered or host.runner._jobs) and time.monotonic()<deadline:app.processEvents();time.sleep(.001)
    assert len(delivered)==1 and delivered[0].state=='cancelled'
    assert not host.busy and not host.runner._jobs and window._action_owner is host
