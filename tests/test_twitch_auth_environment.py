from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback
from unittest.mock import Mock

import pytest
from native.services import twitch_auth as auth, twitch_api as api
from native.services import native_export_client as process
from native.services.twitch_publication import intake_environment
from tests.test_twitch_publication import clip


@pytest.fixture(autouse=True)
def prevent_live_access(monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError('Live auth/settings/network forbidden')
    for name in ('load_settings','save_settings'):
        monkeypatch.setattr(auth, name, forbidden)
    monkeypatch.setattr(auth.urllib.request, 'urlopen', forbidden)


def valid(**changes):
    return dict(login='owner', user_id='42', client_id='client',
                scopes=[], expires_in=300) | changes


@pytest.mark.parametrize('bad', [None, [], {}, valid(user_id=''), valid(login=' '),
    valid(client_id='other'), valid(scopes='channel:manage:clips'), valid(scopes=[None]),
    valid(expires_in=True), valid(expires_in=0), valid(expires_in=float('inf'))])
def test_failed_finalization_does_not_persist_and_preserves_previous(monkeypatch, bad):
    previous={'access_token':'authored-old-token','user_id':'99','scopes':[]}; original=deepcopy(previous)
    writes=[]
    monkeypatch.setattr(auth,'validate',lambda *args, **kwargs:bad)
    monkeypatch.setattr(auth,'save_session',lambda value:(writes.append(value), previous.update(value)))
    token_payload={'access_token':'authored-private-token','expires_in':100}
    with pytest.raises(RuntimeError) as caught:
        auth._finalize_session(token_payload,expected_client_id='client')
    assert previous==original and writes==[]
    assert 'authored-private-token' not in ''.join(traceback.format_exception(caught.value))


def test_fresh_metadata_login_uses_validated_grants_and_bounded_expiry(monkeypatch):
    writes=[]; monkeypatch.setattr(auth,'validate',lambda *a, **k:valid())
    monkeypatch.setattr(auth,'save_session',writes.append); monkeypatch.setattr(auth.time,'time',lambda:1000)
    session=auth._finalize_session({'access_token':'authored-token','expires_in':900,
                                    'scope':['channel:manage:clips']},expected_client_id='client')
    assert session['scopes']==[] and session['expires_at']==1300 and writes==[session]
    assert auth.DEFAULT_SCOPES==''


@pytest.mark.parametrize('info', [valid(), valid(user_id='99',scopes=['channel:manage:clips']), valid(scopes=[True])])
def test_live_intake_refuses_stored_claims_without_mutating_session(monkeypatch, info):
    monkeypatch.setattr(auth,'current_token',lambda:'authored-private-token')
    monkeypatch.setattr(auth,'validate',lambda *a, **k:info)
    save=Mock(); monkeypatch.setattr(auth,'save_session',save)
    with pytest.raises(RuntimeError):
        auth.validated_intake_connection(expected_broadcaster_id='42',bound_client_id='client')
    save.assert_not_called()


def test_bound_account_and_resolver_keep_one_connection_after_settings_switch(monkeypatch):
    reads=[]
    def token(): reads.append(1); return 'authored-private-token'
    monkeypatch.setattr(auth,'current_token',token)
    monkeypatch.setattr(auth,'validate',lambda *a,**k:valid(scopes=['channel:manage:clips']))
    connection=auth.validated_intake_connection(expected_broadcaster_id='42',bound_client_id='client')
    monkeypatch.setattr(auth,'current_token',lambda:pytest.fail('Connection silently reloaded switched token'))
    monkeypatch.setattr(auth,'client_id',lambda:pytest.fail('Connection silently reloaded switched client'))
    calls=[]
    def get(path,params,pinned):
        assert pinned is connection; calls.append((path,params))
        return {'data':[{'id':'42'}]} if path=='users' else {'data':[{'clip_id':clip().id,
            'landscape_download_url':'https://cdn.example.test/clip.mp4?signature=authored-signed'}]}
    monkeypatch.setattr(api,'_get_bound',get)
    result=api.own_clip_download(clip(),connection=connection)
    assert reads==[1] and [r[0] for r in calls]==['users','clips/downloads']
    assert 'authored-private-token' not in repr(connection) and 'authored-signed' not in repr(result)
    assert 'channel:manage:clips' in connection.scopes


def test_bound_mismatched_or_expired_connection_never_resolves(monkeypatch):
    request=Mock(); monkeypatch.setattr(api,'_get_bound',request)
    for user,expiry in [('99',time.time()+300),('42',0)]:
        connection=auth.ValidatedTwitchConnection('authored-token','client',user,'owner',('channel:manage:clips',),expiry)
        with pytest.raises(api.TwitchAuthError): api.own_clip_download(clip(),connection=connection)
    request.assert_not_called()


def test_incremental_grant_normalizes_without_starting_authorization():
    assert auth.clip_download_scopes(['user:read:email','channel:manage:clips','user:read:email']) == 'channel:manage:clips user:read:email'
    for value in ('user:read:email',[None],['BAD SCOPE']):
        with pytest.raises(RuntimeError): auth.clip_download_scopes(value)


def test_device_flow_preserves_captured_client_and_grant(monkeypatch):
    calls=[]; final=[]
    monkeypatch.setattr(auth,'client_id',lambda:'client')
    def post(url,payload):
        calls.append((url,payload))
        return (200,{'device_code':'dummy','user_code':'code','interval':1,'expires_in':300}) if url==auth.DEVICE_URL else (200,{'access_token':'authored-token'})
    monkeypatch.setattr(auth,'_post_form',post)
    device=auth.request_device_code(auth.clip_download_scopes([]))
    monkeypatch.setattr(auth,'client_id',lambda:'switched-client')
    monkeypatch.setattr(auth,'_finalize_session',lambda data,**kw:final.append(kw) or {'safe':'fixture'})
    class Cancel:
        def is_set(self): return False
        def wait(self,seconds): return False
    auth.poll_token(Mock(),device,Cancel())
    assert calls[0][1]['scopes']=='channel:manage:clips'
    assert calls[1][1]['client_id']=='client' and final==[{'expected_client_id':'client'}]
    assert 'client_id=client' in auth.authorize_url('state','channel:manage:clips',bound_client_id='client')


@pytest.mark.parametrize('environment', [None,{}, {'ONLY':'authored-value','RAYON_NUM_THREADS':'99'}, {'rayon_num_threads':'99','omp_num_threads':'99'}])
@pytest.mark.skipif(os.name != 'nt', reason='The owned process boundary requires Windows Job Objects')
def test_owned_process_environment_default_and_explicit_are_distinct(tmp_path,monkeypatch,environment):
    captured=[]; source=deepcopy(environment)
    monkeypatch.setenv('CLIPLINE_AUTHORED_PARENT_SECRET','authored-parent-secret')
    def launch(*args,**kwargs): captured.append(kwargs['env']); raise OSError('authored launch failure')
    monkeypatch.setattr(process.subprocess,'Popen',launch)
    with pytest.raises(OSError,match='authored launch failure'):
        process.OwnedProcess([str(Path(sys.executable))],tmp_path/'scope',threading.Event(),time.monotonic()+5,environment=environment)
    child=captured[0]
    assert child['RAYON_NUM_THREADS']==child['OMP_NUM_THREADS']=='2'
    assert ('CLIPLINE_AUTHORED_PARENT_SECRET' in child)==(environment is None)
    assert environment==source
    if environment is not None:
        assert set(child)=={k for k in environment if k.upper() not in {'RAYON_NUM_THREADS','OMP_NUM_THREADS'}}|{'RAYON_NUM_THREADS','OMP_NUM_THREADS'}
    for item in (tmp_path/'scope').glob('*.json'):
        assert 'authored-parent-secret' not in item.read_text() and 'authored-value' not in item.read_text()


@pytest.mark.parametrize('environment',[{'BAD=KEY':'x'},{'X':'\0'}, {'X':True}, {'':'x'}])
def test_invalid_environment_fails_before_launch_or_directory(tmp_path,monkeypatch,environment):
    launch=Mock(); monkeypatch.setattr(process.subprocess,'Popen',launch)
    with pytest.raises(process.ExportRuntimeError):
        process.OwnedProcess([],tmp_path/'scope',threading.Event(),time.monotonic()+5,environment=environment)
    launch.assert_not_called(); assert not (tmp_path/'scope').exists()


@pytest.mark.skipif(os.name != 'nt', reason='Physical Windows process environment qualification')
def test_explicit_environment_real_owned_python_child_has_no_parent_secret(tmp_path,monkeypatch):
    monkeypatch.setenv('CLIPLINE_AUTHORED_PARENT_SECRET','authored-parent-secret')
    gate=tmp_path/'gate.py'
    gate.write_text('import json,os,sys\nsys.stdin.readline()\nprint(json.dumps({"secret": "CLIPLINE_AUTHORED_PARENT_SECRET" in os.environ,"rayon": os.environ.get("RAYON_NUM_THREADS"),"omp":os.environ.get("OMP_NUM_THREADS")}))\n',encoding='utf8')
    execution=process.ExportExecution({'test':'explicit environment'})
    child=process.OwnedProcess([sys.executable,'-I','-B',str(gate)],tmp_path/'scope',threading.Event(),time.monotonic()+10,
                               execution=execution,environment=intake_environment(os.environ))
    try:
        child.send(b'go\n'); assert child.wait()==0
    finally:
        proof=execution.stop()
    assert proof['closed'] and proof['scopes'][0]['active_processes']==0
    value=json.loads((tmp_path/'scope'/'stdout.log').read_text())
    assert value=={'secret':False,'rayon':'2','omp':'2'}
    assert 'CLIPLINE_AUTHORED_PARENT_SECRET' not in intake_environment(os.environ)


@pytest.mark.parametrize('body', [b'x'*65537,b'[]',b'{"data":NaN}'],ids=['oversize','array','nonfinite'])
def test_bound_response_limit_and_shape_refuse_without_provider_text(monkeypatch,body):
    reads=[]
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self,n): reads.append(n); return body
    monkeypatch.setattr(auth,'_open_bound_request',lambda request:Response())
    connection=auth.ValidatedTwitchConnection('authored-private-token','client','42','owner',('channel:manage:clips',),time.time()+300)
    with pytest.raises(api.ClipDownloadUnavailable) as caught: api._get_bound('users',{},connection)
    assert reads==[65537] and 'authored-private-token' not in str(caught.value)


def test_bound_network_failure_suppresses_provider_body_and_signed_url(monkeypatch):
    signed='https://cdn.example.test/file?private=authored-signed-url'
    def fail(request): raise RuntimeError(signed)
    monkeypatch.setattr(auth,'_open_bound_request',fail)
    connection=auth.ValidatedTwitchConnection('authored-private-token','client','42','owner',('channel:manage:clips',),time.time()+300)
    with pytest.raises(api.ClipDownloadUnavailable) as caught: api._get_bound('users',{},connection)
    rendered=''.join(traceback.format_exception(caught.value))
    assert signed not in rendered and 'authored-private-token' not in rendered


def test_validation_bound_is_opt_in_and_pinned_auth_never_follows_redirect(monkeypatch):
    reads=[]
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self,*args): reads.append(args); return b'{}'
    monkeypatch.setattr(auth.urllib.request,'urlopen',lambda *a,**k:Response())
    bound=[]
    monkeypatch.setattr(auth,'_open_bound_request',lambda request:bound.append(request) or Response())
    assert auth.validate('authored-token')=={}
    assert reads==[()] and bound==[]
    assert auth.validate('authored-token',max_bytes=65536)=={}
    assert reads[-1]==(65537,) and len(bound)==1
    with pytest.raises(RuntimeError,match='redirect refused'):
        auth._RejectAuthRedirect().redirect_request(None,None,302,'',{},'https://other.example.test')
