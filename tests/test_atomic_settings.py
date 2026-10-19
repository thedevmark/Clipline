import json
import os
from unittest.mock import patch

import pytest
from native.services import settings, twitch_auth as auth

@pytest.fixture
def private_settings(tmp_path,monkeypatch):
    path=tmp_path/'settings.json'
    monkeypatch.setattr(settings,'APP_STATE_DIR',tmp_path)
    monkeypatch.setattr(settings,'SETTINGS_FILE',path)
    previous={'theme':'authored','twitch_session':{'access_token':'OLD-PRIVATE','user_id':'owner'}}
    path.write_text(json.dumps(previous,indent=2),encoding='utf8')
    return path,previous,path.read_bytes()

@pytest.mark.parametrize('fault',['serialization','write','flush','replace'],ids=['serialize','write','flush','replace'])
def test_failure_keeps_previous_exact_bytes(private_settings,monkeypatch,fault):
    path,previous,before=private_settings
    value={'theme':'new','twitch_session':{'access_token':'NEW-PRIVATE'}}
    if fault=='serialization':value['bad']=object()
    elif fault=='write':
        def broken(value,stream,**kwargs):stream.write('{"partial":');raise OSError('authored write failure')
        monkeypatch.setattr(settings.json,'dump',broken)
    elif fault=='flush':monkeypatch.setattr(settings.os,'fsync',lambda fd:(_ for _ in ()).throw(OSError('authored flush failure')))
    else:monkeypatch.setattr(settings.os,'replace',lambda *args:(_ for _ in ()).throw(OSError('authored replace failure')))
    with pytest.raises((OSError,TypeError)):settings.save_settings(value)
    assert path.read_bytes()==before
    assert list(path.parent.glob('settings.json.*.tmp'))==[]
    assert settings.load_settings()==previous

def test_success_preserves_settings_and_auth_session(private_settings):
    path,previous,before=private_settings
    session={'access_token':'NEW-PRIVATE','user_id':'owner','scopes':['channel:manage:clips']}
    auth.save_session(session)
    assert settings.load_settings()=={'theme':'authored','twitch_session':session}
    assert path.read_bytes()==json.dumps({'theme':'authored','twitch_session':session},indent=2).replace('\n',os.linesep).encode('utf8')
    assert list(path.parent.glob('settings.json.*.tmp'))==[]

def test_auth_storage_failure_is_sanitized_and_preserves_previous(private_settings,monkeypatch):
    path,previous,before=private_settings
    monkeypatch.setattr(settings.os,'replace',lambda *args:(_ for _ in ()).throw(OSError('NEW-PRIVATE')))
    monkeypatch.setattr(auth,'validate',lambda *args,**kwargs:dict(client_id='bound',user_id='owner',login='creator',
        scopes=['channel:manage:clips'],expires_in=120))
    def login(job,cancel,scopes,**kwargs):
        return auth._finalize_session({'access_token':'NEW-PRIVATE'},expected_client_id=kwargs['bound_client_id'],
            expected_user_id=kwargs['expected_user_id'],required_scopes=kwargs['required_scopes'],cancel=cancel)
    monkeypatch.setattr(auth,'loopback_login',login)
    import threading
    with pytest.raises(RuntimeError) as error:auth.authorize_clip_downloads(object(),threading.Event(),
        expected_broadcaster_id='owner',bound_client_id='bound')
    assert 'PRIVATE' not in str(error.value)
    assert error.value.__suppress_context__
    assert path.read_bytes()==before
