"""Real SQL and Qt; probe/native execution is doubled, never presented as media proof."""
import json,sqlite3
from pathlib import Path
from unittest.mock import patch
import pytest
from native.core.creator_library import CreatorLibrary,LibraryError
from native.services.source_moment_preparation import SourceMomentPreparation
from native.services.local_video_probe import LocalVideoProbe
from native.services.caption_worker.client import ExecutionUncertain
from native.services import native_moment
from native.ui.creator_library import CreatorLibraryWindow
from native.ui.workspace_editor import LayoutSelection
from tests import test_source_moment_preparation as t
from tests.test_source_moment_preparation import app,case


class Probe:
    has_unsettled=False
    def inspect_video(self,asset,directory):return t.facts()


def local(case):
    workspace,service,kit,bg=case
    source=workspace.directory/'My local gameplay.mp4';source.write_bytes(b'Private identity fixture, not real video')
    with CreatorLibrary(workspace.database) as library:asset=library.register_asset(source)
    service.local_probe=Probe()
    context=bg(service.inspect_local,asset.id);choice=bg(service.kit,kit,1)
    return workspace,service,kit,bg,asset,context,choice


def create(service,context,choice,bg,**changes):
    fields=dict(asset_id=context.source.asset.id,expected_source_ticket=context.source.ticket,title='Local last shot',
        source_in_us=250001,source_out_us=1750001,kit_id=choice.kit_id,kit_version=choice.version,
        profile_id=choice.profiles[1][0],scene_key=choice.profiles[1][1],expected_kit_sha256=choice.descriptor_sha256)
    fields.update(changes);return bg(service.create,**fields)


def test_local_moment_native_admission_exact_profile_and_no_twitch_identity(case):
    workspace,service,kit,bg,asset,context,choice=local(case)
    moment=create(service,context,choice,bg)
    with CreatorLibrary(workspace.database) as library:
        origin=library.moment_origin(moment.id)
        assert origin['kind']=='local' and origin['asset_id']==asset.id
        assert origin['source_facts']==t.facts() and origin['profile_id']=='profile-b'
        assert not any(key in origin for key in ('clip_id','job_id','result_id','selected_metadata'))
        assert library.connection.execute('SELECT count(*) FROM durable_job_intents').fetchone()[0]==0
        assert library.connection.execute('SELECT count(*) FROM moment_twitch_origins').fetchone()[0]==0
        value=native_moment.admission(library,moment.id,1,'profile-b','OBS facecam close-up')
        assert value.source==asset and value.kit.version==1
        assert value.moment.source_in_us==250001 and value.moment.source_out_us==1750001
        library.set_overrides(moment.id,{},expected_revision=1)
        assert library.moment_origin(moment.id)==origin
        with pytest.raises(sqlite3.IntegrityError,match='immutable'):
            library.connection.execute('UPDATE moment_local_origins SET scene_key=?',('other',))
    edit=bg(workspace.moment_edit_context,moment.id,2)
    assert edit.selected_layout==('profile-b','OBS facecam close-up')
    page=bg(workspace.page,'moments')
    assert page.entries[0].columns[1].startswith('Local ·') and 'Twitch' not in page.entries[0].details


@pytest.mark.parametrize('change',[
    {'source_out_us':2000002},{'source_in_us':True},{'expected_source_ticket':'f'*64},
    {'expected_kit_sha256':'f'*64},{'scene_key':'foreign'},{'profile_id':'foreign'},
    {'clip_id':'fake-twitch'},{'kit_version':99}],ids=['bound','integer','source-ticket','kit-ticket','scene','profile','both-sources','version'])
def test_local_exact_admission_refuses_without_moment(case,change):
    workspace,service,kit,bg,asset,context,choice=local(case)
    with pytest.raises((LibraryError,ValueError)):create(service,context,choice,bg,**change)
    with CreatorLibrary(workspace.database) as library:assert not library.list_moments()['items']


def test_changed_file_cold_service_and_origin_insert_failure_preserve_catalog(case):
    workspace,service,kit,bg,asset,context,choice=local(case)
    with pytest.raises(LibraryError,match='Inspect this local'):
        create(SourceMomentPreparation(workspace),context,choice,bg)
    with CreatorLibrary(workspace.database) as library:
        library.connection.execute("CREATE TRIGGER refuse_local BEFORE INSERT ON moment_local_origins BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
    with pytest.raises(sqlite3.IntegrityError,match='fixture failure'):create(service,context,choice,bg)
    Path(asset.path).write_bytes(b'changed')
    with pytest.raises(LibraryError):create(service,context,choice,bg)
    with CreatorLibrary(workspace.database) as library:assert not library.list_moments()['items']


def test_local_origin_install_rolls_back_and_preserves_existing_schema4(case):
    workspace,service,kit,bg,asset,context,choice=local(case)
    with CreatorLibrary(workspace.database) as library:
        old=library.create_moment(title='Existing Moment',source_asset_id=asset.id,source_in_us=0,
            source_out_us=100,kit_id=kit,kit_version=1)
        library.connection.execute('DROP TABLE moment_local_origins')
        def authorizer(action,*args):
            return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_CREATE_TRIGGER else sqlite3.SQLITE_OK
        library.connection.set_authorizer(authorizer)
        with pytest.raises(sqlite3.DatabaseError):library._install_local_origin_schema()
        library.connection.set_authorizer(None)
        assert library.connection.execute("SELECT name FROM sqlite_master WHERE name='moment_local_origins'").fetchone() is None
        assert library.moment(old.id)==old and library.connection.execute('PRAGMA user_version').fetchone()[0]==4
        library._install_local_origin_schema()
        assert library.moment_origin(old.id) is None


def probe_payload():
    facts=t.facts();v=facts['video']
    return {'streams':[{'codec_type':'video','codec_name':'h264',**{k:v for k,v in v.items() if k!='codec'}}],
            'format':{'format_name':'mov,mp4','duration':facts['duration']}}


def test_real_probe_owner_profile_and_diagnostic_failure_retention(case,monkeypatch):
    workspace,service,kit,bg,asset,context,choice=local(case)
    binary=workspace.directory/'ffprobe.fixture';binary.write_bytes(b'no executable')
    probe=LocalVideoProbe();probe.ffprobe=str(binary)
    calls=[]
    def tool(argv,label,**fields):calls.append(argv);return json.dumps(probe_payload()).encode()
    monkeypatch.setattr(probe,'_tool',tool)
    assert bg(probe.inspect_video,asset,workspace.directory/'probes')==t.facts()
    assert not probe.has_unsettled and '-format_whitelist' in calls[0] and 'mov' in calls[0]
    from native.services import local_video_probe as module
    with patch.object(module,'write_once',side_effect=OSError('diagnostic full')):
        with pytest.raises(ExecutionUncertain) as caught:bg(probe.inspect_video,asset,workspace.directory/'probes')
    assert caught.value.owner is probe and probe.has_unsettled
    with pytest.raises(ExecutionUncertain):bg(probe.inspect_video,asset,workspace.directory/'probes')
    assert len(calls)==2
    proof=bg(probe.observe_execution)
    assert not probe.has_unsettled and not proof['owner_unsettled'] and probe._source_guard is None


def test_probe_rejects_unsupported_profile_without_guessing_native_clocks(case,monkeypatch):
    workspace,service,kit,bg,asset,context,choice=local(case)
    binary=workspace.directory/'ffprobe.fixture';binary.write_bytes(b'no executable')
    probe=LocalVideoProbe();probe.ffprobe=str(binary)
    payload=probe_payload();payload['streams'][0]['codec_name']='hevc'
    monkeypatch.setattr(probe,'_tool',lambda *a,**k:json.dumps(payload).encode())
    with pytest.raises(LibraryError,match='H.264 MP4'):bg(probe.inspect_video,asset,workspace.directory/'probes')
    assert not probe.has_unsettled


def test_missing_probe_has_actionable_error_without_launch(case):
    workspace,service,kit,bg,asset,context,choice=local(case)
    probe=LocalVideoProbe();probe.ffprobe='ffprobe'
    with pytest.raises(LibraryError,match='FFprobe is unavailable'):
        bg(probe.inspect_video,asset,workspace.directory/'probes')
    assert not probe.has_unsettled


def test_shared_files_prepare_save_and_existing_open_edit(app,case,monkeypatch):
    workspace,service,kit,bg,asset,context,choice=local(case)
    monkeypatch.setattr('native.services.local_video_probe.LocalVideoProbe',Probe)
    opened=[]
    window=CreatorLibraryWindow(workspace.directory,open_moment=lambda *values:opened.append(values))
    window.show();t.wait(app,lambda:not window.runner._jobs)
    window.load_view('assets');t.wait(app,lambda:not window.runner._jobs)
    row=next(i for i,e in enumerate(window.model.entries) if e.id==asset.id)
    window.table.selectRow(row)
    binding=window.moment_preparation_binding
    assert binding.prepare.isVisible() and binding.prepare.isEnabled()
    binding.prepare.click()
    t.wait(app,lambda:not window.runner._jobs and binding.dialog is not None and not binding.dialog.busy)
    dialog=binding.dialog
    assert 'Local shared video' in dialog.source_label.text() and 'Twitch' not in dialog.source_label.text()
    assert dialog.title.text()=='My local gameplay'
    dialog.versions.setCurrentIndex(next(i for i in range(dialog.versions.count()) if dialog.versions.itemData(i)==1))
    t.wait(app,lambda:not window.runner._jobs and not dialog.busy)
    dialog.scenes.setCurrentIndex(1);dialog.start.setValue(.250001);dialog.end.setValue(1.750001)
    dialog.save_button.click();t.wait(app,lambda:not window.runner._jobs and binding.dialog is None)
    assert window.kind=='moments' and window._action_owner is None
    moment=window.selected_entry()
    edit=bg(workspace.moment_edit_context,moment.id,1)
    layout=LayoutSelection(edit)
    assert layout.selected_layout()==('profile-b','OBS facecam close-up');layout.reject()
    window.open.click();assert opened==[(moment.id,1)]
    window.close()


def test_unknown_probe_retains_shared_gate_until_explicit_check(app,case,monkeypatch):
    workspace,service,kit,bg,asset,context,choice=local(case)
    class UncertainProbe(Probe):
        def inspect_video(self,*args):
            self.has_unsettled=True
            raise ExecutionUncertain('owned fixture remains unresolved',self,None)
        def observe_execution(self,**kwargs):
            self.has_unsettled=False
            return {'resources_released':True}
    monkeypatch.setattr('native.services.local_video_probe.LocalVideoProbe',UncertainProbe)
    window=CreatorLibraryWindow(workspace.directory);window.show();t.wait(app,lambda:not window.runner._jobs)
    window.load_view('assets');t.wait(app,lambda:not window.runner._jobs)
    window.table.selectRow(next(i for i,e in enumerate(window.model.entries) if e.id==asset.id))
    binding=window.moment_preparation_binding;binding.prepare.click()
    t.wait(app,lambda:not window.runner._jobs)
    assert window._action_owner is binding and binding.service.has_unsettled
    assert not binding.prepare.isEnabled() and binding.check.isVisible()
    window.close();assert window.isVisible()
    binding.check.click();t.wait(app,lambda:not window.runner._jobs)
    assert window._action_owner is None and not binding.service.has_unsettled
    window.close()
