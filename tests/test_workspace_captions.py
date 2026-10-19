"""Real catalog/ProjectStore, authored PCM and native/worker doubles; no external tools."""
import copy
import hashlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid
import wave

from native.core.creator_library import CreatorLibrary
from native.services.caption_audio import PreparedAudio,CaptionAudioPreparer
from native.services.caption_worker.client import RunResult,ExecutionUncertain
from native.services.caption_worker.evidence import digest,hash_file
from native.services.workspace_captions import WorkspaceCaptions
from native.services.moment_captions import MomentCaptionSelector
from native.services import workspace_captions as adjunct
from tests import test_managed_close as close
from tests.fixtures import workspace_editor as fixture


class CaptionSession(close.ManagedSession):
    def __init__(self,*args):
        super().__init__(*args)
        self.caption_metadata=None;self.apply_calls=0;self.lose_apply=False
        self.track_count=1;self.missing=False;self.muted=False
        self.monitor_item=None;self.source_seconds=None;self.monitor_actions=[]
        if self.working.path.read_bytes().startswith(b'CAPTION-DOUBLE:'):
            self.caption_metadata=json.loads(self.working.path.read_bytes()[15:])
            self.content=self.working.path.read_bytes()
    def validation(self):
        result=super().validation();row=result['owners'][0]
        row['sequences']={'shared':211,'landscape':455,'portrait':799}
        role=row['units'][0]['roles'][0]
        role.update(audio='inherit',sourceItem=10,resolvedMediaItem=10,nativeAudioClips={'shared':[2511],'landscape':[4951],'portrait':[8391]})
        return result
    def execute(self,command,params):
        if command=='source.open':self.monitor_item=params['item'];return None
        if command=='source.setPlayhead':self.source_seconds=params['seconds'];return {'time':0}
        if command=='source.inspect':return {'item':self.monitor_item,'media':self.monitor_item}
        if command=='metadata.get':
            result=super().execute(command,params)
            if self.caption_metadata is not None:result['metadata']['clipline.acceptedCaptions.v1']=json.dumps(self.caption_metadata)
            return result
        if command=='sequence.inspect':
            seq=params['item']
            return {'id':seq,'audio':[{'enabled':True,'muted':self.muted,'solo':False,'items':[{'clip':{211:2511,455:4951,799:8391}[seq],'enabled':True}]}],'captionTracks':[] if self.missing else [
                {'id':seq+40+i,'label':'C'+str(i+1),'name':'Saved styled captions '+str(i+1),'format':'WebVTT','language':'en',
                 'enabled':True,'locked':False,'style':{'font':'Fixture font','size':36,'color':'#ffffff'}} for i in range(self.track_count)]}
        if command=='clipline.validate':return self.validation()
        if command=='clipline.acceptCaptions':
            self.apply_calls+=1
            projection=params['projectionInput'];accepted=projection['accepted']
            pin={'transcript_id':accepted['transcript_id'],'revision':accepted['revision'],'payload_sha256':digest(projection)}
            self.caption_metadata={'schema':1,'owner':5,'pin':pin,'input':copy.deepcopy(projection),'tracks':copy.deepcopy(params['tracks'])}
            self.content=b'CAPTION-DOUBLE:'+json.dumps(self.caption_metadata,sort_keys=True).encode()
            self.revision+=1
            if self.lose_apply:raise fixture.OutcomeUnknown('fake-apply','engine.execute','Injected lost caption reply')
            return {'owner':5,'accepted':pin,'revision':self.revision,'captions':len(accepted['words'])}
        return super().execute(command,params)
    def call(self,method,params):
        if method=='ui.menu.invoke' and params['id'].startswith('source.playback.'):
            self.monitor_actions.append(params['id']);return None
        return super().call(method,params)


class CaptionWorkflowTests(unittest.TestCase):
    def provenance(self):
        value=fixture.ControllerCase.provenance(self)
        value['requestedSourceRangeUs']={'start':self.moment.source_in_us,'end':self.moment.source_out_us}
        return value
    create=fixture.ControllerCase.create
    open=fixture.ControllerCase.open
    checkpoint=fixture.ControllerCase.checkpoint
    cleanup=fixture.ControllerCase.cleanup
    assert_protected=fixture.ControllerCase.assert_protected
    assert_writable=fixture.ControllerCase.assert_writable

    def setUp(self):
        close.SafeCloseTests.setUp(self)
        self.profile=self.root/'profile.json';self.profile.write_text('{}')
        projection={'rules':{'maxChars':18,'lines':1,'minDurationUs':0,'maxDurationUs':2000000,'gapFrames':0,'breakPauseUs':50000},'defaultSettings':'line:88% align:center'}
        self.runtime=SimpleNamespace(ffmpeg=('fixture-ffmpeg','a'*64),ffprobe=('fixture-probe','b'*64),
            python=('fixture-python','c'*64),worker=('fixture-worker','d'*64),profile=(str(self.profile),hash_file(self.profile)),
            projection=projection,display_name='Local evaluation profile',verify=lambda:None)
        self.workflow=WorkspaceCaptions(self.controller,self.runtime);self.controller._captions=self.workflow
        self.patches=[]
        for obj,name,value in [(self.workflow.preparer,'inspect_source',self.inspect),(self.workflow.preparer,'prepare',self.prepare),
                               (self.workflow.client,'run',self.worker)]:
            p=patch.object(obj,name,side_effect=value);p.start();self.patches.append(p)
        self.prepared_selections=[];self.worker_requests=[]
    def tearDown(self):
        for p in self.patches:p.stop()
        # Test teardown is explicit fixture cleanup, not a product recovery path.
        self.controller._captions=None
        close.SafeCloseTests.tearDown(self)
    def start(self,store,working,editor,**kwargs):
        session=CaptionSession(self,working,kwargs['managed_exit_nonce']);self.sessions.append(session)
        if self.start_hook:self.start_hook(session)
        return session
    def bg(self,fn,*args,**kwargs):return self.pool.submit(fn,*args,**kwargs).result(15)
    def inspect(self,*args,**kwargs):
        return {'streams':[{'stream_index':7,'channels':[0,1],'sample_rate':48000,'duration_us':4000000}]}
    def prepare(self,selection,root,**kwargs):
        self.prepared_selections.append(copy.deepcopy(selection))
        directory=Path(root)/str(uuid.uuid4());directory.mkdir(parents=True)
        wavpath=directory/'prepared.wav';count=(selection['end_us']-selection['start_us'])*16000//1000000;pcm=b'\0\0'*count
        with wave.open(str(wavpath),'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(pcm)
        identity={'source':{'asset_id':selection['asset_id'],'sha256':selection['source_sha256'],'path':selection['source_path'],'audio_stream_index':selection['stream_index'],'channels':selection['channels'],'mix':selection['mix']},
            'audio':{'path':str(wavpath),'wav_sha256':hash_file(wavpath),'pcm_sha256':hashlib.sha256(pcm).hexdigest(),'sample_rate':16000,'sample_count':count,'channels':1,'sample_width':2,'sample_zero_source_us':selection['start_us'],
                'extraction':{'source_sha256':selection['source_sha256'],'argv':['authored tool double'],'tool_version':'double'}},
            'range':{'start_us':selection['start_us'],'end_us':selection['end_us']}}
        request={'schema_version':1,'run_id':str(uuid.uuid4()),'task':'transcribe','language':'en',**identity,'kit_revision':selection['kit_revision'],'profile_path':selection['profile_path'],'profile_sha256':selection['profile_sha256']}
        (directory/'request.json').write_text(json.dumps(request));(directory/'receipt.json').write_text(json.dumps({'request_sha256':digest(request)}))
        return PreparedAudio('complete',None,directory,directory/'request.json')
    def worker(self,request,root,**kwargs):
        self.worker_requests.append(copy.deepcopy(request));directory=Path(root)/request['run_id'];directory.mkdir(parents=True)
        identity={k:copy.deepcopy(request[k]) for k in ('source','audio','range')}
        words=[{'id':str(uuid.uuid4()),'text':x} for x in ['Good','shott']]
        h={'run_id':request['run_id'],'request_sha256':digest(request),'processing_state':'complete',**identity,'words':words}
        a={'run_id':str(uuid.uuid4()),'request_sha256':digest(request),'hypothesis_run_id':h['run_id'],'target_transcript_revision':None,
            'words':[{'word_id':w['id'],'text':w['text'],'timing':{'start_us':request['range']['start_us']+i*500000,'end_us':request['range']['start_us']+i*500000+200000,'status':'aligned'}} for i,w in enumerate(words)]}
        for name,value in [('hypothesis',h),('alignment',a)]:
            (directory/(name+'.json')).write_text(json.dumps(value))
        receipt={'schema_version':1,'state':'complete','error_code':None,'run_id':request['run_id'],'hashes':{'request':digest(request),'hypothesis':digest(h),'alignment':digest(a)},'process_scope':{'disposition':'stopped','active_processes':0},'streams_closed':True,'worker_exit_code':0}
        (directory/'receipt.json').write_text(json.dumps(receipt))
        return RunResult('complete',None,directory,directory/'hypothesis.json',directory/'alignment.json')
    def context(self):
        if self.controller._session is None:self.assertEqual(self.open().state,'open')
        outcome=self.bg(self.controller.caption_context,self.moment.id,self.moment.revision)
        self.assertNotEqual(outcome.state,'blocked',outcome.message);return outcome
    def generate(self,context=None):
        context=context or self.context()
        tracks=[{'sequence':g['sequence'],'track':g['tracks'][0]['id']} for g in context.track_choices]
        outcome=self.bg(self.controller.generate_captions,context.context_token,{'stream_index':7,'channels':[0,1],'tracks':tracks})
        self.assertEqual(outcome.state,'proposal',outcome.message);return outcome
    def accept(self,proposal):
        p=proposal.proposal
        result=self.bg(self.controller.accept_caption_proposal,proposal.context_token,p['job_id'],p['transcript_id'],p['hypothesis_id'],p['alignment_id'])
        self.assertEqual(result.state,'saved_pending',result.message);return result

    def test_generate_accept_apply_correct_retime_checkpoint_normal_close_and_fresh_reopen(self):
        proposal=self.generate();tid=proposal.proposal['transcript_id']
        with CreatorLibrary(self.workspace.database) as library:self.assertEqual(library.captions.head(tid),0)
        accepted=self.accept(proposal)
        applied=self.bg(self.controller.repair_captions,proposal.context_token,'apply',accepted.repair)
        self.assertEqual(applied.state,'saved_applied',applied.message)
        old=applied.repair.words[1]
        corrected=self.bg(self.controller.repair_captions,proposal.context_token,'replace',applied.repair,word_id=old.id,text='shot')
        self.assertEqual(corrected.state,'saved_applied',corrected.message)
        word=corrected.repair.words[1];self.assertEqual((word.start_us,word.end_us),(old.start_us,old.end_us));self.assertEqual(word.timing_status,'inherited')
        timed=self.bg(self.controller.repair_captions,proposal.context_token,'retime',corrected.repair,word_id=word.id,start_us=1700000,end_us=1900000)
        self.assertEqual(timed.state,'saved_applied',timed.message);self.assertEqual(timed.repair.words[1].id,word.id)
        adapter=self.workflow._adapter
        checkpoint=self.checkpoint();self.assertEqual(checkpoint.state,'open',checkpoint.message)
        reloaded=self.bg(self.controller.repair_captions,proposal.context_token,'reload',transcript_id=tid)
        self.assertEqual(reloaded.state,'saved_applied',reloaded.message);self.assertIsNot(self.workflow._adapter,adapter)
        self.assertEqual(self.workflow._adapter.working,self.controller.status().working)
        self.assertEqual(self.bg(self.controller.save_and_close).state,'closed')
        self.assertEqual(self.sessions[0].stops,0)
        reopened=self.context();self.assertEqual(reopened.repair.revision,timed.repair.revision)
        self.assertEqual(reopened.repair.words[1].text,'shot')
        self.assertEqual(reopened.repair.words[1].start_us,1700000)
        with CreatorLibrary(self.workspace.database) as library:self.assertEqual(library.captions.revision(tid,1)['words'][1]['text'],'shott')

    def test_lost_apply_blocks_checkpoint_close_cleanup_and_unrelated_recovery_cannot_clear(self):
        proposal=self.generate();accepted=self.accept(proposal);session=self.sessions[0];session.lose_apply=True
        result=self.bg(self.controller.repair_captions,proposal.context_token,'apply',accepted.repair)
        self.assertEqual(result.state,'saved_uncertain',result.message);self.assertTrue(self.workflow.unsettled)
        calls=session.apply_calls
        for fn in [self.controller.checkpoint,self.controller.save_and_close,self.controller.cleanup]:
            self.assertEqual(self.bg(fn).state,'blocked');self.assertEqual(session.stops,0)
        self.bg(self.controller.recover_working)
        self.assertTrue(self.workflow._native_uncertain);self.assertFalse(self.controller.status().can_checkpoint)
        session.lose_apply=False
        checked=self.bg(self.controller.repair_captions,proposal.context_token,'check',result.repair)
        self.assertEqual(checked.state,'saved_applied',checked.message);self.assertFalse(self.workflow.unsettled)
        self.assertEqual(session.apply_calls,calls);self.assertTrue(self.controller.status().can_checkpoint)

    def test_rerun_remains_exact_selected_proposal_and_preserves_human_history(self):
        one=self.generate();accepted=self.accept(one)
        applied=self.bg(self.controller.repair_captions,one.context_token,'apply',accepted.repair)
        corrected=self.bg(self.controller.repair_captions,one.context_token,'replace',applied.repair,word_id=applied.repair.words[1].id,text='Human')
        with CreatorLibrary(self.workspace.database) as library:old=library.captions.revision(one.proposal['transcript_id'])
        newer=self.generate()
        self.assertNotEqual(newer.proposal['job_id'],one.proposal['job_id'])
        with CreatorLibrary(self.workspace.database) as library:
            self.assertEqual(library.captions.head(newer.proposal['transcript_id']),0)
            self.assertEqual(library.captions.revision(one.proposal['transcript_id']),old)

    def test_other_job_wrong_context_and_forged_track_never_generate_or_accept(self):
        context=self.context();selection={'stream_index':7,'channels':[0,1],'tracks':[{'sequence':g['sequence'],'track':99999} for g in context.track_choices]}
        outcome=self.bg(self.controller.generate_captions,context.context_token,selection)
        self.assertEqual(outcome.state,'blocked');self.assertEqual(self.worker_requests,[])
        selection['tracks']=[{'sequence':g['sequence'],'track':g['tracks'][0]['id']} for g in context.track_choices]
        self.assertEqual(self.bg(self.controller.generate_captions,'foreign',selection).state,'blocked')
        proposal=self.generate(context);p=proposal.proposal
        result=self.bg(self.controller.accept_caption_proposal,context.context_token,str(uuid.uuid4()),p['transcript_id'],p['hypothesis_id'],p['alignment_id'])
        self.assertEqual(result.state,'blocked')
        with CreatorLibrary(self.workspace.database) as library:self.assertEqual(library.captions.head(p['transcript_id']),0)

    def test_missing_tracks_give_concrete_setup_and_multiple_tracks_require_selection(self):
        self.open();self.sessions[0].missing=True
        result=self.bg(self.controller.caption_context,self.moment.id,1)
        self.assertEqual(result.state,'blocked');self.assertIn('save an enabled, unlocked caption track',result.message)
        self.sessions[0].missing=False;self.sessions[0].track_count=2
        context=self.context();self.assertEqual(context.tracks,());self.assertEqual(len(context.track_choices[0]['tracks']),2)

    def test_unknown_preparer_is_retained_before_jobrunner_and_blocks_editor_lifetime(self):
        context=self.context();actual=self.workflow.preparer
        with patch.object(actual,'prepare',side_effect=ExecutionUncertain('synthetic unknown',actual,self.root)):
            tracks=[{'sequence':g['sequence'],'track':g['tracks'][0]['id']} for g in context.track_choices]
            result=self.bg(self.controller.generate_captions,context.context_token,{'stream_index':7,'channels':[0,1],'tracks':tracks})
        self.assertEqual(result.state,'blocked');self.assertIs(self.workflow._retained,actual)
        self.assertIs(self.controller._session,self.sessions[0]);self.assertFalse(self.controller.status().can_checkpoint)
        self.assertEqual(self.bg(self.controller.cleanup).state,'blocked');self.assertEqual(self.worker_requests,[])

    def test_returned_context_cannot_mutate_private_track_admission(self):
        context=self.context();context.track_choices[0]['tracks'][0]['id']=99999
        selection={'stream_index':7,'channels':[0,1],'tracks':[{'sequence':g['sequence'],'track':g['tracks'][0]['id']} for g in context.track_choices]}
        self.assertEqual(self.bg(self.controller.generate_captions,context.context_token,selection).state,'blocked')
        self.assertEqual(self.prepared_selections,[])

    def test_selection_changes_during_artifact_read_cannot_accept_old_choice(self):
        first=self.generate();second=self.generate(first)
        def select(library,proposal,revision):
            p=proposal.proposal
            return MomentCaptionSelector(library,self.workflow.directory/'artifacts').select(self.moment.id,
                expected_moment_revision=1,expected_selection_revision=revision,
                **{k:p[k] for k in ('job_id','transcript_id','hypothesis_id','alignment_id')})
        with CreatorLibrary(self.workspace.database) as library:select(library,first,2)
        actual=adjunct._evidence;calls=[]
        def evidence(raw,pin):
            result=actual(raw,pin);calls.append(1)
            if len(calls)==3:
                with CreatorLibrary(self.workspace.database) as other:select(other,second,3)
            return result
        with patch.object(adjunct,'_evidence',side_effect=evidence):
            p=first.proposal
            result=self.bg(self.controller.accept_caption_proposal,first.context_token,p['job_id'],p['transcript_id'],p['hypothesis_id'],p['alignment_id'])
        self.assertEqual(result.state,'blocked');self.assertIn('selected proposal changed',result.message)
        with CreatorLibrary(self.workspace.database) as library:
            self.assertEqual(library.captions.head(first.proposal['transcript_id']),0)
            self.assertEqual(library.captions.head(second.proposal['transcript_id']),0)
            self.assertEqual(library.connection.execute('SELECT count(*) FROM caption_events').fetchone()[0],0)

    def test_concurrent_initial_accept_cas_creates_exactly_one_revision_and_event(self):
        first=self.generate();p=first.proposal;actual=adjunct._evidence;calls=[];retained=[]
        def evidence(raw,pin):
            result=actual(raw,pin);calls.append(1)
            if len(calls)==3:
                with CreatorLibrary(self.workspace.database) as other:
                    retained.append(other.captions.accept_initial(p['transcript_id'],p['hypothesis_id'],alignment_id=p['alignment_id']))
            return result
        with patch.object(adjunct,'_evidence',side_effect=evidence):
            result=self.bg(self.controller.accept_caption_proposal,first.context_token,p['job_id'],p['transcript_id'],p['hypothesis_id'],p['alignment_id'])
        self.assertEqual(result.state,'blocked');self.assertIn('already exists',result.message)
        with CreatorLibrary(self.workspace.database) as library:
            self.assertEqual(library.captions.revision(p['transcript_id']),retained[0])
            self.assertEqual(library.connection.execute('SELECT count(*) FROM caption_events').fetchone()[0],1)

    def test_exact_job_dispatch_leaves_older_queued_caption_job_untouched(self):
        context=self.context()
        with CreatorLibrary(self.workspace.database) as library:
            older=library.jobs.submit('caption',{'request_ref':{'id':str(uuid.uuid4()),'sha256':'f'*64},
                'assets':[{'asset_id':self.asset.id,'sha256':self.asset.sha256}],
                'profile_sha256':self.runtime.profile[1],'kit':{'kit_id':self.kit_id,'version':1},
                'range':{'start_us':1000000,'end_us':3000000}},idempotency_key=str(uuid.uuid4()))
        proposal=self.generate(context)
        with CreatorLibrary(self.workspace.database) as library:
            self.assertEqual(library.jobs.job(older['id'])['state'],'queued')
            self.assertEqual(library.jobs.job(proposal.proposal['job_id'])['state'],'completed')
        self.assertEqual(len(self.worker_requests),1)

    def test_check_editor_clears_only_caption_cause_and_never_other_reconciliation(self):
        first=self.generate();accepted=self.accept(first);self.sessions[0].lose_apply=True
        unknown=self.bg(self.controller.repair_captions,first.context_token,'apply',accepted.repair)
        self.controller._blocked_reason='Separate publication needs reconciliation'
        self.controller._view_update('reconciliation_required','Separate publication needs reconciliation')
        checked=self.bg(self.controller.repair_captions,first.context_token,'check',unknown.repair)
        self.assertEqual(checked.state,'saved_applied',checked.message)
        self.assertFalse(self.workflow._native_uncertain)
        self.assertEqual(self.controller._blocked_reason,'Separate publication needs reconciliation')
        self.assertFalse(self.controller.status().can_checkpoint)

    def test_cancellation_before_submission_does_not_create_caption_job(self):
        context=self.context();cancel=threading.Event();cancel.set()
        selection={'stream_index':7,'channels':[0,1],'tracks':[{'sequence':g['sequence'],'track':g['tracks'][0]['id']} for g in context.track_choices]}
        outcome=self.bg(self.controller.generate_captions,context.context_token,selection,cancel=cancel)
        self.assertEqual(outcome.state,'cancelled');self.assertEqual(self.worker_requests,[])
        with CreatorLibrary(self.workspace.database) as library:
            self.assertEqual(library.connection.execute('SELECT count(*) FROM durable_job_intents').fetchone()[0],0)

    def test_native_source_monitor_uses_resolved_media_and_native_seconds_without_a_player(self):
        proposal=self.generate();accepted=self.accept(proposal);session=self.sessions[0]
        denied=self.bg(self.controller.caption_source_monitor,proposal.context_token,'play')
        self.assertEqual(denied.state,'blocked');self.assertEqual(session.monitor_actions,[])
        shown=self.bg(self.controller.caption_source_monitor,proposal.context_token,'show',word_id=accepted.repair.words[1].id)
        self.assertNotEqual(shown.state,'blocked',shown.message)
        self.assertEqual(session.monitor_item,10);self.assertEqual(session.source_seconds,1.5)
        self.bg(self.controller.caption_source_monitor,proposal.context_token,'play')
        self.bg(self.controller.caption_source_monitor,proposal.context_token,'stop')
        self.assertEqual(session.monitor_actions,['source.playback.play','source.playback.stop'])
        self.assertEqual(session.apply_calls,0)

    def test_canonical_moment_change_blocks_generation_before_preparation(self):
        context=self.context()
        with CreatorLibrary(self.workspace.database) as library:library.set_overrides(self.moment.id,{},expected_revision=1)
        selection={'stream_index':7,'channels':[0,1],'tracks':[{'sequence':g['sequence'],'track':g['tracks'][0]['id']} for g in context.track_choices]}
        result=self.bg(self.controller.generate_captions,context.context_token,selection)
        self.assertEqual(result.state,'blocked');self.assertEqual(self.prepared_selections,[])

    def test_muted_native_gameplay_gives_setup_requirement_before_audio_probe(self):
        self.open();self.sessions[0].muted=True
        with patch.object(self.workflow.preparer,'inspect_source') as probe:
            outcome=self.bg(self.controller.caption_context,self.moment.id,1)
        self.assertEqual(outcome.state,'blocked');self.assertIn('Enable inherited gameplay audio',outcome.message)
        probe.assert_not_called()

    def test_non_grid_moment_remains_exact_while_preparer_widens_analysis(self):
        from native.core.project_store import ProjectStore
        with CreatorLibrary(self.workspace.database) as library:
            self.moment=library.create_moment(title='Non-grid moment',source_asset_id=self.asset.id,
                source_in_us=1000001,source_out_us=2999999,kit_id=self.kit_id,kit_version=1)
        self.store=ProjectStore(self.workspace.directory/'moments'/self.moment.id)
        self.seed=self.store.publish(b'OPAQUE NON-GRID FIXTURE',expected_generation=None,revision=1,engine='filmcraft',engine_version='fixture',suffix='.fcproj')
        context=self.context();self.assertEqual(context.source_range,(1000001,2999999));self.assertEqual(context.analysis_range,(1000000,3000000))
        self.generate(context)
        with CreatorLibrary(self.workspace.database) as library:
            moment=library.moment(self.moment.id)
            self.assertEqual((moment.source_in_us,moment.source_out_us),(1000001,2999999))
        self.assertEqual((self.prepared_selections[0]['start_us'],self.prepared_selections[0]['end_us']),(1000000,3000000))

    def test_private_acceptance_body_requires_transaction_and_keeps_public_owner_cas(self):
        from native.core.accepted_transcript import TranscriptError
        proposal=self.generate();p=proposal.proposal
        with CreatorLibrary(self.workspace.database) as library:
            with self.assertRaises(TranscriptError):library.captions._accept_initial(p['transcript_id'],p['hypothesis_id'])
            self.assertEqual(library.captions.head(p['transcript_id']),0)
            self.assertEqual(library.connection.execute('SELECT count(*) FROM caption_events').fetchone()[0],0)


def test_preparer_owns_grid_coverage_without_rounding_canonical_moment():
    assert CaptionAudioPreparer.cover_range(1000001,2999999,4000000)==(1000000,3000000)
    from native.services.caption_audio import PreparationError
    with unittest.TestCase().assertRaises(PreparationError):CaptionAudioPreparer.cover_range(0,3000001,3000001)


def test_inspection_uses_same_preparer_tool_with_real_global_index(tmp_path,monkeypatch):
    source=tmp_path/'source.bin';source.write_bytes(b'authored source');probe=tmp_path/'probe.bin';probe.write_bytes(b'authored binary')
    preparer=CaptionAudioPreparer(probe,probe,python=probe);calls=[]
    def tool(argv,label,**kwargs):
        calls.append((argv,label));assert preparer._source_guard is not None
        return json.dumps({'streams':[{'index':0,'codec_type':'video'},{'index':7,'codec_type':'audio','sample_rate':'48000','channels':2,'duration':'4','start_time':'0'}]}).encode()
    monkeypatch.setattr(preparer,'_tool',tool)
    result=preparer.inspect_source(source,hash_file(source),tmp_path/'inspection')
    assert result['streams'][0]['stream_index']==7 and result['streams'][0]['channels']==[0,1]
    assert result['decoded_clock_verified'] is False and not preparer.has_unsettled
    assert len(calls)==1 and calls[0][1]=='inspect' and '-show_streams' in calls[0][0]


def test_inspection_ignores_access_time_changed_by_reading(tmp_path,monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    source=tmp_path/'source.bin';source.write_bytes(b'authored source')
    probe=tmp_path/'probe.bin';probe.write_bytes(b'authored binary')
    expected=hash_file(source)
    preparer=CaptionAudioPreparer(probe,probe,python=probe)
    original=Path.stat;calls={source:0,probe:0}
    def stat(path,*args,**kwargs):
        value=original(path,*args,**kwargs)
        if path not in calls:return value
        calls[path]+=1
        fields={key:getattr(value,key) for key in dir(value) if key.startswith('st_')}
        fields['st_atime']+=calls[path]
        fields['st_atime_ns']+=calls[path]*1000000000
        return SimpleNamespace(**fields)
    monkeypatch.setattr(Path,'stat',stat)
    monkeypatch.setattr(preparer,'_tool',lambda *args,**kw:json.dumps({'streams':[{'index':7,'codec_type':'audio','sample_rate':'48000','channels':2,'duration':'4'}]}).encode())
    result=preparer.inspect_source(source,expected,tmp_path/'inspection')
    assert result['streams'][0]['stream_index']==7
    assert not preparer.has_unsettled


def test_inspection_diagnostic_failure_retains_original_source_guard_until_observation(tmp_path,monkeypatch):
    import native.services.caption_audio as audio
    source=tmp_path/'source.bin';source.write_bytes(b'source');probe=tmp_path/'probe.bin';probe.write_bytes(b'probe')
    preparer=CaptionAudioPreparer(probe,probe,python=probe)
    monkeypatch.setattr(preparer,'_tool',lambda *args,**kw:json.dumps({'streams':[{'index':7,'codec_type':'audio','sample_rate':'48000','channels':2,'duration':'4'}]}).encode())
    original=audio.write_once
    def write(path,value):
        if path.name=='inspection.json':raise OSError('authored diagnostic failure')
        return original(path,value)
    with monkeypatch.context() as p:
        p.setattr(audio,'write_once',write)
        with unittest.TestCase().assertRaises(ExecutionUncertain) as caught:
            preparer.inspect_source(source,hash_file(source),tmp_path/'inspection')
    assert caught.exception.owner is preparer and preparer._source_guard is not None
    with unittest.TestCase().assertRaises(OSError):source.write_bytes(b'changed')
    result=preparer.observe_execution();assert result['ceased'] and not preparer.has_unsettled
    source.write_bytes(b'changed')
