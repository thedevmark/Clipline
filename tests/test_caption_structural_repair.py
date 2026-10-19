"""Structural repair over real SQLite; native/worker boundaries are authored doubles."""
import copy
from dataclasses import replace
import uuid
import pytest
from native.core.creator_library import CreatorLibrary
from native.core.accepted_transcript import _transaction
from tests import test_workspace_captions as base


@pytest.fixture
def case():
    case=base.CaptionWorkflowTests('runTest')
    case.setUp()
    try:
        proposal=case.generate();accepted=case.accept(proposal)
        applied=case.bg(case.controller.repair_captions,proposal.context_token,'apply',accepted.repair)
        assert applied.state=='saved_applied',applied.message
        yield case,proposal.context_token,applied
    finally:case.tearDown()


def action(case,token,name,view,word_ids,texts):
    return case.bg(case.controller.repair_captions,token,name,view,word_ids=word_ids,texts=texts)


def test_split_waits_for_manual_times_then_applies_once_without_worker_rerun(case):
    c,token,applied=case;old=applied.repair.words[1];initial_calls=c.sessions[0].apply_calls
    tracks=copy.deepcopy(c.sessions[0].caption_metadata['tracks'])
    pending=action(c,token,'split',applied.repair,[old.id],['shot','again'])
    assert pending.state=='saved_pending' and pending.repair.can_time and pending.repair.can_structure
    assert not pending.repair.can_apply and 'Set each new word' in pending.message
    assert all(w.start_us is None and w.end_us is None and w.timing_status=='unresolved' for w in pending.repair.words[1:])
    assert c.sessions[0].apply_calls==initial_calls
    assert pending.repair.native_pin==applied.repair.native_pin
    assert c.sessions[0].caption_metadata['pin']==applied.repair.native_pin
    with CreatorLibrary(c.workspace.database) as library:
        saved=library.captions.revision(pending.repair.transcript_id)
        assert all(w['parent_word_ids']==[old.id] and w['text_origin']=='human' for w in saved['words'][1:])
        assert all(w['timing_provenance']['reason']=='split_internal_boundaries_unknown' for w in saved['words'][1:])
        assert library.captions.revision(pending.repair.transcript_id,1)['words'][1]['text']=='shott'
    first=c.bg(c.controller.repair_captions,token,'retime',pending.repair,
        word_id=pending.repair.words[1].id,start_us=1500000,end_us=1600000)
    assert first.state=='saved_pending' and c.sessions[0].apply_calls==initial_calls
    complete=c.bg(c.controller.repair_captions,token,'retime',first.repair,
        word_id=first.repair.words[2].id,start_us=1600000,end_us=1700000)
    assert complete.state=='saved_applied' and c.sessions[0].apply_calls==initial_calls+1
    assert [w.text for w in complete.repair.words]==['Good','shot','again']
    assert c.sessions[0].caption_metadata['tracks']==tracks
    assert len(c.worker_requests)==1


def test_merge_inherits_outer_interval_and_delete_all_clears_projection(case):
    c,token,applied=case;words=applied.repair.words
    merged=action(c,token,'merge',applied.repair,[w.id for w in words],['Goodshot'])
    assert merged.state=='saved_applied',merged.message
    word=merged.repair.words[0]
    assert (word.start_us,word.end_us,word.timing_status)==(words[0].start_us,words[-1].end_us,'inherited')
    assert word.provenance=='Outer interval from revision 1'
    with CreatorLibrary(c.workspace.database) as library:
        saved=library.captions.revision(merged.repair.transcript_id)
        assert saved['words'][0]['parent_word_ids']==[w.id for w in words]
        assert saved['words'][0]['timing_provenance']['mode']=='outer_span'
    cleared=action(c,token,'delete',merged.repair,[word.id],[])
    assert cleared.state=='saved_applied' and cleared.repair.words==()
    assert c.sessions[0].caption_metadata['input']['accepted']['words']==[]
    assert c.sessions[0].caption_metadata['pin']['revision']==3
    with CreatorLibrary(c.workspace.database) as library:
        assert len(library.captions.revision(merged.repair.transcript_id,1)['words'])==2
        assert library.captions.event(merged.repair.transcript_id,3)['operation']=='delete'
    assert len(c.worker_requests)==1


def test_pending_split_can_be_merged_back_without_guessing_child_times(case):
    c,token,applied=case
    pending=action(c,token,'split',applied.repair,[applied.repair.words[1].id],['some','thing'])
    merged=action(c,token,'merge',pending.repair,[w.id for w in pending.repair.words[1:]],['something'])
    assert merged.state=='saved_pending' and merged.repair.can_time
    assert merged.repair.words[1].start_us is None
    assert merged.repair.words[1].timing_status=='unresolved'
    assert c.sessions[0].apply_calls==1


@pytest.mark.parametrize('change',['foreign-transcript','foreign-pin','foreign-source'],ids=['transcript','pin','source'])
def test_mismatched_native_identity_cannot_grant_pending_split_timing(case,change):
    c,token,applied=case
    pending=action(c,token,'split',applied.repair,[applied.repair.words[1].id],['some','thing'])
    metadata=c.sessions[0].caption_metadata
    if change=='foreign-transcript':metadata['pin']['transcript_id']=str(uuid.uuid4())
    elif change=='foreign-pin':metadata['pin']['payload_sha256']='f'*64
    else:metadata['input']['identity']['source']['sha256']='f'*64
    denied=c.bg(c.controller.repair_captions,token,'retime',pending.repair,
        word_id=pending.repair.words[1].id,start_us=1500000,end_us=1600000)
    assert denied.state=='blocked' and c.sessions[0].apply_calls==1
    with CreatorLibrary(c.workspace.database) as library:
        assert library.captions.head(pending.repair.transcript_id)==2


def test_nonhuman_revision_in_real_catalog_lineage_blocks_repair(case):
    c,token,applied=case
    pending=action(c,token,'split',applied.repair,[applied.repair.words[1].id],['some','thing'])
    tid=pending.repair.transcript_id
    with CreatorLibrary(c.workspace.database) as library:
        words=library.captions.revision(tid)['words']
        # Authored corrupt/imported-history fixture, without changing a trigger.
        with _transaction(library.connection):
            library.captions._commit(tid,2,words,{'operation':'retime','origin':'machine','target_word_ids':[]})
    loaded=c.bg(c.controller.repair_captions,token,'load',transcript_id=tid,revision=3)
    assert loaded.state=='saved_pending' and not loaded.repair.can_time and not loaded.repair.can_structure
    assert 'human repair lineage' in loaded.repair.timing_message
    denied=c.bg(c.controller.repair_captions,token,'retime',loaded.repair,
        word_id=loaded.repair.words[1].id,start_us=1500000,end_us=1600000)
    assert denied.state=='blocked'
    with CreatorLibrary(c.workspace.database) as library:assert library.captions.head(tid)==3
    assert c.sessions[0].apply_calls==1


def test_noncontiguous_or_stale_structural_request_has_no_extra_revision(case):
    c,token,applied=case
    split=action(c,token,'split',applied.repair,[applied.repair.words[1].id],['some','thing'])
    ids=[w.id for w in split.repair.words]
    denied=action(c,token,'merge',split.repair,[ids[0],ids[2]],['Wrong'])
    assert denied.state=='blocked'
    stale=action(c,token,'delete',applied.repair,[applied.repair.words[0].id],[])
    assert stale.state=='blocked'
    with CreatorLibrary(c.workspace.database) as library:assert library.captions.head(split.repair.transcript_id)==2
    assert c.sessions[0].apply_calls==1


def test_lost_structural_application_retains_barrier_and_check_never_reapplies(case):
    c,token,applied=case;c.sessions[0].lose_apply=True
    unknown=action(c,token,'merge',applied.repair,[w.id for w in applied.repair.words],['Goodshot'])
    assert unknown.state=='saved_uncertain' and not unknown.repair.can_structure
    assert c.workflow.unsettled
    assert c.bg(c.controller.checkpoint).state=='blocked'
    assert action(c,token,'delete',unknown.repair,[unknown.repair.words[0].id],[]).state=='blocked'
    calls=c.sessions[0].apply_calls
    checked=c.bg(c.controller.repair_captions,token,'check',unknown.repair)
    assert checked.state=='saved_applied' and checked.repair.can_structure
    assert not c.workflow.unsettled and c.sessions[0].apply_calls==calls
