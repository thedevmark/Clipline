"""Bounded fix verification beside the unchanged independent reproductions."""
import threading
import time
import sys

import pytest
from native.core.creator_library import CreatorLibrary
from native.services.durable_native_exports import ExportOwnershipError
from native.services.native_export_client import ExportExecution,OwnedProcess
from native.services.durable_twitch_intake import DurableTwitchIntake
from native.services.twitch_intake_client import IntakeError
from native.services.twitch_publication import VerifiedTwitchPublication
from tests.test_twitch_durable_intake import case,submit,run,background


@pytest.mark.parametrize('trigger',['deadline','cancel'])
def test_final_guard_verification_failure_is_specific_atomic_and_quarantined(case,monkeypatch,trigger):
    import native.services.durable_twitch_intake as intake
    c=case; submitted=submit(c); cancel=threading.Event()
    original=VerifiedTwitchPublication.__init__; original_clock=intake.time.monotonic; expired=[False]
    def clock(): return original_clock()+(1000 if expired[0] else 0)
    def final_verify(self,*args,**kwargs):
        original(self,*args,**kwargs)
        if trigger=='deadline': expired[0]=True
        else: cancel.set()
    monkeypatch.setattr(intake.time,'monotonic',clock)
    monkeypatch.setattr(VerifiedTwitchPublication,'__init__',final_verify)
    outcome=run(c,cancel=cancel,timeout=10)
    assert outcome.job['state']==('failed' if trigger=='deadline' else 'cancelled')
    assert outcome.error_code==('DEADLINE' if trigger=='deadline' else 'CANCELLED')
    assert outcome.execution.closed and len(outcome.execution.proof['scopes'])==2
    assert all(scope['active_processes']==0 for scope in outcome.execution.proof['scopes'])
    assert c['counts']=={'transfer':1,'probe':1}
    with CreatorLibrary(c['db']) as lib:
        assert not lib.jobs.resources() and not lib.list_assets()['items']
        assert lib.twitch_source('Clip-One') is None
    output=submitted.job['output_set_id']
    assert (c['service']/'media'/output/'source.mp4.quarantined').is_file()
    assert (c['service']/'executions'/output/'download.part.quarantined').is_file()


def test_current_instance_rejects_a_different_real_owner_for_same_claim(case,monkeypatch):
    from native.services.caption_worker.process_tree import ProcessTree
    c=case; c['block']=True; submitted=submit(c); original_stop=ProcessTree.stop
    monkeypatch.setattr(ProcessTree,'stop',lambda self:{'disposition':'unknown','active_processes':None})
    def work():
        actual=None; alternate=None
        try:
            with CreatorLibrary(c['db']) as lib:
                owner=DurableTwitchIntake(lib,c['service'],c['client'])
                with pytest.raises(ExportOwnershipError) as caught: owner.run_next(timeout=.2)
                actual=caught.value.executions[0]
                assert owner._execution is actual and not actual.closed
                alternate=ExportExecution(actual.binding)
                child=OwnedProcess([sys.executable,'-I','-B',str(c['fixture'])],c['root']/'alternate-scope',
                                   threading.Event(),time.monotonic()+5,execution=alternate,environment={})
                assert alternate.processes and child.process.poll() is None
                with pytest.raises(IntakeError,match='ORIGINAL_INTAKE_OWNER_REQUIRED'):
                    owner.cleanup_pending(submitted.job['id'],execution=alternate)
                assert not actual.closed and not alternate.closed
                assert child.process.poll() is None
                monkeypatch.setattr(ProcessTree,'stop',original_stop)
                assert owner.cleanup_pending(submitted.job['id'],execution=actual)['closed']
                assert lib.jobs.resources(),'cleanup alone cannot release durable admission'
        finally:
            monkeypatch.setattr(ProcessTree,'stop',original_stop)
            if actual is not None: assert actual.stop()['closed']
            if alternate is not None: assert alternate.stop()['closed']
    background(work)
