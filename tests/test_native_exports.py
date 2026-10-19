"""Real SQLite/admission tests with a replaced native-process boundary.

Authored media/probe/terminal fixtures below are not render or decode evidence.
"""
import copy
import json
from pathlib import Path
import sys
import threading
import uuid

import pytest

from native.core.creator_library import CreatorLibrary
from native.core.durable_jobs import VerificationRequired
from native.core.project_store import ProjectStore
from native.services.caption_worker.evidence import canonical,digest,hash_file
from native.services.caption_worker.client import write_once
from native.services.native_export_client import NativeExportClient,QualifiedProgram,ExportRuntimeError,ExportOutcome,verify_probe,finite_json
from native.services.durable_native_exports import DurableNativeExports,ExportJobOutcome,ExportJobError,RECIPE


def uid():return str(uuid.uuid4())

@pytest.fixture
def case(tmp_path):
    program=tmp_path/'qualified-fixture.exe';program.write_bytes(b'not an executable; external boundary is replaced')
    gate=tmp_path/'gate.py';gate.write_text('# fixture only',encoding='utf8')
    qualified=QualifiedProgram(program,hash_file(program))
    client=NativeExportClient(qualified,qualified,qualified,qualified,gate,engine_version='0.9.0-test-fixture',source_pin_sha256='a'*64)
    database=tmp_path/'library.sqlite'
    with CreatorLibrary(database) as library:
        media=tmp_path/'source.mp4';media.write_bytes(b'authored source bytes')
        template=tmp_path/'template.fcproj';template.write_bytes(b'authored template bytes')
        source=library.register_asset(media);asset=library.register_asset(template)
        descriptor={'name':'Fixture Kit','asset_refs':{'native_template':asset.id},'properties':{}}
        kit=library.create_kit(descriptor)
        moment=library.create_moment(title='Fixture',source_asset_id=source.id,source_in_us=0,source_out_us=1000000,kit_id=kit,kit_version=1)
        snapshot=ProjectStore(tmp_path/'store').publish(b'opaque fixture native snapshot',expected_generation=None,revision=0,engine='filmcraft',engine_version='0.9.0',suffix='.fcproj')
    return {'root':tmp_path,'database':database,'client':client,'snapshot':snapshot,'moment':moment.id,'source':source,'template':asset,'kit':kit,'descriptor':descriptor}

def owner(case,library):return DurableNativeExports(library,case['root']/'jobs',case['client'])
def submit(case,runner,key=None,**changes):
    args={'project_id':case['moment'],'kit_pin':{'kit_id':case['kit'],'version':1},'accepted_pin':None,'owners':[{'owner':5,'revision':0}],'compositions':['landscape','portrait'],'idempotency_key':key or uid()}
    args.update(changes)
    return runner.submit(case['snapshot'],**args)

def background(case,*,timeout=10,cancel=None,retain_owner=False):
    values=[];errors=[]
    def run():
        try:
            with CreatorLibrary(case['database']) as library:
                values.append(owner(case,library).run_next(timeout=timeout,cancel=cancel))
        except BaseException as exc:errors.append(exc)
    thread=threading.Thread(target=run);thread.start();thread.join(10)
    assert not thread.is_alive(),'fixture owner did not stop'
    if errors:raise errors[0]
    if values[0] is None:return None
    assert type(values[0]) is ExportJobOutcome
    if retain_owner:return values[0]
    return values[0].job

def facts():return {'width':64,'height':36,'frameRate':{'num':60,'den':1},'frames':3,'audio':{'sampleRate':48000,'channels':2},'range':{'startTicks':0,'durationTicks':12700800000}}
def probe(f):return {'streams':[{'codec_type':'video','codec_name':'h264','width':f['width'],'height':f['height'],'sample_aspect_ratio':'1:1','r_frame_rate':'60/1','avg_frame_rate':'60/1','nb_read_frames':'3','nb_frames':'3','start_pts':0,'time_base':'1/60','duration_ts':3}, {'codec_type':'audio','codec_name':'aac','sample_rate':'48000','channels':2,'start_pts':0,'time_base':'1/48000','duration_ts':3072}]}

class FixtureStoppedProcess:
    """Authored replacement of the external process boundary, not OS evidence."""
    closed=True
    proof={'disposition':'stopped','active_processes':0,'streams_closed':True}
    def close(self):return dict(self.proof)

def fixture_runtime(self,envelope,directory,*,cancel,timeout,verify_native,execution):
    """Only the slow process boundary is replaced; durable writes stay real."""
    directory.mkdir(parents=True);(directory/'media').mkdir()
    capture={'snapshotSHA256':envelope['snapshot']['sha256'],'outputs':[]}
    native={'state':'completed','capture':capture,'outputs':[]};outputs=[]
    for mode in envelope['compositions']:
        f=facts();f['width'],f['height']=(64,36) if mode=='landscape' else (36,64)
        sequence=13 if mode=='landscape' else 21
        path=directory/'media'/(mode+'.mp4');path.write_bytes(b'authored media fixture '+mode.encode());h=hash_file(path)
        probe_path=directory/(mode+'-probe.json');write_once(probe_path,probe(f))
        decode_path=directory/(mode+'-decode.json');write_once(decode_path,{'schema':'clipline.native-export.decode/1','artifact_sha256':h,'exit_code':0,'stderr_empty':True})
        terminal={'finished':True,'offlineMedia':[],'result':{'frames':3}}
        capture['outputs'].append({'composition':mode,'sequence':sequence,'path':str(path),'facts':f})
        native['outputs'].append({'composition':mode,'sequence':sequence,'path':str(path),'facts':f,'job':sequence,'terminal':terminal})
        outputs.append({'composition':mode,'sequence_id':sequence,'path':str(path),'sha256':h,'bytes':path.stat().st_size,'facts':f,'native_job':sequence,'terminal':terminal,'probe':{'path':str(probe_path),'sha256':hash_file(probe_path)},'decode':{'path':str(decode_path),'sha256':hash_file(decode_path)}})
    capture_path=directory/'native-capture.json';write_once(capture_path,capture)
    execution.register(FixtureStoppedProcess());cleanup=execution.stop()
    result={'schema':1,'state':'completed','process_disposition':'stopped','scopes':cleanup['scopes'],'engine':envelope['engine'],'snapshot_sha256':envelope['snapshot']['sha256'],'capture_sha256':hash_file(capture_path),'native':native,'outputs':outputs}
    write_once(directory/'runtime.json',result)
    return ExportOutcome(result,execution)

def test_explicit_snapshot_kit_assets_and_idempotency_are_durable(case):
    with CreatorLibrary(case['database']) as library:
        runner=owner(case,library);key=uid();job=submit(case,runner,key)
        assert submit(case,runner,key)==job
        assert job['input']['snapshot']['generation']==case['snapshot'].generation
        assert {a['asset_id'] for a in job['input']['assets']}=={case['source'].id,case['template'].id}
        descriptor=copy.deepcopy(case['descriptor']);descriptor['name']='New head'
        library.revise_kit(case['kit'],descriptor,expected_version=1)
        assert submit(case,runner,key)==job
        assert job['input']['kit']['version']==1

@pytest.mark.parametrize('change',[{'owners':[]},{'owners':[{'owner':5,'revision':False}]},{'compositions':['portrait','portrait']},{'compositions':['shared']}])
def test_bad_owner_or_composition_does_not_queue(case,change):
    with CreatorLibrary(case['database']) as library:
        with pytest.raises(ExportJobError):submit(case,owner(case,library),**change)
        assert library.connection.execute('SELECT COUNT(*) FROM durable_job_intents').fetchone()[0]==0

def test_changed_snapshot_fails_before_process_and_releases_claim(case,monkeypatch):
    with CreatorLibrary(case['database']) as library:job=submit(case,owner(case,library))
    case['snapshot'].path.write_bytes(b'changed immutable bytes')
    def forbidden(*a,**k):pytest.fail('native launch was attempted')
    monkeypatch.setattr(NativeExportClient,'run',forbidden)
    assert background(case)['state']=='failed'
    with CreatorLibrary(case['database']) as library:
        assert library.jobs.resources()==[]
        assert library.jobs.settlement(job['id'])['process_disposition']=='never_started'

def test_postlaunch_unknown_retains_both_slots_without_retry(case,monkeypatch):
    with CreatorLibrary(case['database']) as library:job=submit(case,owner(case,library))
    calls=[]
    def uncertain(*a,**k):calls.append(1);raise OSError('lost process outcome')
    monkeypatch.setattr(NativeExportClient,'run',uncertain)
    assert background(case)['state']=='interrupted'
    assert background(case) is None
    with CreatorLibrary(case['database']) as library:
        runner=owner(case,library)
        assert len(runner.jobs.resources())==2 and len(calls)==1
        current=runner.jobs.job(job['id'])
        with pytest.raises(VerificationRequired):runner.jobs.reconcile(job['id'],expected_revision=current['revision'],claim_token=current['claim_token'],outcome='failed',evidence_ref={'id':uid(),'sha256':'b'*64})

def test_other_heavy_job_blocks_render_admission(case,monkeypatch):
    with CreatorLibrary(case['database']) as library:
        runner=owner(case,library);submit(case,runner)
        held=library.jobs.claim_next(owner_id=uid(),kinds=['render'])
        submit(case,runner)
        assert held['state']=='running'
    monkeypatch.setattr(NativeExportClient,'run',lambda *a,**k:pytest.fail('heavy slot bypassed'))
    assert background(case) is None

@pytest.mark.parametrize('timeout',[0,True,float('nan'),float('inf'),3601])
def test_invalid_deadline_leaves_queued_job_unclaimed(case,timeout):
    with CreatorLibrary(case['database']) as library:job=submit(case,owner(case,library))
    with pytest.raises(ExportJobError):background(case,timeout=timeout)
    with CreatorLibrary(case['database']) as library:
        assert library.jobs.job(job['id'])==job and library.jobs.resources()==[]

def test_completed_storage_receipt_rechecks_media_and_evidence(case,monkeypatch):
    monkeypatch.setattr(NativeExportClient,'run',fixture_runtime)
    with CreatorLibrary(case['database']) as library:job=submit(case,owner(case,library))
    assert background(case)['state']=='completed'
    with CreatorLibrary(case['database']) as library:
        runner=owner(case,library);receipt=runner.completed_receipt(job['id'])
        assert receipt['request']['snapshot']['sha256']==case['snapshot'].sha256
        assert [o['composition'] for o in receipt['runtime']['outputs']]==['landscape','portrait']
        assert runner.jobs.resources()==[]
        Path(receipt['runtime']['outputs'][0]['path']).write_bytes(b'changed media')
        with pytest.raises(ExportJobError,match='media changed'):runner.completed_receipt(job['id'])

def test_cancelled_runtime_settles_only_proven_stopped_quarantine(case,monkeypatch):
    def cancelled(self,envelope,directory,**kw):
        directory.mkdir(parents=True)
        execution=kw['execution'];execution.register(FixtureStoppedProcess());cleanup=execution.stop()
        result={'state':'cancelled','process_disposition':'stopped','scopes':cleanup['scopes'],'outputs':[]}
        write_once(directory/'runtime.json',result);kw['cancel'].set();return ExportOutcome(result,execution)
    monkeypatch.setattr(NativeExportClient,'run',cancelled)
    with CreatorLibrary(case['database']) as library:job=submit(case,owner(case,library))
    assert background(case)['state']=='cancelled'
    with CreatorLibrary(case['database']) as library:
        assert library.jobs.resources()==[]
        assert library.jobs.settlement(job['id'])['outputs_disposition']=='quarantined'

@pytest.mark.parametrize('field,value',[('nb_read_frames','2'),('avg_frame_rate','30/1'),('sample_aspect_ratio','16:9'),('width',62)])
def test_probe_rejects_native_clock_or_geometry_mismatch(field,value):
    f=facts();p=probe(f);p['streams'][0][field]=value
    with pytest.raises(ExportRuntimeError):verify_probe(p,f)

def test_nonfinite_child_json_is_rejected():
    with pytest.raises(ExportRuntimeError):finite_json('{"value":NaN}')

def test_main_thread_cannot_run_owner(case):
    with CreatorLibrary(case['database']) as library:
        with pytest.raises(ExportJobError,match='background'):owner(case,library).run_next()

@pytest.mark.parametrize('fault',[None,'legacy-descriptor','template-sha','source-role','unregistered-media'])
def test_actual_native_kit_metadata_contract_binds_descriptor_and_resolved_source(case,fault):
    with CreatorLibrary(case['database']) as library:
        runner=owner(case,library);job=submit(case,runner);envelope=runner._read('requests',job['input']['request_ref'])
        kit={'schema':1,'momentId':case['moment'],'kit':{'id':case['kit'],'version':1,'descriptorSha256':library.kit_record(case['kit'],1).descriptor_sha256},
            'sourceAsset':{'id':case['source'].id,'sha256':case['source'].sha256},'templateAsset':{'id':case['template'].id,'path':case['template'].path,'sha256':case['template'].sha256},'assetRefs':case['descriptor']['asset_refs']}
        role={'role':'gameplay','resolvedMediaPath':case['source'].path}
        dependencies=[{'path':case['source'].path,'offline':False}]
        if fault=='legacy-descriptor':del kit['kit']['descriptorSha256']
        if fault=='template-sha':kit['templateAsset']['sha256']='0'*64
        if fault=='source-role':role['resolvedMediaPath']=case['template'].path
        if fault=='unregistered-media':dependencies.append({'path':str(case['root']/'unregistered.mp4'),'offline':False})
        observation={'recipe':RECIPE,'recipeSHA256':digest(RECIPE),'validation':{'valid':True,'owners':[{'owner':5,'units':[{'roles':[role]}]}]},'ownerMetadata':{'5':{'clipline.creatorKit.v1':canonical(kit).decode()}},'mediaDependencies':dependencies}
        if fault is None:runner._native(envelope,observation)
        else:
            with pytest.raises((ExportJobError,FileNotFoundError)):runner._native(envelope,observation)

def test_false_completed_proof_retains_resources(case,monkeypatch):
    def invalid(self,envelope,directory,**kw):
        outcome=fixture_runtime(self,envelope,directory,**kw)
        Path(outcome.receipt['outputs'][0]['path']).write_bytes(b'partial bytes after verification')
        return outcome
    monkeypatch.setattr(NativeExportClient,'run',invalid)
    with CreatorLibrary(case['database']) as library:job=submit(case,owner(case,library))
    assert background(case)['state']=='interrupted'
    with CreatorLibrary(case['database']) as library:
        assert len(library.jobs.resources())==2
        with pytest.raises(ExportJobError):owner(case,library).completed_receipt(job['id'])



def test_completed_receipt_survives_verified_library_asset_relocation(case,monkeypatch):
    monkeypatch.setattr(NativeExportClient,'run',fixture_runtime)
    with CreatorLibrary(case['database']) as library:job=submit(case,owner(case,library))
    assert background(case)['state']=='completed'
    with CreatorLibrary(case['database']) as library:
        runner=owner(case,library);old=Path(case['source'].path);moved=case['root']/'relocated-source.mp4';old.rename(moved)
        library.relocate_asset(case['source'].id,moved,expected_path=str(old))
        receipt=runner.completed_receipt(job['id'])
        assert receipt['request']['assets'][next(i for i,p in enumerate(receipt['request']['assets']) if p['asset_id']==case['source'].id)]['path']==str(old)
