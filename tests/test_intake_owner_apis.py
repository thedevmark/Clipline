import threading
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from native.core.creator_library import CreatorLibrary
from native.core.durable_jobs import DurableJobs, JobError
from native.services import twitch_auth as auth

def uid(): return str(uuid.uuid4())

def submit(lib):
    return lib.jobs.submit('io',{'request_ref':{'id':uid(),'sha256':'a'*64}},idempotency_key=uid())

def test_selected_job_does_not_claim_older(tmp_path):
    with CreatorLibrary(tmp_path/'library.sqlite') as lib:
        a,b=submit(lib),submit(lib)
        selected=lib.jobs.claim_next(owner_id=uid(),kinds=['io'],job_id=b['id'])
        assert selected['id']==b['id']
        assert lib.jobs.job(a['id'])['state']=='queued'
        assert lib.jobs.claim_next(owner_id=uid(),job_id=b['id']) is None
        assert lib.jobs.claim_next(owner_id=uid())['id']==a['id']

def test_invalid_target_and_no_resource_do_not_claim_another(tmp_path):
    with CreatorLibrary(tmp_path/'library.sqlite') as lib:
        a,b,c=submit(lib),submit(lib),submit(lib)
        lib.jobs.claim_next(owner_id=uid(),job_id=a['id'])
        lib.jobs.claim_next(owner_id=uid(),job_id=b['id'])
        before=lib.jobs.resources()
        assert lib.jobs.claim_next(owner_id=uid(),job_id=c['id']) is None
        assert lib.jobs.claim_next(owner_id=uid(),job_id=uid()) is None
        with pytest.raises(JobError):lib.jobs.claim_next(owner_id=uid(),job_id='bad')
        assert lib.jobs.resources()==before
        assert lib.jobs.job(c['id'])['state']=='queued'

def test_concurrent_target_claim(tmp_path):
    db=tmp_path/'library.sqlite'
    with CreatorLibrary(db) as lib:
        older,target=submit(lib),submit(lib)
    barrier=threading.Barrier(2); results=[]; errors=[]
    def claim():
        try:
            with CreatorLibrary(db) as lib:
                barrier.wait(5)
                results.append(lib.jobs.claim_next(owner_id=uid(),job_id=target['id']))
        except BaseException as error:errors.append(type(error).__name__)
    threads=[threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:thread.start()
    for thread in threads:thread.join(10);assert not thread.is_alive()
    assert not errors
    assert sum(item is not None for item in results)==1
    with CreatorLibrary(db) as lib:assert lib.jobs.job(older['id'])['state']=='queued'

def summary(**changes):
    value=dict(client_id='bound',user_id='owner',login='creator',scopes=['channel:manage:clips'],expires_in=120)
    value.update(changes);return value

@pytest.mark.parametrize('changes',[
    {'client_id':'other'},{'user_id':'other'},{'scopes':[]},{'scopes':['bad scope']}
],ids=['client','user','grant','scope'])
def test_finalize_refuses_wrong_identity_without_save(changes):
    with patch.object(auth,'validate',return_value=summary(**changes)),patch.object(auth,'save_session') as save:
        with pytest.raises(RuntimeError) as error:
            auth._finalize_session({'access_token':'PRIVATE'},expected_client_id='bound',
                expected_user_id='owner',required_scopes=('channel:manage:clips',))
        assert 'PRIVATE' not in str(error.value)
        save.assert_not_called()

def test_finalize_cancel_and_valid_commit_cutoff():
    cancel=threading.Event();cancel.set()
    with patch.object(auth,'validate',return_value=summary()),patch.object(auth,'save_session') as save:
        with pytest.raises(RuntimeError):auth._finalize_session({'access_token':'PRIVATE'},
            expected_client_id='bound',expected_user_id='owner',required_scopes=('channel:manage:clips',),cancel=cancel)
        save.assert_not_called()
        cancel.clear()
        result=auth._finalize_session({'access_token':'PRIVATE'},expected_client_id='bound',expected_user_id='owner',
            required_scopes=('channel:manage:clips',),cancel=cancel)
        save.assert_called_once_with(result)

def test_incremental_request_preserves_recorded_names_and_captured_client():
    previous={'access_token':'PRIVATE','user_id':'owner','scopes':['chat:read','chat:read']}
    fresh={'access_token':'NEWPRIVATE','user_id':'owner','login':'creator','scopes':['channel:manage:clips','chat:read']}
    cancel=threading.Event()
    with (patch.object(auth,'load_session',return_value=previous),
          patch.object(auth,'client_id',side_effect=AssertionError('unbound')),
          patch.object(auth,'loopback_login',return_value=fresh) as login):
        result=auth.authorize_clip_downloads(object(),cancel,expected_broadcaster_id='owner',bound_client_id='bound')
        assert set(login.call_args.args[2].split())=={'chat:read','channel:manage:clips'}
        assert login.call_args.kwargs=={'bound_client_id':'bound','expected_user_id':'owner','required_scopes':('channel:manage:clips',)}
        assert 'PRIVATE' not in repr(result)
        assert auth.DEFAULT_SCOPES==''

@pytest.mark.parametrize('recorded',[['bad scope'],['chat:read']],ids=['invalid','other-account'])
def test_untrusted_recorded_scopes_are_not_permission_proof(recorded):
    previous={'access_token':'PRIVATE','user_id':'other','scopes':recorded}
    with patch.object(auth,'load_session',return_value=previous),patch.object(auth,'loopback_login',side_effect=RuntimeError('PRIVATE BODY')) as login:
        with pytest.raises(RuntimeError) as error:auth.authorize_clip_downloads(object(),threading.Event(),
            expected_broadcaster_id='owner',bound_client_id='bound')
        assert login.call_args.args[2]=='channel:manage:clips'
        assert 'PRIVATE' not in str(error.value)
        assert error.value.__suppress_context__

def test_loopback_uses_bound_client_and_expected_user_without_live_server():
    calls={}
    class Server:
        def __init__(self,address,handler):self.handler=handler;self.timeout=None
        def handle_request(self):
            handler=object.__new__(self.handler)
            handler.path='/capture?state=authored&access_token=PRIVATE&expires_in=120'
            handler._send=lambda body:None
            handler.do_GET()
        def server_close(self):calls['closed']=True
    progress=SimpleNamespace(emit=lambda message:None)
    cancel=threading.Event()
    with (patch.object(auth.http.server,'HTTPServer',Server),
          patch.object(auth.secrets,'token_urlsafe',return_value='authored'),
          patch.object(auth.webbrowser,'open',side_effect=lambda url:calls.update(url=url)),
          patch.object(auth,'client_id',side_effect=AssertionError('must keep captured client')),
          patch.object(auth,'_finalize_session',return_value={'safe':True}) as finalize):
        auth.loopback_login(SimpleNamespace(progress=progress),cancel,'channel:manage:clips',
            bound_client_id='bound',expected_user_id='owner',required_scopes=('channel:manage:clips',))
    assert 'client_id=bound' in calls['url'] and 'PRIVATE' not in calls['url']
    assert calls['closed']
    assert finalize.call_args.kwargs=={'expected_client_id':'bound','expected_user_id':'owner',
        'required_scopes':('channel:manage:clips',),'cancel':cancel}
