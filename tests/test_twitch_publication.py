from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

import pytest
from native.core import creator_library as core
from native.core.creator_library import CreatorLibrary, RevisionConflict
from native.core.durable_jobs import DurableJobs, VerificationRequired, StaleTransition
from native.core.twitch_inbox import TwitchClip
from native.services.twitch_publication import VerifiedTwitchPublication, PublicationGuardError
from native.services.native_export_client import ExportExecution


def ref(): return {'id': str(uuid.uuid4()), 'sha256': 'a' * 64}
def clip(name='Clip-One'):
    return TwitchClip(name, '42', '43', '', 'Selected', '2026-10-18T12:00:00.000000001Z',
                      2_000_000, 5, f'https://clips.twitch.tv/{name}', None, None)


@pytest.fixture
def sample(tmp_path):
    library = CreatorLibrary(tmp_path / 'library.sqlite')
    media = tmp_path / 'media.mp4'; media.write_bytes(b'authored byte fixture, no media claim')
    selected = clip(); library.record_twitch_clips([selected])
    result = ref()
    def attest(job, evidence):
        return {'job_id': job['id'], 'claim_token': job['claim_token'], 'intent_sha256': job['intent_sha256'],
                'output_set_id': job['output_set_id'], 'process_disposition': 'stopped',
                'outputs_disposition': 'verified', 'result_ref': result, 'evidence_ref': evidence}
    jobs = DurableJobs(library.connection, settlement_verifier=attest)
    def claim():
        jobs.submit('io', {'request_ref': ref(), 'profile_sha256': 'b'*64}, idempotency_key=str(uuid.uuid4()))
        return jobs.claim_next(owner_id=str(uuid.uuid4()), kinds=['io'])
    job = claim()
    data = dict(lib=library, media=media, clip=selected, result=result, jobs=jobs, job=job, claim=claim)
    yield data
    library.close()


def publication(s, **changes):
    if os.name != 'nt':
        pytest.skip('Physical Windows read guards are required for publication qualification')
    args = dict(clip=s['clip'], job=s['job'], result_ref=s['result'], profile_sha256='b'*64,
                media_path=s['media'], expected_sha256=hashlib.sha256(s['media'].read_bytes()).hexdigest(),
                expected_bytes=s['media'].stat().st_size)
    args.update(changes)
    return VerifiedTwitchPublication(s['lib'], **args)


def finish(s, outcome='completed'):
    job = s['job']
    return s['jobs'].finish(job['id'], expected_revision=job['revision'], claim_token=job['claim_token'],
                            outcome=outcome, evidence_ref=ref())


def db_state(s):
    c = s['lib'].connection
    return {table: [tuple(r) for r in c.execute('SELECT * FROM '+table)] for table in
            ('assets', 'twitch_clip_sources', 'durable_job_heads', 'durable_job_events',
             'durable_job_holds', 'durable_job_settlements')}


def test_completion_publishes_exact_binding_and_reopen(sample):
    s = sample
    with publication(s) as pub:
        s['jobs'].completed_output_committer = pub.commit
        assert finish(s)['state'] == 'completed'
        bound = s['lib'].twitch_source(s['clip'].id)
        asset = s['lib'].asset(bound['asset_id'])
        assert asset.path == str(s['media']) and asset.sha256 == pub.plan['media']['sha256']
        assert bound['selected_metadata']['created_at'].endswith('000000001Z')
        assert bound['result_id'] == s['result']['id'] and s['jobs'].resources() == []
    with CreatorLibrary(s['lib'].connection.execute('PRAGMA database_list').fetchone()[2]) as reopened:
        assert reopened.twitch_source(s['clip'].id) == bound
        assert reopened.connection.execute('PRAGMA user_version').fetchone()[0] == 4
        for sql in ('UPDATE twitch_clip_sources SET clip_id=clip_id', 'DELETE FROM twitch_clip_sources'):
            with pytest.raises(sqlite3.IntegrityError): reopened.connection.execute(sql)


@pytest.mark.parametrize('fault', ['asset', 'binding', 'return', 'head'])
def test_sql_failure_rolls_back_all_publication_and_settlement(sample, fault):
    s = sample; before = db_state(s)
    with publication(s) as pub:
        if fault == 'asset':
            s['lib'].connection.execute("CREATE TEMP TRIGGER reject_binding BEFORE INSERT ON twitch_clip_sources BEGIN SELECT RAISE(ABORT,'after asset'); END")
            s['jobs'].completed_output_committer = pub.commit
        elif fault == 'head':
            s['lib'].connection.execute("CREATE TEMP TRIGGER reject_head BEFORE UPDATE ON durable_job_heads BEGIN SELECT RAISE(ABORT,'after settlement insert'); END")
            s['jobs'].completed_output_committer = pub.commit
        else:
            def fail(*args):
                pub.commit(*args)
                if fault == 'binding': raise RuntimeError('after binding')
                return 1
            s['jobs'].completed_output_committer = fail
        with pytest.raises((RuntimeError, sqlite3.IntegrityError, VerificationRequired)): finish(s)
        assert db_state(s) == before


def test_second_cas_precedes_callback(sample):
    s = sample; called = []
    s['jobs'].completed_output_committer = lambda *args: called.append(args)
    original = s['jobs'].verifier
    def cancel(job, evidence):
        s['jobs'].request_cancel(job['id'], expected_revision=job['revision'])
        return original(job, evidence)
    s['jobs'].verifier = cancel
    with pytest.raises(StaleTransition): finish(s)
    assert called == [] and not s['lib'].list_assets()['items']
    assert s['jobs'].resources()


@pytest.mark.parametrize('outcome', ['failed', 'cancelled'])
def test_noncompleted_never_calls_publisher(sample, outcome):
    s = sample; called = []
    if outcome == 'cancelled':
        s['job'] = s['jobs'].request_cancel(s['job']['id'], expected_revision=s['job']['revision'])
    original = s['jobs'].verifier
    def absent(job, evidence):
        proof = original(job, evidence); proof.update(outputs_disposition='absent', result_ref=None); return proof
    s['jobs'].verifier = absent
    s['jobs'].completed_output_committer = lambda *args: called.append(args)
    assert finish(s, outcome)['state'] == outcome and called == []


def test_committer_receives_detached_copies(sample):
    s = sample
    def modify(connection, job, attestation):
        assert connection.in_transaction
        job['input']['request_ref']['id'] = 'mutated'
        attestation['result_ref']['id'] = 'mutated'
    s['jobs'].completed_output_committer = modify
    result = finish(s)
    assert result['input'] == s['job']['input']
    assert s['jobs'].settlement(result['id'])['result_ref'] == s['result']


def test_reconciliation_uses_same_completed_committer(sample):
    s = sample
    s['job'] = s['jobs'].mark_interrupted(s['job']['id'], expected_revision=s['job']['revision'], reason='cleanup_uncertain')
    with publication(s) as pub:
        s['jobs'].completed_output_committer = pub.commit
        result = s['jobs'].reconcile(s['job']['id'], expected_revision=s['job']['revision'],
                                     claim_token=s['job']['claim_token'], outcome='completed', evidence_ref=ref())
        assert result['state'] == 'completed' and s['lib'].twitch_source(s['clip'].id)


def test_guard_continues_after_process_execution_stops_and_through_sql(sample):
    s = sample
    execution = ExportExecution({'fixture': 'independent publication guard'})
    execution.hold(s['media'], hashlib.sha256(s['media'].read_bytes()).hexdigest())
    with publication(s) as pub:
        assert execution.stop()['closed'] and not execution.guards
        with pytest.raises(PermissionError): s['media'].write_bytes(b'changed')
        def inspect(*args):
            with pytest.raises(PermissionError): s['media'].unlink()
            pub.commit(*args)
        s['jobs'].completed_output_committer = inspect
        finish(s)
        with pytest.raises(PermissionError): s['media'].write_bytes(b'changed')
    with s['media'].open('ab'): pass


def test_dedup_preserves_original_path_for_two_clip_bindings(sample, tmp_path):
    s = sample; original = tmp_path/'original.mp4'; original.write_bytes(s['media'].read_bytes())
    asset = s['lib'].register_asset(original)
    for selected in (s['clip'], clip('Clip-Two')):
        s['lib'].record_twitch_clips([selected]); s['clip'] = selected
        with publication(s) as pub:
            s['jobs'].completed_output_committer = pub.commit; finish(s)
            assert s['lib'].twitch_source(selected.id)['asset_id'] == asset.id
            assert s['lib'].asset(asset.id).path == str(original)
        if selected.id == 'Clip-One': s['job'] = s['claim']()
    assert len(s['lib'].list_assets()['items']) == 1


@pytest.mark.parametrize('race', ['same-sha-appeared', 'relocation', 'first-binding'])
def test_registration_and_first_binding_cas_rejects_race(sample, tmp_path, race):
    s = sample
    if race == 'relocation': s['lib'].register_asset(s['media'])
    with publication(s) as pub:
        if race == 'same-sha-appeared': s['lib'].register_asset(s['media'])
        elif race == 'relocation':
            duplicate=tmp_path/'copy'; duplicate.write_bytes(s['media'].read_bytes())
            a=s['lib'].list_assets()['items'][0]
            s['lib'].relocate_asset(a.id,duplicate,expected_path=a.path)
        else:
            with publication(s) as winner:
                s['jobs'].completed_output_committer=winner.commit; finish(s)
            # New job still tries the same already-bound clip.
            s['job']=s['claim']()
            pub.plan['job']=deepcopy(s['job'])
        before = db_state(s)
        s['jobs'].completed_output_committer=pub.commit
        with pytest.raises(RevisionConflict): finish(s)
        assert db_state(s)==before


def test_closed_guard_cannot_publish(sample):
    s=sample; pub=publication(s); pub.close(); before=db_state(s)
    s['jobs'].completed_output_committer=pub.commit
    with pytest.raises(core.LibraryError): finish(s)
    assert db_state(s)==before


@pytest.mark.parametrize('old_version', [1, 2])
def test_migration_keeps_existing_asset_and_schema_evidence(tmp_path, old_version):
    db=tmp_path/'old.sqlite'; c=sqlite3.connect(db)
    c.executescript(core._SCHEMA)
    if old_version==2: c.executescript(core._TWITCH_SCHEMA)
    identity=str(uuid.uuid4()); c.execute('INSERT INTO assets VALUES(?,?,?,?,?)',(identity,'c'*64,'old','old',1))
    c.commit(); c.close()
    with CreatorLibrary(db) as lib:
        assert lib.asset(identity).path=='old'
        assert lib.connection.execute('PRAGMA user_version').fetchone()[0]==4


def test_migration_failure_preserves_version2_and_future_rejected(tmp_path, monkeypatch):
    db=tmp_path/'old.sqlite'; c=sqlite3.connect(db); c.executescript(core._SCHEMA+core._TWITCH_SCHEMA); c.close()
    with monkeypatch.context() as patcher:
        patcher.setattr(core,'_TWITCH_SOURCE_SCHEMA',core._TWITCH_SOURCE_SCHEMA+'INVALID SQL;\n')
        with pytest.raises(sqlite3.OperationalError): CreatorLibrary(db)
    c=sqlite3.connect(db)
    assert c.execute('PRAGMA user_version').fetchone()[0]==2
    assert c.execute("SELECT 1 FROM sqlite_master WHERE name='twitch_clip_sources'").fetchone() is None
    c.execute('PRAGMA user_version=99'); c.close()
    with pytest.raises(core.LibraryError): CreatorLibrary(db)


def test_populated_v2_migration_preserves_all_other_owner_tables(sample):
    s=sample; lib=s['lib']; c=lib.connection
    asset=lib.register_asset(s['media']); kid=lib.create_kit({'name':'Existing Kit'})
    lib.create_moment(title='Existing Moment',source_asset_id=asset.id,source_in_us=0,
                      source_out_us=1_000_000,kit_id=kid,kit_version=1)
    tid=str(uuid.uuid4())
    c.execute('INSERT INTO caption_transcripts VALUES(?,?,?,?,?)',(tid,asset.id,asset.sha256,'{}','fixture'))
    c.execute("INSERT INTO caption_revisions VALUES(?,1,NULL,?,'fixture')",(tid,'[]'))
    # Build an actual schema2 fixture before capturing its old-owner records.
    assert c.execute('SELECT count(*) FROM moment_twitch_origins').fetchone()[0]==0
    c.execute('DROP TABLE moment_twitch_origins')
    names=[row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('sqlite_sequence','twitch_clip_sources')")]
    before={name:[tuple(row) for row in c.execute('SELECT * FROM '+name)] for name in names}
    c.execute('DROP TABLE twitch_clip_sources'); c.execute('PRAGMA user_version=2')
    with CreatorLibrary(c.execute('PRAGMA database_list').fetchone()[2]) as reopened:
        after={name:[tuple(row) for row in reopened.connection.execute('SELECT * FROM '+name)] for name in names}
        assert after==before


def test_failed_guard_release_retains_context_for_explicit_cleanup(sample):
    pub=publication(sample); original=list(pub.guards)
    class FailedHandle:
        def CloseHandle(self,handle): return False
    pub.guards=[(FailedHandle(),guard[1]) for guard in original]
    try:
        with pytest.raises(PublicationGuardError) as caught: pub.close()
        assert caught.value.publication is pub and pub.guards and not pub.closed
        with pytest.raises(PermissionError): sample['media'].write_bytes(b'changed')
    finally:
        pub.guards=original; pub.close()
    assert pub.closed
