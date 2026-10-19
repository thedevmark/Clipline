import io
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from native.services import twitch_transfer_worker as worker


class Headers(dict):
    def get_all(self,key,default):
        value=self.get(key)
        return default if value is None else value if isinstance(value,list) else [value]


class Response:
    status=200
    def __init__(self,body,headers=None): self.body=io.BytesIO(body); self.headers=Headers(headers or {})
    def __enter__(self): return self
    def __exit__(self,*args): self.body.close()
    def read(self,n): return self.body.read(n)


def test_one_bounded_copy_no_auth_headers_and_exclusive_target(tmp_path):
    response=Response(b'media',{'Content-Length':'5'})
    opener=Mock(); opener.open.return_value=response
    path=tmp_path/'download.part'
    assert worker.transfer('https://cdn.example.test/media?signed=authored',path,limit=8,deadline=10,clock=lambda:1,opener=opener)==5
    assert path.read_bytes()==b'media' and opener.open.call_count==1
    assert dict(opener.open.call_args.args[0].header_items())=={'Accept-encoding':'identity'}
    opener.open.return_value=Response(b'replacement')
    with pytest.raises(worker.TransferFailure):
        worker.transfer('https://cdn.example.test/media',path,limit=20,deadline=10,clock=lambda:1,opener=opener)
    assert path.read_bytes()==b'media'


@pytest.mark.parametrize('body,headers,limit,code',[
    (b'12345',{},4,'BYTE_LIMIT'),(b'12',{'Content-Length':'3'},4,'TRUNCATED'),
    (b'1234',{'Content-Length':'3'},5,'BYTE_LIMIT'),(b'',{},4,'TRUNCATED'),
    (b'x',{'Content-Length':['1','2']},4,'INVALID_LENGTH'),
    (b'x',{'Content-Length':'1','Transfer-Encoding':'chunked'},4,'INVALID_LENGTH'),
    (b'x',{'Content-Encoding':'gzip'},4,'ENCODING')],ids=['oversize','truncated','overlong','empty','conflicting','chunk-conflict','encoding'])
def test_bounded_length_failures_keep_partial_bytes_without_retry(tmp_path,body,headers,limit,code):
    opener=Mock(); opener.open.return_value=Response(body,headers)
    with pytest.raises(worker.TransferFailure,match=code):
        worker.transfer('https://cdn.example.test/media',tmp_path/'download.part',limit=limit,deadline=10,clock=lambda:1,opener=opener)
    assert opener.open.call_count==1


def test_slow_copy_obeys_deadline_and_retains_partial(tmp_path):
    ticks=iter([1,1,1,11]); opener=Mock(); opener.open.return_value=Response(b'media')
    with pytest.raises(worker.TransferFailure,match='DEADLINE'):
        worker.transfer('https://cdn.example.test/media',tmp_path/'download.part',limit=20,deadline=10,clock=lambda:next(ticks),opener=opener)
    assert opener.open.call_count==1


def test_redirect_has_no_credentials_and_finite_count():
    redirect=worker.BoundedRedirect()
    for _ in range(5):
        request=redirect.redirect_request(None,None,302,'',{},'https://cdn.example.test/path')
        assert dict(request.header_items())=={'Accept-encoding':'identity'}
    with pytest.raises(worker.TransferFailure,match='REDIRECT_LIMIT'):
        redirect.redirect_request(None,None,302,'',{},'https://cdn.example.test/path')
    for value in ('http://example.test/media','https://user:pass@example.test/media','https://example.test/media#fragment'):
        with pytest.raises(worker.TransferFailure): worker.transport_url(value)


def test_top_level_worker_never_prints_provider_exception_or_traceback(monkeypatch):
    sentinel='authored-private-token-and-signed-url'
    def fail(request): raise RuntimeError(sentinel)
    monkeypatch.setattr(worker,'run',fail)
    monkeypatch.setattr(worker.sys,'stdin',type('Input',(),{'buffer':io.BytesIO(b'{}\n')})())
    output=io.StringIO(); monkeypatch.setattr(worker.sys,'stdout',output)
    assert worker.main()==1
    assert json.loads(output.getvalue())=={'event':'result','state':'failed','error_code':'WORKER_FAILED'}
    assert sentinel not in output.getvalue() and 'Traceback' not in output.getvalue()


def test_gate_released_worker_refuses_invalid_live_auth_before_url_or_transfer(tmp_path,monkeypatch):
    from dataclasses import asdict
    from native.core.twitch_inbox import TwitchClip
    from native.services import twitch_auth,twitch_api
    selected=TwitchClip('Clip-One','42','43','','Selected','2026-10-18T12:00:00Z',2_000_000,1,
                        'https://clips.twitch.tv/Clip-One',None,None)
    calls=[]
    def deny(**kwargs):
        calls.append(kwargs)
        raise RuntimeError('authored-private-token-sentinel')
    monkeypatch.setattr(twitch_auth,'validated_intake_connection',deny)
    monkeypatch.setattr(twitch_api,'own_clip_download',lambda *a,**k:pytest.fail('URL resolution happened after auth denial'))
    monkeypatch.setattr(worker,'transfer',lambda *a,**k:pytest.fail('Transfer happened after auth denial'))
    monkeypatch.setattr(worker.sys,'path',list(worker.sys.path))
    request={'schema':1,'code_root':str(tmp_path),'clip':asdict(selected),'target':str(tmp_path/'download.part'),
             'client_id':'client','max_bytes':100,'seconds':5}
    with pytest.raises(worker.TransferFailure,match='AUTH_REQUIRED') as caught: worker.run(request)
    assert 'authored-private-token-sentinel' not in str(caught.value)
    assert calls==[{'expected_broadcaster_id':'42','bound_client_id':'client'}]
    assert not (tmp_path/'download.part').exists()
