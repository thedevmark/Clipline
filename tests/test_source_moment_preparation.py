"""Real SQL/Qt checks around explicitly synthetic completed intake artifacts.

No fixture is media, no transfer/probe runs, and no synthetic attestation proves
real process cessation. The existing trusted intake execution is the test boundary.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
import json
import hashlib
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from unittest.mock import patch

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox

import native.core.creator_library as core
from native.core.creator_library import CreatorLibrary, LibraryError
from native.core.durable_jobs import DurableJobs
from native.core.twitch_inbox import TwitchClip
from native.services.caption_worker.evidence import canonical,digest,hash_file
from native.services.source_moment_preparation import SourceMomentPreparation
from native.services.twitch_intake_client import PROFILE,classify_probe
from native.services.workspace_library import WorkspaceLibrary
from native.ui.creator_library import CreatorLibraryWindow
from native.ui.source_moment_preparation import MomentPreparationDialog
from native.ui.workspace_editor import LayoutSelection
from native.workers import JobRunner



def uid(): return str(uuid.uuid4())


def clip(identity='Clip-A'):
    return TwitchClip(identity,'owner','creator','','Selected '+identity,'2026-10-18T12:00:00Z',
                      99_000_000,5,'https://clips.twitch.tv/'+identity,'VOD-identity',123_000_000)


def profile(identity='profile-a',scene='OBS gameplay'):
    return {'id':identity,'scene_key':scene,'source_width':1920,'source_height':1080,
            'regions':{'gameplay':{'x':0,'y':0,'w':1,'h':1},'facecam':{'x':.05,'y':.05,'w':.2,'h':.2}}}


def facts():
    return classify_probe({'streams':[{'index':0,'codec_type':'video','codec_name':'h264','pix_fmt':'yuv420p',
        'width':1920,'height':1080,'avg_frame_rate':'60/1','start_time':'0.000000',
        'sample_aspect_ratio':'1:1','time_base':'1/15360'}],
        'format':{'format_name':'mov,mp4','duration':'2.000001'}})


def artifact(workspace,kind,key,value):
    path=workspace.directory/'source-intake'/kind/(key+'.json')
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(canonical(value))
    return {'id':key,'sha256':digest(value)}


def seed_source(workspace,identity='Clip-A',mutate_result=None,mutate_request=None):
    """The trusted runtime is doubled; admission/settlement and source SQL are real."""
    selected=clip(identity)
    with CreatorLibrary(workspace.database) as library:
        library.record_twitch_clips([selected])
        request={'schema':1,'clip':asdict(selected),'profile':PROFILE,'runtime':{'fixture':'no-programs'}}
        if mutate_request: mutate_request(request)
        request_ref=artifact(workspace,'requests',uid(),request)
        job=library.jobs.submit('io',{'request_ref':request_ref,'profile_sha256':digest(PROFILE)},idempotency_key=uid())
        job=library.jobs.claim_next(owner_id=uid(),kinds=['io'],job_id=job['id'])
        launch=artifact(workspace,'launches',job['claim_token'],{'fixture':'no-programs'})
        job=library.jobs.record_launch_intent(job['id'],expected_revision=job['revision'],claim_token=job['claim_token'],launch_ref=launch)
        media=workspace.directory/'source-intake'/'media'/job['output_set_id']/'source.mp4'
        media.parent.mkdir(parents=True)
        media.write_bytes(b'Synthetic identity fixture; never decoded or presented as media')
        execution={'database':str(workspace.database.resolve()),'job_id':job['id'],'claim_token':job['claim_token'],
                   'intent_sha256':job['intent_sha256'],'output_set_id':job['output_set_id']}
        result={'schema':1,'job_id':job['id'],'claim_token':job['claim_token'],'intent_sha256':job['intent_sha256'],
            'output_set_id':job['output_set_id'],'selected':asdict(selected),'profile_sha256':digest(PROFILE),
            'runtime':request['runtime'],'media':{'path':str(media),'sha256':hash_file(media),'bytes':media.stat().st_size},
            'source_facts':facts(),'scope_proof':{'owner':digest(execution),'binding':execution,
                'scopes':[{'disposition':'stopped','active_processes':0,'streams_closed':True} for _ in range(2)],
                'closed':True,'processes_stopped':True,'guards_released':True,'guarded_paths':[],
                'guard_faults':[],'cleanup_errors':[]},'candidate_verified':True}
        if mutate_result: mutate_result(result)
        reference=artifact(workspace,'results',job['output_set_id'],result)
        proof={'job_id':job['id'],'claim_token':job['claim_token'],'intent_sha256':job['intent_sha256'],
            'output_set_id':job['output_set_id'],'process_disposition':'stopped','outputs_disposition':'verified',
            'result_ref':reference,'evidence_ref':reference}
        row=library.connection.execute('SELECT * FROM assets WHERE sha256=?',(hash_file(media),)).fetchone()
        plan={'job':job,'clip':asdict(selected),'profile_sha256':digest(PROFILE),'result_ref':reference,
            'media':{'id':uid(),'sha256':hash_file(media),'path':str(media),'path_key':os.path.normcase(str(media)),
                     'bytes':media.stat().st_size},'existing':dict(row) if row is not None else None}
        jobs=DurableJobs(library.connection,settlement_verifier=lambda current,evidence:proof,
            completed_output_committer=lambda connection,current,attestation:
                library._commit_twitch_source(connection,current,attestation,plan))
        jobs.finish(job['id'],expected_revision=job['revision'],claim_token=job['claim_token'],
                    outcome='completed',evidence_ref=reference)
        return library.twitch_source(identity),result


@pytest.fixture(scope='session')
def app():
    application=QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    assert QFontDatabase.addApplicationFont('C:/Windows/Fonts/segoeui.ttf')>=0
    return application


@pytest.fixture
def case(tmp_path):
    workspace=WorkspaceLibrary(tmp_path)
    template=tmp_path/'Fixture template.fcproj'
    template.write_bytes(b'Synthetic template identity; no engine qualification')
    with CreatorLibrary(workspace.database) as library:
        asset=library.register_asset(template)
        descriptor={'name':'Fixture Kit','asset_refs':{'native_template':asset.id},'properties':{},
            'layout_profiles':[profile(),profile('profile-b','OBS facecam close-up')],'vocabulary':['deutschmark']}
        kit=library.create_kit(descriptor)
        library.revise_kit(kit,{**descriptor,'name':'Fixture Kit revised'},expected_version=1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        def bg(fn,*args,**kwargs):return pool.submit(fn,*args,**kwargs).result(timeout=10)
        yield workspace,SourceMomentPreparation(workspace),kit,bg


def create(service,context,choice,bg,**changes):
    arguments=dict(clip_id=context.source.clip.id,expected_source_ticket=context.source.ticket,
        title='Authored Moment',source_in_us=250001,source_out_us=1_750_001,
        kit_id=choice.kit_id,kit_version=choice.version,expected_kit_sha256=choice.descriptor_sha256,
        profile_id=choice.profiles[1][0],scene_key=choice.profiles[1][1])
    arguments.update(changes)
    return bg(service.create,**arguments)


def wait(app,condition):
    end=time.monotonic()+10
    while not condition():
        app.processEvents()
        if time.monotonic()>end:raise AssertionError('Qt operation did not reach its expected state')
        time.sleep(.005)
    app.processEvents()


def test_same_asset_has_two_independent_moment_origins(case):
    workspace,service,kit,bg=case
    first,_=seed_source(workspace)
    second,_=seed_source(workspace,'Clip-B')
    assert first['asset_id']==second['asset_id']
    choice=bg(service.kit,kit,1)
    contexts=[bg(service.inspect,identity) for identity in ('Clip-A','Clip-B')]
    moments=[create(service,context,choice,bg) for context in contexts]
    assert [context.source.duration_us for context in contexts]==[2_000_001,2_000_001]
    assert [context.source.clip.duration_us for context in contexts]==[99_000_000,99_000_000]
    with CreatorLibrary(workspace.database) as library:
        origins=[library.moment_origin(moment.id) for moment in moments]
        assert [origin['clip_id'] for origin in origins]==['Clip-A','Clip-B']
        assert origins[0]['result_id']!=origins[1]['result_id']
        assert origins[0]['selected_metadata']==asdict(clip())
        assert origins[0]['source_facts']==facts()
        assert all(origin['kit_version']==1 and origin['profile_id']=='profile-b' for origin in origins)
        assert all(moment.kit_version==1 and moment.source_in_us==250001 for moment in moments)
    page=bg(workspace.page,'moments')
    assert 'Clip-A' in page.entries[0].columns[1] and 'Clip-B' in page.entries[1].details
    edit=bg(workspace.moment_edit_context,moments[0].id,moments[0].revision)
    assert edit.selected_layout==('profile-b','OBS facecam close-up')


def test_origin_survives_metadata_revisions_and_cache_refresh(case):
    workspace,service,kit,bg=case
    seed_source(workspace)
    context=bg(service.inspect,'Clip-A');choice=bg(service.kit,kit,1)
    moment=create(service,context,choice,bg)
    with CreatorLibrary(workspace.database) as library:
        original=library.moment_origin(moment.id)
        updated=library.set_overrides(moment.id,{},expected_revision=1)
        library.apply_kit_update(moment.id,2,expected_revision=updated.revision)
        library.record_twitch_clips([replace(clip(),title='Refreshed provider title',duration_us=10_000_000)])
        assert library.moment_origin(moment.id)==original
        assert library.moment(moment.id).kit_version==2
        for statement in ('UPDATE moment_twitch_origins SET scene_key=? WHERE moment_id=?',
                          'DELETE FROM moment_twitch_origins WHERE scene_key=? AND moment_id=?'):
            with pytest.raises(sqlite3.IntegrityError,match='immutable'):
                library.connection.execute(statement,('OBS facecam close-up',moment.id))
    assert bg(service.inspect,'Clip-A').source.clip.title=='Selected Clip-A'
    assert bg(workspace.moment_edit_context,moment.id,3).selected_layout is None


@pytest.mark.parametrize('fault',['job','claim','selected','profile','source','facts','scope','request'],ids=['job','claim','clip','profile','source','facts','scope','request'])
def test_self_consistent_but_wrong_result_binding_is_refused(case,fault):
    workspace,service,kit,bg=case
    def mutate(result):
        if fault=='job':result['job_id']=uid()
        elif fault=='claim':result['claim_token']=uid()
        elif fault=='selected':result['selected']=asdict(clip('Other-clip'))
        elif fault=='profile':result['profile_sha256']='a'*64
        elif fault=='source':result['media']['sha256']='b'*64
        elif fault=='facts':result['source_facts']['native_compatibility']=True
        elif fault=='scope':result['scope_proof']['scopes'][0]['active_processes']=1
    request_mutator=(lambda request:request.update(clip=asdict(clip('Other-clip')))) if fault=='request' else None
    seed_source(workspace,mutate_result=mutate,mutate_request=request_mutator)
    with pytest.raises((LibraryError,ValueError)):
        bg(service.inspect,'Clip-A')
    with CreatorLibrary(workspace.database) as library:assert library.list_moments()['items']==[]


@pytest.mark.parametrize('fault',['missing','tampered','oversized','source','job-state'],ids=['missing','tampered','oversized','source','job-state'])
def test_missing_changed_artifacts_source_and_incomplete_job_are_refused(case,fault):
    workspace,service,kit,bg=case
    binding,result=seed_source(workspace)
    path=workspace.directory/'source-intake'/'results'/(binding['result_id']+'.json')
    if fault=='missing':path.unlink()
    elif fault=='tampered':
        result['source_facts']['duration']='4'
        path.write_bytes(canonical(result))
    elif fault=='oversized':path.write_bytes(b' '*65537)
    elif fault=='source':Path(result['media']['path']).write_bytes(b'changed admitted source')
    else:
        with CreatorLibrary(workspace.database) as library:
            library.connection.execute("UPDATE durable_job_heads SET state='running' WHERE job_id=?",(binding['job_id'],))
    with pytest.raises((LibraryError,ValueError)):
        bg(service.inspect,'Clip-A')


@pytest.mark.parametrize('changes',[
    {'source_in_us':-1},{'source_in_us':10,'source_out_us':10},{'source_out_us':2_000_002},
    {'source_in_us':True},{'source_out_us':1.5},{'scene_key':'Wrong OBS scene'},
    {'profile_id':'no-such-profile'},{'expected_kit_sha256':'f'*64},{'kit_version':99},
    {'expected_source_ticket':'f'*64},{'title':' '}],
    ids=['negative','empty','past-end','bool','float','scene','profile','kit-hash','version','source-ticket','title'])
def test_range_and_exact_pins_refuse_without_a_moment(case,changes):
    workspace,service,kit,bg=case
    seed_source(workspace)
    context=bg(service.inspect,'Clip-A');choice=bg(service.kit,kit,1)
    with pytest.raises((LibraryError,ValueError)):
        create(service,context,choice,bg,**changes)
    with CreatorLibrary(workspace.database) as library:
        assert library.list_moments()['items']==[]
        assert library.connection.execute('SELECT count(*) FROM moment_twitch_origins').fetchone()[0]==0


def test_origin_insert_failure_rolls_back_moment_and_hashes_are_background(case):
    workspace,service,kit,bg=case
    seed_source(workspace)
    context=bg(service.inspect,'Clip-A');choice=bg(service.kit,kit,1)
    with CreatorLibrary(workspace.database) as library:
        library.connection.execute("CREATE TRIGGER fixture_reject_origin BEFORE INSERT ON moment_twitch_origins BEGIN SELECT RAISE(ABORT,'fixture origin failure'); END")
    original=CreatorLibrary.verify_asset
    calls=[]
    def verify(library,asset_id):
        assert threading.current_thread() is not threading.main_thread()
        assert not library.connection.in_transaction
        calls.append(asset_id)
        return original(library,asset_id)
    with patch.object(CreatorLibrary,'verify_asset',verify),pytest.raises(sqlite3.IntegrityError,match='fixture origin failure'):
        create(service,context,choice,bg)
    assert len(calls)==2
    with CreatorLibrary(workspace.database) as library:assert library.list_moments()['items']==[]
    with pytest.raises(RuntimeError,match='background owner'):service.inspect('Clip-A')


def seed_schema3_fixture(path, source, *, properties=None, overrides=None, revision=1, second_kit_name=None):
    """Build the pre-origin schema directly, without importing an old product owner.

    The fixture represents a schema3 catalog with one Asset, one Kit, and one
    pinned Moment. Optional values reproduce the independent revision-history case.
    It contains no origin table and is not a copy of a user's database.
    """
    schema = (Path(__file__).parent / 'fixtures/creator-library-v3.sql').read_text(encoding='utf8')
    asset_id, kit_id, moment_id = uid(), uid(), uid()
    descriptor = {'name':'Old Kit','asset_refs':{},'properties':properties or {},'layout_profiles':[],'vocabulary':[]}
    with sqlite3.connect(path) as database:
        database.executescript(schema)
        database.execute('INSERT INTO assets VALUES(?,?,?,?,?)', (asset_id,hashlib.sha256(source.read_bytes()).hexdigest(),
            str(source.resolve()),os.path.normcase(str(source.resolve())),source.stat().st_size))
        database.execute('INSERT INTO kits VALUES(?,?)',(kit_id,2 if second_kit_name else 1))
        database.execute('INSERT INTO kit_versions VALUES(?,?,?)',(kit_id,1,canonical(descriptor).decode('utf8')))
        if second_kit_name:
            database.execute('INSERT INTO kit_versions VALUES(?,?,?)',(kit_id,2,canonical({**descriptor,'name':second_kit_name}).decode('utf8')))
        database.execute('INSERT INTO moments(id,title,source_asset_id,source_in_us,source_out_us,kit_id,kit_version,overrides,revision) VALUES(?,?,?,?,?,?,?,?,?)',
            (moment_id,'Old Moment',asset_id,0,100,kit_id,1,canonical(overrides or {}).decode('utf8'),revision))
        rows={table:[tuple(row) for row in database.execute('SELECT * FROM '+table)]
              for table in ('moments','kit_versions','kits','assets')}
        assert database.execute('PRAGMA user_version').fetchone()[0]==3
    return moment_id,rows


@pytest.mark.parametrize('rollback',[False,True],ids=['migrate','rollback'])
def test_real_schema3_migration_preserves_old_rows_and_is_atomic(tmp_path,rollback):
    path=tmp_path/'old.sqlite'
    source=tmp_path/'old-source.fixture';source.write_bytes(b'old source identity')
    moment_id,rows=seed_schema3_fixture(path,source)
    if rollback:
        with patch.object(core,'_MOMENT_ORIGIN_SCHEMA',core._MOMENT_ORIGIN_SCHEMA.replace('PRAGMA user_version=4;','SELECT missing_fixture_column;\nPRAGMA user_version=4;')):
            with pytest.raises(sqlite3.OperationalError):CreatorLibrary(path)
        with sqlite3.connect(path) as database:
            assert database.execute('PRAGMA user_version').fetchone()[0]==3
            assert database.execute("SELECT name FROM sqlite_master WHERE name='moment_twitch_origins'").fetchone() is None
            assert database.execute('SELECT title FROM moments').fetchone()[0]=='Old Moment'
    with CreatorLibrary(path) as library:
        assert library.connection.execute('PRAGMA user_version').fetchone()[0]==4
        assert library.moment_origin(moment_id) is None
        assert library.moment(moment_id).revision==1
        for table,original in rows.items():
            assert [tuple(row) for row in library.connection.execute('SELECT * FROM '+table)]==original


def test_ui_prepare_select_exact_old_version_profile_and_focus_new_moment(app,case):
    workspace,service,kit,bg=case
    seed_source(workspace);seed_source(workspace,'Clip-B')
    with CreatorLibrary(workspace.database) as library:
        source=library.asset(library.twitch_source('Clip-A')['asset_id'])
        for number in range(55):
            library.create_moment(title=f'Old Moment {number}',source_asset_id=source.id,source_in_us=0,
                                  source_out_us=100,kit_id=kit,kit_version=1)
    opened=[]
    window=CreatorLibraryWindow(workspace.directory,open_moment=lambda *values:opened.append(values))
    window.show()
    wait(app,lambda:not window.runner._jobs)
    window.table.selectRow(1)
    assert window.selected_entry().id=='Clip-B'
    binding=window.moment_preparation_binding
    other=object()
    assert window.acquire_action(other)
    assert not binding.prepare.isEnabled()
    binding._prepare()
    assert binding.dialog is None
    window.release_action(other)
    binding.prepare.click()
    assert window._action_owner is binding
    assert not window.acquire_action(other)
    wait(app,lambda:not window.runner._jobs and binding.dialog is not None and not binding.dialog.busy)
    dialog=binding.dialog
    assert dialog.end.value()==pytest.approx(2.000001)
    assert 'Clip-B' in dialog.source_label.text()
    assert dialog.choice.version==2
    for index in range(dialog.versions.count()):
        if dialog.versions.itemData(index)==1:dialog.versions.setCurrentIndex(index);break
    wait(app,lambda:not window.runner._jobs and not dialog.busy)
    assert dialog.choice.version==1
    dialog.scenes.setCurrentIndex(1)
    dialog.title.setText('Chosen source Moment')
    dialog.start.setValue(.250001);dialog.end.setValue(1.750001)
    QTest.keyClick(dialog.title,Qt.Key.Key_Tab)
    assert dialog.focusWidget() is not None
    dialog.save_button.click()
    dialog.reject()
    assert dialog.isVisible()
    wait(app,lambda:not window.runner._jobs and binding.dialog is None)
    assert window.kind=='moments'
    selected=window.selected_entry()
    assert selected is not None and selected.columns[0]=='Chosen source Moment'
    assert opened==[]
    assert 'Clip-B' in window.details.toPlainText()
    assert window._action_owner is None
    with CreatorLibrary(workspace.database) as library:
        moment=library.moment(selected.id)
        assert (moment.source_in_us,moment.source_out_us,moment.kit_version)==(250001,1750001,1)
        origin=library.moment_origin(moment.id)
        assert origin['clip_id']=='Clip-B' and origin['profile_id']=='profile-b'
        assert len(library.list_moments(limit=100)['items'])==56
    edit=bg(workspace.moment_edit_context,selected.id,1)
    layout=LayoutSelection(edit)
    assert layout.selected_layout()==('profile-b','OBS facecam close-up')
    layout.reject()
    window.open.click()
    assert opened==[(selected.id,1)]
    window.close()


def test_ui_no_source_cancel_failed_save_and_unsupported_kit_keep_intent(app,case):
    workspace,service,kit,bg=case
    with CreatorLibrary(workspace.database) as library:library.record_twitch_clips([clip()])
    window=CreatorLibraryWindow(workspace.directory)
    window.show();wait(app,lambda:not window.runner._jobs)
    window.table.selectRow(0)
    binding=window.moment_preparation_binding
    binding.prepare.click()
    wait(app,lambda:not window.runner._jobs and not binding.active)
    assert binding.dialog is None and 'Download and admit' in window.message.text()
    seed_source(workspace)
    binding.prepare.click()
    wait(app,lambda:not window.runner._jobs and binding.dialog is not None and not binding.dialog.busy)
    dialog=binding.dialog
    dialog.title.setText('Preserved title')
    dialog.start.setValue(1);dialog.end.setValue(.5)
    dialog._save()
    assert not dialog.busy and 'end after' in dialog.message.text()
    dialog.end.setValue(1.5)
    with CreatorLibrary(workspace.database) as library:
        source=library.asset(library.twitch_source('Clip-A')['asset_id'])
    Path(source.path).write_bytes(b'Changed source bytes after inspection')
    dialog._save()
    wait(app,lambda:not window.runner._jobs and not dialog.busy)
    assert dialog.isVisible() and dialog.title.text()=='Preserved title'
    assert dialog.start.value()==1 and dialog.end.value()==1.5
    dialog.reject();app.processEvents()
    assert window._action_owner is None
    with CreatorLibrary(workspace.database) as library:
        assert library.list_moments()['items']==[]
        descriptor=library.kit(kit,2)
        library.revise_kit(kit,{**descriptor,'properties':{'caption.foreign':True}},expected_version=2)
    refusal=bg(service.kit,kit,3)
    assert refusal.refusal
    window.close()


@pytest.mark.parametrize('width,height',[(760,600),(1180,780)],ids=['760','1180'])
def test_ui_narrow_wide_render_and_cancel_are_bounded(app,case,width,height):
    workspace,service,kit,bg=case
    seed_source(workspace)
    context=bg(service.inspect,'Clip-A')
    runner=JobRunner()
    dialog=MomentPreparationDialog(context,service,runner)
    dialog.resize(width,height);dialog.show()
    wait(app,lambda:not runner._jobs and not dialog.busy)
    assert dialog.scroll.horizontalScrollBar().maximum()==0
    assert dialog.buttons.geometry().bottom()<dialog.height()
    render_dir=workspace.directory/'renders';render_dir.mkdir()
    assert dialog.grab().save(str(render_dir/f'prepare-{width}.png'))
    dialog.scroll.verticalScrollBar().setValue(dialog.scroll.verticalScrollBar().maximum())
    app.processEvents()
    assert dialog.grab().save(str(render_dir/f'prepare-{width}-bottom.png'))
    QTest.keyClick(dialog,Qt.Key.Key_Escape)
    assert dialog.result()==QDialog.DialogCode.Rejected
    with CreatorLibrary(workspace.database) as library:assert library.list_moments()['items']==[]


def test_fractional_duration_is_conservatively_bounded_and_template_change_refuses(case):
    workspace,service,kit,bg=case
    seed_source(workspace,mutate_result=lambda result:result['source_facts'].update(duration='1/3'))
    context=bg(service.inspect,'Clip-A');choice=bg(service.kit,kit,1)
    assert context.source.duration_us==333333
    with pytest.raises(LibraryError,match='duration bound'):
        create(service,context,choice,bg,source_in_us=0,source_out_us=333334)
    first=create(service,context,choice,bg,source_in_us=0,source_out_us=333333)
    with CreatorLibrary(workspace.database) as library:
        assert library.moment_origin(first.id)['source_facts']['duration']=='1/3'
        template=library.asset(library.kit(kit,1)['asset_refs']['native_template'])
    Path(template.path).write_bytes(b'Changed template after selection')
    with pytest.raises(LibraryError,match='Kit template is missing or changed'):
        create(service,context,choice,bg,source_in_us=0,source_out_us=200000)
    with CreatorLibrary(workspace.database) as library:assert len(library.list_moments()['items'])==1


def test_versions_are_paginated_without_losing_the_exact_selected_version(app,case):
    workspace,service,kit,bg=case
    seed_source(workspace)
    with CreatorLibrary(workspace.database) as library:
        descriptor=library.kit(kit,2)
        for version in range(2,62):library.revise_kit(kit,descriptor,expected_version=version)
    context=bg(service.inspect,'Clip-A')
    runner=JobRunner();dialog=MomentPreparationDialog(context,service,runner)
    dialog.show();wait(app,lambda:not runner._jobs and not dialog.busy)
    assert dialog.choice.version==62 and dialog.more_versions.isVisible()
    dialog.more_versions.click();wait(app,lambda:not runner._jobs and not dialog.busy)
    assert dialog.versions.count()==62 and dialog.choice.version==62
    for index in range(dialog.versions.count()):
        if dialog.versions.itemData(index)==53:dialog.versions.setCurrentIndex(index);break
    wait(app,lambda:not runner._jobs and not dialog.busy)
    assert dialog.choice.version==53
    dialog.reject()
