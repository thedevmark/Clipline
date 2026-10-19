"""Chosen occurrences over real SQLite and existing actual-session doubles."""
import copy,json,uuid
from pathlib import Path
import pytest
from native.core.creator_library import CreatorLibrary
from native.core.accepted_transcript import TranscriptError,StaleRevision
from native.services.caption_worker.evidence import digest
from tests import test_workspace_captions as base


@pytest.fixture
def case():
    class RepeatedWords(base.CaptionWorkflowTests):
        def worker(self,request,root,**kwargs):
            result=super().worker(request,root,**kwargs)
            h=json.loads(result.hypothesis_path.read_text(encoding='utf-8'))
            a=json.loads(result.alignment_path.read_text(encoding='utf-8'))
            h['words']=[{'id':str(uuid.uuid4()),'text':text} for text in ('shott','Good','shott','Shott','shott!','shott')]
            a['words']=[{'word_id':word['id'],'text':word['text'],'timing':{
                'start_us':request['range']['start_us']+i*100000,'end_us':request['range']['start_us']+i*100000+80000,'status':'aligned'}}
                for i,word in enumerate(h['words'])]
            result.hypothesis_path.write_text(json.dumps(h),encoding='utf-8')
            result.alignment_path.write_text(json.dumps(a),encoding='utf-8')
            path=result.directory/'receipt.json';receipt=json.loads(path.read_text(encoding='utf-8'))
            receipt['hashes'].update(hypothesis=digest(h),alignment=digest(a));path.write_text(json.dumps(receipt),encoding='utf-8')
            return result
    c=RepeatedWords('runTest');c.setUp()
    try:
        proposal=c.generate();accepted=c.accept(proposal)
        applied=c.bg(c.controller.repair_captions,proposal.context_token,'apply',accepted.repair)
        assert applied.state=='saved_applied',applied.message
        yield c,proposal.context_token,applied
    finally:c.tearDown()


def action(c,token,view,ids,source='shott',text='shot'):
    return c.bg(c.controller.repair_captions,token,'replace_matching',view,source_text=source,text=text,word_ids=ids)


def test_chosen_noncontiguous_matches_one_revision_one_projection_preserves_evidence(case):
    c,token,applied=case;view=applied.repair;tid=view.transcript_id
    with CreatorLibrary(c.workspace.database) as library:
        original=library.captions.revision(tid,1)
        source=Path(library.captions.identity(tid)['source']['path'])
        kits=[tuple(row) for row in library.connection.execute('SELECT * FROM kit_versions')]
        other=library.captions.create(library.captions.identity(tid))
        initial=library.captions.event(tid,1)
        h=library.captions.proposal(tid,initial['hypothesis_id']);h['run_id']=str(uuid.uuid4())
        a=library.captions.proposal(tid,initial['alignment_id']);a['run_id']=str(uuid.uuid4());a['hypothesis_run_id']=h['run_id']
        library.captions.propose_hypothesis(other,h);library.captions.propose_alignment(other,h['run_id'],a)
        other_accepted=library.captions.accept_initial(other,h['run_id'],alignment_id=a['run_id'])
        assert other!=tid and library.captions.head(other)==1
        proposals=[tuple(row) for row in library.connection.execute('SELECT * FROM caption_proposals')]
    original_bytes=source.read_bytes()
    tracks=copy.deepcopy(c.sessions[0].caption_metadata['tracks'])
    result=action(c,token,view,[view.words[0].id,view.words[5].id])
    assert result.state=='saved_applied' and result.repair.revision==2,result.message
    assert [w.text for w in result.repair.words]==['shot','Good','shott','Shott','shott!','shot']
    assert c.sessions[0].apply_calls==2 and len(c.worker_requests)==1
    assert c.sessions[0].caption_metadata['tracks']==tracks and c.sessions[0].caption_metadata['pin']['revision']==2
    with CreatorLibrary(c.workspace.database) as library:
        saved=library.captions.revision(tid,2);event=library.captions.event(tid,2)
        assert event['operation']=='replace' and event['origin']=='human'
        assert event['target_word_ids']==[view.words[0].id,view.words[5].id]
        assert library.connection.execute('SELECT count(*) FROM caption_events WHERE transcript_id=?',(tid,)).fetchone()[0]==2
        assert library.captions.revision(tid,1)==original
        assert [tuple(row) for row in library.connection.execute('SELECT * FROM caption_proposals')]==proposals
        assert [tuple(row) for row in library.connection.execute('SELECT * FROM kit_versions')]==kits
        assert library.captions.revision(other)==other_accepted
        for i in range(6):
            before=original['words'][i];after=saved['words'][i]
            if i not in (0,5):assert after==before
            else:
                assert after['timing']=={**before['timing'],'status':'inherited'}
                assert after['parent_word_ids']==[before['id']] and after['machine_refs']==before['machine_refs']
                assert after['timing_provenance']['from_word_ids']==[before['id']]
    assert source.read_bytes()==original_bytes


@pytest.mark.parametrize('fault',['foreign','case','punctuation','order','duplicate','same','substring'],ids=['foreign','case','punctuation','order','duplicate','same','substring'])
def test_exact_match_selection_refuses_without_revision_or_native_apply(case,fault):
    c,token,applied=case;view=applied.repair
    ids=[view.words[0].id];source='shott';text='shot'
    if fault=='foreign':ids=[str(uuid.uuid4())]
    elif fault=='case':ids=[view.words[3].id]
    elif fault=='punctuation':ids=[view.words[4].id]
    elif fault=='order':ids=[view.words[5].id,view.words[0].id]
    elif fault=='duplicate':ids*=2
    elif fault=='same':text=source
    else:source='shot'
    denied=action(c,token,view,ids,source,text)
    assert denied.state=='blocked' and c.sessions[0].apply_calls==1
    with CreatorLibrary(c.workspace.database) as library:assert library.captions.head(view.transcript_id)==1


def test_stale_head_and_changed_native_pin_reject_before_new_projection(case):
    c,token,applied=case;view=applied.repair
    with CreatorLibrary(c.workspace.database) as library:
        library.captions.edit(view.transcript_id,expected_revision=1,operation='replace',
            target_word_ids=[view.words[1].id],replacements=[{'text':'Great'}])
    denied=action(c,token,view,[view.words[0].id,view.words[5].id])
    assert denied.state=='blocked' and c.sessions[0].apply_calls==1
    with CreatorLibrary(c.workspace.database) as library:assert library.captions.head(view.transcript_id)==2
    c.sessions[0].caption_metadata['pin']['payload_sha256']='f'*64
    assert action(c,token,view,[view.words[0].id]).state=='blocked'


def test_uncertain_batch_retains_single_revision_barrier_and_check_does_not_retry(case):
    c,token,applied=case;c.sessions[0].lose_apply=True
    result=action(c,token,applied.repair,[applied.repair.words[i].id for i in (0,2,5)])
    assert result.state=='saved_uncertain' and c.workflow.unsettled
    assert c.bg(c.controller.checkpoint).state=='blocked'
    assert action(c,token,result.repair,[result.repair.words[2].id]).state=='blocked'
    checked=c.bg(c.controller.repair_captions,token,'check',result.repair)
    assert checked.state=='saved_applied' and not c.workflow.unsettled
    assert c.sessions[0].apply_calls==2
    with CreatorLibrary(c.workspace.database) as library:assert library.captions.head(result.repair.transcript_id)==2
