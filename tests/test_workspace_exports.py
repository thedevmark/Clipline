"""Real SQLite/verified delivery with the existing native boundary replaced."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import uuid

import pytest
from native.core.creator_library import CreatorLibrary
from native.services.caption_worker.evidence import hash_file
from native.services.native_export_client import NativeExportClient
from native.services.durable_native_exports import DurableNativeExports
from native.services.workspace_exports import WorkspaceExports, copy_exclusive
from tests.test_native_exports import case, fixture_runtime, submit


def background(function):
    with ThreadPoolExecutor(max_workers=1) as pool:return pool.submit(function).result(10)


class Editor:
    def __init__(self,case):
        self.workspace=SimpleNamespace(database=case['database'],directory=case['root'])
        with CreatorLibrary(case['database']) as library:
            self._facts=SimpleNamespace(moment=library.moment(case['moment']),kit=library.kit_record(case['kit'],1))
        self._view=SimpleNamespace(snapshot=case['snapshot'])
        self.pin=None;self.checkpoints=0;self.native_revision=17;self.change=False
        self._session=SimpleNamespace(bridge=SimpleNamespace(execute=self.metadata))
    def metadata(self,command,params):
        assert command=='metadata.get' and params=={'item':5}
        return {'item':5,'metadata':{'clipline.acceptedCaptions.v1':json.dumps({'pin':self.pin})} if self.pin else {}}
    def _checkpoint_open(self):self.checkpoints+=1;return self.native_revision,[{'owner':5,'revision':0}]
    def _inspect(self):return self.native_revision+int(self.change),[{'owner':5,'revision':0}]
    def _run(self,operation,**kwargs):operation();return SimpleNamespace(message='fixture')


@pytest.fixture
def delivery(case,monkeypatch):
    def forbidden(*args,**kwargs):raise AssertionError('No actual workload subprocess in delivery tests')
    from native.services import native_export_client as native
    monkeypatch.setattr(native.subprocess,'Popen',forbidden)
    monkeypatch.setattr(NativeExportClient,'run',fixture_runtime)
    editor=Editor(case);service=WorkspaceExports(editor,SimpleNamespace(client=lambda:case['client']))
    return editor,service


def accepted_pin(case):
    with CreatorLibrary(case['database']) as library:
        identity={'source':{'asset_id':case['source'].id,'sha256':case['source'].sha256,'path':case['source'].path,'audio_stream_index':0,'channels':[0,1],'mix':'mixed'},
            'audio':{'path':'authored.wav','pcm_sha256':'b'*64,'wav_sha256':'c'*64,'sample_rate':16000,'sample_count':16000,'channels':1,'sample_width':2,'sample_zero_source_us':0,
                'extraction':{'source_sha256':case['source'].sha256,'argv':['fixture'],'tool_version':'fixture'}},'range':{'start_us':0,'end_us':1000000}}
        tid=library.captions.create(identity);run=str(uuid.uuid4());word=str(uuid.uuid4())
        hypothesis={'run_id':run,'processing_state':'complete',**identity,'words':[{'id':word,'text':'Selected'}]}
        library.captions.propose_hypothesis(tid,hypothesis)
        alignment={'run_id':str(uuid.uuid4()),'hypothesis_run_id':run,'target_transcript_revision':None,'words':[{'word_id':word,'text':'Selected','timing':{'status':'aligned','start_us':0,'end_us':500000}}]}
        library.captions.propose_alignment(tid,run,alignment)
        library.captions.accept_initial(tid,run,alignment_id=alignment['run_id'])
        library.captions.edit(tid,expected_revision=1,operation='replace',target_word_ids=[library.captions.revision(tid,1)['words'][0]['id']],replacements=[{'text':'New'}])
    # Authored native metadata fixture; not a computed Rust caption payload hash.
    return {'transcript_id':tid,'revision':1,'payload_sha256':'a'*64}


def test_exact_selected_job_both_deliveries_and_accepted_version_ignore_newer_head(case,delivery):
    editor,service=delivery;editor.pin=accepted_pin(case)
    with CreatorLibrary(case['database']) as library:
        previous=submit(case,DurableNativeExports(library,service.directory,case['client']))
    messages=[]
    result=background(lambda:service.run(case['root'],cancel=threading.Event(),progress=messages.append))
    assert result.state=='completed' and len(result.paths)==2 and editor.checkpoints==1
    assert [Path(path).name for path in result.paths]==['landscape.mp4','portrait.mp4']
    assert all(Path(path).read_bytes()==Path(original).read_bytes() for path,original in zip(result.paths,result.originals))
    with CreatorLibrary(case['database']) as library:
        assert library.jobs.job(previous['id'])['state']=='queued'
        current=library.jobs.job(service.job['id'])
        assert current['state']=='completed' and current['input']['transcript']['revision']==1
    request=json.loads((service.directory/'requests'/(service.job['input']['request_ref']['id']+'.json')).read_text())
    assert request['snapshot']['sha256']==case['snapshot'].sha256 and request['accepted_pin']==editor.pin
    assert request['compositions']==['landscape','portrait'] and request['owners']==[{'owner':5,'revision':0}]
    assert messages[0].startswith('Saving') and any('Rendering' in message for message in messages)


def test_partial_copy_reports_only_finished_file_and_preserves_both_originals(case,delivery,monkeypatch):
    _,service=delivery
    from native.services import workspace_exports as module
    original=module.copy_exclusive
    def copy(source,dest,expected):
        if dest.name=='portrait.mp4':raise OSError('fixture destination failure')
        return original(source,dest,expected)
    monkeypatch.setattr(module,'copy_exclusive',copy)
    result=background(lambda:service.run(case['root'],cancel=threading.Event()))
    assert result.state=='partial' and len(result.paths)==1 and Path(result.paths[0]).name=='landscape.mp4'
    assert len(result.originals)==2 and all(Path(path).is_file() for path in result.originals)
    assert '1 of 2' in result.message and 'Portrait:' in result.message
    assert not (Path(result.paths[0]).parent/'portrait.mp4').exists()


@pytest.mark.parametrize('boundary',['existing','changed'])
def test_exclusive_atomic_copy_cannot_replace_destination_or_publish_changed_bytes(tmp_path,boundary):
    source=tmp_path/'source';source.write_bytes(b'complete verified bytes');target=tmp_path/'final.mp4'
    expected=hash_file(source)
    if boundary=='existing':target.write_bytes(b'keep existing')
    else:source.write_bytes(b'changed')
    with pytest.raises((OSError,ValueError)):copy_exclusive(source,target,expected)
    assert source.exists() and not list(tmp_path.glob('*.part'))
    if boundary=='existing':assert target.read_bytes()==b'keep existing'
    else:assert not target.exists()


def test_editor_change_after_checkpoint_does_not_queue_or_deliver(case,delivery):
    editor,service=delivery;editor.change=True
    result=background(lambda:service.run(case['root'],cancel=threading.Event()))
    assert result.state=='blocked' and 'changed after saving' in result.message and service.job is None
    assert editor.checkpoints==1


def test_cancel_or_missing_folder_never_checkpoints_or_queues(case,delivery):
    editor,service=delivery;cancel=threading.Event();cancel.set()
    result=background(lambda:service.run(case['root'],cancel=cancel))
    assert result.state=='cancelled' and editor.checkpoints==0
    result=background(lambda:service.run(case['root']/'missing',cancel=threading.Event()))
    assert result.state=='blocked' and editor.checkpoints==0 and service.job is None


def test_invalid_exact_dispatch_does_not_leave_backend_lock_held(case):
    def run():
        with CreatorLibrary(case['database']) as library:
            runner=DurableNativeExports(library,case['root']/'jobs',case['client'])
            with pytest.raises(ValueError):runner.run_next(job_id='invalid')
            assert runner.run_next(job_id=str(uuid.uuid4())) is None
    background(run)


def test_actual_export_execution_is_retained_before_error_formatting(case,delivery,monkeypatch):
    _,service=delivery
    from native.services.native_export_client import ExportExecution
    execution=ExportExecution({'fixture':'retained'})
    class Failure(RuntimeError):
        executions=(execution,)
        def __str__(self):
            assert service.execution is execution and service.runner is not None and service.client is case['client']
            return 'bookkeeping fixture'
    def fail(runner,**kwargs):
        runner._executions['fixture']=execution
        raise Failure()
    monkeypatch.setattr(DurableNativeExports,'run_next',fail)
    result=background(lambda:service.run(case['root'],cancel=threading.Event()))
    assert result.state=='blocked' and service.unsettled and result.can_cleanup
    assert service.execution is execution and service.blocked
    execution.stop()


def test_existing_controller_operation_gate_blocks_until_retained_export_cleanup(case):
    from native.services.workspace_editor import WorkspaceEditorController
    from native.services.workspace_library import WorkspaceLibrary
    from native.services.native_moment import QualifiedCreatorKitHelper
    from native.services.filmcraft_session import QualifiedEditor
    from native.services.native_export_client import ExportExecution
    import sys
    qualified=case['client'].helper
    controller=WorkspaceEditorController(WorkspaceLibrary(case['root']),
        helper=QualifiedCreatorKitHelper(qualified.path,qualified.sha256,'fixture'),
        editor=QualifiedEditor(qualified.path,qualified.sha256,'fixture'),python=Path(sys.executable),
        export_runtime=SimpleNamespace(client=lambda:case['client']))
    controller._exports.execution=ExportExecution({'fixture':'live'})
    calls=[]
    result=background(lambda:controller._run(lambda:calls.append('normal')))
    assert result.state=='blocked' and not calls and not controller.status().app_close_allowed
    background(lambda:controller._run(lambda:calls.append('explicit cleanup'),export_cleanup=True))
    assert calls==['explicit cleanup']
    controller._exports.execution.stop()


def test_cancel_after_submission_cancels_only_the_queued_selected_export(case,delivery):
    _,service=delivery;cancel=threading.Event()
    def progress(message):
        if message.startswith('Rendering'):cancel.set()
    result=background(lambda:service.run(case['root'],cancel=cancel,progress=progress))
    assert result.state=='cancelled' and not service.blocked and service.execution is None
    with CreatorLibrary(case['database']) as library:
        assert library.jobs.job(service.job['id'])['state']=='cancelled' and not library.jobs.resources()
