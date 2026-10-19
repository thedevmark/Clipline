"""Real SQLite selection history; job settlement is an authored verifier fixture."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

import pytest

from native.core.creator_library import CreatorLibrary
from native.core.durable_jobs import DurableJobs
from native.core.moment_captions import CaptionSelectionError, StaleCaptionSelection, install_schema
from native.services.moment_captions import MomentCaptionSelector


def uid():
    return str(uuid.uuid4())


def raw(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def case(tmp_path):
    with CreatorLibrary(tmp_path / 'library.sqlite') as library:
        source = tmp_path / 'source.bin'; source.write_bytes(b'authored source')
        asset = library.register_asset(source)
        kit = library.create_kit({'name': 'Saved style', 'properties': {'caption.size': 36}})
        moment = library.create_moment(title='A moment', source_asset_id=asset.id, source_in_us=125,
                                      source_out_us=999_875, kit_id=kit, kit_version=1)
        directory = tmp_path / 'artifacts'; directory.mkdir()
        yield library, moment, asset, directory


def completed(case, *, transcript_id=None, texts=('hello', 'Twitch'), kit=None, source=None,
              start_us=0, end_us=1_000_000, profile='a' * 64, settle=True,
              request_profile=None, encode_envelope=raw, hypothesis_request_sha=None):
    library, moment, asset, directory = case
    source = source or {'asset_id': asset.id, 'sha256': asset.sha256, 'path': asset.path,
                        'audio_stream_index': 1, 'channels': [0, 1], 'mix': 'mixed'}
    kit = kit or {'kit_id': moment.kit_id, 'version': moment.kit_version}
    identity = {'source': source, 'audio': {'path': 'prepared.wav', 'pcm_sha256': 'b' * 64,
                'wav_sha256': 'c' * 64, 'sample_rate': 16000, 'sample_count': 32000,
                'channels': 1, 'sample_width': 2, 'sample_zero_source_us': 0,
                'extraction': {'source_sha256': source['sha256'], 'argv': ['authored-fixture'],
                               'tool_version': 'test only'}},
                'range': {'start_us': start_us, 'end_us': end_us}}
    tid = transcript_id or library.captions.create(identity)
    request = {'schema_version': 1, 'run_id': uid(), 'task': 'transcribe', 'language': 'en',
               'profile_sha256': request_profile or profile, 'kit_revision': kit, **copy.deepcopy(identity)}
    envelope = {'schema': 1, 'transcript_id': tid, 'accepted_revision': None, 'request': request}
    request_bytes = encode_envelope(envelope); request_ref = {'id': uid(), 'sha256': sha(request_bytes)}
    job = library.jobs.submit('caption', {'request_ref': request_ref,
                'assets': [{'asset_id': source['asset_id'], 'sha256': source['sha256']}],
                'profile_sha256': profile, 'kit': kit, 'range': identity['range']}, idempotency_key=uid())
    h = {'run_id': request['run_id'], 'request_sha256': hypothesis_request_sha or sha(raw(request)), 'processing_state': 'complete',
         **copy.deepcopy(identity), 'words': [{'id': uid(), 'text': text} for text in texts]}
    a = {'run_id': uid(), 'request_sha256': sha(raw(request)), 'hypothesis_run_id': h['run_id'],
         'target_transcript_revision': None,
         'words': [{'word_id': w['id'], 'text': w['text'],
                    'timing': {'start_us': start_us + i * 1000, 'end_us': start_us + (i + 1) * 1000,
                               'status': 'aligned'}} for i, w in enumerate(h['words'])]}
    library.captions.propose_hypothesis(tid, h)
    library.captions.propose_alignment(tid, h['run_id'], a)
    claim = library.jobs.claim_next(owner_id=uid(), kinds=['caption'], job_id=job['id'])
    receipt = {'schema_version': 1, 'state': 'complete', 'run_id': request['run_id'],
               'hashes': {'request': sha(raw(request)), 'hypothesis': sha(raw(h)), 'alignment': sha(raw(a))}}
    receipt_bytes = raw(receipt)
    result = {'schema': 1, 'job_id': job['id'], 'claim_token': claim['claim_token'],
              'intent_sha256': job['intent_sha256'], 'output_set_id': job['output_set_id'],
              'request_ref': request_ref, 'outcome': 'completed', 'outputs_disposition': 'verified',
              'process_disposition': 'stopped', 'worker_state': 'complete', 'run_id': request['run_id'],
              'receipt_sha256': sha(receipt_bytes)}
    result_bytes = raw(result); result_ref = {'id': job['output_set_id'], 'sha256': sha(result_bytes)}
    attestation = {'job_id': job['id'], 'claim_token': claim['claim_token'],
                   'intent_sha256': job['intent_sha256'], 'output_set_id': job['output_set_id'],
                   'process_disposition': 'stopped', 'outputs_disposition': 'verified',
                   'result_ref': result_ref, 'evidence_ref': result_ref}
    if settle:
        jobs = DurableJobs(library.connection, settlement_verifier=lambda current, ref: copy.deepcopy(attestation))
        jobs.finish(job['id'], expected_revision=claim['revision'], claim_token=claim['claim_token'],
                    outcome='completed', evidence_ref=result_ref)
    for path, data in ((directory / 'requests' / (request_ref['id'] + '.json'), request_bytes),
                       (directory / 'results' / (result_ref['id'] + '.json'), result_bytes),
                       (directory / 'executions' / job['output_set_id'] / request['run_id'] / 'receipt.json', receipt_bytes)):
        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
    return {'job_id': job['id'], 'transcript_id': tid, 'hypothesis_id': h['run_id'], 'alignment_id': a['run_id'],
            'request_bytes': request_bytes, 'result_bytes': result_bytes, 'receipt_bytes': receipt_bytes}


def choose(case, proof, *, expected_selection_revision=0, **changes):
    library, moment, _, _ = case
    args = {**proof, 'expected_moment_revision': moment.revision,
            'expected_selection_revision': expected_selection_revision, **changes}
    return library.caption_selections.select_completed(moment.id, **args)


def count(library):
    return library.connection.execute('SELECT count(*) FROM moment_caption_selections').fetchone()[0]


def test_reopen_preserves_exact_choice_without_acceptance_or_edit_rounding(case):
    library, moment, _, directory = case
    proof = completed(case)
    selected = choose(case, proof)
    assert selected['hypothesis_id'] == proof['hypothesis_id']
    assert library.captions.head(proof['transcript_id']) == 0
    assert (library.moment(moment.id).source_in_us, library.moment(moment.id).source_out_us) == (125, 999_875)
    selected['request_ref']['sha256'] = 'mutated'
    with CreatorLibrary(directory.parent / 'library.sqlite') as other:
        restored = other.caption_selections.selected(moment.id, moment.revision)
        assert restored['request_ref']['sha256'] == sha(proof['request_bytes'])
        assert other.captions.head(proof['transcript_id']) == 0
        install_schema(other.connection)
        assert other.connection.execute('PRAGMA user_version').fetchone()[0] == 4


def test_rerun_never_selects_latest_or_changes_accepted_corrections(case):
    library, moment, _, _ = case
    first = completed(case); selected = choose(case, first)
    accepted = library.captions.accept_initial(first['transcript_id'], first['hypothesis_id'], alignment_id=first['alignment_id'])
    correction = library.captions.edit(first['transcript_id'], expected_revision=1, operation='replace',
                                      target_word_ids=[accepted['words'][0]['id']], replacements=[{'text': 'Mark'}])
    second = completed(case, transcript_id=first['transcript_id'], texts=('different', 'proposal'))
    assert library.caption_selections.selected(moment.id, moment.revision) == selected
    newer = choose(case, second, expected_selection_revision=1)
    assert newer['selection_revision'] == 2
    assert library.captions.revision(first['transcript_id']) == correction
    history = library.caption_selections.history(moment.id, moment.revision, limit=1)
    assert history['items'] == [selected] and history['next_cursor'] == 1
    assert library.caption_selections.history(moment.id, moment.revision, after=1)['items'] == [newer]


def test_two_catalog_connections_reject_stale_choice_and_keep_history(case):
    library, moment, _, directory = case
    one = completed(case); two = completed(case)
    with CreatorLibrary(directory.parent / 'library.sqlite') as other:
        assert other.caption_selections.selected(moment.id, 1) is None
        choose(case, one)
        with pytest.raises(StaleCaptionSelection):
            other.caption_selections.select_completed(moment.id, expected_moment_revision=1,
                    expected_selection_revision=0, **two)
    assert count(library) == 1


@pytest.mark.parametrize('field', ['job_id', 'transcript_id', 'hypothesis_id', 'alignment_id'])
def test_other_completed_job_or_proposal_cannot_be_substituted(case, field):
    library, _, _, _ = case
    one = completed(case); two = completed(case)
    with pytest.raises((CaptionSelectionError, ValueError)):
        choose(case, one, **{field: two[field]})
    assert count(library) == 0


@pytest.mark.parametrize('field', ['request_bytes', 'result_bytes', 'receipt_bytes'])
def test_changed_artifact_cannot_be_selected(case, field):
    library, _, _, _ = case
    proof = completed(case)
    with pytest.raises(CaptionSelectionError):
        choose(case, proof, **{field: proof[field] + b' '})
    assert count(library) == 0


def test_pending_job_cannot_select_or_release_resources(case):
    library, _, _, _ = case
    proof = completed(case, settle=False)
    with pytest.raises(CaptionSelectionError):
        choose(case, proof)
    assert len(library.jobs.resources()) == 2 and count(library) == 0


def test_wrong_source_or_kit_or_range_does_not_create_choice(case):
    library, _, _, directory = case
    original = completed(case)
    other_source = directory.parent / 'other.bin'; other_source.write_bytes(b'different')
    other = library.register_asset(other_source)
    proofs = [completed(case, source={'asset_id': other.id, 'sha256': other.sha256, 'path': other.path,
                                      'audio_stream_index': 1, 'channels': [0, 1], 'mix': 'mixed'}),
              completed(case, kit={'kit_id': library.create_kit({'name': 'Another Kit'}), 'version': 1}),
              completed(case, start_us=250)]
    for proof in proofs:
        with pytest.raises(CaptionSelectionError):
            choose(case, proof)
    assert count(library) == 0
    assert choose(case, original)['selection_revision'] == 1


def test_moment_revision_change_requires_new_explicit_selection(case):
    library, moment, _, _ = case
    proof = completed(case); old = choose(case, proof)
    updated = library.set_overrides(moment.id, {'caption.size': 40}, expected_revision=1)
    with pytest.raises(StaleCaptionSelection):
        choose(case, proof, expected_selection_revision=1)
    assert library.caption_selections.selected(moment.id, updated.revision) is None
    assert library.caption_selections.selected(moment.id, 1) == old


def test_selection_is_immutable_and_failed_insert_rolls_back(case):
    library, _, _, _ = case
    proof = completed(case); choose(case, proof)
    for statement in ('UPDATE moment_caption_selections SET selection_revision=3',
                      'DELETE FROM moment_caption_selections'):
        with pytest.raises(sqlite3.IntegrityError):
            library.connection.execute(statement)
    library.connection.execute("CREATE TRIGGER fail_choice BEFORE INSERT ON moment_caption_selections BEGIN SELECT RAISE(ABORT,'test rollback'); END")
    with pytest.raises(sqlite3.IntegrityError):
        choose(case, proof, expected_selection_revision=1)
    assert count(library) == 1 and not library.connection.in_transaction


def test_selector_reads_artifacts_before_write_transaction_and_rechecks_hashes(case, monkeypatch):
    library, moment, _, directory = case
    proof = completed(case)
    selector = MomentCaptionSelector(library, directory)
    original = selector._read
    def read(*parts):
        assert not library.connection.in_transaction
        return original(*parts)
    monkeypatch.setattr(selector, '_read', read)
    args = {key: proof[key] for key in ('job_id', 'transcript_id', 'hypothesis_id', 'alignment_id')}
    assert selector.select(moment.id, expected_moment_revision=1, expected_selection_revision=0, **args)['selection_revision'] == 1
    request = json.loads(proof['result_bytes'])['request_ref']['id']
    (directory / 'requests' / (request + '.json')).write_bytes(b'{}')
    with pytest.raises(CaptionSelectionError):
        selector.select(moment.id, expected_moment_revision=1, expected_selection_revision=1, **args)
    assert count(library) == 1


@pytest.mark.parametrize('encoded', [b'{"x":NaN}', b'{"nested":{"x":1e999}}', b'{"x":1,"x":2}',
                                    b'{"x":"\xff"}', b' ' * 65537],
                         ids=['nan', 'overflowing-number', 'duplicate-key', 'invalid-utf8', 'oversized'])
def test_hash_matching_malformed_or_oversized_json_cannot_create_choice(case, encoded):
    library, _, _, _ = case
    proof = completed(case, encode_envelope=lambda envelope: encoded)
    with pytest.raises(CaptionSelectionError):
        choose(case, proof)
    assert count(library) == 0


def test_request_profile_must_match_normalized_job_pin(case):
    library, _, _, _ = case
    proof = completed(case, profile='a' * 64, request_profile='d' * 64)
    with pytest.raises(CaptionSelectionError, match='different source, Kit, profile or range pins'):
        choose(case, proof)
    assert count(library) == 0


def test_manually_retained_proposal_with_wrong_request_digest_cannot_select(case):
    library, _, _, _ = case
    proof = completed(case, hypothesis_request_sha='f' * 64)
    with pytest.raises(CaptionSelectionError, match='hypothesis is not this completed output'):
        choose(case, proof)
    assert library.captions.head(proof['transcript_id']) == 0 and count(library) == 0


def test_exact_explicit_choice_can_leave_alignment_unselected(case):
    library, _, _, _ = case
    proof = completed(case)
    assert choose(case, proof, alignment_id=None)['alignment_id'] is None
    assert library.captions.head(proof['transcript_id']) == 0


def test_additive_migration_preserves_existing_schema4_histories_and_rolls_back(case, monkeypatch):
    library, moment, _, directory = case
    proof = completed(case)
    accepted = library.captions.accept_initial(proof['transcript_id'], proof['hypothesis_id'], alignment_id=proof['alignment_id'])
    database = directory.parent / 'library.sqlite'
    # Represent a real catalog from before this additive domain, preserving all prior rows.
    for name in ('moment_caption_selections', 'moment_caption_schema'):
        library.connection.execute('DROP TABLE ' + name)
    original = Path.read_text
    def broken_sql(path, *args, **kwargs):
        text = original(path, *args, **kwargs)
        return text + '\nINVALID SQL;\n' if path.name == 'moment_caption_schema.sql' else text
    with monkeypatch.context() as patcher:
        patcher.setattr(Path, 'read_text', broken_sql)
        with pytest.raises(sqlite3.OperationalError):
            CreatorLibrary(database)
    assert library.connection.execute("SELECT name FROM sqlite_master WHERE name LIKE 'moment_caption_%'").fetchall() == []
    assert library.captions.revision(proof['transcript_id']) == accepted
    with CreatorLibrary(database) as reopened:
        assert reopened.moment(moment.id) == moment
        assert reopened.captions.revision(proof['transcript_id']) == accepted
        assert reopened.jobs.job(proof['job_id'])['state'] == 'completed'
        assert reopened.caption_selections.selected(moment.id, moment.revision) is None
        assert reopened.connection.execute('PRAGMA user_version').fetchone()[0] == 4
        assert reopened.connection.execute('PRAGMA foreign_key_check').fetchall() == []
