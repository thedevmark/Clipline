"""Real offscreen Qt selection/dialog interaction; no provider or native runtime."""
from dataclasses import replace
import uuid
import pytest
from PySide6.QtCore import QItemSelectionModel,QTimer
from PySide6.QtWidgets import QApplication,QInputDialog,QMessageBox
from PySide6.QtGui import QFontDatabase,QFont
from native.ui.caption_timing import TimingRepairPanel
from native.services.caption_repair import WordView
from tests.test_workspace_captions_ui import sample
from tests.test_workspace_captions_ui import mounted


@pytest.fixture(scope='module')
def app():
    app=QApplication.instance() or QApplication([])
    QFontDatabase.addApplicationFont('C:/Windows/Fonts/segoeui.ttf')
    app.setFont(QFont('Segoe UI'))
    return app


@pytest.fixture
def panel(app):
    panel=TimingRepairPanel()
    view=sample().repair
    extra=WordView(str(uuid.uuid4()),'again',1800000,1900000,'aligned','Accepted alignment','machine_accepted')
    panel.set_view(replace(view,words=view.words+(extra,),can_structure=True))
    panel.show();app.processEvents()
    yield panel
    panel.close();panel.deleteLater();app.processEvents()


def rows(panel,*indices):
    panel.table.clearSelection()
    for row in indices:
        panel.table.selectionModel().select(panel.table.model().index(row,0),
            QItemSelectionModel.SelectionFlag.Select|QItemSelectionModel.SelectionFlag.Rows)


def test_split_modal_emits_authored_tokens_without_timing_or_rerun(panel,app):
    events=[];panel.structure_requested.connect(lambda *args:events.append(args))
    rows(panel,1);assert panel.split.isEnabled()
    def answer():
        dialog=QApplication.activeModalWidget()
        assert isinstance(dialog,QInputDialog)
        dialog.setTextValue('some thing');dialog.accept()
    QTimer.singleShot(0,answer);panel.split.click()
    assert len(events)==1 and events[0][1]=='split'
    assert events[0][2]==[panel.view.words[1].id] and events[0][3]==['some','thing']


def test_merge_requires_contiguous_rows_and_disables_single_word_controls(panel,app):
    events=[];panel.structure_requested.connect(lambda *args:events.append(args))
    rows(panel,0,2)
    assert not panel.merge.isEnabled() and not panel.delete.isEnabled()
    rows(panel,1,2)
    assert panel.merge.isEnabled() and not panel.split.isEnabled()
    assert not panel.spelling.isEnabled() and not panel.start_time.isEnabled()
    def answer():
        dialog=QApplication.activeModalWidget();dialog.setTextValue('shotagain');dialog.accept()
    QTimer.singleShot(0,answer);panel.merge.click()
    assert events[0][1]=='merge' and events[0][2]==[w.id for w in panel.view.words[1:]]
    assert events[0][3]==['shotagain']


def test_delete_is_explicit_and_drafts_disable_structure(panel,app):
    events=[];panel.structure_requested.connect(lambda *args:events.append(args))
    rows(panel,1);panel.spelling.setText('draft')
    assert not panel.split.isEnabled() and not panel.delete.isEnabled()
    panel.spelling.setText(panel.view.words[1].text)
    panel.start_time.setText('1.600000')
    assert not panel.split.isEnabled() and not panel.delete.isEnabled()
    panel.start_time.setText('1.500000')
    assert panel.delete.isEnabled()
    QTimer.singleShot(0,lambda:QApplication.activeModalWidget().reject())
    panel.delete.click();assert events==[]
    QTimer.singleShot(0,lambda:QApplication.activeModalWidget().button(QMessageBox.StandardButton.Yes).click())
    panel.delete.click()
    assert events[0][1:] == ('delete',[panel.view.words[1].id],[])


def test_uncertain_or_busy_words_cannot_offer_structural_mutations(panel):
    panel.set_busy(True)
    assert not panel.split.isEnabled() and not panel.merge.isEnabled() and not panel.delete.isEnabled()
    panel.set_busy(False)
    panel.set_view(replace(panel.view,state='saved_uncertain',can_structure=False,can_edit=False,can_time=False))
    assert panel.check.isVisible()
    assert not panel.split.isEnabled() and not panel.delete.isEnabled()


def test_structural_signal_uses_existing_workspace_dispatch_and_gate(mounted,app):
    window,host,dialog,runner,caption=mounted
    caption=replace(caption,repair=replace(caption.repair,can_structure=True))
    current=[caption];calls=[]
    host.controller.caption_status=lambda:current[0]
    def repair(token,action,view,**fields):
        calls.append((token,action,view,fields))
        current[0]=replace(caption,state='saved_pending',repair=replace(view,state='saved_pending',can_edit=False,can_time=True))
        return current[0]
    host.controller.repair_captions=repair
    dialog.refresh(caption)
    dialog.repair.table.selectRow(1)
    def answer():
        value=QApplication.activeModalWidget();value.setTextValue('some thing');value.accept()
    QTimer.singleShot(0,answer)
    dialog.repair.split.click()
    assert host.busy and window._action_owner is host
    assert not window.acquire_action(object()) and not host.save.isEnabled()
    fn,callbacks=runner.calls[-1]
    callbacks['on_finished'](fn(None))
    assert calls[0][0]==caption.context_token and calls[0][1]=='split'
    assert calls[0][3]=={'word_ids':[caption.repair.words[1].id],'texts':['some','thing']}
    assert not host.busy and window._action_owner is host
