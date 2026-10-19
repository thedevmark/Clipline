"""Real gated private Python processes; no native helper/media/model is launched."""
import json
import gc
import os
from pathlib import Path
import sys
import threading
import time
import weakref

import pytest
import native.services.native_export_client as runtime_module

from native.services.native_export_client import OwnedProcess,QualifiedProgram,NativeExportClient,ExportOutcome
from native.services.caption_worker.evidence import canonical,hash_file
from native.services.caption_worker.client import write_once
from native.core.creator_library import CreatorLibrary
from native.core.durable_jobs import VerificationRequired
from native.services.durable_native_exports import DurableNativeExports,ExportJobOutcome,ExportJobError,ExportOwnershipError
from tests.test_native_exports import case,owner,submit,background,uid

pytestmark=pytest.mark.skipif(os.name!='nt',reason='Physical Windows read guards and JobObject qualification required')


@pytest.mark.parametrize('failure',['unknown','throw'])
def test_unknown_stop_keeps_real_scope_queryable_until_verified_retry(tmp_path,monkeypatch,failure):
    gate=tmp_path/'gate.py';gate.write_text('import json,subprocess,sys\nraise SystemExit(subprocess.run(json.loads(sys.stdin.readline()),stdin=subprocess.DEVNULL).returncode)\n',encoding='utf8')
    ready=tmp_path/'ready'
    child_script=tmp_path/'child.py';child_script.write_text('import pathlib,sys,threading\npathlib.Path(sys.argv[1]).write_text("ready")\nthreading.Event().wait(20)\n',encoding='utf8')
    child=OwnedProcess([sys.executable,'-I','-B',str(gate)],tmp_path/'process',threading.Event(),time.monotonic()+10)
    tree=child.tree;original=tree.stop
    try:
        child.send(canonical([sys.executable,'-I','-B',str(child_script),str(ready)])+b'\n')
        deadline=time.monotonic()+3
        while not ready.exists():
            assert time.monotonic()<deadline,'owned gated fixture did not become ready'
            time.sleep(.005)
        def uncertain():
            if failure=='throw':raise OSError('authored query exception')
            return {'disposition':'unknown','active_processes':None,'authored_query_failure':True}
        monkeypatch.setattr(tree,'stop',uncertain)
        unknown=child.close()
        assert unknown['disposition']=='unknown'
        assert tree.job is not None,'Unknown stop discarded the only queryable JobObject handle'
        assert child.closed is False,'Unknown stop incorrectly made cleanup permanently idempotent'
        assert child.process.poll() is None,'An unknown scope was closed instead of retained'
        monkeypatch.setattr(tree,'stop',original)
        stopped=child.close()
        assert stopped['disposition']=='stopped' and stopped['active_processes']==0
        assert stopped['streams_closed'] is True and tree.job is None
    finally:
        monkeypatch.setattr(tree,'stop',original)
        # An old candidate may already have discarded its handle. The actual
        # JobObject kill-on-close then stopped this private fixture; no PID adoption.
        if tree.job is not None:tree.stop();tree.close()
        if child.process.poll() is None:child.process.kill()
        child.process.wait(timeout=5)

@pytest.mark.parametrize('receipt_fault',[False,True])
def test_unknown_durable_execution_retains_source_program_guards_and_returned_owner(case,monkeypatch,receipt_fault):
    root=case['root'];gate=root/'real-gate.py'
    gate.write_text('import json,subprocess,sys\nraise SystemExit(subprocess.run(json.loads(sys.stdin.readline()),stdin=subprocess.DEVNULL).returncode)\n',encoding='utf8')
    programs=[]
    for name in ('helper','ffprobe','ffmpeg'):
        path=root/(name+'.exe');path.write_bytes(('authored unused '+name).encode());programs.append(QualifiedProgram(path,hash_file(path)))
    python=QualifiedProgram(Path(sys.executable),hash_file(sys.executable))
    case['client']=NativeExportClient(*programs,python,gate,engine_version='0.9.0-test-fixture',source_pin_sha256='a'*64)
    ready=root/'child-ready';script=root/'bounded-child.py'
    script.write_text('import pathlib,sys,threading\npathlib.Path(sys.argv[1]).write_text("ready")\nthreading.Event().wait(20)\n',encoding='utf8')
    guarded=[p.path for p in programs]+[gate,Path(case['source'].path),Path(case['template'].path),case['snapshot'].path]
    holder={};values=[];errors=[];controller=[]
    if receipt_fault:
        original_write=DurableNativeExports._write
        def fail_uncertain(self,kind,*args):
            if kind=='uncertain':raise OSError('authored uncertain-receipt write failure')
            return original_write(self,kind,*args)
        monkeypatch.setattr(DurableNativeExports,'_write',fail_uncertain)
    if os.environ.get('CLIPLINE_TEST_DISABLE_PROGRAM_GUARDS')=='1':monkeypatch.setattr(NativeExportClient,'admit',lambda *a:None)
    def uncertain(self,envelope,directory,*,execution,**kwargs):
        for path in guarded:
            with pytest.raises(PermissionError):
                with path.open('ab'):pass
        assert python.path.resolve() in execution.guards,'Interpreter must be read-guarded before launch'
        directory.mkdir(parents=True)
        child=OwnedProcess([sys.executable,'-I','-B',str(gate)],directory/'process',threading.Event(),time.monotonic()+10,execution=execution)
        holder.update(child=child,execution=execution,original_stop=child.tree.stop)
        child.send(canonical([sys.executable,'-I','-B',str(script),str(ready)])+b'\n')
        deadline=time.monotonic()+3
        while not ready.exists():
            assert time.monotonic()<deadline,'owned process did not become ready'
            time.sleep(.005)
        monkeypatch.setattr(child.tree,'stop',lambda:{'disposition':'unknown','active_processes':None,'authored_query_failure':True})
        cleanup=execution.stop()
        receipt={'state':'unknown','process_disposition':'unknown','scopes':cleanup['scopes'],'outputs':[]}
        write_once(directory/'runtime.json',receipt)
        return ExportOutcome(receipt,execution)
    monkeypatch.setattr(NativeExportClient,'run',uncertain)
    with CreatorLibrary(case['database']) as library:job=submit(case,owner(case,library))
    def work():
        try:
            with CreatorLibrary(case['database']) as library:
                runner=owner(case,library);controller.append(weakref.ref(runner))
                try:values.append(runner.run_next(timeout=10))
                except ExportOwnershipError as error:
                    assert receipt_fault and len(error.executions)==1
                    values.append(ExportJobOutcome(library.jobs.job(job['id']),error.executions[0]))
        except BaseException as error:errors.append(error)
    thread=threading.Thread(target=work);thread.start();thread.join(10)
    try:
        assert not thread.is_alive() and not errors,errors
        result=values[0];assert type(result) is ExportJobOutcome and result.job['state']=='interrupted'
        execution=result.execution;assert execution is holder['execution'] and not execution.closed
        gc.collect();assert controller[0]() is None,'The outcome must retain ownership independently of the originating service'
        assert holder['child'].tree.job is not None and holder['child'].process.poll() is None
        for path in guarded:
            with pytest.raises(PermissionError):
                with path.open('ab'):pass
        with CreatorLibrary(case['database']) as library:
            assert len(library.jobs.resources())==2
            submit(case,owner(case,library))
        assert background(case) is None,'Unknown execution must continue blocking heavy admission'
        monkeypatch.setattr(holder['child'].tree,'stop',holder['original_stop'])
        cleared=[];cleanup_errors=[]
        def cleanup():
            try:
                with CreatorLibrary(case['database']) as library:
                    runner=owner(case,library)
                    with pytest.raises(ExportJobError,match='original live execution'):runner.cleanup_pending(job['id'])
                    cleared.append(runner.cleanup_pending(job['id'],execution=execution))
            except BaseException as error:cleanup_errors.append(error)
        cleaner=threading.Thread(target=cleanup);cleaner.start();cleaner.join(10)
        assert not cleaner.is_alive() and not cleanup_errors,cleanup_errors
        assert cleared[0]['scope']['closed'] is True and execution.closed
        assert holder['child'].tree.job is None
        for path in guarded:
            with path.open('ab'):pass
        with CreatorLibrary(case['database']) as library:
            assert library.jobs.job(job['id'])==result.job and len(library.jobs.resources())==2
            with pytest.raises(VerificationRequired):
                owner(case,library).jobs.reconcile(job['id'],expected_revision=result.job['revision'],claim_token=result.job['claim_token'],outcome='failed',evidence_ref=cleared[0]['cleanup_ref'])
    finally:
        if holder:
            monkeypatch.setattr(holder['child'].tree,'stop',holder['original_stop'])
            assert holder['execution'].stop()['closed'],'private child and read guards must be cleaned'

@pytest.mark.parametrize('changed',['helper','ffprobe','ffmpeg','python','gate'])
def test_changed_qualified_input_fails_before_process_intent_and_releases_guards(case,monkeypatch,changed):
    root=case['root'];qualified=[];paths={}
    for name in ('helper','ffprobe','ffmpeg','python'):
        path=root/(name+'-pin.exe');path.write_bytes(('private authored '+name).encode());paths[name]=path;qualified.append(QualifiedProgram(path,hash_file(path)))
    paths['gate']=root/'tool-gate.py';paths['gate'].write_text('# private authored gate',encoding='utf8')
    case['client']=NativeExportClient(*qualified,paths['gate'],engine_version='0.9.0-test-fixture',source_pin_sha256='a'*64)
    with CreatorLibrary(case['database']) as library:job=submit(case,owner(case,library))
    paths[changed].write_bytes(b'changed after queue')
    monkeypatch.setattr(NativeExportClient,'run',lambda *a,**k:pytest.fail('process launch was admitted for changed qualified bytes'))
    result=background(case);assert result['state']=='failed'
    with CreatorLibrary(case['database']) as library:
        assert library.jobs.resources()==[]
        assert library.jobs.settlement(job['id'])['process_disposition']=='never_started'
    for path in paths.values():
        with path.open('ab'):pass

def test_unreleased_gate_attachment_failure_reaps_exact_child_without_settling_job(case,monkeypatch):
    root=case['root'];gate=root/'unreleased-gate.py';started=root/'must-not-start'
    gate.write_text('import pathlib,sys,threading\nline=sys.stdin.readline()\nif line: pathlib.Path(sys.argv[1]).write_text("released")\nthreading.Event().wait(20)\n',encoding='utf8')
    def reject_attachment(process):raise OSError('authored JobObject attachment rejection')
    monkeypatch.setattr(runtime_module,'ProcessTree',reject_attachment)
    correct_close=OwnedProcess.close
    if os.environ.get('CLIPLINE_TEST_ATTACHMENT_REVERSION')=='1':
        def unproven_reaped_parent(self):
            if self.tree is None and self.process is not None and not self.gate_released:
                if self.process.poll() is None:self.process.kill()
                self.process.wait(timeout=5)
                self.proof={'disposition':'unknown','active_processes':None,'authored_reversion':True}
                return self.proof
            return correct_close(self)
        monkeypatch.setattr(OwnedProcess,'close',unproven_reaped_parent)
    def fail(self,envelope,directory,*,execution,**kw):
        directory.mkdir(parents=True);execution.hold(gate,hash_file(gate));execution.hold(sys.executable,hash_file(sys.executable))
        OwnedProcess([sys.executable,'-I','-B',str(gate),str(started)],directory/'process',threading.Event(),time.monotonic()+10,execution=execution)
        pytest.fail('attachment failure must propagate before gate release')
    monkeypatch.setattr(NativeExportClient,'run',fail)
    with CreatorLibrary(case['database']) as library:job=submit(case,owner(case,library))
    result=background(case,retain_owner=True);assert result.job['state']=='interrupted'
    execution=result.execution
    try:
        assert execution.closed and execution.guards=={}
        assert len(execution.processes)==1
        child=execution.processes[0]
        assert child.process.poll() is not None and child.gate_released is False and child.tree is None
        assert child.proof['scope']=='unreleased_gate_process' and child.proof['active_processes']==0 and child.proof['streams_closed'] is True
        assert not started.exists()
        before=child.close_attempts;assert child.close()==child.proof and child.close_attempts==before
        assert execution.stop()['closed']
        for path in (gate,Path(case['source'].path),case['client'].helper.path):
            with path.open('ab'):pass
        with CreatorLibrary(case['database']) as library:
            assert library.jobs.job(job['id'])==result.job and len(library.jobs.resources())==2
            assert library.jobs.settlement(job['id']) is None
    finally:
        monkeypatch.setattr(OwnedProcess,'close',correct_close)
        assert execution.stop()['closed'],'attachment fixture must release its private read guards'


@pytest.mark.parametrize('failed_reader',[1,2])
def test_reader_start_failure_keeps_original_error_and_releases_stopped_scope(tmp_path,monkeypatch,failed_reader):
    gate=tmp_path/'unreleased-reader-gate.py'
    gate.write_text('import sys,threading\nsys.stdin.readline()\nthreading.Event().wait(20)\n',encoding='utf8')
    execution=runtime_module.ExportExecution({'fixture':'reader-start','reader':failed_reader})
    execution.hold(gate,hash_file(gate));execution.hold(sys.executable,hash_file(sys.executable))
    original_start=threading.Thread.start;calls=[];known_unstarted=[]
    def fail_selected_reader(thread):
        calls.append(thread)
        if len(calls)==failed_reader:
            known_unstarted.append(thread)
            raise RuntimeError('authored reader start failure')
        return original_start(thread)
    monkeypatch.setattr(threading.Thread,'start',fail_selected_reader)
    try:
        with pytest.raises(RuntimeError,match='^authored reader start failure$') as raised:
            OwnedProcess([sys.executable,'-I','-B',str(gate)],tmp_path/'process',threading.Event(),time.monotonic()+10,execution=execution)
        monkeypatch.setattr(threading.Thread,'start',original_start)
        assert len(execution.processes)==1
        child=execution.processes[0]
        assert raised.value.owner is child
        stopped=execution.stop()
        assert not stopped['closed'] and execution.guards
        assert not child.closed and child.process.poll() is not None and child.tree.job is not None
        assert child.proof['disposition']=='unknown' and child.proof['streams_closed'] is False
        assert not child.gate_released and len(child.threads)==failed_reader
        pending=known_unstarted[0]
        assert pending is child._starting_reader and pending is child.threads[-1]
        assert pending.ident is None and not pending.is_alive()
        # Only this fixture knows start never reached the OS. Its first actual start
        # reads EOF from the already stopped gate; production cleanup never starts it.
        original_start(pending);pending.join(2)
        assert not pending.is_alive()
        stopped=execution.stop()
        assert stopped['closed'] and execution.guards=={}
        assert child.closed and child.tree.job is None
        assert child.proof['disposition']=='stopped' and child.proof['active_processes']==0
        assert child.proof['streams_closed'] is True and len(child.threads)==failed_reader
        assert all(pipe.closed for pipe in (child.process.stdin,child.process.stdout,child.process.stderr))
        with gate.open('ab'):pass
    finally:
        monkeypatch.setattr(threading.Thread,'start',original_start)
        # Complete this explicitly known-unstarted fixture reader without dropping it.
        for thread in known_unstarted:
            if thread.ident is None:original_start(thread)
            thread.join(2)
        assert execution.stop()['closed'],'reader-start fixture must release every private handle'
