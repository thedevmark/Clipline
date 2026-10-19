"""Real Qt occurrence review and existing workspace dispatch; no live editor."""
from dataclasses import replace
import uuid
from PySide6.QtCore import Qt,QTimer
from PySide6.QtWidgets import QApplication
from native.ui.caption_repair import SpellingMatchDialog
from native.ui.caption_timing import TimingRepairPanel
from tests.test_caption_structural_ui import app
from tests.test_workspace_captions_ui import sample,mounted


def repeated_view():
    view=sample().repair
    words=tuple(replace(view.words[0],id=str(uuid.uuid4()),text=text,start_us=1000000+i*100000,end_us=1080000+i*100000)
        for i,text in enumerate(('shott','Good','shott','Shott','shott!','shott')))
    return replace(view,words=words,can_structure=True)


def test_dialog_reviews_exact_tokens_and_only_explicit_checked_occurrences(app):
    view=repeated_view();dialog=SpellingMatchDialog(view,view.words[0].id)
    assert dialog.table.rowCount()==3 and dialog.selected_ids()==[view.words[0].id]
    assert not dialog.save.isEnabled()
    dialog.replacement.setText('shot');assert dialog.save.isEnabled()
    dialog.select_all.click();assert dialog.selected_ids()==[view.words[i].id for i in (0,2,5)]
    dialog.table.item(1,0).setCheckState(Qt.CheckState.Unchecked)
    assert dialog.selected_ids()==[view.words[i].id for i in (0,5)]
    dialog.replacement.setText('two words');assert not dialog.save.isEnabled()
    dialog.replacement.setText('shott');assert not dialog.save.isEnabled()
    dialog.replacement.setText('shot');dialog.clear.click();assert not dialog.save.isEnabled()
    dialog.reject()


def test_panel_cancel_drafts_and_uncertain_state_never_emit_matching_edit(app):
    panel=TimingRepairPanel();panel.set_view(repeated_view());panel.table.selectRow(0)
    events=[];panel.matching_requested.connect(lambda *args:events.append(args))
    assert panel.matching.isEnabled()
    QTimer.singleShot(0,lambda:QApplication.activeModalWidget().reject())
    panel.matching.click();assert not events
    panel.spelling.setText('draft');assert not panel.matching.isEnabled()
    panel.spelling.setText('shott');panel.start_time.setText('1.010000')
    assert not panel.matching.isEnabled()
    panel.set_view(replace(panel.view,state='saved_uncertain',can_edit=False))
    assert not panel.matching.isEnabled()
    panel.close()


def test_reviewed_selection_uses_same_workspace_action_token_and_busy_gate(mounted,app):
    window,host,dialog,runner,caption=mounted
    caption=replace(caption,repair=repeated_view());calls=[]
    host.controller.caption_status=lambda:caption
    def repair(token,action,view,**fields):
        calls.append((token,action,view,fields));return caption
    host.controller.repair_captions=repair
    dialog.refresh(caption);dialog.repair.table.selectRow(0)
    def answer():
        review=QApplication.activeModalWidget();assert isinstance(review,SpellingMatchDialog)
        review.replacement.setText('shot');review.select_all.click()
        review.table.item(1,0).setCheckState(Qt.CheckState.Unchecked)
        review.accept()
    QTimer.singleShot(0,answer);dialog.repair.matching.click()
    assert host.busy and window._action_owner is host and not window.acquire_action(object())
    assert not dialog.repair.matching.isEnabled()
    fn,callbacks=runner.calls[-1];callbacks['on_finished'](fn(None))
    assert calls[0][0]==caption.context_token and calls[0][1]=='replace_matching'
    assert calls[0][3]=={'source_text':'shott','text':'shot','word_ids':[caption.repair.words[i].id for i in (0,5)]}
    assert not host.busy
