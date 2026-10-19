from dataclasses import asdict,replace
import gc
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
import uuid

import pytest
from native.core.creator_library import CreatorLibrary
from native.core.twitch_inbox import TwitchClip
from native.services.native_export_client import QualifiedProgram, OwnedProcess
from native.services.caption_worker.evidence import canonical,hash_file
from native.services.durable_native_exports import ExportOwnershipError
from native.services.twitch_publication import PublicationGuardError,VerifiedTwitchPublication
from native.services.twitch_intake_client import TwitchIntakeClient,IntakeTools,classify_probe,PROFILE,IntakeError
from native.services.durable_twitch_intake import DurableTwitchIntake
PRODUCT = Path(__file__).resolve().parents[1]

ORIGINAL_TRANSFER=TwitchIntakeClient.transfer

pytestmark=pytest.mark.skipif(os.name!='nt',reason='Real shared Windows process/read-guard boundary required')

def uid(): return str(uuid.uuid4())
def clip(name='Clip-One'):
    return TwitchClip(name,'42','43','','Selected clip','2026-10-18T12:00:00.000000001Z',2_000_000,5,
                      f'https://clips.twitch.tv/{name}','vod',123_000_000)
def facts():
    return {'streams':[{'index':0,'codec_type':'video','codec_name':'h264','pix_fmt':'yuv420p','width':1920,'height':1080,
            'avg_frame_rate':'60/1','start_time':'0.000000','sample_aspect_ratio':'1:1','time_base':'1/15360'}],
            'format':{'format_name':'mov,mp4,m4a,3gp,3g2,mj2','duration':'2.000000'}}


def background(action):
    values=[]; errors=[]
    def run():
        try: values.append(action())
        except BaseException as error: errors.append(error)
    thread=threading.Thread(target=run); thread.start(); thread.join(15)
    assert not thread.is_alive(),'bounded private fixture thread did not finish'
    if errors: raise errors[0]
    return values[0] if values else None


@pytest.fixture
def case(tmp_path,monkeypatch):
    if os.name != 'nt': pytest.skip('Windows owned process/read-guard qualification')
    code=tmp_path/'code'
    for source in (PRODUCT,):
        for path in (source/'native').rglob('*'):
            if path.is_file() and path.suffix in ('.py','.sql'):
                target=code/path.relative_to(source); target.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(path,target)
    worker=code/'native/services/twitch_transfer_worker.py'
    shutil.copyfile(PRODUCT/'native/services/twitch_transfer_worker.py',worker)
    fixture=code/'native/fixture.py'
    fixture.write_text('import json,pathlib,sys,time\nr=json.loads(sys.stdin.buffer.readline())\n'
        'if r["kind"]=="transfer":\n pathlib.Path(r["target"]).write_bytes(b"owned authored bytes")\n'
        ' if r.get("block"): time.sleep(20)\n'
        ' print(json.dumps({"event":"result","state":"completed","bytes":20}),flush=True)\n'
        'else: print(json.dumps(r["facts"]),flush=True)\n',encoding='utf8')
    gate=tmp_path/'gate.py'; shutil.copyfile(PRODUCT/'native/services/caption_audio_gate.py',gate)
    probe=tmp_path/'ffprobe-placeholder.exe'; probe.write_bytes(b'never executed; actual probe boundary is locally doubled')
    qualified=lambda p:QualifiedProgram(p,hash_file(p))
    closure={str(p.relative_to(code)).replace('\\','/'):hash_file(p) for p in code.rglob('*') if p.is_file() and p.suffix in ('.py','.sql')}
    client=TwitchIntakeClient(IntakeTools(qualified(Path(sys.executable)),qualified(worker),qualified(gate),qualified(probe),code,closure,'client'))
    value={'root':tmp_path,'db':tmp_path/'library.sqlite','service':tmp_path/'intake','client':client,'fixture':fixture,
           'block':False,'probe_bad':False,'counts':{'transfer':0,'probe':0},'ready':threading.Event()}
    def transfer(self,selected,target,directory,execution,cancel,deadline):
        value['counts']['transfer']+=1; value['ready'].set()
        wire={'kind':'transfer','target':str(target),'block':value['block']}
        status,event,_,_=self._owned([sys.executable,'-I','-B',str(fixture)],canonical(wire)+b'\n',directory,execution,cancel,deadline,event=True)
        assert status==0 and event['state']=='completed'
    def probe_method(self,target,directory,execution,cancel,deadline):
        value['counts']['probe']+=1
        body=facts()
        if value['probe_bad']: body['streams'][0]['codec_name']='hevc'
        wire={'kind':'probe','facts':body}
        status,_,out,err=self._owned([sys.executable,'-I','-B',str(fixture)],canonical(wire)+b'\n',directory,execution,cancel,deadline)
        assert status==0 and not err.stat().st_size
        return classify_probe(json.loads(out.read_bytes()))
    monkeypatch.setattr(TwitchIntakeClient,'transfer',transfer)
    monkeypatch.setattr(TwitchIntakeClient,'probe',probe_method)
    with CreatorLibrary(value['db']) as lib: lib.record_twitch_clips([clip()])
    return value


def submit(c,operation=None,clip_id='Clip-One'):
    def work():
        with CreatorLibrary(c['db']) as lib:
            return DurableTwitchIntake(lib,c['service'],c['client']).submit(clip_id,idempotency_key=operation or uid())
    return background(work)


def run(c,**kwargs):
    def work():
        with CreatorLibrary(c['db']) as lib:
            return DurableTwitchIntake(lib,c['service'],c['client']).run_next(**kwargs)
    return background(work)


def test_completed_atomic_binding_uses_two_real_owned_scopes_and_reuses_without_auth(case):
    c=case; op=uid(); first=submit(c,op); assert submit(c,op).job==first.job
    outcome=run(c)
    assert outcome.job['state']=='completed' and outcome.execution.closed
    assert len(outcome.execution.proof['scopes'])==2
    assert all(s['active_processes']==0 for s in outcome.execution.proof['scopes'])
    assert Path(outcome.asset.path).read_bytes()==b'owned authored bytes'
    assert outcome.binding['selected_metadata']['vod_offset_us']==123_000_000
    assert outcome.binding['selected_metadata']['duration_us']==2_000_000
    second=submit(c); assert second.reused and second.asset==outcome.asset and second.job is None
    assert c['counts']=={'transfer':1,'probe':1}
    with CreatorLibrary(c['db']) as lib: assert lib.jobs.resources()==[]


def test_two_clips_same_bytes_dedup_without_relocation(case):
    c=case; submit(c); first=run(c)
    with CreatorLibrary(c['db']) as lib: lib.record_twitch_clips([clip('Clip-Two')])
    submit(c,clip_id='Clip-Two'); second=run(c)
    assert first.asset==second.asset
    assert Path(first.asset.path).read_bytes()==b'owned authored bytes'
    assert second.binding['clip_id']=='Clip-Two'


def test_probe_failure_quarantines_no_asset(case):
    c=case; c['probe_bad']=True; job=submit(c).job; outcome=run(c)
    assert outcome.job['state']=='failed' and outcome.error_code=='UNSUPPORTED_SOURCE'
    with CreatorLibrary(c['db']) as lib:
        assert lib.list_assets()['items']==[] and lib.twitch_source('Clip-One') is None and lib.jobs.resources()==[]
    assert (c['service']/'executions'/job['output_set_id']/'download.part.quarantined').is_file()


def test_existing_managed_directory_collision_never_overwrites(case):
    c=case; job=submit(c).job
    folder=c['service']/'media'/job['output_set_id']; folder.mkdir(parents=True)
    foreign=folder/'source.mp4'; foreign.write_bytes(b'preexisting unrelated bytes')
    outcome=run(c)
    assert outcome.job['state']=='failed' and foreign.read_bytes()==b'preexisting unrelated bytes'
    with CreatorLibrary(c['db']) as lib: assert not lib.list_assets()['items']


def test_sql_publication_failure_quarantines_new_owned_media_and_keeps_no_asset(case,monkeypatch):
    from native.core import creator_library as library_module
    c=case; job=submit(c).job; original=library_module.CreatorLibrary._commit_twitch_source
    def fail(self,*args): original(self,*args); raise RuntimeError('authored failure after binding')
    monkeypatch.setattr(library_module.CreatorLibrary,'_commit_twitch_source',fail)
    outcome=run(c)
    assert outcome.job['state']=='failed'
    assert (c['service']/'media'/job['output_set_id']/'source.mp4.quarantined').is_file()
    with CreatorLibrary(c['db']) as lib:
        assert lib.twitch_source('Clip-One') is None and not lib.list_assets()['items'] and not lib.jobs.resources()


def test_deadline_stops_one_owned_worker_without_probe_or_retry(case):
    c=case; c['block']=True; submit(c); result=run(c,timeout=.2)
    assert result.job['state']=='failed' and result.execution.closed
    assert c['counts']=={'transfer':1,'probe':0}


def test_cancellation_stops_owned_worker_quarantines_and_never_publishes(case):
    c=case; c['block']=True; job=submit(c).job; errors=[]; values=[]
    def action():
        try: values.append(run(c,timeout=5))
        except BaseException as error: errors.append(error)
    thread=threading.Thread(target=action); thread.start(); assert c['ready'].wait(3)
    deadline=time.monotonic()+3
    while not (c['service']/'executions'/job['output_set_id']/'download.part').exists():
        assert time.monotonic()<deadline
        time.sleep(.01)
    with CreatorLibrary(c['db']) as lib:
        current=lib.jobs.job(job['id']); lib.jobs.request_cancel(job['id'],expected_revision=current['revision'])
    thread.join(10); assert not thread.is_alive() and not errors
    assert values[0].job['state']=='cancelled' and values[0].execution.closed
    with CreatorLibrary(c['db']) as lib: assert not lib.list_assets()['items'] and not lib.jobs.resources()


def test_unknown_scope_returns_actual_owner_and_retains_holds_until_explicit_cleanup(case,monkeypatch):
    from native.services.caption_worker.process_tree import ProcessTree
    c=case; c['block']=True; job=submit(c).job; original=ProcessTree.stop
    monkeypatch.setattr(ProcessTree,'stop',lambda self:{'disposition':'unknown','active_processes':None})
    error=None
    try:
        with pytest.raises(ExportOwnershipError) as caught: run(c,timeout=.2)
        error=caught.value; gc.collect(); execution=error.executions[0]
        assert not execution.closed and execution.processes[0].tree.job is not None
        with CreatorLibrary(c['db']) as lib:
            assert lib.jobs.job(job['id'])['state']=='interrupted' and lib.jobs.resources()
            assert lib.twitch_source('Clip-One') is None
        assert c['counts']=={'transfer':1,'probe':0}
    finally:
        monkeypatch.setattr(ProcessTree,'stop',original)
        if error:
            def cleanup():
                with CreatorLibrary(c['db']) as lib:
                    return DurableTwitchIntake(lib,c['service'],c['client']).cleanup_pending(job['id'],execution=error.executions[0])
            assert background(cleanup)['closed']
    with CreatorLibrary(c['db']) as lib: assert lib.jobs.resources(),'explicit cleanup does not fabricate settlement'


def test_publication_close_failure_after_commit_retains_guard_and_admitted_source(case,monkeypatch):
    c=case; job=submit(c).job; original=VerifiedTwitchPublication.close
    class FailClose:
        def CloseHandle(self,handle): return False
    held=[]
    def fail(self):
        saved=list(self.guards); held.append((self,saved))
        self.guards=[(FailClose(),guard[1]) for guard in saved]
        return original(self)
    monkeypatch.setattr(VerifiedTwitchPublication,'close',fail)
    try:
        with pytest.raises(PublicationGuardError) as caught: run(c)
        pub=caught.value.publication
        with CreatorLibrary(c['db']) as lib:
            assert lib.jobs.job(job['id'])['state']=='completed'
            asset=lib.asset(lib.twitch_source('Clip-One')['asset_id'])
        assert Path(asset.path).is_file()
        with pytest.raises(PermissionError): Path(asset.path).write_bytes(b'changed')
    finally:
        monkeypatch.setattr(VerifiedTwitchPublication,'close',original)
        for pub,saved in held: pub.guards=saved; original(pub)


def test_actual_worker_with_child_local_provider_http_doubles_has_no_secret_egress(case,monkeypatch):
    c=case; root=c['client'].tools.code_root
    (root/'native/core').mkdir(exist_ok=True)
    shutil.copyfile(PRODUCT/'native/core/twitch_inbox.py',root/'native/core/twitch_inbox.py')
    child_code='''import io,urllib.request,sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from native.services import twitch_auth,twitch_api,twitch_transfer_worker
def validated_intake_connection(*,expected_broadcaster_id,bound_client_id):
 assert expected_broadcaster_id=="42" and bound_client_id=="client"
 return object()
class Headers(dict):
 def get_all(self,key,default): return ['20'] if key=='Content-Length' else default
class Response:
 status=200
 headers=Headers()
 def __init__(self): self.data=io.BytesIO(b'owned authored bytes')
 def __enter__(self): return self
 def __exit__(self,*args): pass
 def read(self,n): return self.data.read(n)
class Opener:
 def open(self,request,timeout):
  assert request.full_url.endswith('signature=authored-private-signed')
  assert not request.has_header('Authorization')
  return Response()
def own_clip_download(selected,*,connection):
 assert selected.id=='Clip-One'
 urllib.request.build_opener=lambda *args:Opener()
 return SimpleNamespace(landscape_url='https://cdn.example.test/source?signature=authored-private-signed')
twitch_auth.validated_intake_connection=validated_intake_connection
twitch_api.own_clip_download=own_clip_download
raise SystemExit(twitch_transfer_worker.main())
'''
    # Keep caller/worker source pins identical; inject provider doubles only in
    # the child. Other collected tests can already have loaded the auth owner.
    wrapper=root/'native/fixture_transfer.py'
    wrapper.write_text(child_code,encoding='utf8')
    closure={str(p.relative_to(root)).replace('\\','/'):hash_file(p) for p in root.rglob('*') if p.is_file() and p.suffix in ('.py','.sql')}
    c['client']=TwitchIntakeClient(replace(c['client'].tools,code=closure))
    owned=TwitchIntakeClient._owned
    def child_with_provider_doubles(self,argv,*args,**kwargs):
        if argv[-1]==str(self.tools.worker.path):
            argv=[*argv[:-1],str(wrapper)]
        return owned(self,argv,*args,**kwargs)
    monkeypatch.setattr(TwitchIntakeClient,'_owned',child_with_provider_doubles)
    # Use the production transfer dispatch, not the per-case generic child double.
    monkeypatch.setattr(TwitchIntakeClient,'transfer',ORIGINAL_TRANSFER)
    submit(c); result=run(c)
    assert result.job['state']=='completed' and result.asset.sha256==hash_file(result.asset.path)
    transfer_log=c['service']/'executions'/result.job['output_set_id']/'transfer'/'stdout.log'
    assert json.loads(transfer_log.read_bytes())=={'event':'result','state':'completed','bytes':20}
    for path in c['service'].rglob('*'):
        if path.is_file() and path.suffix in ('.json','.log'):
            assert b'authored-private-signed' not in path.read_bytes()


def test_real_production_worker_waits_for_gate_and_invalid_request_never_reads_auth(case):
    from native.services.native_export_client import ExportExecution
    c=case; client=c['client']; execution=ExportExecution({'fixture':'real worker gate'})
    client.admit(execution)
    child=OwnedProcess([sys.executable,'-I','-B',str(client.tools.worker.path)],c['root']/'gate-wait',threading.Event(),time.monotonic()+5,
                       events=True,execution=execution,environment={})
    try:
        assert child.process.poll() is None
        child.send(b'{}\n'); child.process.stdin.close()
        assert child.event('result')=={'event':'result','state':'failed','error_code':'INVALID_REQUEST'}
        assert child.wait()==1
    finally: assert execution.stop()['closed']
    assert not (c['root']/'download.part').exists()


def test_hash_failure_keeps_no_asset_and_quarantines_owned_bytes(case,monkeypatch):
    from native.services import durable_twitch_intake as owner_module
    c=case; job=submit(c).job; original=owner_module.hash_file
    monkeypatch.setattr(owner_module,'hash_file',lambda path:'b'*64 if Path(path).name=='download.part' else original(path))
    outcome=run(c)
    assert outcome.job['state']=='failed' and c['counts']['probe']==0
    assert (c['service']/'executions'/job['output_set_id']/'download.part.quarantined').is_file()
    with CreatorLibrary(c['db']) as lib: assert not lib.list_assets()['items'] and not lib.jobs.resources()


def test_changed_qualified_code_prevents_any_child_before_launch(case):
    c=case; job=submit(c).job; c['fixture'].write_bytes(b'changed after source qualification')
    outcome=run(c)
    assert outcome.job['state']=='failed' and c['counts']=={'transfer':0,'probe':0}
    assert not outcome.execution.processes and outcome.execution.closed


def test_mutated_result_reference_rejects_publication_and_settles_failure(case,monkeypatch):
    c=case; submit(c); original=DurableTwitchIntake._write
    def mutate(self,kind,key,value):
        reference=original(self,kind,key,value)
        if kind=='results': (self.directory/kind/(key+'.json')).write_bytes(b'{}')
        return reference
    monkeypatch.setattr(DurableTwitchIntake,'_write',mutate)
    outcome=run(c)
    assert outcome.job['state']=='failed'
    with CreatorLibrary(c['db']) as lib: assert lib.twitch_source('Clip-One') is None and not lib.list_assets()['items']


def test_committed_result_read_failure_never_quarantines_admitted_asset(case,monkeypatch):
    c=case; job=submit(c).job; original=CreatorLibrary.twitch_source
    def fail(self,clip_id):
        row=self.connection.execute("SELECT state FROM durable_job_heads WHERE job_id=?",(job['id'],)).fetchone()
        if row and row[0]=='completed': raise OSError('authored post-commit read failure')
        return original(self,clip_id)
    monkeypatch.setattr(CreatorLibrary,'twitch_source',fail)
    with pytest.raises(IntakeError,match='COMMITTED_RESULT_READ_FAILED'): run(c)
    monkeypatch.setattr(CreatorLibrary,'twitch_source',original)
    with CreatorLibrary(c['db']) as lib:
        assert lib.jobs.job(job['id'])['state']=='completed'
        asset=lib.asset(lib.twitch_source('Clip-One')['asset_id'])
        assert Path(asset.path).is_file() and hash_file(asset.path)==asset.sha256
        assert not Path(asset.path+'.quarantined').exists()


def test_unknown_bookkeeping_exception_keeps_actual_owner_before_string_conversion(case,monkeypatch):
    from native.services.caption_worker.process_tree import ProcessTree
    c=case; c['block']=True; job=submit(c).job; stop=ProcessTree.stop
    monkeypatch.setattr(ProcessTree,'stop',lambda self:{'disposition':'unknown','active_processes':None})
    def fail(self,job): raise RuntimeError('authored-private-diagnostic-sentinel')
    monkeypatch.setattr(DurableTwitchIntake,'_interrupt',fail)
    error=None
    try:
        with pytest.raises(ExportOwnershipError) as caught: run(c,timeout=.2)
        error=caught.value
        assert error.executions and not error.executions[0].closed
        assert 'authored-private-diagnostic-sentinel' not in str(error)
        with CreatorLibrary(c['db']) as lib: assert lib.jobs.resources() and not lib.list_assets()['items']
    finally:
        monkeypatch.setattr(ProcessTree,'stop',stop)
        if error: assert error.executions[0].stop()['closed']


@pytest.mark.parametrize('field,value', [('avg_frame_rate','NaN'),('avg_frame_rate','0/0'),
    ('time_base','0/1'),('pix_fmt','yuv420p10le'),('width',True),('start_time','1'),('index',True)])
def test_probe_source_envelope_rejects_unsupported_typed_facts(field,value):
    body=facts(); body['streams'][0][field]=value
    with pytest.raises(IntakeError,match='UNSUPPORTED_SOURCE'): classify_probe(body)


def test_probe_preserves_global_audio_index_and_optional_silence():
    body=facts(); assert classify_probe(body)['audio'] is None
    body['streams'].append({'index':1,'codec_type':'audio','codec_name':'aac','channels':2,'channel_layout':'stereo',
                            'sample_rate':'48000','start_time':'0','time_base':'1/48000'})
    result=classify_probe(body)
    assert result['audio']['index']==1 and result['video']['index']==0 and result['native_compatibility'] is False
