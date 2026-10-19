from dataclasses import asdict
import json
import threading
from pathlib import Path
import pytest
from tests.test_twitch_durable_intake import case, submit, run, background, uid, clip
from native.core.creator_library import CreatorLibrary
from native.services.durable_twitch_intake import DurableTwitchIntake
from native.services.twitch_publication import VerifiedTwitchPublication
from native.services.twitch_intake_client import TwitchIntakeClient, IntakeError


def test_deadline_expiry_during_publication_verification_prevents_completion(case, monkeypatch):
    import native.services.durable_twitch_intake as intake
    c=case; submitted=submit(c)
    original_init=VerifiedTwitchPublication.__init__
    original_clock=intake.time.monotonic
    expired=[False]
    def clock(): return original_clock() + (1000 if expired[0] else 0)
    def verify_then_expire(self,*args,**kwargs):
        original_init(self,*args,**kwargs)
        expired[0]=True
    monkeypatch.setattr(intake.time,'monotonic',clock)
    monkeypatch.setattr(VerifiedTwitchPublication,'__init__',verify_then_expire)
    outcome=run(c,timeout=10)
    print('post-publication-expiry job state:', outcome.job['state'])
    assert outcome.job['state'] != 'completed', 'job completed after publication verification crossed the absolute deadline'


def test_cancel_during_publication_verification_rejects_completed_sql(case, monkeypatch):
    c=case; submitted=submit(c)
    original_init=VerifiedTwitchPublication.__init__
    def verify_then_cancel(self,*args,**kwargs):
        original_init(self,*args,**kwargs)
        with CreatorLibrary(c['db']) as other:
            job=other.jobs.job(submitted.job['id'])
            other.jobs.request_cancel(job['id'],expected_revision=job['revision'])
    monkeypatch.setattr(VerifiedTwitchPublication,'__init__',verify_then_cancel)
    outcome=run(c)
    assert outcome.job['state']=='cancelled'
    assert outcome.execution.closed and all(s['active_processes']==0 for s in outcome.execution.proof['scopes'])
    with CreatorLibrary(c['db']) as lib:
        assert not lib.jobs.resources() and lib.twitch_source('Clip-One') is None
        assert not lib.list_assets()['items']


def test_source_mutation_after_execution_stop_is_independently_rejected(case, monkeypatch):
    import native.services.durable_twitch_intake as intake
    c=case; submitted=submit(c)
    original_link=intake.os.link
    def replace_then_link(source,target,*args,**kwargs):
        Path(source).write_bytes(b'changed after transfer and probe closure')
        return original_link(source,target,*args,**kwargs)
    monkeypatch.setattr(intake.os,'link',replace_then_link)
    outcome=run(c)
    assert outcome.job['state']=='failed' and outcome.execution.closed
    with CreatorLibrary(c['db']) as lib:
        assert lib.twitch_source('Clip-One') is None and not lib.list_assets()['items']
        assert not lib.jobs.resources()


def test_preexisting_execution_directory_never_quarantines_foreign_file(case):
    c=case; submitted=submit(c)
    path=c['service']/'executions'/submitted.job['output_set_id']/'download.part'
    path.parent.mkdir(parents=True); path.write_bytes(b'foreign bytes')
    outcome=run(c)
    assert outcome.job['state']=='failed' and path.read_bytes()==b'foreign bytes'
    assert not path.with_name(path.name+'.quarantined').exists()
    assert c['counts']=={'transfer':0,'probe':0}


def test_duplicate_queued_clip_does_not_change_canonical_source(case):
    c=case; first=submit(c); second=submit(c)
    assert first.job['id']!=second.job['id']
    one=run(c); two=run(c)
    assert one.job['state']=='completed' and two.job['state']=='failed'
    with CreatorLibrary(c['db']) as lib:
        assert lib.twitch_source('Clip-One')['asset_id']==one.asset.id
        assert lib.verify_asset(one.asset.id)==one.asset
        assert len(lib.list_assets()['items'])==1 and not lib.jobs.resources()

def test_cleanup_rejects_reconstructed_empty_owner_while_actual_scope_remains_unknown(case, monkeypatch):
    from native.services.caption_worker.process_tree import ProcessTree
    from native.services.native_export_client import ExportExecution
    from native.services.durable_native_exports import ExportOwnershipError
    c=case; c['block']=True; submitted=submit(c)
    original_stop=ProcessTree.stop
    monkeypatch.setattr(ProcessTree,'stop',lambda self:{'disposition':'unknown','active_processes':None})
    actual=None
    try:
        with pytest.raises(ExportOwnershipError) as caught:
            run(c,timeout=.2)
        actual=caught.value.executions[0]
        assert not actual.closed and actual.processes[0].tree.job is not None
        reconstructed=ExportExecution(actual.binding)
        def cleanup():
            with CreatorLibrary(c['db']) as lib:
                owner=DurableTwitchIntake(lib,c['service'],c['client'])
                return owner.cleanup_pending(submitted.job['id'],execution=reconstructed)
        with pytest.raises(IntakeError,match='ORIGINAL_INTAKE_OWNER_REQUIRED'):
            result=background(cleanup)
            print('reconstructed cleanup result:',result,'actual closed:',actual.closed)
    finally:
        monkeypatch.setattr(ProcessTree,'stop',original_stop)
        if actual is not None:
            assert actual.stop()['closed']