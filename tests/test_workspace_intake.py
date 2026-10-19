from dataclasses import replace
import threading
from types import SimpleNamespace
from unittest.mock import patch
import uuid
from contextlib import contextmanager

import pytest
from native.core.creator_library import CreatorLibrary
from native.core.twitch_inbox import TwitchClip
from native.services.workspace_library import WorkspaceLibrary
from native.services import workspace_intake as module
from native.services.durable_twitch_intake import IntakeOutcome
from native.services.native_export_client import ExportExecution
from native.services.durable_native_exports import ExportOwnershipError
from native.services.twitch_publication import PublicationGuardError

def background(fn):
    values=[];errors=[]
    def run():
        try:values.append(fn())
        except BaseException as error:errors.append(error)
    thread=threading.Thread(target=run);thread.start();thread.join(10)
    assert not thread.is_alive()
    if errors:raise errors[0]
    return values[0]

def clip(name='clipA',duration=4_000_000):
    return TwitchClip(name,'owner','creator','','Authored clip','2026-10-18T00:00:00Z',duration,10,
                      'https://clips.twitch.tv/'+name,None,None)

@pytest.fixture
def controller(tmp_path):
    workspace=WorkspaceLibrary(tmp_path/'workspace')
    background(lambda:seed(workspace))
    return module.WorkspaceIntakeController(workspace,SimpleNamespace(client_id='bound'))

def seed(workspace):
    with workspace._library() as lib:lib.record_twitch_clips([clip(),clip('long',181_000_000)])

class Boundary:
    """No process/transfer/probe; real private SQL jobs around the runtime boundary."""
    def __init__(self,lib,*,reused=False,error=None,execution=None):
        self.lib=lib;self.reused=reused;self.error=error;self.execution=execution;self.targets=[];self.submits=[]
    def submit(self,clip_id,*,idempotency_key):
        self.submits.append(clip_id)
        if self.reused:return IntakeOutcome(None,None,None,None,True)
        job=self.lib.jobs.submit('io',{'request_ref':{'id':str(uuid.uuid4()),'sha256':'a'*64}},idempotency_key=idempotency_key)
        return IntakeOutcome(job,None,None,None)
    def run_next(self,*,job_id,cancel):
        self.targets.append(job_id)
        if self.error is not None:raise self.error
        return None
    def cleanup_pending(self,jid,*,execution):
        assert execution is self.execution
        execution.stop()

def test_main_thread_refuses_without_connection(controller):
    assert controller.download('clipA',cancel=threading.Event()).state=='blocked'
    assert controller.status().app_close_allowed

def test_queue_exact_then_cancel_without_resubmit(controller):
    owners=[]
    def owner(lib):owners.append(Boundary(lib));return owners[-1]
    with patch.object(controller,'_owner',side_effect=owner):
        view=background(lambda:controller.download('clipA',cancel=threading.Event()))
        assert view.state=='pending' and view.can_resume and not view.app_close_allowed
        assert owners[0].targets==[view.job_id]
        background(lambda:controller.download('clipA',cancel=threading.Event()))
        assert len(owners)==1
        resumed=background(lambda:controller.resume(cancel=threading.Event()))
        assert resumed.job_id==view.job_id and owners[-1].targets==[view.job_id]
        cancelled=background(controller.cancel_pending)
        assert cancelled.state=='cancelled' and cancelled.app_close_allowed

def test_unsupported_and_precancel_never_submit(controller):
    with patch.object(controller,'_owner',side_effect=AssertionError('must not admit')):
        assert background(lambda:controller.download('long',cancel=threading.Event())).state=='unsupported'
        cancel=threading.Event();cancel.set()
        assert background(lambda:controller.download('clipA',cancel=cancel)).state=='cancelled'

def test_reused_source_does_not_run(controller):
    def owner(lib):return Boundary(lib,reused=True)
    with patch.object(controller,'_owner',side_effect=owner):
        view=background(lambda:controller.download('clipA',cancel=threading.Event()))
    assert view.state=='reused' and view.app_close_allowed and view.job_id is None

def test_guard_only_unknown_retained_then_actual_cleanup(controller):
    actual=ExportExecution({'reuse_clip':'clipA'})
    actual.guards={'authored':object()}
    def stop():actual.guards.clear();actual.closed=True
    with (patch.object(controller,'_owner',side_effect=lambda lib:Boundary(lib,error=ExportOwnershipError('PRIVATE',[actual]))),
          patch.object(actual,'stop',side_effect=stop)):
        view=background(lambda:controller.download('clipA',cancel=threading.Event()))
        assert view.state=='pending' and view.can_cleanup and not view.app_close_allowed
        assert controller._executions==[actual]
        assert 'PRIVATE' not in view.message
        view=background(controller.cleanup)
        # The actual guard is stopped, but the still-queued claim remains explicit.
        assert actual.closed
        assert view.can_resume and not view.app_close_allowed
        background(controller.cancel_pending)
        assert controller.status().app_close_allowed

def test_reuse_guard_unknown_before_job_remains_cleanable(controller):
    actual=ExportExecution({'reuse_clip':'clipA'})
    actual.guards={'authored':object()}
    class ReuseFault(Boundary):
        def submit(self,*args,**kwargs):raise ExportOwnershipError('REUSE_GUARD',[actual])
    def stop():actual.guards.clear();actual.closed=True
    with (patch.object(controller,'_owner',side_effect=lambda lib:ReuseFault(lib)),
          patch.object(actual,'stop',side_effect=stop)):
        view=background(lambda:controller.download('clipA',cancel=threading.Event()))
        assert view.job_id is None and view.can_cleanup and not view.app_close_allowed
        assert not actual.processes
        view=background(controller.cleanup)
    assert actual.closed and view.app_close_allowed

def test_publication_close_fault_never_drops_actual_context(controller):
    publication=SimpleNamespace(closed=False)
    def fail():raise PublicationGuardError(publication)
    publication.close=fail
    with patch.object(controller,'_owner',side_effect=lambda lib:Boundary(lib,error=PublicationGuardError(publication))):
        view=background(lambda:controller.download('clipA',cancel=threading.Event()))
        assert view.can_cleanup and controller._publications[0] is publication
        view=background(controller.cleanup)
        assert view.can_cleanup and not view.app_close_allowed
        publication.close=lambda:setattr(publication,'closed',True)
        background(controller.cleanup)
        assert publication.closed

def test_authorization_uses_selected_account_and_manifest(controller):
    calls=[]
    def authorize(job,cancel,**kwargs):
        calls.append((threading.current_thread(),kwargs));return {'login':'creator'}
    with patch('native.services.twitch_auth.authorize_clip_downloads',side_effect=authorize):
        view=background(lambda:controller.authorize('clipA',object(),cancel=threading.Event()))
    assert view.state=='authorized'
    assert calls[0][0] is not threading.main_thread()
    assert calls[0][1]=={'expected_broadcaster_id':'owner','bound_client_id':'bound'}

def test_runtime_exact_job_passthrough():
    from native.services.durable_twitch_intake import DurableTwitchIntake
    owner=object.__new__(DurableTwitchIntake)
    owner._busy=threading.Lock();owner._execution=owner._publication=None
    targets=[];owner.jobs=SimpleNamespace(claim_next=lambda **kwargs:targets.append(kwargs))
    target=str(uuid.uuid4());owner.owner_id=str(uuid.uuid4())
    assert background(lambda:owner.run_next(job_id=target)) is None
    assert targets==[{'owner_id':owner.owner_id,'kinds':['io'],'job_id':target}]

def test_capability_error_is_retained_before_any_string_conversion(controller):
    class OpaqueError(ExportOwnershipError):
        def __str__(self):raise AssertionError('capability error must not be formatted')
    actual=ExportExecution({'reuse_clip':'clipA'})
    with patch.object(controller,'_owner',side_effect=lambda lib:Boundary(lib,error=OpaqueError('opaque',[actual]))):
        view=background(lambda:controller.download('clipA',cancel=threading.Event()))
    assert view.can_cleanup and controller._executions==[actual]
    actual.stop()

def test_context_exit_exception_retains_publication(controller):
    original=controller.workspace._library
    publication=SimpleNamespace(closed=False,close=lambda:None)
    @contextmanager
    def context():
        with original() as lib:yield lib
        raise PublicationGuardError(publication)
    with (patch.object(controller.workspace,'_library',context),
          patch.object(controller,'_owner',side_effect=lambda lib:Boundary(lib,reused=True))):
        view=background(lambda:controller.download('clipA',cancel=threading.Event()))
    assert view.can_cleanup and not view.app_close_allowed
    assert controller._publications==[publication]
    publication.closed=True

@pytest.mark.parametrize(('state','error','expected'),[
    ('completed',None,'completed'),('cancelled',None,'cancelled'),
    ('failed','AUTH_REQUIRED','authorization_required'),('failed','UNSUPPORTED_SOURCE','unsupported'),
    ('failed','DEADLINE','failed'),('interrupted',None,'interrupted')],ids=['complete','cancel','auth','unsupported','deadline','interrupted'])
def test_runtime_outcome_states(controller,state,error,expected):
    # Detached runtime boundary receipts, not transfer/settlement proof.
    outcome=IntakeOutcome({'id':str(uuid.uuid4()),'state':state},None,None,None,error_code=error)
    def operation():controller._accept(outcome,None)
    view=background(lambda:controller._run(operation))
    assert view.state==expected and view.app_close_allowed
    if state=='interrupted':
        assert not view.can_download and not view.can_authorize
        with patch.object(controller,'_owner',side_effect=AssertionError('must not retry interrupted task')):
            background(lambda:controller.download('clipA',cancel=threading.Event()))
