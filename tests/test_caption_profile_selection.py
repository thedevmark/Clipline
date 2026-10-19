"""Configured runtime selection with real catalog state and authored native/worker doubles."""
import copy
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from native.core.creator_library import CreatorLibrary
from native.services.caption_runtime import configured_caption_runtimes,load_caption_runtimes
from native.services.caption_worker.evidence import hash_file
from native.services.workspace_captions import WorkspaceCaptions
from tests.test_caption_runtime import manifest
from tests import test_workspace_captions as base


def test_explicit_profiles_load_in_order_without_discovery_or_model_io(tmp_path):
    paths=[]
    for index in range(2):
        value=manifest(tmp_path)
        value['profile']['sha256']=str(index)*64
        value['display_name']=('Quick local','Best local')[index]
        path=tmp_path/f'runtime-{index}.json';path.write_text(json.dumps(value));paths.append(path)
    runtimes=load_caption_runtimes(paths)
    assert [r.display_name for r in runtimes]==['Quick local','Best local']
    assert configured_caption_runtimes(runtimes[0])==(runtimes[0],)
    with pytest.raises(ValueError):load_caption_runtimes([paths[0],paths[0]])
    with pytest.raises(ValueError):load_caption_runtimes([])
    with pytest.raises(ValueError):load_caption_runtimes(paths*3)
    assert not (tmp_path/'authored.bin').exists()


@pytest.fixture
def case():
    c=base.CaptionWorkflowTests('runTest');c.setUp()
    path=c.root/'second-profile.json';path.write_text('{"authored":"second model"}')
    best=SimpleNamespace(**(vars(c.runtime)|{'display_name':'Best local authored fixture','profile':(str(path),hash_file(path))}))
    try:
        with patch('native.services.workspace_captions.CaptionAudioPreparer',return_value=c.workflow.preparer), \
             patch('native.services.workspace_captions.CaptionClient',return_value=c.workflow.client):
            c.workflow=WorkspaceCaptions(c.controller,(c.runtime,best));c.controller._captions=c.workflow
            yield c,best
    finally:c.tearDown()


def switch(c,best):
    return c.bg(c.controller.caption_context,c.moment.id,1,profile_sha256=best.profile[1])


def test_profile_switch_preserves_accepted_native_words_and_pins_next_request(case):
    c,best=case
    proposal=c.generate();accepted=c.accept(proposal)
    applied=c.bg(c.controller.repair_captions,proposal.context_token,'apply',accepted.repair)
    native=copy.deepcopy(c.sessions[0].caption_metadata)
    selected=switch(c,best)
    assert selected.state=='saved_applied' and selected.repair.words==applied.repair.words
    assert selected.repair.native_pin==applied.repair.native_pin
    assert selected.context_token!=proposal.context_token and selected.profile_sha256==best.profile[1]
    assert selected.profile_name==best.display_name and len(selected.profile_choices)==2
    assert c.sessions[0].caption_metadata==native and len(c.worker_requests)==1
    stale=c.bg(c.controller.generate_captions,proposal.context_token,{'stream_index':7,'channels':[0,1],'tracks':[]})
    assert stale.state=='blocked' and len(c.worker_requests)==1
    selected=switch(c,best)
    generated=c.generate(selected)
    assert c.prepared_selections[-1]['profile_sha256']==best.profile[1]
    assert c.worker_requests[-1]['profile_path']==best.profile[0]
    assert generated.repair==selected.repair
    assert c.sessions[0].caption_metadata==native
    with CreatorLibrary(c.workspace.database) as library:
        assert library.captions.head(applied.repair.transcript_id)==1
        assert library.captions.head(generated.proposal['transcript_id'])==0


def test_unknown_changed_or_unsettled_profile_cannot_switch_or_run(case):
    c,best=case
    initial=c.context()
    unknown=c.bg(c.controller.caption_context,c.moment.id,1,profile_sha256='f'*64)
    assert unknown.state=='blocked' and c.workflow.runtime is c.runtime
    best.verify=lambda:(_ for _ in ()).throw(ValueError('Changed authored profile'))
    changed=switch(c,best)
    assert changed.state=='blocked' and c.workflow.runtime is c.runtime
    best.verify=lambda:None
    c.workflow._native_uncertain=True
    blocked=switch(c,best)
    assert blocked.state=='blocked' and c.workflow.runtime is c.runtime
    assert c.worker_requests==[]
    c.workflow._native_uncertain=False


def test_profile_switch_retains_visible_pending_human_split_and_native_pin(case):
    c,best=case
    proposal=c.generate();accepted=c.accept(proposal)
    applied=c.bg(c.controller.repair_captions,proposal.context_token,'apply',accepted.repair)
    pending=c.bg(c.controller.repair_captions,proposal.context_token,'split',applied.repair,
        word_ids=[applied.repair.words[1].id],texts=['some','thing'])
    selected=switch(c,best)
    assert selected.state=='saved_pending' and selected.repair.words==pending.repair.words
    assert selected.repair.native_pin==applied.repair.native_pin
    assert selected.repair.can_time and not selected.repair.can_apply
    assert len(c.worker_requests)==1


def test_profile_selector_uses_shared_gate_and_fresh_context_without_generation(mounted,app):
    window,host,dialog,runner,caption=mounted
    choices=({'sha256':'a'*64,'name':'Quick local'},{'sha256':'b'*64,'name':'Best local'})
    caption=replace(caption,profile_choices=choices,profile_sha256='a'*64,profile_name='Quick local')
    selected=replace(caption,context_token='fresh-authored-token',profile_sha256='b'*64,profile_name='Best local')
    calls=[]
    def context(moment,revision,**kwargs):
        calls.append((moment,revision,kwargs));return selected
    host.controller.caption_context=context
    dialog.refresh(caption)
    dialog.profile_choice.setCurrentIndex(1)
    assert host.busy and window._action_owner is host
    assert not dialog.generate.isEnabled() and not dialog.profile_choice.isEnabled()
    cancel=host.cancel;assert cancel is not None
    operation,callbacks=runner.calls[-1];callbacks['on_finished'](operation(None))
    assert calls[0][2]['profile_sha256']=='b'*64
    assert calls[0][2]['cancel'] is cancel
    assert dialog.outcome.context_token=='fresh-authored-token'
    assert dialog.repair.view==caption.repair and not host.busy
    assert dialog.profile_choice.currentData()=='b'*64


def test_profile_selection_is_disabled_for_unsaved_draft_and_uncertain_state(mounted,app):
    _,host,dialog,_,caption=mounted
    choices=({'sha256':'a'*64,'name':'Quick local'},{'sha256':'b'*64,'name':'Best local'})
    caption=replace(caption,profile_choices=choices,profile_sha256='a'*64)
    dialog.refresh(caption);dialog.repair.table.selectRow(1)
    dialog.repair.spelling.setText('Unsaved human correction')
    assert not dialog.profile_choice.isEnabled()
    dialog.refresh(replace(caption,requires_reconciliation=True))
    assert not dialog.profile_choice.isEnabled()


from tests.test_workspace_captions_ui import app,mounted
