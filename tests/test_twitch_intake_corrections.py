"""Focused independent-review corrections; no live auth/network/media."""
import hashlib
import time
import traceback
import urllib.error

import pytest
from native.core.creator_library import LibraryError
from native.services import twitch_auth as auth, twitch_api as api
from native.services.twitch_publication import PublicationGuardError
from tests.test_twitch_publication import sample, publication, finish, db_state, clip


@pytest.mark.parametrize('failed_guard', ['managed','existing'])
def test_partial_close_revokes_publication_before_any_guard_release(sample,tmp_path,failed_guard):
    s=sample; original=tmp_path/'registered.mp4'; original.write_bytes(s['media'].read_bytes())
    s['lib'].register_asset(original)
    pub=publication(s); managed,existing=pub.guards
    class FailedClose:
        def CloseHandle(self,handle):
            # Re-entry during the first close attempt is already forbidden.
            assert pub.publication_revoked
            with pytest.raises(LibraryError): pub.__enter__()
            return False
    failed=managed if failed_guard=='managed' else existing
    released=existing if failed_guard=='managed' else managed
    pub.guards=[(FailedClose(),failed[1]),released]
    try:
        with pytest.raises(PublicationGuardError): pub.close()
        assert len(pub.guards)==1 and not pub.closed and pub.publication_revoked
        changed=original if failed_guard=='managed' else s['media']
        changed.write_bytes(b'changed after partial cleanup')
        before=db_state(s)
        s['jobs'].completed_output_committer=pub.commit
        with pytest.raises(LibraryError): finish(s)
        assert db_state(s)==before
        with pytest.raises(LibraryError): pub.__enter__()
    finally:
        pub.guards=[failed]; pub.close()
    assert pub.closed and pub.publication_revoked
    # Successful cleanup cannot turn this context into a new verification.
    with pytest.raises(LibraryError): pub.__enter__()
    with pytest.raises(LibraryError): finish(s)
    assert db_state(s)==before


@pytest.mark.parametrize('route',['users','clips/downloads'])
@pytest.mark.parametrize('status',[401,403,429,500])
def test_bound_http_classification_is_sanitized_for_both_routes(monkeypatch,route,status):
    private_url='https://cdn.example.test/file?signature=authored-private-url'
    private_token='authored-private-token'
    class Body:
        closed=False
        def read(self,*args): pytest.fail('Provider error body must not be read')
        def close(self): self.closed=True
    body=Body()
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self,n): return b'{"data":[{"id":"42"}]}'
    def request(req):
        if route=='clips/downloads' and '/users?' in req.full_url: return Response()
        raise urllib.error.HTTPError(private_url,status,private_token,{},body)
    monkeypatch.setattr(auth,'_open_bound_request',request)
    monkeypatch.setattr(auth,'current_token',lambda:pytest.fail('Pinned auth unexpectedly reloaded'))
    connection=auth.ValidatedTwitchConnection(private_token,'client','42','owner',('channel:manage:clips',),time.time()+300)
    expected=api.TwitchAuthError if status in (401,403) else api.ClipDownloadUnavailable
    with pytest.raises(expected) as caught: api.own_clip_download(clip(),connection=connection)
    if status in (401,403): assert 'Reconnect Twitch' in str(caught.value)
    rendered=''.join(traceback.format_exception(caught.value))
    assert private_url not in rendered and private_token not in rendered
    assert body.closed
